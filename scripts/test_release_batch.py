from contextlib import ExitStack
from contextlib import redirect_stdout
from datetime import datetime, timezone
import json
import io
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import release


class BatchedReviewTests(unittest.TestCase):
    def prepare_candidate(self, full, delta, findings=(), *, published=False, changed_policy=False,
                          recipe_failure=False, verified_policy=False, shared=False,
                          bootstrap=False, mismatched_bootstrap=False, adopted_config=None,
                          reuse_artifact=False, bootstrap_decision="reuse",
                          bootstrap_bundle_verified=True, bootstrap_bundle_malformed=False):
        config, upstream, candidate = "a" * 40, "b" * 40, "c" * 40
        shared_candidate = {"schema": 1, "repository": "maintainer/zed-fork",
                            "upstream_sha": upstream, "application_sha": candidate,
                            "configuration_sha": config, "patchset_sha256": "d" * 64,
                            "recipe_sha256": "e" * 64, "platform": "macos/aarch64"}
        outputs = {}
        order = []
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            app = Path(temporary) / "app"

            def run(*args, **kwargs):
                if args[:2] == ("git", "init"):
                    manifest = app / "crates/zed/Cargo.toml"
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text('[package]\nversion = "1.24.2"\n')
                if args[:3] == ("git", "rev-parse", "HEAD"):
                    return candidate
                if args[:2] == ("git", "show"):
                    return '[package]\nversion = "1.24.2"\n'
                return ""

            def pages(path):
                if "/releases" in path:
                    tag = release.release_tag("1.24.2", upstream, "d" * 64, candidate)
                    if published:
                        return [{"draft": False, "tag_name": tag}]
                    if shared and reuse_artifact:
                        return [{"draft": True, "tag_name": tag}]
                    return []
                if "/pulls?" in path:
                    return [{"number": 1, "state": "open", "head": {"sha": candidate}}]
                review_config = adopted_config or config
                return [{"body": (f"<!-- zed-codex:{candidate} -->\n"
                                   f"<!-- zed-full:{candidate}:{review_config} -->"),
                         "user": {"login": "maintainer"},
                         "created_at": datetime.now(timezone.utc).isoformat()}]

            environment = {
                "GITHUB_REPOSITORY": "maintainer/zed-fork", "GITHUB_SHA": config,
                "GITHUB_RUN_ID": "123",
            }
            if verified_policy:
                environment["ZED_TRUSTED_POLICY_SHA"] = adopted_config or config
            if bootstrap:
                receipt_candidate = {**shared_candidate}
                if mismatched_bootstrap:
                    receipt_candidate["configuration_sha"] = "9" * 40
                environment["BOOTSTRAP_MAINTENANCE_CANDIDATE"] = json.dumps(
                    receipt_candidate, sort_keys=True, separators=(",", ":"))
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            for name, value in {
                "mirror_main": None, "proposal": None,
                "active_snapshot": None,
                "select_upstream": ("v1.24.2", upstream),
                "patch_identity": ([Path("fixture.patch")], "d" * 64),
                "ensure_forward_release": None, "push_review_refs": None,
                "check_review": full, "delta_review": (2, "e" * 40, config, *delta),
                "minor_findings": list(findings), "review_records": list(findings),
                "retire_superseded_reviews": None, "pending_review": None,
                "verify_published_release": None,
            }.items():
                stack.enter_context(patch.object(release, name, return_value=value))
            if shared:
                private = type("PrivateLifecycle", (), {})()
                private._maintenance_lock_identity = {
                    "repository": "warelik/machine-maintenance", "revision": "a" * 40,
                    "module_sha256": "b" * 64, "api_version": 1}
                private.candidate_identity = lambda _candidate: "f" * 64
                def validated_bundle(*_args, **_kwargs):
                    if not bootstrap_bundle_verified:
                        return None
                    proof = {"decision": "wrong-proof" if bootstrap_bundle_malformed
                             else "reuse-policy-validation", "candidate_id": "f" * 64,
                             "heavy_build_allowed": False, "application_artifact_verified": False,
                             "shared_source": private._maintenance_lock_identity}
                    return proof
                private.validated_bundle = validated_bundle
                baseline = {"variable": {"value": json.dumps({"publication": {
                    "published_at": "2026-10-19T10:00:00Z"}})}}
                stack.enter_context(patch.object(
                    release, "maintenance_candidate",
                    side_effect=lambda _repo, reviewed, *_args: {
                        **shared_candidate, "configuration_sha": reviewed}))
                stack.enter_context(patch.object(release, "accepted_baseline",
                                                 return_value=(baseline, {}, {})))
                stack.enter_context(patch.object(release, "review_context_commit",
                                                 return_value=candidate))
                stack.enter_context(patch.object(release, "maintenance_pair", return_value={
                    "state": "ready", "findings": [], "known_findings": [],
                    "review_dispositions": [], "fresh_findings": []}))
                final_decision = ({"decision": bootstrap_decision}
                                  if bootstrap else
                                  {"decision": "already-validated",
                                   "validated_run": {"id": 12, "run_attempt": 3},
                                   "artifact_workflow_sha": "9" * 40}
                                  if reuse_artifact else {"decision": "policy-only"})
                authorize = stack.enter_context(patch.object(
                    release, "authorize_shared_final", return_value=final_decision))
                stack.enter_context(patch.object(release, "owner_token", return_value="owner-token"))
                stack.enter_context(patch.object(release.maintenance, "load", return_value=private))
                if adopted_config:
                    stack.enter_context(patch.object(release, "adopted_review_configuration",
                                                     return_value=adopted_config))
                stack.enter_context(patch.object(
                    release, "delta_review",
                    return_value=(2, "e" * 40, adopted_config or config, *delta)))
            if changed_policy:
                stack.enter_context(patch.object(release, "policy_snapshot",
                                                side_effect=[{"release.py": "new"}, {"release.py": "checked"}]))
            stack.enter_context(patch.object(release, "run", side_effect=run))
            stack.enter_context(patch.object(release, "pages", side_effect=pages))
            stack.enter_context(patch.object(release, "output", side_effect=lambda **v: outputs.update(v)))
            stack.enter_context(patch.object(release, "api", side_effect=lambda path, **kw:
                                {"sha": config if path.endswith("/downstream") else upstream}))
            recipe = stack.enter_context(patch.object(
                release, "verify_upstream_recipes",
                side_effect=ValueError("upstream recipe drift") if recipe_failure else None))
            final = stack.enter_context(patch.object(release, "final_attempt",
                                       side_effect=lambda *a, **kw: order.append("final")))
            reserve = stack.enter_context(patch.object(release, "reserve_release",
                                         side_effect=lambda *a: order.append("reserve") or 10))
            failure = None
            try:
                release.prepare(app, shared=shared)
            except ValueError as error:
                failure = str(error)
            self.last_order = order
            self.last_recipe_checks = recipe.call_args_list
            self.last_authorize_calls = authorize.call_count if shared else 0
            self.last_authorize_kwargs = authorize.call_args.kwargs if shared and authorize.call_args else {}
            return outputs, final.call_count, reserve.call_count, failure

    def test_upstream_recipe_drift_stops_before_reservation_and_heavy_receipt(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), recipe_failure=True, verified_policy=True)
        self.assertFalse(outputs["ready"])
        self.assertEqual(failure, "upstream recipe drift")
        self.assertEqual((final, reserve), (0, 0))
        self.assertEqual(len(self.last_recipe_checks), 1)
        self.assertEqual(self.last_recipe_checks[0].args[1], "b" * 40)

    def test_unverified_policy_port_defers_to_its_checked_linux_controller(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), recipe_failure=True)
        self.assertIsNone(failure, "the obsolete controller must not block its reviewed replacement")
        self.assertTrue(outputs["ready"])
        self.assertFalse(outputs["policy_verified"])
        self.assertEqual(self.last_recipe_checks, [])
        self.assertEqual((final, reserve), (1, 1))

    def test_one_blocking_report_waits_for_the_other_complete_list(self):
        for full, delta in (
            ((False, "P1 found"), (False, release.WAITING_REVIEW)),
            ((False, release.WAITING_REVIEW), (False, "P1 found")),
        ):
            with self.subTest(full=full, delta=delta):
                outputs, final, reserve, failure = self.prepare_candidate(full, delta)
                self.assertFalse(outputs["ready"])
                self.assertIsNone(failure, "do not dispatch a partial repair batch")
                self.assertEqual((final, reserve), (0, 0))

    def test_complete_reports_include_minor_findings_before_final_validation(self):
        findings = [{"path": "crates/a.rs", "line": 1, "body": "P1 first bug"},
                    {"path": "crates/b.rs", "line": 2, "body": "P2 second bug"}]
        outputs, final, reserve, failure = self.prepare_candidate(
            (False, "P1 found"), (True, "complete"), findings)
        self.assertFalse(outputs["ready"])
        self.assertIn("P1 first bug", failure)
        self.assertIn("P2 second bug", failure)
        self.assertEqual((final, reserve), (0, 0))

    def test_only_two_clean_complete_reports_start_one_final_sequence(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"))
        self.assertIsNone(failure)
        self.assertTrue(outputs["ready"])
        self.assertEqual((final, reserve), (1, 1))
        self.assertEqual(self.last_order, ["reserve", "final"])

    def test_new_policy_is_validated_even_when_the_application_is_already_published(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), published=True, changed_policy=True)
        self.assertIsNone(failure)
        self.assertTrue(outputs["ready"])
        self.assertTrue(outputs["already_published"])
        self.assertEqual((final, reserve), (1, 0), "validate policy without reserving another app build")

    def test_missing_pin_bootstrap_reaches_activation_after_verified_bundle_for_published_source(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), published=True,
            shared=True, bootstrap=True, verified_policy=True)
        self.assertIsNone(failure)
        self.assertTrue(outputs["ready"])
        self.assertTrue(outputs["already_published"])
        self.assertTrue(outputs["policy_verified"])
        self.assertEqual((final, reserve), (0, 0))
        self.assertEqual(self.last_authorize_calls, 1)
        self.assertTrue(self.last_authorize_kwargs["allow_reuse"])

    def test_unverified_bootstrap_does_not_bypass_published_early_exit(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), published=True, shared=True)
        self.assertIsNone(failure)
        self.assertFalse(outputs["ready"])
        self.assertTrue(outputs["already_published"])
        self.assertEqual((final, reserve), (0, 0))

    def test_mismatched_bootstrap_candidate_fails_before_existing_release_shortcut(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), published=True,
            shared=True, bootstrap=True, mismatched_bootstrap=True, verified_policy=True)
        self.assertIn("Prepared source differs from the native private policy bundle candidate", failure)
        self.assertFalse(outputs.get("ready", False))
        self.assertEqual((final, reserve), (0, 0))

    def test_verified_bootstrap_with_new_application_uses_normal_final_authorization(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), shared=True,
            bootstrap=True, verified_policy=True, bootstrap_decision="allow")
        self.assertIsNone(failure)
        self.assertTrue(outputs["ready"])
        self.assertTrue(outputs["policy_verified"])
        self.assertEqual(outputs["final_candidate"], "f" * 64)
        self.assertEqual((final, reserve), (0, 1))

    def test_exact_baseline_bootstrap_requires_a_fresh_bundle_receipt(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), published=True,
            shared=True, bootstrap=True, verified_policy=True,
            bootstrap_decision="reuse", bootstrap_bundle_verified=False)
        self.assertIn("Verified bootstrap bundle could not be revalidated", failure)
        self.assertFalse(outputs.get("ready", False))
        self.assertEqual((final, reserve), (0, 0))
        self.assertEqual(self.last_authorize_calls, 0)

    def test_malformed_bootstrap_bundle_fails_before_final_reservation(self):
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), shared=True, bootstrap=True,
            verified_policy=True, bootstrap_bundle_malformed=True)
        self.assertIn("native policy bundle returned an invalid proof", failure)
        self.assertFalse(outputs.get("ready", False))
        self.assertEqual((final, reserve), (0, 0))
        self.assertEqual(self.last_authorize_calls, 0)

    def test_adopted_config_recovers_the_canonical_review_candidate_for_exact_artifact_reuse(self):
        reviewed = "9" * 40
        outputs, final, reserve, failure = self.prepare_candidate(
            (True, "complete"), (True, "complete"), shared=True,
            verified_policy=True, adopted_config=reviewed, reuse_artifact=True)
        self.assertIsNone(failure)
        self.assertTrue(outputs["ready"])
        self.assertEqual(outputs["candidate_config"], reviewed)
        self.assertTrue(outputs["artifact_reuse"])
        self.assertEqual((outputs["artifact_run_id"], outputs["artifact_run_attempt"]), (12, 3))
        self.assertEqual((final, reserve), (0, 1))

    def test_existing_final_receipt_blocks_a_callback_from_rebuilding(self):
        sha, config = "c" * 40, "a" * 40
        receipt = {"body": f"<!-- zed-final:{sha} -->",
                   "user": {"login": "github-actions[bot]"}}
        with (patch.object(release, "pages", return_value=[receipt]),
              patch.object(release, "api") as api,
              patch.object(release, "output") as output,
              self.assertRaisesRegex(ValueError, "already started")):
            release.final_attempt("maintainer/zed-fork", 1, sha, config)
        api.assert_not_called()
        self.assertTrue(output.call_args.kwargs["final_failed"])

    def final_receipt_fixture(self, *, macos="skipped", revision_matches=True, pull_target=False,
                              earlier_started=False):
        sha, old_config, revision = "c" * 40, "a" * 40, "d" * 40
        receipt = {"body": f"Final validation and single build of `{sha}` / `{old_config}`: "
                           "https://github.com/maintainer/zed-fork/actions/runs/123.\n"
                           f"<!-- zed-final:{sha}:{old_config} -->\n"
                           f"<!-- zed-final:{sha} -->\n<!-- zed-final-workflow:{revision} -->",
                   "user": {"login": "github-actions[bot]"}}
        created = []

        def api(path, **kwargs):
            if kwargs.get("method") == "POST":
                created.append(kwargs["data"]["body"])
                return {"id": 1}
            if path.endswith("/runs/123"):
                return {"status": "completed", "conclusion": "failure",
                        "head_sha": "f" * 40 if pull_target else revision if revision_matches else "e" * 40,
                        "event": "pull_request_target" if pull_target else "workflow_dispatch",
                        "head_branch": "repair/checked-policy" if pull_target else "downstream",
                        "head_repository": {"full_name": "maintainer/zed-fork"},
                        "path": ".github/workflows/release.yml"}
            if path.endswith("/jobs?filter=all&per_page=100"):
                jobs = [{
                    "name": "Test, Build And Inspect One macOS Application",
                    "status": "completed", "conclusion": macos,
                    "steps": [] if macos == "skipped" else [{"name": "Compile"}],
                }]
                if earlier_started:
                    jobs.insert(0, {"name": "Test, Build And Inspect One macOS Application",
                                    "status": "completed", "conclusion": "failure",
                                    "steps": [{"name": "Compile", "conclusion": "failure"}]})
                return {"total_count": len(jobs), "jobs": jobs}
            raise AssertionError(path)

        return receipt, api, created

    def test_corrected_policy_can_validate_same_app_only_before_native_macos_started(self):
        sha, config = "c" * 40, "b" * 40
        for pull_target in (False, True):
            with self.subTest(pull_target=pull_target):
                receipt, api, created = self.final_receipt_fixture(pull_target=pull_target)
                with (patch.dict(os.environ, {"GITHUB_RUN_ID": "456", "GITHUB_SHA": config}, clear=True),
                      patch.object(release, "pages", return_value=[receipt]),
                      patch.object(release, "api", side_effect=api)):
                    release.final_attempt("maintainer/zed-fork", 1, sha, config)
                self.assertEqual(len(created), 1)
                self.assertIn(f"<!-- zed-final:{sha}:{config} -->", created[0])
                self.assertIn(f"<!-- zed-final:{sha} -->", created[0])

    def test_new_configuration_cannot_refund_started_or_unverifiable_heavy_attempt(self):
        for macos, matching, earlier in [("failure", True, False), ("success", True, False),
                                         ("skipped", False, False), ("skipped", True, True)]:
            with self.subTest(macos=macos, matching=matching, earlier=earlier):
                receipt, api, created = self.final_receipt_fixture(
                    macos=macos, revision_matches=matching, earlier_started=earlier)
                with (patch.object(release, "pages", return_value=[receipt]),
                      patch.object(release, "api", side_effect=api),
                      patch.object(release, "output") as output,
                      self.assertRaisesRegex(ValueError, "already started")):
                    release.final_attempt("maintainer/zed-fork", 1, "c" * 40, "b" * 40)
                self.assertEqual(created, [])
                self.assertTrue(output.call_args.kwargs["final_failed"])

    def test_same_candidate_callback_cannot_repeat_failed_light_validation(self):
        receipt, api, created = self.final_receipt_fixture()
        with (patch.object(release, "pages", return_value=[receipt]),
              patch.object(release, "api", side_effect=api),
              patch.object(release, "output") as output,
              self.assertRaisesRegex(ValueError, "already started")):
            release.final_attempt("maintainer/zed-fork", 1, "c" * 40, "a" * 40)
        self.assertEqual(created, [])
        self.assertFalse(output.call_args.kwargs["final_failed"], "no heavy budget was spent")

    def test_fresh_full_request_cannot_reuse_an_older_same_source_completion(self):
        sha, upstream = "c" * 40, "b" * 40
        owner = {"login": "maintainer", "type": "User"}
        marker = f"<!-- zed-codex:{sha} -->"
        comments = [
            {"body": marker, "user": owner, "created_at": "2026-10-18T01:00:00Z"},
            {"body": marker, "user": owner, "created_at": "2026-10-18T03:00:00Z"},
            {"body": "<!-- codex-review-state:v1 " + json.dumps({"code_review": {
                "commit_sha": sha, "status": "completed", "has_any_findings": False,
                "has_unresolved_findings": False}}) + " -->",
             "user": {"login": release.BOT, "type": "Bot"},
             "performed_via_github_app": {"slug": release.APP},
             "updated_at": "2026-10-18T02:00:00Z"},
        ]
        pull = {"state": "open", "head": {"sha": sha, "repo": {"full_name": "maintainer/zed-fork"}},
                "base": {"ref": f"review-base/{upstream}", "sha": upstream,
                         "repo": {"full_name": "maintainer/zed-fork"}}}
        with (patch.object(release, "api", return_value=pull),
              patch.object(release, "pages", side_effect=lambda path:
                           comments if "/issues/1/comments" in path else [])):
            ready, reason = release.check_review("maintainer/zed-fork", 1, sha, upstream)
        self.assertFalse(ready)
        self.assertEqual(reason, release.WAITING_REVIEW)

    def test_publication_requires_the_exact_configuration_delta_review(self):
        with (patch.dict(os.environ, {"DELTA_SHA": "a" * 40}, clear=True),
              self.assertRaisesRegex(ValueError, "exact configuration")):
            release.check_delta_for_publication("maintainer/zed-fork", "b" * 40)

    def test_final_failure_escalates_without_another_coding_task(self):
        with (patch.dict(os.environ, {"GITHUB_REPOSITORY": "maintainer/zed-fork",
                                    "GITHUB_SHA": "a" * 40,
                                    "FINAL_VALIDATION_FAILED": "true"}, clear=True),
              patch.object(release, "api", return_value={"sha": "a" * 40}),
              patch.object(release, "repair_pull", return_value={"number": 1, "head": "b" * 40,
                                                               "upstream": "c" * 40}),
              patch.object(release, "escalation", side_effect=ValueError("final stopped")) as escalate,
              patch.object(release, "owner_token") as token,
              self.assertRaisesRegex(ValueError, "final stopped")):
            release.request_repair(Path("/unused"), evidence="actual final check failure")
        token.assert_not_called()
        self.assertIn("actual final check failure", escalate.call_args.args[3])

    def test_final_macos_tests_share_the_shipped_build_target(self):
        # Legacy identity retained: forcing a test target differs from upstream CI.
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(release.sys, "platform", "darwin"),
              patch.dict(os.environ, {"CARGO_BUILD_TARGET": "x86_64-apple-darwin"})):
            environment = release.isolated_environment(Path(directory))
        self.assertNotIn("CARGO_BUILD_TARGET", environment)

    def test_adoption_cannot_change_reviewed_executable_files(self):
        reviewed, adopted = "a" * 40, "b" * 40
        def tree(path):
            return {"truncated": False, "tree": [{"path": "scripts/release.py", "type": "blob",
                    "mode": "100644", "sha": "c" * 40 if reviewed in path else "d" * 40}]}
        with (patch.object(release, "api", side_effect=tree),
              self.assertRaisesRegex(ValueError, "product files differ")):
            release.verify_adopted_configuration("maintainer/zed-fork", reviewed, adopted)

    def test_failed_final_compilation_keeps_its_captured_diagnostics(self):
        failure = subprocess.CalledProcessError(1, ["cargo", "test"], output="actual compiler error\n")
        output = io.StringIO()
        with (patch.object(release.subprocess, "check_output", side_effect=failure),
              redirect_stdout(output), self.assertRaises(subprocess.CalledProcessError)):
            release.capture(["cargo", "test"], cwd=Path("/unused"), env={})
        self.assertIn("actual compiler error", output.getvalue())

    def test_bounded_logs_cannot_hide_either_complete_review_report(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "maintainer/zed-fork",
                                    "CANDIDATE_PR": "49", "CANDIDATE_SHA": "c" * 40,
                                    "DELTA_PR": "50", "DELTA_SHA": "d" * 40}, clear=True):
            prompt = release.repair_text({"config": "a" * 40, "upstream": "b" * 40,
                                          "head": "e" * 40}, 1, "x" * 50000)
        self.assertIn("/pull/49", prompt)
        self.assertIn("/pull/50", prompt)
        self.assertIn("Fetch ALL pages", prompt)
        self.assertIn("fork-review-disposition:v1", prompt)
        self.assertIn("git diff --no-ext-diff --no-renames --binary", prompt)
        self.assertIn("The trusted controller adds the candidate-bound owner record", prompt)
        self.assertIn("Do not compile, run tests, build or install dependencies during repair", prompt)
        self.assertTrue(prompt.endswith("<!-- zed-fix:" + "e" * 40 + ":1 -->"))

    def test_compiler_errors_survive_artifact_noise_and_escalation_size_limits(self):
        log = ("2026-10-18T10:29:55Z error[E0063]: missing lifecycle fields\n"
               "2026-10-18T10:29:55Z  --> crates/agent_ui/src/store.rs:1107:24\n"
               + ('2026-10-18T10:42:14Z {"reason":"compiler-artifact","noise":"' + "x" * 500 + '"}\n') * 100
               + "2026-10-18T10:42:15Z error[E0308]: second independent compiler error\n"
               "2026-10-18T10:42:15Z  --> crates/sidebar/src/sidebar.rs:42:1\n")
        job = {"id": 17, "name": "Rust regressions", "conclusion": "failure",
               "html_url": "https://github.com/maintainer/zed-fork/actions/runs/123/job/17"}
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "123"}, clear=True),
              patch.object(release, "api", return_value={"total_count": 1, "jobs": [job]}),
              patch.object(release, "run", side_effect=lambda *a: "" if a[-1] == "--help" else log)):
            evidence = release.failure_evidence("maintainer/zed-fork")
        # The native escalation retains only the final 16000 characters.
        bounded = evidence[-16000:]
        self.assertIn("error[E0063]", bounded)
        self.assertIn("store.rs:1107:24", bounded)
        self.assertIn("error[E0308]", bounded)
        self.assertIn("sidebar.rs:42:1", bounded)
        self.assertIn("https://github.com/maintainer/zed-fork/actions/runs/123", bounded)

    def test_policy_snapshot_ignores_docs_but_detects_executable_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            left, right = Path(directory) / "left", Path(directory) / "right"
            for root in (left, right):
                (root / "scripts").mkdir(parents=True)
                (root / "scripts/release.py").write_text("checked code\n")
                (root / "README.md").write_text(str(root))
            self.assertEqual(release.policy_snapshot(left), release.policy_snapshot(right))
            (right / "scripts/release.py").write_text("proposed code\n")
            self.assertNotEqual(release.policy_snapshot(left), release.policy_snapshot(right))

    def test_credentialed_preparation_imports_the_pinned_controller_only(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        preparation = workflow.split("\n  prepare:\n", 1)[1].split("\n  policy-check:\n", 1)[0]
        self.assertNotIn("  bootstrap-policy:", workflow)
        self.assertNotIn("needs.bootstrap-policy", workflow)
        self.assertIn("needs: [verify-bootstrap-bundle]", preparation)
        self.assertIn("needs.verify-bootstrap-bundle.result == 'success'", preparation)
        self.assertIn("steps.control.outputs.policy_ref", preparation)
        self.assertIn("pathlib.Path('trusted/scripts')", preparation)
        self.assertIn("release.ROOT = pathlib.Path('configuration')", preparation)
        policy_selection = next(line for line in preparation.splitlines() if "TRUSTED_POLICY:" in line)
        self.assertIn("needs.verify-bootstrap-bundle.outputs.policy_ref", policy_selection)
        self.assertNotIn("bootstrap-policy", policy_selection)
        self.assertNotIn("python3 scripts/release.py prepare", preparation)
        self.assertLess(preparation.index("Require The Trusted Default Branch"),
                        preparation.index("Record A Successful Read-Only Bootstrap"))
        verifier = workflow.split("\n  verify-bootstrap-bundle:\n", 1)[1]
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.policy_bootstrap", verifier)
        self.assertIn("ref: ${{ github.workflow_sha }}", verifier)
        self.assertIn("path: workflow-source", verifier)
        self.assertIn("candidate_loader.read_bytes() != workflow_loader.read_bytes()", verifier)
        self.assertIn("candidate_lock.read_bytes() != workflow_lock.read_bytes()", verifier)
        self.assertIn("workflow_sha != os.environ['GITHUB_SHA']", verifier)
        self.assertIn("default_head != workflow_sha", verifier)
        self.assertIn("pathlib.Path('candidate/scripts/maintenance.py').read_bytes()", verifier)
        self.assertIn("sys.path.insert(0, str(workflow_scripts))", verifier)
        self.assertIn("proof = maintenance.validated_bundle(candidate, comments, read_native)", verifier)
        self.assertIn(r"contents/(?:fork_lifecycle\.py|bootstrap\.py|bundle_runner\.py|", verifier)
        self.assertIn(r"\.github/workflows/policy\.yml)\?ref=[0-9a-f]{40}", verifier)
        self.assertIn("if private_route.fullmatch(route):", verifier)
        self.assertNotIn("contents/.*", verifier)
        self.assertNotIn("sys.path.insert(0, str(pathlib.Path('candidate/scripts').resolve()))", verifier)
        self.assertNotIn("importlib.import_module('maintenance')", verifier)
        self.assertNotIn("importlib.import_module('release')", verifier)
        self.assertIn("proof.get('decision') != 'reuse-policy-validation'", verifier)
        self.assertIn("proof.get('application_artifact_verified') is not False", verifier)
        self.assertIn("BOOTSTRAP_MAINTENANCE_CANDIDATE: ${{ needs.verify-bootstrap-bundle.outputs.maintenance_candidate }}",
                      preparation)
        policy = workflow.split("\n  policy-check:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertIn("always() && needs.prepare.result == 'success'", policy)
        self.assertIn("needs: [prepare]", policy)
        self.assertNotIn("bootstrap-policy", policy)
        repair = workflow.split("\n  repair:\n", 1)[1]
        self.assertIn("ref: ${{ needs.activate-policy.outputs.policy_ref ||", repair)
        self.assertIn("needs.verify-bootstrap-bundle.outputs.policy_ref", repair)
        self.assertIn("needs.verify-bootstrap-bundle.result == 'failure'", repair)
        self.assertIn("BOOTSTRAP_BUNDLE_FAILED", repair)
        self.assertIn("github.ref == 'refs/heads/downstream'", repair)
        activation = workflow.split("\n  activate-policy:\n", 1)[1].split("\n  repair:\n", 1)[0]
        self.assertIn("needs: [prepare, policy-check, verify-bootstrap-bundle]", activation)
        self.assertNotIn("bootstrap-policy", activation)

    def test_bootstrap_receipt_reuse_survives_advancing_the_policy_pin(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        verifier = workflow.split("\n  verify-bootstrap-bundle:\n", 1)[1]
        self.assertIn("candidate.get('configuration_sha') == policy", verifier)
        self.assertNotIn("target.get('trusted_policy_sha') == policy", verifier)
        self.assertIn("proof = maintenance.validated_bundle(candidate, comments, read_native)", verifier)
        self.assertIn("proof.get('decision') != 'reuse-policy-validation'", verifier)

    def test_current_adoption_receipt_advances_the_authenticated_history_cutoff(self):
        commit = "a" * 40
        record = {"body": f"Reviewed repair consolidated as `{commit}`; source-context files were not merged. "
                           + release.ADOPTION_NOTE,
                  "created_at": "2026-10-18T01:00:00Z",
                  "user": {"login": "github-actions[bot]", "type": "Bot"}}
        with (patch.object(release, "pages", return_value=[record]),
              patch.object(release, "api", return_value={"status": "identical",
                                                         "merge_base_commit": {"sha": commit}})):
            cutoff = release.adoption_time("maintainer/zed-fork", {"state": "closed", "number": 1}, commit)
        self.assertEqual(cutoff, datetime(2026, 10, 18, 1, tzinfo=timezone.utc))

    def test_adopted_artifact_candidate_requires_exact_native_blob_preserving_receipt(self):
        repo, reviewed, adopted = "maintainer/zed-fork", "b" * 40, "c" * 40
        candidate = {"schema": 1, "repository": repo, "upstream_sha": "d" * 40,
                     "application_sha": "e" * 40, "configuration_sha": adopted,
                     "patchset_sha256": "f" * 64, "recipe_sha256": "1" * 64,
                     "platform": "macos/aarch64"}
        old_candidate = {**candidate, "configuration_sha": reviewed}
        state = {"schema": 1, "config": "a" * 40, "seed": "f" * 40,
                 "upstream": candidate["upstream_sha"]}
        pull = {"number": 17, "state": "closed",
                "user": {"login": "github-actions[bot]", "type": "Bot"},
                "head": {"ref": "repair/zed-source", "sha": reviewed,
                         "repo": {"full_name": repo}},
                "base": {"ref": "downstream", "repo": {"full_name": repo}},
                "body": "<!-- zed-repair:v1 " + json.dumps(state, sort_keys=True) + " -->"}
        receipt = {"id": 23, "created_at": "2026-10-20T03:00:00Z",
                   "updated_at": "2026-10-20T03:00:00Z",
                   "user": {"login": "github-actions[bot]", "type": "Bot"},
                   "body": f"Reviewed repair consolidated as `{adopted}`; source-context files were not merged. "
                           + release.ADOPTION_NOTE}

        def recover(*, comments=None, comparison=None, manifests=(b"same", b"same"),
                    product_candidate=None, edited=False, wrong_author=False,
                    adopted_parent=None):
            current_receipt = {**receipt}
            if edited:
                current_receipt["updated_at"] = "2026-10-20T03:01:00Z"
            if wrong_author:
                current_receipt["user"] = {"login": "maintainer", "type": "User"}

            def native_pages(path):
                return [pull] if path.endswith("pulls?state=all&base=downstream") else (
                    [current_receipt] if comments is None else comments)

            def source(_repo, _revision, _path):
                if _path == ".repair/context.json":
                    return json.dumps(state).encode()
                return manifests[0] if _revision == reviewed else manifests[1]

            def native_api(route, **_kwargs):
                if route.endswith(f"git/ref/pull/17/head"):
                    return {"object": {"type": "commit", "sha": reviewed}}
                if route == f"repos/{repo}/git/commits/{state['seed']}":
                    return {"parents": [{"sha": state["config"]}]}
                if route == f"repos/{repo}/git/commits/{adopted}":
                    return {"sha": adopted, "parents": [{"sha": adopted_parent or state["config"]}]}
                if route == f"repos/{repo}/compare/{state['seed']}...{reviewed}":
                    return comparison or {"status": "ahead", "merge_base_commit": {"sha": state["seed"]}}
                return comparison or {"status": "ahead", "merge_base_commit": {"sha": state["seed"]}}

            with (patch.object(release, "pages", side_effect=native_pages),
                  patch.object(release, "api", side_effect=native_api) as api,
                  patch.object(release, "verify_adopted_configuration") as verify,
                  patch.object(release, "native_source_file", side_effect=source),
                  patch.object(release, "maintenance_candidate",
                               return_value=product_candidate or old_candidate)):
                value = release.adopted_review_configuration(repo, adopted, candidate)
            return value, api, verify

        value, api, verify = recover()
        self.assertEqual(value, reviewed)
        self.assertEqual([call.args[0] for call in api.call_args_list], [
            f"repos/{repo}/git/commits/{state['seed']}",
            f"repos/{repo}/compare/{state['seed']}...{reviewed}",
            f"repos/{repo}/git/ref/pull/17/head",
            f"repos/{repo}/git/commits/{adopted}"])
        verify.assert_called_once_with(repo, reviewed, adopted)

        value, _, verify = recover(comments=[])
        self.assertIsNone(value)
        verify.assert_not_called()
        product_changes = (
            ("application", {"application_sha": "2" * 40}),
            ("upstream", {"upstream_sha": "2" * 40}),
            ("patchset", {"patchset_sha256": "2" * 64}),
            ("recipe", {"recipe_sha256": "2" * 64}),
            ("platform", {"platform": "linux/aarch64"}),
        )
        for name, options in (
            ("edited adoption receipt", {"edited": True}),
            ("owner-authored adoption receipt", {"wrong_author": True}),
            ("wrong config-to-adopted parent", {"adopted_parent": "9" * 40}),
            ("wrong repair ancestry", {"comparison": {"status": "diverged",
                                                       "merge_base_commit": {"sha": "9" * 40}}}),
            ("changed selectors", {"manifests": (b"reviewed selectors", b"new selectors")}),
            *((f"changed {name}", {"product_candidate": {**old_candidate, **changes}})
              for name, changes in product_changes),
        ):
            with self.subTest(case=name):
                value, _, _ = recover(**options)
                self.assertIsNone(value)

    def test_missing_pin_bundle_verifier_failure_uses_verified_escalation_not_a_coding_task(self):
        import ast
        import textwrap
        from types import SimpleNamespace
        from unittest.mock import Mock

        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        repair = workflow.split("\n  repair:\n", 1)[1]
        self.assertNotIn("bootstrap-policy", repair)
        self.assertIn("needs.verify-bootstrap-bundle.result == 'failure'", repair)
        actual_repair = repair.split("      - name: Repair The Actual Failure Without Publishing Or Disrupting Editors\n", 1)[1]
        script = textwrap.dedent(actual_repair.split("python3 -B - <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0])
        guard = next(node for node in ast.parse(script).body if isinstance(node, ast.If))
        controller = SimpleNamespace(
            SHA=release.SHA, failure_evidence=Mock(return_value="actual bootstrap failure"),
            api=Mock(return_value={
                "number": 1,
                "head": {"repo": {"full_name": "maintainer/zed-fork"}, "ref": "review/" + "b" * 40},
                "base": {"repo": {"full_name": "maintainer/zed-fork"},
                         "ref": "review-base/" + "b" * 40, "sha": "b" * 40},
            }),
            escalation=Mock(side_effect=ValueError("protected escalation")),
            request_repair=Mock(side_effect=AssertionError("a missing private receipt must not start a coding task")),
        )
        environment = {"BOOTSTRAP_BUNDLE_FAILED": "true", "GITHUB_REPOSITORY": "maintainer/zed-fork",
                       "UPSTREAM_SHA": "", "CONFIG_SHA": "a" * 40,
                       "GITHUB_RUN_ID": "123",
                       "BOOTSTRAP_FULL_PR": "1", "BOOTSTRAP_DELTA_PR": "2"}
        with patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(ValueError, "protected escalation"):
            exec(compile(ast.fix_missing_locations(ast.Module(body=[guard], type_ignores=[])),
                         "release-workflow-bootstrap-report", "exec"), {"os": os, "release": controller})
        controller.request_repair.assert_not_called()
        controller.escalation.assert_called_once()
        self.assertEqual(controller.escalation.call_args.args[:3],
                         ("maintainer/zed-fork", "a" * 40, "b" * 40))
        self.assertIn("/actions/runs/123", controller.escalation.call_args.args[3])
        self.assertIn("private native policy bundle could not be verified", controller.escalation.call_args.args[3])
        self.assertIn("actual bootstrap failure", controller.escalation.call_args.args[3])

    def test_inflight_snapshot_is_not_replaced_by_new_upstream_discovery(self):
        sha, patchset = "b" * 40, "d" * 64
        pull = {"number": 49, "head": {"ref": f"review/{sha}-{patchset}",
                                       "repo": {"full_name": "maintainer/zed-fork"}},
                "base": {"ref": f"review-base/{sha}", "sha": sha},
                "user": {"login": "github-actions[bot]"},
                "body": f"patch SHA-256: `{patchset}`"}
        with patch.object(release, "pages", return_value=[pull]):
            self.assertEqual(release.active_snapshot("maintainer/zed-fork", patchset), sha)




class NativeReviewDispositionAdapterTests(unittest.TestCase):
    def candidate(self):
        return {"repository": "owner/zed", "configuration_sha": "a" * 40}

    def test_normalized_disposition_keeps_exact_native_owner_comment_identity(self):
        candidate = self.candidate()
        native = {"id": 31, "created_at": "2026-10-19T12:00:00Z",
                  "updated_at": "2026-10-19T12:00:00Z",
                  "user": {"login": "owner", "type": "User", "id": 7}}
        payload = {"schema": 1, "candidate_id": "b" * 64,
                   "source_sha": candidate["configuration_sha"],
                   "findings": [{"finding_id": "review:10:9", "status": "disproved",
                                  "evidence": "Current code and regression prove the finding false."}]}
        comment = {**native, "body": "<!-- fork-review-disposition:v1 "
                                  + json.dumps(payload, separators=(",", ":")) + " -->"}
        with patch.object(release.maintenance, "load") as load:
            load.return_value.candidate_identity.return_value = payload["candidate_id"]
            result = release.review_dispositions_from_comments([comment], candidate)
        self.assertEqual(result, [{"finding_id": "review:10:9", "candidate_id": payload["candidate_id"],
                                  "source_sha": candidate["configuration_sha"], "status": "disproved",
                                  "evidence": payload["findings"][0]["evidence"],
                                  "native_comment": native}])

    def test_edited_native_owner_disposition_is_rejected(self):
        candidate = self.candidate()
        payload = {"schema": 1, "candidate_id": "b" * 64,
                   "source_sha": candidate["configuration_sha"], "findings": []}
        comment = {"id": 31, "created_at": "2026-10-19T12:00:00Z",
                   "updated_at": "2026-10-19T12:01:00Z",
                   "user": {"login": "owner", "type": "User"},
                   "body": "<!-- fork-review-disposition:v1 "
                           + json.dumps(payload, separators=(",", ":")) + " -->"}
        with (patch.object(release.maintenance, "load") as load,
              self.assertRaisesRegex(ValueError, "duplicated or edited")):
            load.return_value.candidate_identity.return_value = payload["candidate_id"]
            release.review_dispositions_from_comments([comment], candidate)


class FinalRetryAuthorizationTests(unittest.TestCase):
    repo = "maintainer/zed-fork"
    application = "a" * 40
    configuration = "c" * 40

    def comments(self):
        owner = {"login": "maintainer", "type": "User"}
        records = [{"id": n, "user": {"login": "github-actions[bot]", "type": "Bot"},
                    "body": f"Final validation and single build of `{self.application}` / `{'b' * 40}`: "
                            f"https://github.com/{self.repo}/actions/runs/{n}\n"
                            f"<!-- zed-final:{self.application} -->"}
                   for n in (41, 42)]
        records.append({"id": 100, "user": owner, "created_at": "2026-10-18T17:10:00Z",
                        "body": f"Owner explicitly granted one additional final attempt.\n"
                                f"<!-- zed-final-retry:{self.application}:{self.configuration}:42 -->"})
        return records

    def stopped_run(self):
        return {"status": "completed", "conclusion": "cancelled",
                "path": ".github/workflows/release.yml", "updated_at": "2026-10-18T17:09:00Z"}

    def test_owner_grant_is_exact_and_bound_to_latest_stopped_run(self):
        with patch.object(release, "api", return_value=self.stopped_run()) as api:
            self.assertEqual(release.final_retry_authorization(
                self.repo, self.application, self.configuration, self.comments()), 100)
            api.assert_called_once_with(f"repos/{self.repo}/actions/runs/42")

    def test_ordinary_callbacks_wrong_candidates_and_stale_grants_stay_blocked(self):
        for mutation in ("bot", "wrong-config", "earlier-run", "before-stop", "missing-run"):
            with self.subTest(mutation=mutation):
                comments = self.comments()
                if mutation == "bot": comments[-1]["user"] = {"login": "github-actions[bot]", "type": "Bot"}
                elif mutation == "wrong-config": comments[-1]["body"] = comments[-1]["body"].replace(self.configuration, "d" * 40)
                elif mutation == "earlier-run": comments[-1]["body"] = comments[-1]["body"].replace(":42 -->", ":41 -->")
                elif mutation == "before-stop": comments[-1]["created_at"] = "2026-10-18T17:08:00Z"
                else: comments[0]["body"] = f"<!-- zed-final:{self.application} -->"
                with patch.object(release, "api", return_value=self.stopped_run()):
                    self.assertIsNone(release.final_retry_authorization(
                        self.repo, self.application, self.configuration, comments))

    def test_live_successful_or_wrong_workflow_runs_cannot_supply_a_grant(self):
        for changes in ({"status": "in_progress"}, {"conclusion": "success"}, {"path": ".github/workflows/other.yml"}):
            with self.subTest(changes=changes), patch.object(release, "api", return_value=self.stopped_run() | changes):
                self.assertIsNone(release.final_retry_authorization(
                    self.repo, self.application, self.configuration, self.comments()))

    def test_grant_is_consumed_by_one_complete_candidate_receipt(self):
        comments = self.comments()
        posted = []
        def api(path, *, method="GET", data=None, **kwargs):
            if method == "GET": return self.stopped_run()
            self.assertEqual(method, "POST")
            posted.append(data)
            comments.append({"id": 101, "user": {"login": "github-actions[bot]", "type": "Bot"}, **data})
            return {"id": 101}
        with (patch.object(release, "pages", side_effect=lambda _: list(comments)),
              patch.object(release, "api", side_effect=api),
              patch.object(release, "unspent_heavy_attempt", return_value=None),
              patch.object(release, "output"),
              patch.dict(os.environ, {"GITHUB_RUN_ID": "43", "GITHUB_SHA": self.configuration})):
            release.final_attempt(self.repo, 69, self.application, self.configuration)
            with self.assertRaisesRegex(ValueError, "already started"):
                release.final_attempt(self.repo, 69, self.application, self.configuration)
        self.assertEqual(len(posted), 1)
        self.assertIn("<!-- zed-final-retry-used:100 -->", posted[0]["body"])
        self.assertIn(f"<!-- zed-final:{self.application}:{self.configuration} -->", posted[0]["body"])


class CrossApplicationFinalHistoryTests(unittest.TestCase):
    repo = "maintainer/zed-fork"
    application = "a" * 40
    configuration = "b" * 40
    upstream = "d" * 40

    def history(self, runs=(42,)):
        owner = {"login": "maintainer", "type": "User"}
        pull = {
            "number": 7, "updated_at": "2026-10-18T18:00:00Z", "user": owner,
            "base": {"sha": self.upstream, "ref": f"review-base/{self.upstream}"},
            "head": {"sha": self.application, "repo": {"full_name": self.repo},
                     "ref": f"review/{self.upstream}-{'1' * 64}"},
        }
        comments = [{
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": f"Final validation and single build of `{self.application}` / "
                    f"`{self.configuration}`: https://github.com/{self.repo}/actions/runs/{number}\n"
                    f"<!-- zed-final:{self.application}:{self.configuration} -->\n"
                    f"<!-- zed-final-workflow:{self.configuration} -->",
        } for number in runs]
        published = [{"id": 1, "draft": False, "prerelease": False,
                      "published_at": "2026-10-18T16:00:00Z"}]
        metadata = {
            number: {"status": "completed", "conclusion": "failure",
                     "path": ".github/workflows/release.yml", "head_sha": self.configuration,
                     "event": "workflow_dispatch", "updated_at": f"2026-10-18T17:{number - 30:02d}:00Z"}
            for number in runs
        }
        jobs = {
            number: {"total_count": 1, "jobs": [{
                "name": "Test, Build And Inspect One macOS Application",
                "status": "completed", "conclusion": "failure",
                "started_at": "2026-10-18T17:00:00Z",
                "completed_at": f"2026-10-18T17:{number - 31:02d}:00Z",
                "steps": [{"name": "Compile", "conclusion": "failure"}],
            }]} for number in runs
        }

        def pages(path):
            if path.endswith("/releases"):
                return published
            if "/pulls?" in path:
                return [pull]
            if path.endswith("/issues/7/comments"):
                return comments
            raise AssertionError(path)

        def api(path, **kwargs):
            for number in runs:
                if path.endswith(f"/runs/{number}"):
                    return metadata[number]
                if path.endswith(f"/runs/{number}/jobs?filter=all&per_page=100"):
                    return jobs[number]
            raise AssertionError(path)

        return pages, api, metadata, jobs, published

    def test_new_application_and_review_pr_keep_the_previous_failed_attempt(self):
        pages, api, _, _, _ = self.history()
        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            self.assertEqual(release.previous_final_failure(self.repo), 42)
        application, config = "e" * 40, "f" * 40
        receipt = {"id": 101, "user": {"login": "github-actions[bot]"},
                   "body": f"https://github.com/{self.repo}/actions/runs/43\n"
                           f"<!-- zed-final:{application}:{config} -->"}
        with (patch.object(release, "pages", return_value=[receipt]),
              patch.object(release, "previous_final_failure", return_value=42),
              patch.object(release, "api", return_value={"total_count": 0, "jobs": []}) as requests,
              patch.dict(os.environ, {"GITHUB_RUN_ID": "43"}),
              self.assertRaisesRegex(ValueError, "explicit owner grant")):
            release.check_final_history(self.repo, 99, application, config)
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET"
                            for call in requests.call_args_list))

    def test_failed_history_blocks_before_creating_another_release_draft(self):
        with (patch.dict(os.environ, {"GITHUB_RUN_ID": "43"}),
              patch.object(release, "pages", return_value=[]),
              patch.object(release, "previous_final_failure", return_value=42),
              patch.object(release, "api", return_value={"total_count": 0, "jobs": []}) as requests,
              patch.object(release, "output") as output,
              self.assertRaisesRegex(ValueError, "explicit owner grant")):
            release.reserve_release(self.repo, "candidate", "e" * 40, self.upstream,
                                    "1" * 64, "1.24.2", "f" * 40, 99)
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET"
                            for call in requests.call_args_list))
        self.assertTrue(output.call_args.kwargs["final_failed"])

    def test_later_skipped_macos_does_not_refund_an_earlier_heavy_failure(self):
        pages, api, _, jobs, _ = self.history((42, 43))
        jobs[43]["jobs"][0].update(conclusion="skipped", steps=[],
                                   started_at=None, completed_at=None)
        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            self.assertEqual(release.previous_final_failure(self.repo), 42)

    def test_current_reservation_is_excluded_without_erasing_previous_failure(self):
        pages, api, metadata, jobs, _ = self.history((42, 43))
        metadata[43]["status"] = "in_progress"
        jobs[43]["jobs"][0].update(status="queued", started_at=None, completed_at=None)
        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            self.assertEqual(release.previous_final_failure(self.repo, exclude_run=43), 42)

    def test_completion_order_selects_the_latest_stopped_run(self):
        pages, api, _, jobs, _ = self.history((41, 42))
        jobs[41]["jobs"][0]["completed_at"] = "2026-10-18T17:20:00Z"
        with (patch.object(release, "pages", side_effect=pages),
              patch.object(release, "api", side_effect=api)):
            self.assertEqual(release.previous_final_failure(self.repo), 41)

    def test_successful_final_or_verified_publication_closes_old_failure_history(self):
        for outcome in ("successful-final", "new-publication"):
            with self.subTest(outcome=outcome):
                pages, api, metadata, jobs, published = self.history((41, 42))
                if outcome == "successful-final":
                    metadata[42]["conclusion"] = "success"
                    jobs[42]["jobs"][0]["conclusion"] = "success"
                else:
                    published[0]["published_at"] = "2026-10-18T17:30:00Z"
                with (patch.object(release, "pages", side_effect=pages),
                      patch.object(release, "api", side_effect=api)):
                    self.assertIsNone(release.previous_final_failure(self.repo))

    def test_missing_truncated_or_active_native_evidence_blocks_the_next_heavy_attempt(self):
        for invalid in ("missing", "truncated", "active", "wrong-workflow", "wrong-revision"):
            with self.subTest(invalid=invalid):
                pages, api, metadata, jobs, _ = self.history()
                if invalid == "missing": jobs[42] = {"total_count": 0, "jobs": []}
                elif invalid == "truncated": jobs[42]["total_count"] = 101
                elif invalid == "active": jobs[42]["jobs"][0]["completed_at"] = None
                elif invalid == "wrong-workflow": metadata[42]["path"] = ".github/workflows/other.yml"
                else: metadata[42]["head_sha"] = "f" * 40
                with (patch.object(release, "pages", side_effect=pages),
                      patch.object(release, "api", side_effect=api),
                      self.assertRaises(ValueError)):
                    release.previous_final_failure(self.repo)

    def test_cross_application_owner_grant_is_exact_latest_and_consumed_once(self):
        application, config = "e" * 40, "f" * 40
        for invalid in (None, "bot", "old-run", "wrong-source", "before-stop"):
            with self.subTest(invalid=invalid):
                comments = [{
                    "id": 100, "user": {"login": "maintainer", "type": "User"},
                    "created_at": "2026-10-18T18:00:00Z",
                    "body": f"<!-- zed-final-retry:{application}:{config}:42 -->",
                }, {
                    "id": 101, "user": {"login": "github-actions[bot]"},
                    "body": f"https://github.com/{self.repo}/actions/runs/43\n"
                            f"<!-- zed-final:{application}:{config} -->",
                }]
                if invalid == "bot": comments[0]["user"] = {"login": "github-actions[bot]", "type": "Bot"}
                elif invalid == "old-run": comments[0]["body"] = comments[0]["body"].replace(":42 -->", ":41 -->")
                elif invalid == "wrong-source": comments[0]["body"] = comments[0]["body"].replace(application, "c" * 40)
                elif invalid == "before-stop": comments[0]["created_at"] = "2026-10-18T16:00:00Z"
                consumed = []
                current_jobs = {"total_count": 0, "jobs": []}

                def api(path, *, method="GET", data=None, **kwargs):
                    if path.endswith("/runs/43/jobs?filter=all&per_page=100"):
                        return current_jobs
                    if method == "GET":
                        return {"status": "completed", "conclusion": "failure",
                                "path": ".github/workflows/release.yml",
                                "updated_at": "2026-10-18T17:30:00Z"}
                    self.assertEqual((path, method), (f"repos/{self.repo}/issues/comments/101", "PATCH"))
                    consumed.append(data)
                    comments[1].update(data)
                    return {"id": 101}

                with (patch.object(release, "pages", side_effect=lambda _: list(comments)),
                      patch.object(release, "previous_final_failure", return_value=42),
                      patch.object(release, "api", side_effect=api),
                      patch.dict(os.environ, {"GITHUB_RUN_ID": "43"})):
                    if invalid is None:
                        release.check_final_history(self.repo, 99, application, config)
                        current_jobs.update(total_count=1, jobs=[{
                            "name": "Test, Build And Inspect One macOS Application",
                            "status": "completed", "conclusion": "failure",
                            "started_at": "2026-10-18T18:01:00Z", "steps": [{"name": "Compile"}],
                        }])
                        with self.assertRaisesRegex(ValueError, "already started"):
                            release.check_final_history(self.repo, 99, application, config)
                    else:
                        with self.assertRaisesRegex(ValueError, "explicit owner grant"):
                            release.check_final_history(self.repo, 99, application, config)
                self.assertEqual(len(consumed), 1 if invalid is None else 0)
                if consumed:
                    self.assertIn("<!-- zed-final-retry-used:100 -->", consumed[0]["body"])

    def test_old_policy_cannot_reserve_before_the_checked_bundle_guard(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("  bootstrap-policy:", workflow)
        self.assertNotIn("needs.bootstrap-policy", workflow)
        preparation = workflow.split("\n  prepare:\n", 1)[1].split("\n  policy-check:\n", 1)[0]
        guard = preparation.index('callable(getattr(release, "check_final_history", None))')
        self.assertLess(guard, preparation.index("release.prepare("))
        self.assertIn("release.output(final_failed=True)", preparation[:preparation.index("release.prepare(")])
        self.assertIn("existing reviewed read-only bootstrap", preparation)
        selection = next(line for line in preparation.splitlines() if "TRUSTED_POLICY:" in line)
        self.assertIn("needs.verify-bootstrap-bundle.result == 'success'", selection)
        self.assertLess(selection.index("needs.verify-bootstrap-bundle.outputs.policy_ref"),
                        selection.index("vars.ZED_TRUSTED_POLICY"))

    def test_checked_linux_history_gate_blocks_macos_before_pin_or_adoption(self):
        workflow = (release.ROOT / ".github/workflows/release.yml").read_text()
        activation = workflow.split("\n  activate-policy:\n", 1)[1].split("\n  repair:\n", 1)[0]
        history_step = activation.split(
            "\n      - name: Recheck The Exact Shared Reviews And Current Attempt Reservation\n",
            1)[1].split("\n      - name: Pin The Checked Controller For Subsequent Runs\n", 1)[0]
        publication_check = history_step.index("release.check_shared_publication(")
        final_reauthorization = history_step.index("release.reauthorize_reserved_final(")
        pin = activation.index("Pin The Checked Controller For Subsequent Runs")
        self.assertLess(publication_check, final_reauthorization)
        self.assertLess(activation.index("release.reauthorize_reserved_final("), pin)
        for identity in (
            "CANDIDATE_PR: ${{ needs.prepare.outputs.pr }}",
            "CANDIDATE_SHA: ${{ needs.prepare.outputs.sha }}",
            "UPSTREAM_SHA: ${{ needs.prepare.outputs.upstream }}",
            "PATCHSET_SHA256: ${{ needs.prepare.outputs.patchset }}",
            "REVIEW_CONFIG_SHA: ${{ needs.prepare.outputs.candidate_config }}",
            "MAINTENANCE_CANDIDATE: ${{ needs.prepare.outputs.maintenance_candidate }}",
            "MAINTENANCE_BASELINE: ${{ needs.prepare.outputs.maintenance_baseline }}",
            "FULL_REVIEW_SHA: ${{ needs.prepare.outputs.full_review_sha }}",
            "DELTA_PR: ${{ needs.prepare.outputs.delta_pr }}",
            "DELTA_BASE: ${{ needs.prepare.outputs.delta_base }}",
            "DELTA_SHA: ${{ needs.prepare.outputs.delta_sha }}",
            "REPAIR_PR: ${{ needs.prepare.outputs.repair_pr }}",
            "FORK_FINAL_CANDIDATE: ${{ needs.prepare.outputs.final_candidate }}",
            "FORK_FINAL_RUN_ID: ${{ needs.prepare.outputs.final_run_id }}",
            "FORK_FINAL_ATTEMPT: ${{ needs.prepare.outputs.final_attempt }}",
            "FORK_FINAL_WORKFLOW_SHA: ${{ needs.prepare.outputs.final_workflow_sha }}",
        ):
            self.assertIn(identity, history_step)
        self.assertNotIn("\n          CONFIG_SHA:", history_step)
        self.assertIn("if os.environ['FORK_FINAL_CANDIDATE']:", history_step)
        self.assertIn("except Exception:", history_step)
        failure_output = history_step.index("release.output(final_failed=True)")
        self.assertLess(failure_output, history_step.index("raise", failure_output))
        self.assertIn("final_failed: ${{ steps.history.outputs.final_failed }}", activation)
        self.assertLess(pin, activation.index("Finish Policy-Only Maintenance Without Rebuilding"))
        application = workflow.split("\n  test:\n", 1)[1].split("\n  publish:\n", 1)[0]
        self.assertIn("needs.activate-policy.result == 'success'", application)
        self.assertIn("needs.activate-policy.outputs.final_failed == 'true'", workflow)


if __name__ == "__main__":
    unittest.main()
