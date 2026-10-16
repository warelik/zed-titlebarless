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
        formal_p1 = {
            "user": {"login": release.BOT, "type": "Bot"},
            "commit_id": HEAD,
            "path": "crates/agent/src/threads.rs",
            "line": 8,
            "body": "[P1] Formal exact-SHA finding",
        }
        inline_p2 = {
            "user": {"login": release.BOT, "type": "Bot"},
            "commit_id": SEED,
            "original_commit_id": HEAD,
            "path": "open-sse/utils/example.js",
            "line": 3,
            "body": "[P2] Restore custom call type",
        }
        native_state = {
            "code_review": {
                "commit_sha": HEAD,
                "status": "completed",
            }
        }
        native_p2 = {
            "user": {"login": release.BOT, "type": "Bot"},
            "performed_via_github_app": {"slug": release.APP},
            "path": "crates/agent/src/threads.rs",
            "line": 12,
            "body": (
                "[P2] Native issue finding\n"
                f"<!-- codex-review-state:v1 {json.dumps(native_state)} -->"
            ),
        }
        stale_original = {
            **inline_p2,
            "commit_id": SEED,
            "original_commit_id": SEED,
            "body": "[P1] Stale original commit finding",
        }
        spoofed_actor = {
            **formal_p1,
            "user": {"login": "somebody", "type": "User"},
            "body": "[P1] Spoofed actor finding",
        }
        wrong_app = {
            **native_p2,
            "performed_via_github_app": {"slug": "another-app"},
            "body": (
                "[P1] Wrong application finding\n"
                f"<!-- codex-review-state:v1 {json.dumps(native_state)} -->"
            ),
        }
        jobs = {"total_count": 1, "jobs": [{"id": 9, "conclusion": "failure",
                "name": "Regression", "html_url": "https://github.com/example"}]}

        def api(path, **_kwargs):
            if path == f"repos/{REPO}/actions/runs/{ENV['GITHUB_RUN_ID']}/jobs?per_page=100":
                return jobs
            raise AssertionError(path)

        def pages(path):
            if path == f"repos/{REPO}/pulls/1/reviews":
                return [formal_p1, spoofed_actor]
            if path == f"repos/{REPO}/pulls/1/comments":
                return [inline_p2, stale_original]
            if path == f"repos/{REPO}/issues/1/comments":
                return [native_p2, wrong_app]
            raise AssertionError(path)

        commands = []
        failure_log = "\x1b[31mActual assertion failed\x1b[0m\x01"

        def run(*args, **_kwargs):
            commands.append(args)
            if args == ("gh", "api", "--help"):
                return "gh api [flags]\n  --allow-escape-sequences"
            if args == (
                "gh", "api", f"repos/{REPO}/actions/jobs/9/logs",
                "--allow-escape-sequences",
            ):
                return failure_log
            raise AssertionError(args)

        with (patch.dict(os.environ, ENV), patch.object(release, "api", side_effect=api),
              patch.object(release, "pages", side_effect=pages),
              patch.object(release, "run", side_effect=run)):
            evidence = release.failure_evidence(REPO)
        self.assertIn("Failed job: Regression", evidence)
        self.assertIn("Actual assertion failed", evidence)
        self.assertIn("open-sse/utils/example.js:3", evidence)
        self.assertIn(f"Exact review: https://github.com/{REPO}/pull/1 at {HEAD}", evidence)
        self.assertIn("[P1] Formal exact-SHA finding", evidence)
        self.assertIn("[P2] Restore custom call type", evidence)
        self.assertIn("[P2] Native issue finding", evidence)
        self.assertNotIn("[P1] Stale original commit finding", evidence)
        self.assertNotIn("[P1] Spoofed actor finding", evidence)
        self.assertNotIn("[P1] Wrong application finding", evidence)
        self.assertEqual(commands, [
            ("gh", "api", "--help"),
            ("gh", "api", f"repos/{REPO}/actions/jobs/9/logs", "--allow-escape-sequences"),
        ])
        self.assertNotIn("\x1b", evidence)
        self.assertNotIn("\x01", evidence)

    def test_failure_evidence_uses_legacy_gh_without_escape_flag(self):
        jobs = {"total_count": 1, "jobs": [{"id": 9, "conclusion": "failure",
                "name": "Regression", "html_url": "https://github.com/example"}]}
        commands = []

        def run(*args, **_kwargs):
            commands.append(args)
            if args == ("gh", "api", "--help"):
                return "gh api [flags]"
            if args == ("gh", "api", f"repos/{REPO}/actions/jobs/9/logs"):
                return "\x1b[33mLegacy job log body\x1b[0m\x02"
            raise AssertionError(args)

        with (
            patch.dict(os.environ, ENV),
            patch.object(release, "api", return_value=jobs),
            patch.object(release, "pages", return_value=[]),
            patch.object(release, "run", side_effect=run),
        ):
            evidence = release.failure_evidence(REPO)
        self.assertIn("Legacy job log body", evidence)
        self.assertNotIn("\x1b", evidence)
        self.assertNotIn("\x02", evidence)
        self.assertEqual(commands, [
            ("gh", "api", "--help"),
            ("gh", "api", f"repos/{REPO}/actions/jobs/9/logs"),
        ])

    def test_failure_evidence_keeps_exact_review_body_with_a_healthy_job(self):
        native_state = {"code_review": {"commit_sha": HEAD, "status": "completed"}}
        finding = {
            "user": {"login": release.BOT, "type": "Bot"},
            "performed_via_github_app": {"slug": release.APP},
            "body": f"[P2] Healthy-job finding\n<!-- codex-review-state:v1 {json.dumps(native_state)} -->",
        }
        jobs = {"total_count": 1, "jobs": [{"id": 10, "conclusion": "success",
                                             "name": "Regression", "html_url": "https://github.com/example"}]}
        with (
            patch.dict(os.environ, ENV),
            patch.object(release, "api", return_value=jobs),
            patch.object(
                release,
                "pages",
                side_effect=lambda path: [finding] if path.endswith("/issues/1/comments") else [],
            ),
            patch.object(release, "run", side_effect=AssertionError("healthy job has no logs")),
        ):
            evidence = release.failure_evidence(REPO)
        self.assertIn(f"https://github.com/{REPO}/pull/1 at {HEAD}", evidence)
        self.assertIn("[P2] Healthy-job finding", evidence)
        self.assertNotIn("Failed job:", evidence)

    def test_blocked_policy_proposal_exports_pr_and_exact_head(self):
        state = {**STATE, "head": HEAD, "seed": SEED}
        policy_request = {
            "user": {"login": REPO.split("/")[0], "type": "User"},
            "created_at": STATE["created_at"],
            "body": f"@codex review\n\n<!-- zed-policy:{HEAD} -->",
        }

        def pages(path):
            if path == f"repos/{REPO}/issues/{state['number']}/comments":
                return [policy_request]
            raise AssertionError(path)

        with (
            patch.object(release, "repair_pull", return_value=state),
            patch.object(release, "repair_changes", return_value=(["scripts/release.py"], True)),
            patch.object(release, "pages", side_effect=pages),
            patch.object(release, "policy_review", return_value=(False, "Exact-SHA review blocked")),
            patch.object(release, "output") as output,
            self.assertRaisesRegex(ValueError, "Exact-SHA review blocked"),
        ):
            release.proposal(REPO, CONFIG, Path("/unused"))

        final_output = output.call_args.kwargs
        self.assertFalse(final_output["ready"])
        self.assertEqual(final_output["mode"], "policy")
        self.assertEqual(final_output["pr"], state["number"])
        self.assertEqual(final_output["sha"], state["head"])

    def test_p2_remains_nonblocking_but_gets_an_automatic_followup(self):
        bot = {"login": release.BOT, "type": "Bot"}
        app = {"slug": release.APP}
        formal = {"id": 11, "user": bot, "commit_id": HEAD, "body": "[P2] Formal bug"}
        inline = {"id": 12, "user": bot, "commit_id": CONFIG,
                  "original_commit_id": HEAD, "path": "src/example.rs", "line": 7,
                  "body": "[P3] Inline bug"}
        native = {"id": 13, "user": bot, "performed_via_github_app": app,
                  "body": f"[P2] Native bug\n**Reviewed commit:** `{HEAD[:10]}`"}
        duplicate = {"id": 14, "user": bot, "performed_via_github_app": app,
                     "body": ("[P2] One result\n"
                              f"<!-- codex-review-state:v1 {json.dumps({'code_review': {'commit_sha': HEAD}})} -->\n"
                              f"**Reviewed commit:** `{HEAD}`")}
        excluded_reviews = [
            {**formal, "id": 20, "commit_id": CONFIG},
            {**formal, "id": 21, "user": {"login": "human", "type": "User"}},
            {**formal, "id": 22, "user": {"login": release.BOT, "type": "User"}},
        ]
        excluded_inline = [
            {**inline, "id": 23, "original_commit_id": CONFIG},
            {**inline, "id": 24, "user": {"login": "human", "type": "User"}},
        ]
        excluded_comments = [
            {**native, "id": 25, "performed_via_github_app": None},
            {**native, "id": 26, "performed_via_github_app": {"slug": "other"}},
            {**native, "id": 27, "user": {"login": "human", "type": "User"}},
            {**native, "id": 28, "body": "[P2] unrelated"},
            {**native, "id": 29, "body": "[P2] **Reviewed commit:** `malformed`"},
            {**native, "id": 30, "body": (f"[P2] **Reviewed commit:** `{HEAD}`\n"
                                             f"**Reviewed commit:** `{CONFIG}`")},
            {**native, "id": 31, "body": f"[P2] **Reviewed commit:** `{CONFIG[:10]}`"},
        ]

        def pages(path):
            if path.endswith("/reviews"):
                return [formal, *excluded_reviews]
            if path.endswith("/pulls/1/comments"):
                return [inline, *excluded_inline]
            if path.endswith("/issues/1/comments"):
                return [native, duplicate, *excluded_comments]
            raise AssertionError(path)

        def api(path, **_kwargs):
            prefix = path.rsplit("/", 1)[-1]
            if prefix in {HEAD, HEAD[:10]}:
                return {"sha": HEAD}
            if prefix == CONFIG[:10]:
                return {"sha": CONFIG}
            raise subprocess.CalledProcessError(1, path)

        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            self.assertEqual(release.minor_findings(REPO, 1, HEAD),
                             [formal, inline, native, duplicate])
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


class AdoptedRepairBudgetTests(unittest.TestCase):
    CYCLE = "e" * 64
    ADOPTED = "f" * 40

    def previous_pull(self, state="closed", number=1):
        identity = {
            "schema": 1,
            "config": CONFIG,
            "upstream": UPSTREAM,
            "seed": SEED,
            "cycle": self.CYCLE,
            "nonblocking": False,
        }
        return {
            "number": number,
            "state": state,
            "user": {"login": REPO.split("/")[0], "type": "User"},
            "head": {"repo": {"full_name": REPO}, "ref": f"repair/previous-{number}"},
            "body": "<!-- zed-repair:v1 " + json.dumps(identity) + " -->",
        }

    def previous_requests(self, attempts=(1, 2, 3), number=1, timestamps=None):
        owner = {"login": REPO.split("/")[0], "type": "User"}
        heads = {1: SEED, 2: UPSTREAM, 3: CONFIG}
        timestamps = timestamps or {}
        requests = []
        for attempt in attempts:
            comment = request(heads[attempt], attempt, age=4 - attempt)
            comment["created_at"] = timestamps.get(
                attempt, f"2026-01-01T00:0{attempt}:00Z")
            comment["user"] = owner
            comment["html_url"] = f"https://github.com/{REPO}/pull/{number}#issuecomment-{attempt}"
            requests.append(comment)
        return requests

    def adoption_comment(self, user=None, created_at="2026-01-01T01:00:00Z"):
        comment = {
            "user": user or {"login": REPO.split("/")[0], "type": "User"},
            "body": (
                f"Verified repair adopted as `{self.ADOPTED}`; "
                "source-context files were not merged."
            ),
        }
        if created_at is not None:
            comment["created_at"] = created_at
        return comment

    def collect_requests(
        self,
        *,
        previous_state="closed",
        include_adoption=True,
        adoption_user=None,
        adoption_created_at="2026-01-01T01:00:00Z",
        comparison=None,
        compare_error=False,
        current_requests=None,
    ):
        state = {**STATE, "cycle": self.CYCLE}
        previous = self.previous_pull(previous_state)
        previous_requests = self.previous_requests()
        previous_comments = list(previous_requests)
        if include_adoption:
            previous_comments.append(self.adoption_comment(adoption_user, adoption_created_at))
        current_requests = current_requests or []
        compare_path = f"repos/{REPO}/compare/{self.ADOPTED}...{CONFIG}"

        def pages(path):
            if path == f"repos/{REPO}/issues/{state['number']}/comments":
                return current_requests
            if path == f"repos/{REPO}/pulls?state=all&base=downstream":
                return [previous]
            if path == f"repos/{REPO}/issues/{previous['number']}/comments":
                return previous_comments
            raise AssertionError(path)

        def api(path, **_kwargs):
            if path == compare_path:
                if compare_error:
                    raise subprocess.CalledProcessError(1, path)
                if comparison is None:
                    raise AssertionError("unexpected comparison")
                return comparison
            raise AssertionError(path)

        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            actual = release.repair_requests(REPO, state)
        return actual, previous_requests

    def test_verified_closed_adoption_excludes_only_previous_attempts(self):
        current = request(HEAD, 1)
        current["user"] = {"login": REPO.split("/")[0], "type": "User"}
        current["created_at"] = "2026-01-01T00:59:00Z"
        for status in ("ahead", "identical"):
            with self.subTest(status=status):
                actual, previous = self.collect_requests(
                    comparison={
                        "status": status,
                        "merge_base_commit": {"sha": self.ADOPTED},
                    },
                    current_requests=[current],
                )
                self.assertEqual(len(previous), 3)
                self.assertEqual(actual, [current])

    def test_unverified_previous_repairs_retain_their_attempts(self):
        cases = (
            ("open previous pull", {"previous_state": "open"}),
            ("missing adoption marker", {"include_adoption": False}),
            (
                "foreign adoption actor",
                {"adoption_user": {"login": "stranger", "type": "User"}},
            ),
            (
                "diverged comparison",
                {"comparison": {
                    "status": "diverged",
                    "merge_base_commit": {"sha": self.ADOPTED},
                }},
            ),
            (
                "nonmatching merge base",
                {"comparison": {
                    "status": "ahead",
                    "merge_base_commit": {"sha": "9" * 40},
                }},
            ),
            ("comparison API error", {"compare_error": True}),
        )
        for name, arguments in cases:
            with self.subTest(case=name):
                actual, previous = self.collect_requests(**arguments)
                self.assertEqual(len(previous), 3)
                self.assertEqual(actual, previous)

    def test_missing_malformed_or_naive_adoption_time_keeps_attempts(self):
        cases = (
            ("missing", None),
            ("malformed", "not-a-timestamp"),
            ("naive", "2026-01-01T01:00:00"),
        )
        for name, created_at in cases:
            with self.subTest(case=name):
                actual, previous = self.collect_requests(
                    adoption_created_at=created_at,
                    comparison={
                        "status": "ahead",
                        "merge_base_commit": {"sha": self.ADOPTED},
                    },
                )
                self.assertEqual(len(previous), 3)
                self.assertEqual(actual, previous)

    def test_later_historical_request_counts_after_offset_adoption_cutoff(self):
        state = {**STATE, "cycle": self.CYCLE, "number": 5}
        previous_one = self.previous_pull(number=1)
        previous_three = self.previous_pull(number=3)
        previous_four = self.previous_pull(number=4)
        attempts_one = self.previous_requests((1, 2), number=1)
        attempt_three = self.previous_requests((3,), number=3)
        later_request = self.previous_requests(
            (1,), number=4, timestamps={1: "2026-01-01T01:01:00Z"})
        adoption = self.adoption_comment(created_at="2026-01-01T03:00:00+02:00")
        compare_path = f"repos/{REPO}/compare/{self.ADOPTED}...{CONFIG}"

        def pages(path):
            if path == f"repos/{REPO}/issues/{state['number']}/comments":
                return []
            if path == f"repos/{REPO}/pulls?state=all&base=downstream":
                return [previous_one, previous_three, previous_four]
            if path == f"repos/{REPO}/issues/{previous_one['number']}/comments":
                return attempts_one
            if path == f"repos/{REPO}/issues/{previous_three['number']}/comments":
                return [*attempt_three, adoption]
            if path == f"repos/{REPO}/issues/{previous_four['number']}/comments":
                return later_request
            raise AssertionError(path)

        def api(path, **_kwargs):
            if path == compare_path:
                return {
                    "status": "ahead",
                    "merge_base_commit": {"sha": self.ADOPTED},
                }
            raise AssertionError(path)

        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            actual = release.repair_requests(REPO, state)
        self.assertEqual(actual, later_request)

    def test_request_repair_starts_at_attempt_one_after_verified_adoption(self):
        state = {**STATE, "cycle": self.CYCLE, "number": 5}
        previous_one = self.previous_pull(number=1)
        previous_three = self.previous_pull(number=3)
        attempts_one = self.previous_requests((1, 2), number=1)
        attempt_three = self.previous_requests((3,), number=3)
        previous_comments = [*attempt_three, self.adoption_comment()]
        pages_calls = []
        api_calls = []
        request_body = []
        compare_path = f"repos/{REPO}/compare/{self.ADOPTED}...{CONFIG}"

        def pages(path):
            pages_calls.append(path)
            if path == f"repos/{REPO}/issues/{state['number']}/comments":
                return []
            if path == f"repos/{REPO}/pulls?state=all&base=downstream":
                return [previous_one, previous_three]
            if path == f"repos/{REPO}/issues/{previous_one['number']}/comments":
                return attempts_one
            if path == f"repos/{REPO}/issues/{previous_three['number']}/comments":
                return previous_comments
            raise AssertionError(path)

        def api(path, *, method="GET", data=None, token=None):
            api_calls.append((path, method, data, token))
            if path == f"repos/{REPO}/commits/downstream" and method == "GET":
                return {"sha": CONFIG}
            if path == compare_path and method == "GET":
                return {
                    "status": "identical",
                    "merge_base_commit": {"sha": self.ADOPTED},
                }
            if path == f"repos/{REPO}/issues/{state['number']}/comments" and method == "POST":
                request_body.append(data["body"])
                return {"id": 77}
            raise AssertionError((path, method))

        with (
            patch.dict(os.environ, ENV),
            patch.object(release, "repair_pull", return_value=state),
            patch.object(release, "pages", side_effect=pages),
            patch.object(release, "api", side_effect=api),
            patch.object(release, "failure_evidence", return_value="actual fixture failure"),
            patch.object(release, "owner_token", return_value="fixture-owner-token") as owner_token,
        ):
            release.request_repair(Path("/unused"))

        owner_token.assert_called_once_with(REPO)
        self.assertEqual(len(attempts_one) + len(attempt_three), 3)
        self.assertEqual(len(request_body), 1)
        self.assertIn(f"Attempt 1/{release.MAX_FIX_ATTEMPTS}.", request_body[0])
        self.assertIn(f"<!-- zed-fix:{HEAD}:1 -->", request_body[0])
        self.assertIn("actual fixture failure", request_body[0])
        self.assertIn(f"repos/{REPO}/pulls?state=all&base=downstream", pages_calls)
        self.assertIn(f"repos/{REPO}/issues/{previous_one['number']}/comments", pages_calls)
        self.assertIn(f"repos/{REPO}/issues/{previous_three['number']}/comments", pages_calls)
        self.assertTrue(any(
            path == compare_path and method == "GET"
            for path, method, _, _ in api_calls
        ))
        posted = next(
            call for call in api_calls
            if call[0] == f"repos/{REPO}/issues/{state['number']}/comments"
            and call[1] == "POST"
        )
        self.assertEqual(posted[3], "fixture-owner-token")
        self.assertEqual(release.MAX_FIX_ATTEMPTS, 3)


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
