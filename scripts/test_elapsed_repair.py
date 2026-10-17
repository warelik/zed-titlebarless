from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

import release
from test_repair import CONFIG, ENV, HEAD, REPO, STATE, request


class ElapsedRepairContinuationTests(unittest.TestCase):
    def history(self, count, *, first_age=0):
        return [
            request(f"{index + 1:040x}", index + 1,
                    age=first_age if index == 0 else 0)
            for index in range(count)
        ]

    def setup_calls(self, stack, requests):
        stack.enter_context(patch.dict(release.os.environ, ENV))
        api = stack.enter_context(patch.object(
            release, "api", return_value={"sha": CONFIG}))
        stack.enter_context(patch.object(release, "repair_pull", return_value=STATE))
        stack.enter_context(patch.object(release, "owner_token", return_value="fixture"))
        stack.enter_context(patch.object(release, "repair_comments", return_value=requests))
        stack.enter_context(patch.object(
            release, "failure_evidence", return_value="Actual fixture failure"))
        escalate = stack.enter_context(patch.object(
            release, "escalation", side_effect=ValueError("expected elapsed-budget failure")))
        return api, escalate

    def test_fresh_fourth_fifth_and_eleventh_requests_do_not_escalate_by_count(self):
        for count in (3, 4, 10):
            with self.subTest(count=count), ExitStack() as stack:
                api, escalate = self.setup_calls(stack, self.history(count))
                release.request_repair(Path("/unused"))
                escalate.assert_not_called()
                self.assertEqual(api.call_count, 2)
                sent = api.call_args
                self.assertEqual(sent.args[0], f"repos/{REPO}/issues/2/comments")
                self.assertEqual(sent.kwargs["method"], "POST")
                self.assertEqual(sent.kwargs["token"], "fixture")
                body = sent.kwargs["data"]["body"]
                self.assertIn(f"Attempt {count + 1}.", body)
                self.assertIn(f"<!-- zed-fix:{HEAD}:{count + 1} -->", body)
                self.assertIn("Actual fixture failure", body)

    def test_same_head_fourth_request_is_not_dispatched_again(self):
        requests = [*self.history(3), request(HEAD, 4)]
        with ExitStack() as stack:
            api, escalate = self.setup_calls(stack, requests)
            release.request_repair(Path("/unused"))
            escalate.assert_not_called()
            self.assertEqual(api.call_count, 1)

    def test_expired_budget_still_blocks_the_next_request_after_four_fixes(self):
        with ExitStack() as stack:
            api, escalate = self.setup_calls(
                stack, self.history(4, first_age=release.MAX_REPAIR_SECONDS + 1))
            with self.assertRaisesRegex(ValueError, "expected elapsed-budget failure"):
                release.request_repair(Path("/unused"))
            self.assertEqual(api.call_count, 1)
            escalate.assert_called_once()
            self.assertIn("exceeded", escalate.call_args.args[3])

    def test_positive_ordinals_include_more_than_three_but_not_malformed_counts(self):
        for ordinal in (1, 3, 4, 10, 100):
            with self.subTest(ordinal=ordinal):
                match = release.FIX_REQUEST.fullmatch(
                    f"<!-- zed-fix:{HEAD}:{ordinal} -->")
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), HEAD)
                self.assertEqual(match.group(2), str(ordinal))
        for ordinal in ("0", "01", "-1", "1.0", "x"):
            with self.subTest(ordinal=ordinal):
                self.assertIsNone(release.FIX_REQUEST.fullmatch(
                    f"<!-- zed-fix:{HEAD}:{ordinal} -->"))

    def test_elapsed_review_and_platform_budgets_are_preserved(self):
        self.assertEqual(release.MAX_REPAIR_SECONDS, 85500)
        self.assertEqual(release.REVIEW_WAIT_SECONDS, 7200)
        self.assertEqual(release.MACOS_JOB_TIMEOUT_MINUTES, 355)


if __name__ == "__main__":
    unittest.main()
