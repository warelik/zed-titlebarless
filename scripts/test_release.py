import json
import hashlib
import os
from pathlib import Path
import tempfile
import subprocess
import shutil
import unittest
from unittest.mock import patch

import release


HEAD = "a" * 40
OTHER = "b" * 40
REQUESTED = "2026-10-16T10:00:00Z"
COMPLETED = "2026-10-16T10:01:00Z"
BOT = {"login": release.BOT, "type": "Bot"}
APP = {"slug": release.APP}


def receipt(sha=HEAD):
    return {
        "user": BOT, "performed_via_github_app": APP, "created_at": COMPLETED,
        "body": f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{sha}`",
    }


def native_state(sha=HEAD, **changes):
    state = {
        "commit_sha": sha, "status": "completed",
        "has_any_findings": False, "has_unresolved_findings": False, **changes,
    }
    return {
        "user": BOT, "performed_via_github_app": APP,
        "created_at": "2026-10-16T09:00:00Z", "updated_at": COMPLETED,
        "body": f"<!-- codex-review-state:v1 {json.dumps({'code_review': state})} -->",
    }


class ReviewPolicyTests(unittest.TestCase):
    def gate(self, *, sha=HEAD, requested=REQUESTED, reviews=(), inline=(), comments=(), resolve=None):
        return release.review_gate(
            sha, requested, reviews, inline, comments,
            resolve or (lambda prefix: HEAD if HEAD.startswith(prefix) else OTHER),
        )[0]

    def test_native_receipt_full_sha(self):
        self.assertTrue(self.gate(comments=[receipt()]))

    def test_native_receipt_resolves_abbreviated_sha(self):
        self.assertTrue(self.gate(comments=[receipt(HEAD[:10])]))

    def test_receipt_for_another_commit_is_not_a_review(self):
        self.assertFalse(self.gate(comments=[receipt(OTHER)]))

    def test_unresolvable_prefix_is_not_a_review(self):
        self.assertFalse(self.gate(comments=[receipt()], resolve=lambda _: None))

    def test_edited_native_summary_is_accepted_after_request(self):
        self.assertTrue(self.gate(comments=[native_state()]))

    def test_native_summary_requires_current_commit_and_completion(self):
        for record in (native_state(OTHER), native_state(status="in_progress"),
                       native_state(has_any_findings=True),
                       native_state(has_unresolved_findings=None),
                       native_state(has_any_findings=0)):
            with self.subTest(record=record):
                self.assertFalse(self.gate(comments=[record]))

    def test_stale_native_state_is_not_a_review(self):
        comment = native_state()
        comment["updated_at"] = "2026-10-16T09:59:00Z"
        self.assertFalse(self.gate(comments=[comment]))

    def test_bot_and_app_identity_are_required_for_native_comments(self):
        for changes in (
            {"user": {"login": "maintainer", "type": "User"}},
            {"user": {"login": release.BOT, "type": "User"}},
            {"performed_via_github_app": None},
            {"performed_via_github_app": {"slug": "another-app"}},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.gate(comments=[{**receipt(), **changes}]))

    def test_ambiguous_reviewed_commit_text_is_not_accepted(self):
        record = receipt()
        record["body"] += f"\nReviewed commit: `{OTHER}`"
        self.assertFalse(self.gate(comments=[record]))

    def test_silence_and_thumbs_up_text_are_not_reviews(self):
        self.assertFalse(self.gate())
        self.assertFalse(self.gate(comments=[{**receipt(), "body": "+1"}]))

    def test_request_must_be_exact_sha_and_present(self):
        self.assertFalse(self.gate(sha=HEAD[:10], comments=[receipt()]))
        self.assertFalse(self.gate(requested="", comments=[receipt()]))

    def test_exact_inline_p1_overrides_clean_summary(self):
        finding = {"user": BOT, "commit_id": HEAD, "body": "**[P1]** Broken request"}
        self.assertFalse(self.gate(inline=[finding], comments=[native_state(), receipt()]))

    def test_reanchored_old_inline_p1_does_not_block_current_review(self):
        finding = {"user": BOT, "commit_id": HEAD, "original_commit_id": OTHER,
                   "body": "**[P1]** Broken old request"}
        self.assertTrue(self.gate(inline=[finding], comments=[receipt()]))

    def test_original_current_inline_p1_blocks_even_when_display_is_reanchored(self):
        finding = {"user": BOT, "commit_id": OTHER, "original_commit_id": HEAD,
                   "body": "**[P1]** Broken current request"}
        self.assertFalse(self.gate(inline=[finding], comments=[receipt()]))

    def test_legacy_inline_p1_without_original_commit_still_blocks(self):
        finding = {"user": BOT, "commit_id": HEAD, "body": "**[P1]** Broken request"}
        self.assertFalse(self.gate(inline=[finding], comments=[receipt()]))

    def test_p0_p1_in_structured_summary_cannot_bypass_severity_gate(self):
        for severity in ("P0", "P1"):
            with self.subTest(severity=severity):
                comment = native_state()
                comment["body"] += f"\n**[{severity}]** Broken request despite clean JSON"
                self.assertFalse(self.gate(comments=[comment]))

    def test_duplicate_structured_markers_cannot_hide_an_exact_commit_p1(self):
        comment = native_state()
        comment["body"] += "\n" + native_state()["body"] + "\n[P1] Broken request"
        self.assertFalse(self.gate(comments=[comment, receipt()]))

    def test_old_commit_finding_does_not_block_current_commit(self):
        finding = {"user": BOT, "commit_id": OTHER, "body": "[P1] Broken old request"}
        self.assertTrue(self.gate(inline=[finding], comments=[receipt()]))

    def test_p2_does_not_block_a_completed_formal_review(self):
        review = {"id": 12, "user": BOT, "commit_id": HEAD, "state": "COMMENTED", "submitted_at": COMPLETED}
        finding = {"user": BOT, "commit_id": HEAD, "pull_request_review_id": 12, "body": "[P2] Detail"}
        self.assertTrue(self.gate(reviews=[review], inline=[finding]))

    def test_quota_comment_without_completed_review_is_not_success(self):
        review = {"user": BOT, "commit_id": HEAD, "state": "COMMENTED",
                  "submitted_at": COMPLETED, "body": "Review quota exceeded"}
        self.assertFalse(self.gate(reviews=[review]))
        self.assertFalse(self.gate(comments=[{**receipt(), "body": "Review quota exceeded"}]))

    def test_later_negative_formal_review_blocks_publication(self):
        reviews = [
            {"id": 1, "user": BOT, "commit_id": HEAD, "state": "APPROVED", "submitted_at": COMPLETED},
            {"id": 2, "user": BOT, "commit_id": HEAD, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-10-16T10:02:00Z"},
        ]
        self.assertFalse(self.gate(reviews=reviews, comments=[receipt()]))

    def test_changed_pr_head_is_rejected_before_reading_reviews(self):
        with patch.object(release, "api", return_value={"head": {"sha": OTHER}, "state": "open"}):
            self.assertFalse(release.check_review("maintainer/zed-fork", 1, HEAD, OTHER)[0])

    def test_closed_pr_cannot_publish(self):
        with patch.object(release, "api", return_value={"head": {"sha": HEAD}, "state": "closed"}):
            self.assertFalse(release.check_review("maintainer/zed-fork", 1, HEAD, OTHER)[0])

    def test_only_the_exact_clean_upstream_base_can_be_reviewed(self):
        repo = {"full_name": "maintainer/zed-fork"}
        correct = {"ref": f"review-base/{OTHER}", "sha": OTHER, "repo": repo}
        request = {
            "user": {"login": "maintainer", "type": "User"}, "created_at": REQUESTED,
            "body": f"<!-- zed-codex:{HEAD} -->",
        }
        for base, expected in (
            (correct, True),
            ({**correct, "ref": "master"}, False),
            ({**correct, "sha": HEAD}, False),
            ({**correct, "repo": {"full_name": "someone/zed"}}, False),
        ):
            pull = {"state": "open", "head": {"sha": HEAD, "repo": repo}, "base": base}
            with (self.subTest(base=base), patch.object(release, "api", return_value=pull),
                  patch.object(release, "pages", side_effect=[[request, native_state()], [], []])):
                self.assertEqual(release.check_review("maintainer/zed-fork", 1, HEAD, OTHER)[0], expected)

class ApplicationRegressionExtractorTests(unittest.TestCase):
    def test_unannotated_test_named_helpers_are_excluded(self):
        diff = """diff --git a/crates/auto_update/src/auto_update.rs b/crates/auto_update/src/auto_update.rs
--- a/crates/auto_update/src/auto_update.rs
+++ b/crates/auto_update/src/auto_update.rs
@@ -1,2 +1,10 @@
 #[test]
 fn unchanged_upstream_case() {}
+fn test_fork_manifest_helper() {}
+#[test]
+fn test_fork_added_case() {}
+#[test]
+fn added_without_prefix() {}
+#[gpui::test(iterations = 10)]
+async fn added_gpui_without_prefix(cx: &mut TestAppContext) {}
"""
        self.assertEqual(
            release.added_cases(diff),
            {
                ("auto_update", "test_fork_added_case"),
                ("auto_update", "added_without_prefix"),
                ("auto_update", "added_gpui_without_prefix"),
            },
        )

    def test_manifest_accepts_rust_names_and_rejects_malformed_identities(self):
        self.assertEqual(
            release.manifest_cases({"test_cases": [["askpass", "preserves_reason"]]}),
            {("askpass", "preserves_reason")},
        )
        invalid = ("has space", "1leading", "has-dash", "has::separator", "")
        for name in invalid:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Invalid preserved"):
                release.manifest_cases({"test_cases": [["askpass", name]]})
        for case in ("askpass::preserves_reason", ["askpass"], ["askpass", "valid", "extra"],
                     ["askpass", 1]):
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, "Invalid preserved"):
                release.manifest_cases({"test_cases": [case]})

    def test_baseline_manifest_preserves_nonprefixed_cases_and_metadata(self):
        manifest = json.loads((Path(__file__).parents[1] / "patches/source.json").read_text())
        self.assertEqual(manifest["baseline_tag"], "v1.24.2")
        self.assertEqual(manifest["baseline_upstream"], "fb69067729cb16a7f6135e2c1600c26a28c59659")
        self.assertEqual(manifest["baseline_config"], "12726e69aa18e2674099e36a5092c78f1f6ebf3d")
        self.assertTrue({
            ("askpass", "ask_password_preserves_cancelled_by_user"),
            ("askpass", "ask_password_preserves_prompt_unavailable"),
            ("askpass", "askpass_result_preserves_prompt_failure_reason"),
            ("askpass", "askpass_run_propagates_injected_reason_at_channel_boundary"),
            ("remote_connection", "background_delegate_answers_prompt_unavailable"),
            ("remote_connection", "dropped_prompt_ui_answers_prompt_unavailable"),
        }.issubset(release.manifest_cases(manifest)))
