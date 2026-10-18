from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release


class BatchedReviewTests(unittest.TestCase):
    def prepare_candidate(self, full, delta, findings=()):
        config, upstream, candidate = "a" * 40, "b" * 40, "c" * 40
        outputs = {}
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
                    return []
                if "/pulls?" in path:
                    return [{"number": 1, "state": "open", "head": {"sha": candidate}}]
                return [{"body": f"<!-- zed-codex:{candidate} -->",
                         "user": {"login": "maintainer"},
                         "created_at": datetime.now(timezone.utc).isoformat()}]

            stack.enter_context(patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "maintainer/zed-fork", "GITHUB_SHA": config,
                "GITHUB_RUN_ID": "123",
            }, clear=True))
            for name, value in {
                "mirror_main": None, "proposal": None,
                "select_upstream": ("v1.24.2", upstream),
                "patch_identity": ([Path("fixture.patch")], "d" * 64),
                "ensure_forward_release": None, "push_review_refs": None,
                "check_review": full, "delta_review": (2, "e" * 40, config, *delta),
                "minor_findings": list(findings), "review_records": list(findings),
                "retire_superseded_reviews": None, "pending_review": None,
            }.items():
                stack.enter_context(patch.object(release, name, return_value=value))
            stack.enter_context(patch.object(release, "run", side_effect=run))
            stack.enter_context(patch.object(release, "pages", side_effect=pages))
            stack.enter_context(patch.object(release, "output", side_effect=lambda **v: outputs.update(v)))
            stack.enter_context(patch.object(release, "api", side_effect=lambda path, **kw:
                                {"sha": config if path.endswith("/downstream") else upstream}))
            final = stack.enter_context(patch.object(release, "final_attempt"))
            reserve = stack.enter_context(patch.object(release, "reserve_release", return_value=10))
            failure = None
            try:
                release.prepare(app)
            except ValueError as error:
                failure = str(error)
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

    def test_existing_final_receipt_blocks_a_callback_from_rebuilding(self):
        sha, config = "c" * 40, "a" * 40
        receipt = {"body": f"<!-- zed-final:{sha}:{config} -->",
                   "user": {"login": "github-actions[bot]"}}
        with (patch.object(release, "pages", return_value=[receipt]),
              patch.object(release, "api") as api,
              patch.object(release, "output") as output,
              self.assertRaisesRegex(ValueError, "already started")):
            release.final_attempt("maintainer/zed-fork", 1, sha, config)
        api.assert_not_called()
        self.assertTrue(output.call_args.kwargs["final_failed"])

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


if __name__ == "__main__":
    unittest.main()
