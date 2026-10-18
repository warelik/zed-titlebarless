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
    def prepare_candidate(self, full, delta, findings=(), *, published=False, changed_policy=False):
        config, upstream, candidate = "a" * 40, "b" * 40, "c" * 40
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
                    return [{"draft": False, "tag_name": release.release_tag(
                        "1.24.2", upstream, "d" * 64, candidate)}] if published else []
                if "/pulls?" in path:
                    return [{"number": 1, "state": "open", "head": {"sha": candidate}}]
                return [{"body": (f"<!-- zed-codex:{candidate} -->\n"
                                   f"<!-- zed-full:{candidate}:{config} -->"),
                         "user": {"login": "maintainer"},
                         "created_at": datetime.now(timezone.utc).isoformat()}]

            stack.enter_context(patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "maintainer/zed-fork", "GITHUB_SHA": config,
                "GITHUB_RUN_ID": "123",
            }, clear=True))
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
            if changed_policy:
                stack.enter_context(patch.object(release, "policy_snapshot",
                                                side_effect=[{"release.py": "new"}, {"release.py": "checked"}]))
            stack.enter_context(patch.object(release, "run", side_effect=run))
            stack.enter_context(patch.object(release, "pages", side_effect=pages))
            stack.enter_context(patch.object(release, "output", side_effect=lambda **v: outputs.update(v)))
            stack.enter_context(patch.object(release, "api", side_effect=lambda path, **kw:
                                {"sha": config if path.endswith("/downstream") else upstream}))
            final = stack.enter_context(patch.object(release, "final_attempt",
                                       side_effect=lambda *a, **kw: order.append("final")))
            reserve = stack.enter_context(patch.object(release, "reserve_release",
                                         side_effect=lambda *a: order.append("reserve") or 10))
            failure = None
            try:
                release.prepare(app)
            except ValueError as error:
                failure = str(error)
            self.last_order = order
            return outputs, final.call_count, reserve.call_count, failure

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
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(release.sys, "platform", "darwin")):
            environment = release.isolated_environment(Path(directory))
        self.assertEqual(environment["CARGO_BUILD_TARGET"], "aarch64-apple-darwin")

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
        self.assertIn("steps.control.outputs.policy_ref", preparation)
        self.assertIn("pathlib.Path('trusted/scripts')", preparation)
        self.assertIn("release.ROOT = pathlib.Path('configuration')", preparation)
        self.assertNotIn("python3 scripts/release.py prepare", preparation)
        self.assertLess(preparation.index("Require The Trusted Default Branch"),
                        preparation.index("Record A Successful Read-Only Bootstrap"))
        bootstrap = workflow.split("\n  bootstrap-policy:\n", 1)[1].split("\n  prepare:\n", 1)[0]
        self.assertNotIn("secrets.", bootstrap)
        self.assertLess(bootstrap.index("Require The Trusted Default Branch"),
                        bootstrap.index("Run The Final Approved Policy Suite Once"))
        self.assertIn("Require Both Complete Exact-Source Reviews Before Any Tests", bootstrap)
        self.assertIn("ref: dbf40ff4bd626082e0ad3d9873c5b6ad5fa7f941", bootstrap)
        self.assertNotIn("ref: ${{ vars.ZED_TRUSTED_POLICY", bootstrap)
        self.assertIn("for directory in ['gate/scripts', 'baseline/scripts']:", bootstrap)
        self.assertIn("if policy != os.environ['GITHUB_SHA']:", bootstrap)
        self.assertIn("if base != shipped_config:", bootstrap)
        self.assertIn("gate.asset_bytes(repo, assets['provenance.json'])", bootstrap)
        self.assertIn("ref: 71d0f4314a4a9fadc9d404f6879586f6be2d93ae", bootstrap)
        self.assertIn("/baseline/scripts /candidate/scripts", bootstrap)
        self.assertIn("target=/baseline,readonly", bootstrap)
        self.assertIn('sys.path.insert(0,"/candidate/scripts");import release', bootstrap)
        policy = workflow.split("\n  policy-check:\n", 1)[1].split("\n  test:\n", 1)[0]
        self.assertIn("always() && needs.prepare.result == 'success'", policy)
        repair = workflow.split("\n  repair:\n", 1)[1]
        self.assertIn("github.ref == 'refs/heads/downstream'", repair)
        self.assertIn("(vars.ZED_TRUSTED_POLICY != '' || needs.bootstrap-policy.result == 'success')", repair)

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

    def test_inflight_snapshot_is_not_replaced_by_new_upstream_discovery(self):
        sha, patchset = "b" * 40, "d" * 64
        pull = {"number": 49, "head": {"ref": f"review/{sha}-{patchset}",
                                       "repo": {"full_name": "maintainer/zed-fork"}},
                "base": {"ref": f"review-base/{sha}", "sha": sha},
                "user": {"login": "github-actions[bot]"},
                "body": f"patch SHA-256: `{patchset}`"}
        with patch.object(release, "pages", return_value=[pull]):
            self.assertEqual(release.active_snapshot("maintainer/zed-fork", patchset), sha)


if __name__ == "__main__":
    unittest.main()
