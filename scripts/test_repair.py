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
    def test_shared_patch_registration_precedes_review_for_direct_and_repair_candidates(self):
        for repaired in (False, True):
            for registered in (False, True):
                with self.subTest(repaired=repaired, registered=registered), \
                        tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    patch_path = root / "new.patch"
                    patch_path.write_text(
                        "diff --git a/crates/workspace/src/tests.rs b/crates/workspace/src/tests.rs\n"
                        "+++ b/crates/workspace/src/tests.rs\n"
                        "+#[gpui::test]\n+async fn test_new_workspace_case() {}\n")
                    state = {**STATE, "waiting": False, "policy": False,
                             "patches": root} if repaired else None
                    manifest = {"schema": 1, "test_cases": [["workspace", "test_existing_workspace_case"]],
                                "regression_cases": [["workspace", "test_new_workspace_case"]]
                                if registered else []}
                    with (patch.dict(os.environ, ENV, clear=True),
                          patch.object(release, "api", return_value={"sha": CONFIG}),
                          patch.object(release, "mirror_main"),
                          patch.object(release, "proposal", return_value=state),
                          patch.object(release, "patch_identity", return_value=([patch_path], "e" * 64)),
                          patch.object(release, "active_snapshot", return_value=UPSTREAM),
                          patch.object(release, "output"),
                          patch.object(release, "policy_snapshot", return_value={}),
                          patch.object(release, "validate_paths", side_effect=RuntimeError("registration accepted")) as next_stage,
                          patch.object(release, "run", side_effect=lambda *args, **kwargs:
                                       json.dumps(manifest) if args[:2] == ("git", "show")
                                       else HEAD if args == ("git", "rev-parse", "HEAD") else "") as run,
                          patch.object(release, "reserve_release") as reserve):
                        if registered:
                            with self.assertRaisesRegex(RuntimeError, "registration accepted"):
                                release.prepare(root / "app", shared=True)
                            next_stage.assert_called_once()
                        else:
                            with self.assertRaisesRegex(ValueError, "workspace::test_new_workspace_case"):
                                release.prepare(root / "app", shared=True)
                            next_stage.assert_not_called()
                        expected_source = HEAD if repaired else CONFIG
                        manifest_reads = [call for call in run.call_args_list
                                          if call.args == ("git", "show", f"{expected_source}:patches/source.json")]
                        self.assertEqual(len(manifest_reads), 1)
                        self.assertEqual(manifest_reads[0].kwargs, {"cwd": release.ROOT})
                        reserve.assert_not_called()

    def test_deferred_proposal_preserves_recovery_and_bounded_repair_paths(self):
        # An unresolved repair must not fall back to building the original tree.
        # Expiration remains observable even if the native task never responds.
        for nonblocking in (False, True):
            for expired in (False, True):
                with self.subTest(nonblocking=nonblocking, expired=expired):
                    state = {**STATE, "nonblocking": nonblocking}
                    old = request(HEAD, age=release.MAX_REPAIR_SECONDS + 1 if expired else 0)
                    outputs = {}
                    with (tempfile.TemporaryDirectory() as directory,
                          patch.dict(os.environ, ENV, clear=True),
                          patch.object(release, "api", return_value={"sha": CONFIG}),
                          patch.object(release, "mirror_main"),
                          patch.object(release, "repair_pull", return_value=state),
                          patch.object(release, "repair_comments", return_value=[old]),
                          patch.object(release, "output", side_effect=lambda **v: outputs.update(v)),
                          patch.object(release, "run") as run,
                          patch.object(release, "final_attempt") as final,
                          patch.object(release, "request_repair") as repair):
                        if expired:
                            with self.assertRaisesRegex(ValueError, "exceeded"):
                                release.prepare(Path(directory) / "app")
                        else:
                            release.prepare(Path(directory) / "app")
                            self.assertIn("Codex", outputs["reason"])
                        self.assertFalse(outputs["ready"])
                        run.assert_not_called()
                        final.assert_not_called()
                        repair.assert_not_called()

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
              patch.object(release, "repair_comments", return_value=[request(), review]),
              patch.object(release, "repair_changes", return_value=(["scripts/release.py"], True)),
              patch.object(release, "policy_review", return_value=(True, "Accepted")),
              patch.object(release, "minor_findings", return_value=[{"body": "P2 fix"}]),
              patch.object(release, "copy_patch_series", return_value=Path("/unused/proposal")),
              patch.object(release, "output") as output):
            result = release.proposal(REPO, CONFIG, Path("/unused"))
        self.assertTrue(result["policy"])
        self.assertFalse(result["waiting"])
        self.assertFalse(output.call_args.kwargs["ready"])
        # Gathering a policy proposal is not acceptance: prepare still requires
        # both full reports and includes minor findings in the repair batch.

    def test_recovered_candidate_closes_only_the_unchanged_blocking_repair(self):
        with (patch.object(release, "api", side_effect=[
                  {"sha": CONFIG}, {"state": "open", "head": {"sha": HEAD}}, {}, {}]) as api,
              patch.object(release, "cleanup_closed_pull_refs") as cleanup):
            release.close_recovered_repair(REPO, CONFIG, state=STATE)
        self.assertEqual(api.call_args.kwargs, {"method": "PATCH", "data": {"state": "closed"}})
        cleanup.assert_called_once_with(REPO, 2, HEAD, shared=False)

    def test_late_lookup_closes_the_captured_unchanged_blocking_repair(self):
        with (patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "api", side_effect=[
                  {"sha": CONFIG}, {"state": "open", "head": {"sha": HEAD}}, {}, {}]) as api,
              patch.object(release, "cleanup_closed_pull_refs") as cleanup):
            release.close_recovered_repair(REPO, CONFIG, expected_head=HEAD)
        self.assertEqual(api.call_args.kwargs, {"method": "PATCH", "data": {"state": "closed"}})
        cleanup.assert_called_once_with(REPO, 2, HEAD, shared=False)

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
              patch.object(release, "check_review", return_value=(True, "Accepted")),
              patch.object(release, "check_delta_for_publication"),
              patch.object(release, "complete_adoption"),
              patch.object(release, "minor_findings", return_value=[finding]),
              patch.object(release, "api", side_effect=api),
              patch.object(release, "pages", return_value=[]),
              patch.object(release, "run", side_effect=run),
              patch.object(release.subprocess, "check_output", return_value=b"verified diff"),
              patch.object(release.subprocess, "run", return_value=SimpleNamespace(stdout="", returncode=1)),
              patch.object(release, "request_repair") as followup,
              patch.object(release, "cleanup_closed_pull_refs")):
            with self.assertRaisesRegex(ValueError, "Outstanding review batch") as error:
                release.land_repair(Path(directory))
        followup.assert_not_called()
        self.assertIn("P2 policy followup", str(error.exception))

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
              patch.object(release, "check_delta_for_publication"),
              patch.object(release, "complete_adoption"),
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
            with self.assertRaisesRegex(ValueError, "Outstanding review batch") as error:
                release.land_repair(Path(directory))
        findings.assert_called_once_with(REPO, 7, application)
        followup.assert_not_called()
        self.assertIn(application, str(error.exception))
        self.assertIn("crates/app.rs:17\nP2 application followup", str(error.exception))


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
        prepared = {
            "repository": REPO,
            "pull_request": 7,
            "head": "e" * 40,
            "seed": "f" * 40,
            "config": "9" * 40,
            "current_config": CONFIG,
            "upstream": UPSTREAM,
            "baseline_patches_tree": "8" * 40,
            "changed_paths": ["patches/0014-Fix-terminal-cleanup.patch"],
        }
        with (contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5],
              patch.object(release, "prepared_app_candidate", return_value=prepared) as prepare):
            release.request_repair(Path("/unused"))
        sent = api.call_args
        self.assertEqual(sent.args[0], f"repos/{REPO}/issues/2/comments")
        self.assertEqual(sent.kwargs["token"], "test-only")
        body = sent.kwargs["data"]["body"]
        prepare.assert_called_once_with(REPO, STATE)
        self.assertTrue(body.startswith("@codex fix "))
        self.assertIn(f"<!-- zed-fix:{HEAD}:1 -->", body)
        self.assertIn("push the actual fix", body)
        self.assertIn("Actual failed assertion", body)
        self.assertEqual(body.count(json.dumps(prepared, sort_keys=True)), 1)

    def test_same_head_is_not_requested_twice(self):
        contexts = self.invoke([request(HEAD)])
        with contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5]:
            release.request_repair(Path("/unused"))
        self.assertEqual(api.call_count, 1)

    def test_third_failed_fix_escalates_without_a_fourth_task(self):
        # The retained boundary is elapsed time, not an owner-imposed retry count.
        requests = [
            request(SEED, 1, age=release.MAX_REPAIR_SECONDS + 1),
            request(UPSTREAM, 2), request(CONFIG, 3)]
        contexts = self.invoke(requests)
        with (contexts[0], contexts[1] as api, contexts[2], contexts[3], contexts[4], contexts[5],
              patch.object(release, "escalation", side_effect=ValueError("escalated")) as escalate,
              self.assertRaisesRegex(ValueError, "escalated")):
            release.request_repair(Path("/unused"))
        self.assertEqual(api.call_count, 1)
        self.assertEqual(escalate.call_count, 1)
        self.assertIn("exceeded", escalate.call_args.args[3])

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
        self.assertIn(f"Complete project review: https://github.com/{REPO}/pull/1 at {HEAD}", evidence)
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
            "created_at": request()["created_at"],
            "body": f"@codex implement\n\n<!-- zed-fix:{SEED}:1 -->",
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
            patch.object(release, "copy_patch_series", return_value=Path("/unused/proposal")),
            patch.object(release, "output") as output,
        ):
            result = release.proposal(REPO, CONFIG, Path("/unused"))

        final_output = output.call_args.kwargs
        self.assertFalse(final_output["ready"])
        self.assertTrue(result["policy"])
        self.assertEqual(final_output["repair_pr"], state["number"])
        self.assertEqual(final_output["repair_head"], state["head"])

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
            patch.object(release, "prepared_app_candidate", return_value=None) as prepared_candidate,
        ):
            release.request_repair(Path("/unused"))

        owner_token.assert_called_once_with(REPO)
        prepared_candidate.assert_called_once_with(REPO, state)
        self.assertEqual(len(attempts_one) + len(attempt_three), 3)
        self.assertEqual(len(request_body), 1)
        self.assertRegex(request_body[0], r"Attempt 1(?:/3)?\.")
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


class PreparedCandidateTests(unittest.TestCase):
    CYCLE = "e" * 64

    def git(self, root, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, root, message, paths):
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", "--", *paths],
            cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertEqual(ignored.returncode, 1, ignored.stdout + ignored.stderr)
        self.git(root, "add", "--", *paths)
        self.git(
            root,
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false",
            "commit", "-m", message,
        )
        return self.git(root, "rev-parse", "HEAD")

    def make_fixture(
        self,
        root,
        *,
        current_tree_change=None,
        context_change=None,
        candidate_delta="application",
    ):
        root.mkdir()
        self.git(root, "init")
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        (root / "patches").mkdir()
        (root / "scripts").mkdir()
        base_patch = root / "patches/0013-existing.patch"
        source_manifest = root / "patches/source.json"
        release_script = root / "scripts/release.py"
        base_patch.write_text("diff --git a/example b/example\n")
        source_manifest.write_text('{"source":"fixture"}\n')
        release_script.write_text("POLICY_VERSION = 1\n")
        config = self.commit(
            root,
            "Trusted original configuration",
            ["patches/0013-existing.patch", "patches/source.json", "scripts/release.py"],
        )

        self.git(root, "switch", "-c", "current-policy", config)
        release_script.write_text("POLICY_VERSION = 1\nPOLICY_CHANGE = True\n")
        current_paths = ["scripts/release.py"]
        if current_tree_change == "patch":
            base_patch.write_text("different baseline patch bytes\n")
            current_paths.append("patches/0013-existing.patch")
        elif current_tree_change == "source":
            source_manifest.write_text('{"source":"changed fixture"}\n')
            current_paths.append("patches/source.json")
        current_config = self.commit(root, "Separate policy configuration", current_paths)

        self.git(root, "switch", "--detach", config)
        context = {
            "schema": 1,
            "config": config,
            "upstream": UPSTREAM,
            "cycle": self.CYCLE,
            "nonblocking": False,
        }
        if context_change:
            context.update(context_change)
        (root / ".repair").mkdir()
        context_path = root / ".repair/context.json"
        context_path.write_text(json.dumps(context, indent=2) + "\n")
        seed = self.commit(root, "Immutable repair context seed", [".repair/context.json"])

        candidate_paths = []
        if candidate_delta in {"application", "mixed"}:
            (root / "patches/0014-new.patch").write_text("diff --git a/new b/new\n")
            candidate_paths.append("patches/0014-new.patch")
        if candidate_delta in {"policy", "mixed"}:
            (root / "scripts/test_policy_candidate.py").write_text("POLICY_TEST = True\n")
            candidate_paths.append("scripts/test_policy_candidate.py")
        head = self.commit(root, "Prepared repair candidate", candidate_paths) if candidate_paths else seed
        return {
            "root": root,
            "config": config,
            "current_config": current_config,
            "seed": seed,
            "head": head,
            "upstream": UPSTREAM,
            "cycle": self.CYCLE,
            "context_path": context_path,
        }

    def pull_for(self, fixture, *, number=7, user=None, head_repo=REPO,
                 head_ref="repair/prepared", head_sha=None, prior_changes=None):
        prior = {
            "schema": 1,
            "config": fixture["config"],
            "upstream": fixture["upstream"],
            "seed": fixture["seed"],
            "cycle": fixture["cycle"],
            "nonblocking": False,
        }
        prior.update(prior_changes or {})
        return {
            "number": number,
            "state": "open",
            "user": user or {"login": REPO.split("/")[0], "type": "User"},
            "head": {
                "repo": {"full_name": head_repo},
                "ref": head_ref,
                "sha": head_sha or fixture["head"],
            },
            "body": "<!-- zed-repair:v1 " + json.dumps(prior) + " -->",
        }

    def call_candidate(self, fixture, pull, *, reject_git=False):
        current = {
            "number": 8,
            "config": fixture["current_config"],
            "upstream": fixture["upstream"],
            "cycle": fixture["cycle"],
        }
        commands = []
        fetches = []
        real_run = release.run

        def pages(path):
            if path == f"repos/{REPO}/pulls?state=all&base=downstream":
                return [pull]
            raise AssertionError(path)

        def run(*args, cwd=None, env=None):
            commands.append(args)
            if args[:3] == ("git", "fetch", "--no-tags"):
                fetches.append(args)
                if reject_git:
                    raise AssertionError("Invalid candidate must be rejected before Git")
                return ""
            if reject_git:
                raise AssertionError("Invalid candidate must be rejected before Git")
            return real_run(*args, cwd=cwd, env=env)

        with (
            patch.object(release, "ROOT", fixture["root"]),
            patch.object(release, "pages", side_effect=pages),
            patch.object(release, "run", side_effect=run),
        ):
            result = release.prepared_app_candidate(REPO, current)
        return result, commands, fetches

    def test_valid_prepared_candidate_returns_exact_descriptor_without_copying(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(Path(temporary) / "repo")
            pull = self.pull_for(fixture)
            context_bytes = fixture["context_path"].read_bytes()
            result, commands, fetches = self.call_candidate(fixture, pull)
            expected_tree = self.git(
                fixture["root"], "rev-parse", f"{fixture['current_config']}:patches")
            self.assertEqual(result, {
                "repository": REPO,
                "pull_request": 7,
                "head": fixture["head"],
                "seed": fixture["seed"],
                "config": fixture["config"],
                "current_config": fixture["current_config"],
                "upstream": fixture["upstream"],
                "baseline_patches_tree": expected_tree,
                "changed_paths": ["patches/0014-new.patch"],
            })
            self.assertNotEqual(fixture["config"], fixture["current_config"])
            self.assertEqual(
                self.git(fixture["root"], "rev-parse", f"{fixture['config']}:patches"),
                expected_tree,
            )
            self.assertEqual(
                self.git(fixture["root"], "diff", "--name-only",
                         fixture["config"], fixture["current_config"]),
                "scripts/release.py",
            )
            self.assertEqual(len(fetches), 2)
            self.assertTrue(any(command[:2] == ("git", "merge-base") for command in commands))
            self.assertTrue(any(command[:2] == ("git", "diff") for command in commands))
            self.assertTrue(any(command[:2] == ("git", "ls-tree") for command in commands))
            self.assertTrue(any(command[:2] == ("git", "show") for command in commands))
            self.assertEqual(fixture["context_path"].read_bytes(), context_bytes)
            self.assertFalse((fixture["root"] / "proposal").exists())
            self.assertEqual(self.git(fixture["root"], "status", "--short"), "")
            self.assertEqual(
                self.git(fixture["root"], "merge-base", "--is-ancestor",
                         fixture["config"], fixture["current_config"]),
                "",
            )

    def test_identity_filters_reject_without_fetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_fixture(Path(temporary) / "repo")
            variants = (
                ("wrong login", {"user": {"login": "stranger", "type": "User"}}, {}),
                ("wrong user type", {"user": {"login": REPO.split("/")[0], "type": "Bot"}}, {}),
                ("wrong app actor type", {"user": {"login": "github-actions[bot]", "type": "User"}}, {}),
                ("fork head repository", {"head_repo": "other/fork"}, {}),
                ("non-repair branch", {"head_ref": "review/prepared"}, {}),
                ("changed upstream", {}, {"upstream": "9" * 40}),
                ("changed cycle", {}, {"cycle": "8" * 64}),
                ("malformed config SHA", {}, {"config": "invalid"}),
                ("malformed seed SHA", {}, {"seed": "invalid"}),
                ("malformed upstream SHA", {}, {"upstream": "invalid"}),
                ("malformed head SHA", {"head_sha": "invalid"}, {}),
            )
            for name, pull_changes, prior_changes in variants:
                with self.subTest(case=name):
                    pull = self.pull_for(fixture, prior_changes=prior_changes, **pull_changes)
                    result, commands, fetches = self.call_candidate(fixture, pull, reject_git=True)
                    self.assertIsNone(result)
                    self.assertEqual(commands, [])
                    self.assertEqual(fetches, [])

    def test_different_baseline_patch_or_source_manifest_bytes_are_rejected(self):
        for change in ("patch", "source"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_fixture(
                    Path(temporary) / "repo",
                    current_tree_change=change,
                )
                result, commands, fetches = self.call_candidate(
                    fixture, self.pull_for(fixture))
                self.assertIsNone(result)
                self.assertEqual(len(fetches), 2)
                self.assertTrue(any(command[:2] == ("git", "rev-parse") for command in commands))

    def test_context_mismatch_policy_only_mixed_and_invalid_seed_are_rejected(self):
        cases = (
            ("context mismatch", {"context_change": {"cycle": "7" * 64}, "candidate_delta": "application"},
             None),
            ("policy-only delta", {"candidate_delta": "policy"}, None),
            ("mixed delta", {"candidate_delta": "mixed"}, None),
            ("invalid seed ancestry", {"candidate_delta": "application"}, "config"),
        )
        for name, fixture_args, invalid_seed in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_fixture(Path(temporary) / "repo", **fixture_args)
                prior_changes = {"seed": fixture["config"]} if invalid_seed else None
                result, _, fetches = self.call_candidate(
                    fixture,
                    self.pull_for(fixture, prior_changes=prior_changes),
                )
                self.assertIsNone(result)
                self.assertEqual(len(fetches), 2)


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

    def test_missing_delta_review_cannot_reach_index_or_publication(self):
        commands = []

        def execute(args, **kwargs):
            commands.append(args)
            self.assertNotEqual(args[:2], ["git", "apply"], "unreviewed delta must never reach the index")
            return SimpleNamespace(stdout="", returncode=1)

        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, ENV, clear=True),
              patch.object(release, "repair_pull", return_value=STATE),
              patch.object(release, "repair_changes", return_value=(["patches/0009-fix.patch"], False)),
              patch.object(release, "check_review", return_value=(True, "reviewed")),
              patch.object(release, "minor_findings", return_value=[]),
              patch.object(release, "api", return_value={"sha": CONFIG}),
              patch.object(release, "run", return_value="") as run,
              patch.object(release.subprocess, "check_output", return_value=b"delta"),
              patch.object(release.subprocess, "run", side_effect=execute),
              self.assertRaisesRegex(ValueError, "Delta review does not identify")):
            release.land_repair(Path(directory))
        self.assertTrue(commands)
        self.assertFalse(any(call.args[:2] == ("git", "push") for call in run.call_args_list))

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
            for limit in ("--pids-limit=256", "--cpus=2", "--memory=2g"):
                self.assertIn(limit, args)
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
                  "CODEX_REQUEST_TOKEN": "fake", "NINEROUTER_KEY": "fake", "GITHUB_ENV": "/not-allowed",
                  "FORK_MAINTENANCE_ROOT": ""}),
              patch.object(release.subprocess, "run", side_effect=execute),
              patch.object(release, "urlopen") as fetch,
              self.assertRaisesRegex(ValueError, "actionlint archive integrity")):
            fetch.return_value.read.return_value = b"not an authenticated release"
            release.check_policy(Path(directory))
        self.assertEqual(suites[0], str(release.ROOT / "scripts"))
        self.assertEqual(suites[1], str(Path(directory) / "scripts"))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_policy_timeout_stops_owned_container_and_collects_next_suite(self):
        calls = []
        def execute(args, **kwargs):
            calls.append(args)
            if args[:3] == ["docker", "stop", "--timeout=0"]:
                self.assertEqual(args[-1], calls[0][calls[0].index("--name") + 1])
                self.assertEqual(kwargs["timeout"], 20)
                return SimpleNamespace(returncode=0)
            self.assertEqual(kwargs["timeout"], 180)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, 180)
            return SimpleNamespace(returncode=0)
        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": ""}),
              patch.object(release.subprocess, "run", side_effect=execute),
              patch.object(release, "urlopen", side_effect=AssertionError("Failed policy cannot reach lint")),
              self.assertRaises(subprocess.TimeoutExpired)):
            release.check_policy(Path(directory))
        self.assertEqual(len(calls), 3)
        self.assertNotEqual(calls[0][-1], calls[2][-1])

    def test_policy_timeout_refuses_to_continue_when_container_cleanup_fails(self):
        calls = []
        def execute(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, 180)
            return SimpleNamespace(returncode=1, args=args, stdout="", stderr="daemon unavailable")
        with (tempfile.TemporaryDirectory() as directory,
              patch.dict(os.environ, {"FORK_MAINTENANCE_ROOT": ""}),
              patch.object(release.subprocess, "run", side_effect=execute),
              patch.object(release, "urlopen", side_effect=AssertionError("Cleanup failure blocks lint")),
              self.assertRaises(subprocess.CalledProcessError)):
            release.check_policy(Path(directory))
        self.assertEqual(len(calls), 2)

    def test_failed_policy_tests_cannot_reach_linting_or_write_credentials(self):
        with (patch.object(release.subprocess, "run",
                          side_effect=subprocess.CalledProcessError(1, "unittest")),
              patch.object(release, "urlopen", side_effect=AssertionError("Stop at failed tests")),
              self.assertRaises(subprocess.CalledProcessError)):
            release.check_policy(Path("/unused"))
