import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quota_sentry import core


NOW = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)
HARD_BLOCK = core.QuotaConfig(weekly_mode=core.WEEKLY_MODE_HARD_BLOCK)


def reset_in(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def claude_body(five, week, *, five_reset=3600, week_reset=3 * 86400):
    return {
        "five_hour": {"utilization": five, "resets_at": reset_in(five_reset)},
        "seven_day": {"utilization": week, "resets_at": reset_in(week_reset)},
        "seven_day_oauth_apps": None,
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": None,
                        "used_credits": None, "utilization": None},
    }


class ClaudeReadPlanTest(unittest.TestCase):
    """Approved pacing: slow reads when no reading can change anything, else one a minute.

    The daemon's 60 s read spacing turns the 30 s critical interval into one read a minute.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name) / "state.json"

    def tearDown(self):
        self.directory.cleanup()

    def read(self, five, week=40, **resets):
        body = claude_body(five, week, **resets)
        return core.poll_once(
            self.state, fetcher=lambda: core.claude_usage_to_usage(body, now=NOW), now=NOW,
            quota_config=HARD_BLOCK, provider=core.CLAUDE_PROVIDER)

    def plan(self, decision, *, seconds_later=1):
        return core.claude_read_plan(decision, core.read_state(self.state),
                                     now=NOW + timedelta(seconds=seconds_later))

    def test_far_from_limits_reads_every_five_minutes(self):
        self.assertEqual(self.plan(self.read(50, 50)), (300, True))

    def test_five_hour_from_85_percent_reads_every_minute(self):
        self.assertEqual(self.plan(self.read(85, 50)), (60, True))

    def test_weekly_below_93_percent_stays_slow(self):
        self.assertEqual(self.plan(self.read(50, 92)), (300, True))

    def test_weekly_from_93_percent_reads_every_minute(self):
        self.assertEqual(self.plan(self.read(50, 93)), (30, True))

    def test_blocked_before_reset_reads_every_five_minutes_and_ignores_hook_requests(self):
        self.assertEqual(self.plan(self.read(95)), (300, False))

    def test_blocked_read_lands_on_the_reported_reset_time(self):
        self.assertEqual(self.plan(self.read(95, five_reset=100)), (99, False))

    def test_after_the_reported_reset_time_reads_every_minute_again(self):
        decision = self.read(95, five_reset=60)

        self.assertEqual(self.plan(decision, seconds_later=120), (30, True))

    def test_a_later_weekly_reset_keeps_reads_slow_after_the_five_hour_reset(self):
        decision = self.read(95, 99, five_reset=60)

        self.assertEqual(self.plan(decision, seconds_later=120), (300, False))

    def test_a_block_without_a_reset_time_reads_every_minute(self):
        decision = self.read(95)
        state = core.read_state(self.state)
        for window in state["enforcement"]["windows"]:
            window.pop("resetsAt", None)

        plan = core.claude_read_plan(decision, state, now=NOW + timedelta(seconds=1))

        self.assertEqual(plan, (30, True))

    def test_unknown_reading_retries_every_minute(self):
        def broken():
            raise RuntimeError("usage endpoint unavailable")

        decision = core.poll_once(self.state, fetcher=broken, now=NOW, quota_config=HARD_BLOCK,
                                  provider=core.CLAUDE_PROVIDER)

        self.assertEqual(self.plan(decision), (30, True))


if __name__ == "__main__":
    unittest.main()
