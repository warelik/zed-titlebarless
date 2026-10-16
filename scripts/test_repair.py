from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import release


REPO = "maintainer/zed-fork"
CONFIG = "a" * 40
SEED = "b" * 40
HEAD = "c" * 40
UPSTREAM = "d" * 40
STATE = {"schema": 1, "config": CONFIG, "upstream": UPSTREAM, "seed": SEED,
         "head": HEAD, "number": 2, "branch": "repair/example",
         "created_at": "2026-10-16T10:00:00Z"}
ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": CONFIG, "GITHUB_RUN_ID": "42",
       "REPAIR_PR": "2", "REPAIR_HEAD": HEAD, "CANDIDATE_PR": "1", "CANDIDATE_SHA": HEAD}


def request(head=SEED, attempt=1, age=0):
    return {
        "created_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
        "body": f"<!-- zed-fix:{head}:{attempt} -->",
    }


class RepairBoundaryTests(unittest.TestCase):
    def test_deferred_proposal_preserves_recovery_and_bounded_repair_paths(self):
        candidate, patchset = "7" * 40, "8" * 64
        tag = f"build-1.24.2-{UPSTREAM[:12]}-{patchset[:12]}"

        for nonblocking, broken, expected_followup in (
            (False, False, False), (True, False, True), (False, True, False),
        ):
            with self.subTest(nonblocking=nonblocking, broken=broken):
                state = {**STATE, "nonblocking": nonblocking}
                old = request(HEAD, age=release.MAX_REPAIR_SECONDS + 60)
                outputs, writes = {}, []

                def api(path, *, method="GET", data=None, token=None):
                    if method != "GET":
                        writes.append((path, method, data))
                        return {}
                    if path == f"repos/{REPO}/commits/downstream": return {"sha": CONFIG}
                    if path == f"repos/{release.UPSTREAM}/commits/v1.24.2": return {"sha": UPSTREAM}
                    if path == f"repos/{REPO}/pulls/2": return {"state": "open", "head": {"sha": HEAD}}
                    raise AssertionError(path)

                def pages(path):
                    if path == f"repos/{REPO}/releases":
                        return [{"tag_name": tag, "draft": False}]
                    if path.startswith(f"repos/{REPO}/pulls?"):
                        return [{"number": 7, "head": {"sha": candidate}}]
                    raise AssertionError(path)

                def run(*args, cwd=None, env=None):
                    if args[:2] == ("git", "init"):
                        manifest = Path(args[2]) / "crates/zed/Cargo.toml"
                        manifest.parent.mkdir(parents=True)
                        manifest.write_text('[package]\nversion = "1.24.2"\n')
                    if args[:2] == ("git", "am") and broken:
                        raise subprocess.CalledProcessError(1, args)
                    if args == ("git", "rev-parse", "HEAD"): return candidate
                    if args[:2] == ("git", "show"): return '[package]\nversion = "1.24.2"\n'
                    if args[:2] == ("git", "diff"): return "crates/auto_update/src/auto_update.rs"
                    return ""

                with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    stack.enter_context(patch.dict(os.environ, {
                        "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": CONFIG, "GITHUB_RUN_ID": "42",
                    }, clear=True))
                    for name, replacement in (("api", api), ("pages", pages), ("run", run),
                                               ("output", lambda **values: outputs.update(values))):
                        stack.enter_context(patch.object(release, name, side_effect=replacement))
                    stack.enter_context(patch.object(release, "repair_pull", return_value=state))
                    stack.enter_context(patch.object(release, "repair_comments", return_value=[old]))
                    stack.enter_context(patch.object(release, "repair_requests", return_value=[old]))
                    stack.enter_context(patch.object(release, "mirror_main"))
                    stack.enter_context(patch.object(release, "select_upstream", return_value=("v1.24.2", UPSTREAM)))
                    stack.enter_context(patch.object(release, "patch_identity", return_value=([Path("fixture.patch")], patchset)))
                    stack.enter_context(patch.object(release, "ensure_forward_release"))
                    stack.enter_context(patch.object(release, "verify_published_release"))
                    stack.enter_context(patch.object(release, "check_review", return_value=(True, "reviewed")))
                    stack.enter_context(patch.object(release, "minor_findings", return_value=[]))
                    stack.enter_context(patch.object(release, "close_published_reviews"))
                    stack.enter_context(patch.object(release, "cleanup_closed_pull_refs"))
                    stack.enter_context(patch.object(release, "owner_token", return_value="fake"))
                    escalation = stack.enter_context(patch.object(
                        release, "escalation", side_effect=ValueError("expected escalation")))
                    try:
                        release.prepare(Path(directory) / "app")
                    except subprocess.CalledProcessError:
                        self.assertTrue(broken)
                    closed = any(method == "PATCH" and data == {"state": "closed"}
                                 for _, method, data in writes)
                    if broken or outputs.get("followup"):
                        with self.assertRaisesRegex(ValueError, "expected escalation"):
                            release.request_repair(Path(directory) / "repair", evidence="failure")
                        escalation.assert_called_once()
                    else:
                        self.assertTrue(closed)
                        escalation.assert_not_called()
                    self.assertEqual(bool(outputs.get("followup")), expected_followup)
                    self.assertFalse(any("/comments" in path and "@codex" in str(data)
                                         for path, _, data in writes))

    def test_only_patch_changes_or_policy_changes_are_eligible(self):
        for changed, policy in (
            ("patches/0009-fix.patch", False), ("scripts/release.py", True),
            ("scripts/test_new_case.py", True), (".github/workflows/release.yml", True),
        ):
            with (self.subTest(changed=changed),
                  patch.object(release, "run", side_effect=["", "", "", CONFIG, changed,
                                                          "100644 blob " + HEAD + "\t" + changed])):
                self.assertEqual(release.repair_changes(STATE), ([changed], policy))

    def test_context_manifest_instructions_and_mixed_changes_cannot_land(self):
        for changed in (
            ".repair/context.json", ".repair/upstream.tar.gz", "patches/source.json",
            "AGENTS.md", ".gitignore", "patches/0009-fix.patch\nscripts/release.py",
        ):
            with (self.subTest(changed=changed),
                  patch.object(release, "run", side_effect=["", "", "", CONFIG, changed]),
                  self.assertRaises(ValueError)):
                release.repair_changes(STATE)

    def test_symlinks_and_policy_deletion_are_rejected(self):
        for entry in ("", "120000 blob " + HEAD + "\tscripts/release.py"):
            with (self.subTest(entry=entry), patch.object(release, "run",
                  side_effect=["", "", "", CONFIG, "scripts/release.py", entry]),
                  self.assertRaises(ValueError)):
                release.repair_changes(STATE)

    def test_seed_must_directly_extend_the_trusted_configuration(self):
        with (patch.object(release, "run", side_effect=["", "", "", UPSTREAM]),
              self.assertRaisesRegex(ValueError, "trusted configuration")):
            release.repair_changes(STATE)

    def test_renames_are_examined_as_delete_and_add(self):
        with patch.object(release, "run", side_effect=["", "", "", CONFIG, ""]) as run:
            release.repair_changes(STATE)
        self.assertIn("--no-renames", run.call_args.args)

    def test_copied_patches_keep_the_exact_reviewed_bytes(self):
        content = b"From test\r\n\r\n---\r\n\r\n"
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(release, "run", return_value="patches/0009-fix.patch\npatches/source.json"),
              patch.object(release.subprocess, "check_output", return_value=content)):
            root = release.copy_patch_series(HEAD, Path(directory) / "patches")
            self.assertEqual((root / "0009-fix.patch").read_bytes(), content)
            self.assertFalse((root / "source.json").exists())

    def test_a_task_report_without_an_actual_push_is_only_pending(self):
        state = {**STATE, "head": SEED}
        with (patch.object(release, "repair_pull", return_value=state),
              patch.object(release, "repair_comments", return_value=[request()]),
              patch.object(release, "output") as output,
              patch.object(release, "repair_changes", side_effect=AssertionError("No new head"))):
            result = release.proposal(REPO, CONFIG, Path("/unused"))
        self.assertTrue(result["waiting"])
        self.assertEqual(output.call_args.kwargs["mode"], "waiting")
        self.assertFalse(output.call_args.kwargs["ready"])

    def test_review_timeout_fails_instead_of_remaining_green(self):
        fresh = request()
        release.pending_review(fresh["created_at"])
        with self.assertRaisesRegex(ValueError, "two-hour"):
            release.pending_review(request(age=release.REVIEW_WAIT_SECONDS + 1)["created_at"])

    def test_a_new_head_is_processed_instead_of_trusting_an_old_task_request(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_comments", return_value=[request()]),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "copy_patch_series", return_value=Path(directory)) as copy,
              patch.object(release, "output")):
            result = release.proposal(REPO, CONFIG, Path(directory))
        self.assertFalse(result["waiting"])
        copy.assert_called_once_with(HEAD, Path(directory) / "proposal")

    def test_an_empty_push_does_not_prevent_rechecking_the_original_candidate(self):
        with (patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_comments",
                           return_value=[request(age=release.REVIEW_WAIT_SECONDS + 1)]),
              patch.object(release, "repair_changes", return_value=([], False)),
              patch.object(release, "output")):
            result = release.proposal(REPO, CONFIG, Path("/unused"))
        self.assertTrue(result["deferred"])

    def test_policy_minor_findings_request_followup_without_blocking_acceptance(self):
        review = {"body": f"<!-- zed-policy:{HEAD} -->", "created_at": request()["created_at"]}
        with (patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_comments", return_value=[review]),
              patch.object(release, "repair_changes", return_value=(["scripts/release.py"], True)),
              patch.object(release, "policy_review", return_value=(True, "Accepted")),
              patch.object(release, "minor_findings", return_value=[{"body": "P2 fix"}]),
              patch.object(release, "output") as output):
            release.proposal(REPO, CONFIG, Path("/unused"))
        self.assertTrue(output.call_args.kwargs["ready"])
        self.assertTrue(output.call_args.kwargs["followup"])

    def test_recovered_candidate_closes_only_the_unchanged_blocking_repair(self):
        with (patch.object(release, "api", side_effect=[
                  {"sha": CONFIG}, {"state": "open", "head": {"sha": HEAD}}, {}, {}]) as api,
              patch.object(release, "cleanup_closed_pull_refs") as cleanup):
            release.close_recovered_repair(REPO, CONFIG, state=STATE)
        self.assertEqual(api.call_args.kwargs, {"method": "PATCH", "data": {"state": "closed"}})
        cleanup.assert_called_once_with(REPO, 2, HEAD)

    def test_late_lookup_closes_the_captured_unchanged_blocking_repair(self):
        with (patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "api", side_effect=[
                  {"sha": CONFIG}, {"state": "open", "head": {"sha": HEAD}}, {}, {}]) as api,
              patch.object(release, "cleanup_closed_pull_refs") as cleanup):
            release.close_recovered_repair(REPO, CONFIG, expected_head=HEAD)
        self.assertEqual(api.call_args.kwargs, {"method": "PATCH", "data": {"state": "closed"}})
        cleanup.assert_called_once_with(REPO, 2, HEAD)

    def test_late_lookup_does_not_close_a_repair_advanced_after_preparation(self):
        with (patch.object(release, "repair_pull", return_value={**STATE, "head": SEED}),
              patch.object(release, "api", return_value={"sha": CONFIG}) as api):
            release.close_recovered_repair(REPO, CONFIG, expected_head=HEAD)
        self.assertFalse(any(c.kwargs.get("method") in {"POST", "PATCH"} for c in api.call_args_list))

    def test_late_lookup_without_a_captured_head_does_not_discover_a_new_repair(self):
        with (patch.object(release, "repair_pull", side_effect=AssertionError("must not look up repair")),
              patch.object(release, "api", return_value={"sha": CONFIG}) as api):
            release.close_recovered_repair(REPO, CONFIG)
        self.assertFalse(any(c.kwargs.get("method") in {"POST", "PATCH"} for c in api.call_args_list))

    def test_minor_or_changed_repair_is_not_closed_by_default_candidate_recovery(self):
        for state, minor, head in (
            ({**STATE, "nonblocking": True}, False, HEAD),
            (STATE, True, HEAD), (STATE, False, SEED),
        ):
            with (self.subTest(minor=minor, head=head),
                  patch.object(release, "api", side_effect=[
                      {"sha": CONFIG}, {"state": "open", "head": {"sha": head}}]) as api):
                release.close_recovered_repair(REPO, CONFIG, state=state, has_minor=minor)
            self.assertFalse(any(c.kwargs.get("method") in {"POST", "PATCH"} for c in api.call_args_list))

    def test_policy_minor_followup_targets_the_new_adopted_configuration(self):
        adopted = "e" * 40
        review = {"body": f"<!-- zed-policy:{HEAD} -->", "created_at": request()["created_at"]}
        finding = {"body": "P2 policy followup", "path": "scripts/release.py", "line": 1}
        def api(path, **kwargs):
            if path.endswith("/commits/downstream"):
                return {"sha": CONFIG}
            return {"state": "open", "head": {"sha": HEAD}}
        def run(*args, **kwargs):
            if args[:3] == ("git", "worktree", "add"):
                work = Path(args[4])
                (work / "patches").mkdir(parents=True)
                (work / "patches/source.json").write_text(
                    json.dumps({"schema": 1, "test_cases": []}) + "\n")
            return adopted if args == ("git", "rev-parse", "HEAD") else ""
        with (tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["scripts/release.py"], True)),
              patch.object(release, "repair_comments", return_value=[review]),
              patch.object(release, "policy_review", return_value=(True, "Accepted")),
              patch.object(release, "minor_findings", return_value=[finding]),
              patch.object(release, "api", side_effect=api),
              patch.object(release, "pages", return_value=[]),
              patch.object(release, "run", side_effect=run),
              patch.object(release.subprocess, "check_output", return_value=b"verified diff"),
              patch.object(release.subprocess, "run", return_value=SimpleNamespace(stdout="", returncode=1)),
              patch.object(release, "request_repair") as followup,
              patch.object(release, "cleanup_closed_pull_refs")):
            release.land_repair(Path(directory))
        self.assertEqual(followup.call_args.kwargs["config"], adopted)
        self.assertEqual(followup.call_args.kwargs["upstream"], UPSTREAM)
        self.assertTrue(followup.call_args.kwargs["nonblocking"])
        self.assertIn("P2 policy followup", followup.call_args.kwargs["evidence"])

    def test_application_minor_followup_keeps_application_review_identity(self):
        adopted, application = "e" * 40, "f" * 40
        finding = {"body": "P2 application followup", "path": "crates/app.rs", "line": 17}
        environment = {**ENV, "CANDIDATE_SHA": application, "CANDIDATE_PR": "7"}

        def api(path, **kwargs):
            if path.endswith("/commits/downstream"):
                return {"sha": CONFIG}
            return {}

        def run(*args, **kwargs):
            if args[:3] == ("git", "worktree", "add"):
                work = Path(args[4])
                (work / "patches").mkdir(parents=True)
                (work / "patches/source.json").write_text(
                    json.dumps({"schema": 1, "test_cases": []}) + "\n")
            return adopted if args == ("git", "rev-parse", "HEAD") else ""

        with (tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, environment),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "check_review", return_value=(True, "Accepted")),
              patch.object(release, "minor_findings", return_value=[finding]) as findings,
              patch.object(release, "api", side_effect=api),
              patch.object(release, "pages", return_value=[]),
              patch.object(release, "run", side_effect=run),
              patch.object(release, "copy_patch_series", return_value=[]),
              patch.object(release, "patch_identity", return_value=([], "1" * 64)),
              patch.object(release, "patch_regressions", return_value=set()),
              patch.object(release.subprocess, "check_output", return_value=b"verified diff"),
              patch.object(release.subprocess, "run", return_value=SimpleNamespace(stdout="", returncode=1)),
              patch.object(release, "request_repair") as followup,
              patch.object(release, "cleanup_closed_pull_refs")):
            release.land_repair(Path(directory))
        findings.assert_called_once_with(REPO, 7, application)
        self.assertEqual(followup.call_args.kwargs["config"], adopted)
        self.assertEqual(followup.call_args.kwargs["upstream"], UPSTREAM)
        self.assertTrue(followup.call_args.kwargs["nonblocking"])
        evidence = followup.call_args.kwargs["evidence"]
        self.assertIn("application findings", evidence)
        self.assertIn(f"pull/7", evidence)
        self.assertIn(application, evidence)
        self.assertIn("crates/app.rs:17\nP2 application followup", evidence)


class TemporaryRefCleanupTests(unittest.TestCase):
    PATCHSET = "9" * 64

    def review_pull(self, **changes):
        pull = {
            "state": "closed", "user": {"login": "maintainer"},
            "head": {"sha": HEAD, "ref": f"review/{UPSTREAM}-{self.PATCHSET}",
                     "repo": {"full_name": REPO}},
            "base": {"sha": UPSTREAM, "ref": f"review-base/{UPSTREAM}",
                     "repo": {"full_name": REPO}},
        }
        pull.update(changes)
        return pull

    @staticmethod
    def ref(branch, sha):
        return {"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": sha}}

    def invoke_review(self, pull=None, open_pulls=None, refs=None, graphql=None):
        pull = pull or self.review_pull()
        refs = refs or {
            pull["head"]["ref"]: [self.ref(pull["head"]["ref"], HEAD)],
            pull["base"]["ref"]: [self.ref(pull["base"]["ref"], UPSTREAM)],
        }
        writes = []

        def api(path, *, method="GET", data=None, token=None):
            if path == f"repos/{REPO}/pulls/2":
                return pull
            if path == f"repos/{REPO}/git/ref/pull/2/head":
                return {"object": {"type": "commit", "sha": HEAD}}
            prefix = f"repos/{REPO}/git/matching-refs/heads/"
            if path.startswith(prefix):
                return refs.get(path[len(prefix):], [])
            if path == f"repos/{REPO}":
                return {"node_id": "R_test"}
            if path == "graphql":
                writes.append(data)
                return graphql if graphql is not None else {
                    "data": {"updateRefs": {"clientMutationId": None}}}
            raise AssertionError(path)

        with (patch.object(release, "api", side_effect=api),
              patch.object(release, "pages", return_value=open_pulls or [])):
            release.cleanup_closed_pull_refs(REPO, 2, HEAD, upstream=UPSTREAM)
        return writes

    def test_owned_closed_review_deletes_both_exact_refs_atomically(self):
        writes = self.invoke_review()
        self.assertEqual(len(writes), 1)
        updates = writes[0]["variables"]["input"]["refUpdates"]
        self.assertEqual({item["name"] for item in updates}, {
            f"refs/heads/review/{UPSTREAM}-{self.PATCHSET}",
            f"refs/heads/review-base/{UPSTREAM}",
        })
        self.assertEqual({item["beforeOid"] for item in updates}, {HEAD, UPSTREAM})
        self.assertTrue(all(item["afterOid"] == "0" * 40 for item in updates))
        self.assertTrue(all("force" not in item for item in updates))

    def test_shared_review_base_is_retained(self):
        other = {"head": {"ref": "review/other", "repo": {"full_name": REPO}},
                 "base": {"ref": f"review-base/{UPSTREAM}", "repo": {"full_name": REPO}}}
        updates = self.invoke_review(open_pulls=[other])[0]["variables"]["input"]["refUpdates"]
        self.assertEqual([item["name"] for item in updates],
                         [f"refs/heads/review/{UPSTREAM}-{self.PATCHSET}"])

    def test_changed_closed_pull_or_ref_and_noncanonical_identity_are_preserved(self):
        cases = [
            self.review_pull(state="open"),
            self.review_pull(user={"login": "stranger"}),
            self.review_pull(head={"sha": SEED, "ref": f"review/{UPSTREAM}-{self.PATCHSET}",
                                   "repo": {"full_name": REPO}}),
            self.review_pull(head={"sha": HEAD, "ref": "review/maintenance-bootstrap",
                                   "repo": {"full_name": REPO}}),
        ]
        for pull in cases:
            with self.subTest(pull=pull):
                self.assertEqual(self.invoke_review(pull=pull), [])
        branch = self.review_pull()["head"]["ref"]
        self.assertEqual(self.invoke_review(refs={branch: [self.ref(branch, SEED)]}), [])

    def test_missing_archive_or_refs_never_mutates(self):
        with (patch.object(release, "api", side_effect=[
                  self.review_pull(), subprocess.CalledProcessError(1, "gh")]),
              patch.object(release, "pages", side_effect=AssertionError("no listing"))):
            release.cleanup_closed_pull_refs(REPO, 2, HEAD, upstream=UPSTREAM)
        self.assertEqual(self.invoke_review(refs={"unused": []}), [])

    def test_closed_marked_repair_deletes_only_its_head(self):
        state = {"schema": 1, "config": CONFIG, "upstream": UPSTREAM, "seed": SEED}
        pull = {"state": "closed", "user": {"login": "github-actions[bot]"},
                "head": {"sha": HEAD, "ref": "repair/example", "repo": {"full_name": REPO}},
                "base": {"sha": CONFIG, "ref": "downstream", "repo": {"full_name": REPO}},
                "body": "<!-- zed-repair:v1 " + json.dumps(state) + " -->"}
        calls = []
        def api(path, *, method="GET", data=None, token=None):
            if path.endswith("/pulls/2"): return pull
            if path.endswith("/git/ref/pull/2/head"):
                return {"object": {"type": "commit", "sha": HEAD}}
            if "/git/matching-refs/heads/" in path:
                return [self.ref("repair/example", HEAD)]
            if path == f"repos/{REPO}": return {"node_id": "R_test"}
            if path == "graphql":
                calls.append(data)
                return {"data": {"updateRefs": {"clientMutationId": None}}}
            raise AssertionError(path)
        with patch.object(release, "api", side_effect=api), patch.object(release, "pages", return_value=[]):
            release.cleanup_closed_pull_refs(REPO, 2, HEAD)
        updates = calls[0]["variables"]["input"]["refUpdates"]
        self.assertEqual([item["name"] for item in updates], ["refs/heads/repair/example"])

    def test_graphql_rejection_is_reported_without_retry(self):
        with self.assertRaisesRegex(ValueError, "atomic temporary-ref cleanup"):
            self.invoke_review(graphql={"errors": [{"message": "beforeOid changed"}]})


class NativeTaskTests(unittest.TestCase):
    def invoke(self, requests):
        context = (
            patch.dict(os.environ, ENV),
            patch.object(release, "api", return_value={"sha": CONFIG}),
            patch.object(release, "repair_pull", return_value=STATE),
            patch.object(release, "failure_evidence", return_value="Actual failed assertion"),
            patch.object(release, "owner_token", return_value="test-only"),
            patch.object(release, "repair_comments", return_value=requests),
        )
        return context

    def test_first_fix_is_a_native_owner_request_not_a_review_or_a_report(self):
        contexts = self.invoke([])
        with contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5]:
            release.request_repair(Path("/unused"))
        sent = api.call_args
        self.assertEqual(sent.args[0], f"repos/{REPO}/issues/2/comments")
        self.assertEqual(sent.kwargs["token"], "test-only")
        body = sent.kwargs["data"]["body"]
        self.assertTrue(body.startswith("@codex fix "))
        self.assertIn(f"<!-- zed-fix:{HEAD}:1 -->", body)
        self.assertIn("push the actual fix", body)
        self.assertIn("Actual failed assertion", body)

    def test_same_head_is_not_requested_twice(self):
        contexts = self.invoke([request(HEAD)])
        with contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5]:
            release.request_repair(Path("/unused"))
        self.assertEqual(api.call_count, 1)

    def test_third_failed_fix_escalates_without_a_fourth_task(self):
        requests = [request(SEED, 1), request(UPSTREAM, 2), request(CONFIG, 3)]
        contexts = self.invoke(requests)
        with (contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5],
              patch.object(release, "escalation", side_effect=ValueError("escalated")) as escalate,
              self.assertRaisesRegex(ValueError, "escalated")):
            release.request_repair(Path("/unused"))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(escalate.call_count, 1)

    def test_timed_out_task_escalates_without_duplicate_requests(self):
        contexts = self.invoke([request(HEAD, age=release.MAX_REPAIR_SECONDS + 1)])
        with (contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5],
              patch.object(release, "escalation", side_effect=ValueError("escalated")),
              self.assertRaisesRegex(ValueError, "escalated")):
            release.request_repair(Path("/unused"))
        self.assertEqual(api.call_count, 1)

    def test_superseded_configuration_does_not_close_or_repair_newer_work(self):
        with (patch.dict(os.environ, ENV), patch.object(release, "api", return_value={"sha": HEAD}),
              patch.object(release, "repair_pull", side_effect=AssertionError("Newer work is not stale"))):
            release.request_repair(Path("/unused"))

    def test_a_concurrent_new_push_gets_verified_not_repaired_again(self):
        with (patch.dict(os.environ, ENV), patch.object(release, "api", return_value={"sha": CONFIG}),
              patch.object(release, "repair_pull", return_value={**STATE, "head": SEED}),
              patch.object(release, "failure_evidence", side_effect=AssertionError("A new run owns this head"))):
            release.request_repair(Path("/unused"))

    def test_failure_evidence_reads_the_jobs_object_and_exact_review_findings(self):
        finding = {"user": {"login": release.BOT, "type": "Bot"}, "commit_id": HEAD,
                   "path": "open-sse/utils/example.js", "line": 3, "body": "[P2] Restore custom call type"}
        jobs = {"total_count": 1, "jobs": [{"id": 9, "conclusion": "failure",
                "name": "Regression", "html_url": "https://github.com/example"}]}
        with (patch.dict(os.environ, ENV), patch.object(release, "api", return_value=jobs),
              patch.object(release, "pages", return_value=[finding]),
              patch.object(release, "run", return_value="Actual assertion failed")):
            evidence = release.failure_evidence(REPO)
        self.assertIn("Actual assertion failed", evidence)
        self.assertIn("[P2] Restore custom call type", evidence)

    def test_p2_remains_nonblocking_but_gets_an_automatic_followup(self):
        finding = {"user": {"login": release.BOT, "type": "Bot"}, "commit_id": HEAD, "body": "[P2] Bug"}
        with patch.object(release, "pages", return_value=[
            finding, {**finding, "commit_id": CONFIG},
            {**finding, "user": {"login": "somebody", "type": "User"}},
        ]):
            self.assertEqual(release.minor_findings(REPO, 1, HEAD), [finding])
        self.assertIsNone(release.SEVERITY.search("[P2] Bug"))

    def test_policy_configuration_changes_do_not_reset_the_repair_budget(self):
        cycle = "e" * 64
        state = {**STATE, "cycle": cycle}
        previous = {
            "number": 1, "user": {"login": "github-actions[bot]", "type": "Bot"},
            "head": {"repo": {"full_name": REPO}, "ref": "repair/previous"},
            "body": "<!-- zed-repair:v1 " + json.dumps({**state, "config": SEED}) + " -->",
        }
        first, second = request(SEED, 1, age=2), request(UPSTREAM, 2, age=1)
        with (patch.object(release, "pages", return_value=[previous]),
              patch.object(release, "repair_comments", side_effect=[[], [first, second]])):
            self.assertEqual(release.repair_requests(REPO, state), [first, second])


class AdoptionTests(unittest.TestCase):
    def test_changed_proposal_head_cannot_land(self):
        with (patch.dict(os.environ, ENV),
              patch.object(release, "repair_pull", return_value={**STATE, "head": SEED}),
              patch.object(release, "run", side_effect=AssertionError("No Git writes")),
              self.assertRaisesRegex(ValueError, "changed after verification")):
            release.land_repair(Path("/unused"))

    def test_new_default_configuration_cannot_be_overwritten(self):
        with (patch.dict(os.environ, ENV),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "check_review", return_value=(True, "reviewed")),
              patch.object(release, "minor_findings", return_value=[]),
              patch.object(release, "api", return_value={"sha": HEAD}),
              patch.object(release, "run", side_effect=AssertionError("No Git writes")),
              self.assertRaisesRegex(ValueError, "stale repairs")):
            release.land_repair(Path("/unused"))

    def test_unreviewed_repair_cannot_land_even_if_tests_succeeded(self):
        with (patch.dict(os.environ, ENV),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "check_review", return_value=(False, "Review head changed")),
              patch.object(release, "api", side_effect=AssertionError("No API writes")),
              self.assertRaisesRegex(ValueError, "Review head changed")):
            release.land_repair(Path("/unused"))

    def test_ignored_repair_paths_cannot_be_added_through_git_apply(self):
        commands = []

        def execute(args, **kwargs):
            commands.append(args)
            if args[:2] == ["git", "apply"]:
                self.fail("Ignored source must not reach the index")
            return SimpleNamespace(stdout="ignored by fixture\n", returncode=0)

        with (tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "check_review", return_value=(True, "reviewed")),
              patch.object(release, "minor_findings", return_value=[]),
              patch.object(release, "api", return_value={"sha": CONFIG}),
              patch.object(release, "run", return_value=""),
              patch.object(release.subprocess, "check_output", return_value=b"delta"),
              patch.object(release.subprocess, "run", side_effect=execute),
              self.assertRaisesRegex(ValueError, "ignored path")):
            release.land_repair(Path(directory))
        self.assertTrue(commands)

    def test_policy_checks_use_only_disposable_paths_and_no_inherited_credentials(self):
        paths = []
        suites = []

        def execute(args, **kwargs):
            env = kwargs["env"]
            self.assertFalse({"GH_TOKEN", "GITHUB_TOKEN", "CODEX_REQUEST_TOKEN",
                              "NINEROUTER_KEY", "GITHUB_ENV", "GITHUB_PATH"} & env.keys())
            self.assertEqual(env["CI"], "true")
            self.assertEqual(args[:4], ["docker", "run", "--rm", "--read-only"])
            self.assertIn("--network=none", args)
            self.assertIn("--cap-drop=ALL", args)
            self.assertIn("--security-opt=no-new-privileges", args)
            mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
            self.assertEqual(sum(m.endswith(",readonly") for m in mounts), 2)
            for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                         "XDG_STATE_HOME", "XDG_CACHE_HOME", "DATA_DIR"):
                path = Path(env[name])
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                paths.append(path)
            suites.append(args[-1])
            return SimpleNamespace(returncode=0)

        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"GH_TOKEN": "fake", "GITHUB_TOKEN": "fake",
                  "CODEX_REQUEST_TOKEN": "fake", "NINEROUTER_KEY": "fake", "GITHUB_ENV": "/not-allowed"}),
              patch.object(release.subprocess, "run", side_effect=execute),
              patch.object(release, "urlopen") as fetch,
              self.assertRaisesRegex(ValueError, "actionlint archive integrity")):
            fetch.return_value.read.return_value = b"not an authenticated release"
            release.check_policy(Path(directory))
        self.assertEqual(suites[0], str(release.ROOT / "scripts"))
        self.assertEqual(suites[1], str(Path(directory) / "scripts"))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_failed_policy_tests_cannot_reach_linting_or_write_credentials(self):
        with (patch.object(release.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(1, "unittest")),
              patch.object(release, "urlopen", side_effect=AssertionError("Stop at failed tests")),
              self.assertRaises(subprocess.CalledProcessError)):
            release.check_policy(Path("/unused"))
