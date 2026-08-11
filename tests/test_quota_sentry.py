import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

from quota_sentry import core
from quota_sentry import cli


NOW = datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)


def codexbar_payload(used_percent=94, resets_at="2026-06-01T21:23:05Z"):
    return [
        {
            "provider": "codex",
            "source": "codex-cli",
            "usage": {
                "primary": {
                    "usedPercent": used_percent,
                    "windowMinutes": 300,
                    "resetsAt": resets_at,
                },
                "secondary": {
                    "usedPercent": 39,
                    "windowMinutes": 10080,
                    "resetsAt": "2026-06-07T21:45:36Z",
                },
                "updatedAt": "2026-06-01T16:29:59Z",
            },
        }
    ]


def app_server_rate_limits_response(
    primary_used_percent=51,
    secondary_used_percent=97,
    primary_resets_at="2026-06-01T21:23:05Z",
    secondary_resets_at="2026-06-07T21:45:36Z",
    rate_limit_reached_type=None,
):
    return {
        "planType": "plus",
        "rateLimits": {
            "limitId": "codex",
            "rateLimitReachedType": rate_limit_reached_type,
            "primary": {
                "usedPercent": primary_used_percent,
                "windowDurationMins": 300,
                "resetsAt": int(datetime.fromisoformat(primary_resets_at.replace("Z", "+00:00")).timestamp()),
            },
            "secondary": {
                "usedPercent": secondary_used_percent,
                "windowDurationMins": 10080,
                "resetsAt": int(datetime.fromisoformat(secondary_resets_at.replace("Z", "+00:00")).timestamp()),
            },
        },
    }


class ParseCodexbarUsageTest(unittest.TestCase):
    def test_extract_json_skips_codex_notify_prefix(self):
        payload = core.extract_json(
            "[codex notify] remoteControl/status/changed\n"
            '[{"provider":"codex","usage":{"primary":{"usedPercent":1}}}]'
        )

        self.assertEqual(payload[0]["provider"], "codex")

    def test_allows_when_five_hour_window_is_below_threshold(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=94),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "open")
        self.assertEqual(decision.used_percent, 94)
        self.assertEqual(decision.window_minutes, 300)
        self.assertIsNone(decision.blocked_until)

    def test_blocks_until_reset_plus_buffer_at_threshold(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=95),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.used_percent, 95)
        self.assertEqual(
            decision.blocked_until,
            datetime(2026, 6, 1, 21, 24, 5, tzinfo=timezone.utc),
        )

    def test_high_usage_with_past_reset_is_not_authoritative_open(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=99, resets_at="2026-06-01T16:00:00Z"),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "unknown")
        self.assertTrue(decision.fail_open)
        self.assertIn("awaiting authoritative", decision.reason)

    def test_valid_weekly_block_wins_over_malformed_short_term_window(self):
        payload = codexbar_payload(used_percent=50, resets_at="not-a-date")
        payload[0]["usage"]["secondary"]["usedPercent"] = 99

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_WEEKLY)
        self.assertEqual(decision.used_percent, 99)

    def test_non_codex_provider_is_not_accepted_as_codex_usage(self):
        payload = codexbar_payload(used_percent=100)
        payload[0]["provider"] = "other-provider"

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "unknown")
        self.assertIn("no provider", decision.reason)

    def test_backend_rate_limit_state_blocks_even_below_percent_thresholds(self):
        response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=98,
            rate_limit_reached_type="rate_limit_reached",
        )

        payload = core.codex_app_server_rate_limits_to_usage(response, now=NOW)
        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_ACCOUNT)
        self.assertEqual(decision.rate_limit_reached_type, "rate_limit_reached")

    def test_backend_rate_limit_state_blocks_without_quota_windows(self):
        payload = core.codex_app_server_rate_limits_to_usage(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "rateLimitReachedType": "workspace_member_usage_limit_reached",
                }
            },
            now=NOW,
        )

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_ACCOUNT)
        self.assertIsNotNone(decision.blocked_until)
        self.assertEqual(decision.quota_windows[0].kind, core.WINDOW_KIND_ACCOUNT)
        self.assertEqual(core.next_poll_interval_seconds(decision), 30)

    def test_fails_open_on_provider_error(self):
        decision = core.parse_codexbar_usage(
            [{"provider": "codex", "error": {"message": "cookie access denied"}}],
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "unknown")
        self.assertTrue(decision.fail_open)
        self.assertIn("cookie access denied", decision.reason)

    def test_invalid_reset_timestamp_fails_open(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=99, resets_at="not-a-date"),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "unknown")
        self.assertTrue(decision.fail_open)
        self.assertIn("invalid resetsAt", decision.reason)

    def test_prefers_the_five_hour_window_even_if_it_is_not_primary(self):
        payload = codexbar_payload(used_percent=12)
        payload[0]["usage"]["primary"]["windowMinutes"] = 10080
        payload[0]["usage"]["primary"]["usedPercent"] = 15
        payload[0]["usage"]["secondary"]["windowMinutes"] = 300
        payload[0]["usage"]["secondary"]["usedPercent"] = 97

        decision = core.parse_codexbar_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.used_percent, 97)
        self.assertEqual(decision.window_minutes, 300)

    def test_weekly_only_primary_slot_hard_blocks_by_default(self):
        payload = codexbar_payload(used_percent=99)
        payload[0]["usage"]["primary"]["windowMinutes"] = 10080
        payload[0]["usage"]["primary"]["resetsAt"] = "2026-06-07T21:45:36Z"
        del payload[0]["usage"]["secondary"]

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )
        state = core.state_from_decision(decision, now=NOW)

        self.assertEqual(decision.status, "blocked")
        self.assertFalse(decision.fail_open)
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_WEEKLY)
        self.assertTrue(decision.weekly_hard_block_enabled)
        self.assertIsNone(decision.short_term_window)
        self.assertEqual(decision.weekly_window.used_percent, 99)
        self.assertIsNone(state["shortTerm"])
        self.assertIsNone(state["primary"])
        self.assertEqual(state["weekly"]["sourceSlot"], "primary")
        self.assertEqual(state["windows"][0]["kind"], "weekly")
        self.assertEqual(state["blockedWindow"], core.WINDOW_KIND_WEEKLY)
        self.assertTrue(state["weeklyHardBlockEnabled"])

    def test_weekly_only_primary_slot_remains_open_when_advisory_is_explicit(self):
        payload = codexbar_payload(used_percent=99)
        payload[0]["usage"]["primary"]["windowMinutes"] = 10080
        payload[0]["usage"]["primary"]["resetsAt"] = "2026-06-07T21:45:36Z"
        del payload[0]["usage"]["secondary"]

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
            quota_config=core.QuotaConfig(
                weekly_mode=core.WEEKLY_MODE_ADVISORY,
                weekly_threshold_percent=99,
            ),
        )

        self.assertEqual(decision.status, "open")
        self.assertIsNone(decision.blocked_window)
        self.assertFalse(decision.weekly_hard_block_enabled)

    def test_non_five_hour_short_term_window_uses_short_term_policy(self):
        payload = codexbar_payload(used_percent=95)
        payload[0]["usage"]["primary"]["windowMinutes"] = 15
        del payload[0]["usage"]["secondary"]

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_SHORT_TERM)
        self.assertEqual(decision.window_minutes, 15)

    def test_unfamiliar_long_term_window_is_recorded_and_fails_open(self):
        payload = codexbar_payload(used_percent=99)
        payload[0]["usage"]["primary"]["windowMinutes"] = 2880
        del payload[0]["usage"]["secondary"]

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "open")
        self.assertTrue(decision.fail_open)
        self.assertEqual(decision.quota_windows[0].kind, core.WINDOW_KIND_LONG_TERM)
        self.assertIn("unfamiliar", decision.reason)

    def test_fractional_usage_is_preserved(self):
        payload = codexbar_payload(used_percent=94.75)

        decision = core.parse_codex_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.used_percent, 94.75)

    def test_weekly_window_hard_blocks_by_default(self):
        payload = codexbar_payload(used_percent=50)
        payload[0]["usage"]["secondary"]["usedPercent"] = 99

        decision = core.parse_codexbar_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )
        state = core.state_from_decision(decision, now=NOW)

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.used_percent, 99)
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_WEEKLY)
        self.assertEqual(decision.weekly_window.used_percent, 99)
        self.assertEqual(state["weekly"]["usedPercent"], 99)
        self.assertEqual(state["weekly"]["windowMinutes"], 10080)
        self.assertTrue(state["weeklyHardBlockEnabled"])

    def test_weekly_classification_tolerates_minor_duration_drift(self):
        for window_minutes in (10079, 10081):
            with self.subTest(window_minutes=window_minutes):
                payload = codexbar_payload(used_percent=40)
                payload[0]["usage"]["secondary"]["windowMinutes"] = window_minutes
                payload[0]["usage"]["secondary"]["usedPercent"] = 99

                decision = core.parse_codex_usage(payload, now=NOW)

                self.assertEqual(decision.status, "blocked")
                self.assertEqual(decision.blocked_window, core.WINDOW_KIND_WEEKLY)

    def test_exhausted_unfamiliar_canonical_window_blocks_as_fail_safe(self):
        payload = codexbar_payload(used_percent=100)
        payload[0]["usage"]["primary"]["windowMinutes"] = 43200

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_LONG_TERM)

    def test_multiple_uncategorized_buckets_without_a_canonical_limit_are_unknown(self):
        payload = [
            {
                "provider": "codex",
                "source": core.CODEX_APP_SERVER_SOURCE,
                "usage": {
                    "updatedAt": "2026-06-01T16:30:00Z",
                    "windows": [
                        {
                            "usedPercent": 10,
                            "windowMinutes": 300,
                            "resetsAt": "2026-06-01T21:23:05Z",
                            "limitId": "codex_next",
                            "isDefaultLimit": False,
                        },
                        {
                            "usedPercent": 100,
                            "windowMinutes": 10080,
                            "resetsAt": "2026-06-07T21:45:36Z",
                            "limitId": "codex_other",
                            "isDefaultLimit": False,
                        },
                    ],
                },
            }
        ]

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "unknown")

    def test_malformed_unfamiliar_canonical_window_is_unknown(self):
        payload = codexbar_payload(used_percent=50)
        payload[0]["usage"]["primary"]["windowMinutes"] = 43200
        payload[0]["usage"]["secondary"] = {
            "usedPercent": "not-a-percentage",
            "windowMinutes": 43200,
            "resetsAt": "2026-07-01T21:45:36Z",
        }

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "unknown")

    def test_durationless_canonical_window_is_unknown_below_exhaustion(self):
        payload = codexbar_payload(used_percent=50)
        payload[0]["usage"]["primary"]["windowMinutes"] = None

        decision = core.parse_codex_usage(payload, now=NOW)

        self.assertEqual(decision.status, "unknown")

    def test_weekly_window_hard_blocks_when_explicitly_enabled(self):
        payload = codexbar_payload(used_percent=50)
        payload[0]["usage"]["secondary"]["usedPercent"] = 99

        decision = core.parse_codexbar_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
            quota_config=core.QuotaConfig(
                weekly_mode=core.WEEKLY_MODE_HARD_BLOCK,
                weekly_threshold_percent=99,
            ),
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.used_percent, 99)
        self.assertEqual(decision.window_minutes, 10080)
        self.assertEqual(decision.blocked_window, "weekly")
        self.assertEqual(
            decision.blocked_until,
            datetime(2026, 6, 7, 21, 46, 36, tzinfo=timezone.utc),
        )

    def test_weekly_hard_block_waits_for_later_reset_when_both_windows_block(self):
        payload = codexbar_payload(
            used_percent=95,
            resets_at="2026-06-01T17:00:00Z",
        )
        payload[0]["usage"]["secondary"]["usedPercent"] = 99

        decision = core.parse_codexbar_usage(
            payload,
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
            quota_config=core.QuotaConfig(
                weekly_mode=core.WEEKLY_MODE_HARD_BLOCK,
                weekly_threshold_percent=99,
            ),
        )

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, "weekly")
        self.assertEqual(decision.window_minutes, 10080)


class StateTest(unittest.TestCase):
    def test_state_round_trips_decision_as_json(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=95),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, decision, now=NOW)
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["usedPercent"], 95)
        self.assertEqual(loaded["blockedUntil"], "2026-06-01T21:24:05Z")
        self.assertEqual(loaded["primary"]["usedPercent"], 95)
        self.assertEqual(loaded["weekly"]["usedPercent"], 39)

    def test_should_block_from_state_requires_fresh_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:29:30Z",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertEqual(block_until, datetime(2026, 6, 1, 21, 24, 5, tzinfo=timezone.utc))

    def test_confirmed_block_remains_active_when_state_is_stale(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:00:00Z",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertEqual(block_until, datetime(2026, 6, 1, 21, 24, 5, tzinfo=timezone.utc))

    def test_confirmed_block_remains_active_after_estimated_reset(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:00:00Z",
            "blockedUntil": "2026-06-01T16:15:00Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertIsNotNone(block_until)
        self.assertGreater(block_until, NOW)

    def test_future_dated_open_state_is_not_treated_as_fresh(self):
        state = {
            "status": "open",
            "updatedAt": "2026-06-01T17:30:00Z",
        }

        self.assertFalse(
            core.state_is_fresh(
                state,
                now=NOW,
                max_state_age_seconds=420,
            )
        )

    def test_confirmed_block_survives_unknown_poll_result(self):
        blocked = core.parse_codex_usage(
            codexbar_payload(used_percent=99),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )
        unknown = core.QuotaDecision(
            status="unknown",
            reason="quota fetch failed: simulated outage",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, unknown, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["sourceStatus"], "unknown")
        self.assertIn("simulated outage", loaded["sourceReason"])
        self.assertEqual(loaded["enforcement"]["status"], "blocked")
        self.assertEqual(loaded["enforcement"]["windows"][0]["kind"], "short-term")

    def test_authoritative_below_threshold_poll_clears_confirmed_block(self):
        blocked = core.parse_codex_usage(
            codexbar_payload(used_percent=99),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )
        opened = core.parse_codex_usage(
            codexbar_payload(used_percent=12),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, opened, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "open")
        self.assertEqual(loaded["sourceStatus"], "open")
        self.assertEqual(loaded["enforcement"], {"status": "open", "windows": []})

    def test_weekly_latch_survives_total_poll_source_failure(self):
        payload = codexbar_payload(used_percent=50)
        payload[0]["usage"]["secondary"]["usedPercent"] = 99
        blocked = core.parse_codex_usage(payload, now=NOW)

        def failed_fetcher():
            raise RuntimeError("simulated total source failure")

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            decision = core.poll_once(
                state_path,
                fetcher=failed_fetcher,
                quota_config=core.QuotaConfig(),
                now=NOW + timedelta(minutes=10),
            )
            loaded = core.read_state(state_path)

        self.assertEqual(decision.status, "unknown")
        self.assertTrue(decision.weekly_hard_block_enabled)
        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["sourceStatus"], "unknown")
        self.assertEqual(loaded["blockedWindow"], core.WINDOW_KIND_WEEKLY)

    def test_stale_codexbar_open_cannot_clear_app_server_weekly_latch(self):
        blocked_payload = core.codex_app_server_rate_limits_to_usage(
            app_server_rate_limits_response(secondary_used_percent=99),
            now=NOW,
        )
        blocked = core.parse_codex_usage(blocked_payload, now=NOW)
        stale_open_payload = codexbar_payload(used_percent=10)
        stale_open_payload[0]["usage"]["updatedAt"] = "2026-06-01T15:00:00Z"
        stale_open_payload[0]["usage"]["secondary"]["usedPercent"] = 10
        stale_open = core.parse_codex_usage(
            stale_open_payload,
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, stale_open, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["blockedWindow"], core.WINDOW_KIND_WEEKLY)
        self.assertEqual(loaded["enforcement"]["windows"][0]["source"], "codex-app-server")

    def test_current_app_server_open_clears_app_server_weekly_latch(self):
        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=99),
                now=NOW,
            ),
            now=NOW,
        )
        opened = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=10),
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, opened, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "open")

    def test_older_app_server_snapshot_cannot_clear_weekly_latch(self):
        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=99),
                now=NOW,
            ),
            now=NOW,
        )
        older_open = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=10),
                now=NOW - timedelta(hours=1),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, older_open, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["blockedWindow"], core.WINDOW_KIND_WEEKLY)

    def test_different_canonical_limit_cannot_clear_weekly_latch(self):
        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=99),
                now=NOW,
            ),
            now=NOW,
        )
        other_limit_response = app_server_rate_limits_response(secondary_used_percent=10)
        other_limit_response["rateLimits"]["limitId"] = "codex_other"
        other_limit_open = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                other_limit_response,
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, other_limit_open, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["blockedLimitId"], "codex")

    def test_exhausted_long_term_latch_clears_below_one_hundred_percent(self):
        blocked_payload = codexbar_payload(used_percent=100)
        blocked_payload[0]["usage"]["primary"]["windowMinutes"] = 43200
        opened_payload = codexbar_payload(used_percent=99)
        opened_payload[0]["usage"]["primary"]["windowMinutes"] = 43200
        opened_payload[0]["usage"]["updatedAt"] = "2026-06-01T16:39:59Z"
        blocked = core.parse_codex_usage(blocked_payload, now=NOW)
        opened = core.parse_codex_usage(
            opened_payload,
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, opened, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "open")

    def test_backend_limit_latch_clears_only_when_backend_reports_clear(self):
        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(
                    primary_used_percent=40,
                    secondary_used_percent=40,
                    rate_limit_reached_type="rate_limit_reached",
                ),
                now=NOW,
            ),
            now=NOW,
        )
        opened = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(
                    primary_used_percent=40,
                    secondary_used_percent=40,
                    rate_limit_reached_type=None,
                ),
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, opened, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "open")

    def test_spend_control_latch_requires_explicit_false_to_clear(self):
        blocked_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
        )
        blocked_response["rateLimits"]["spendControlReached"] = True
        unavailable_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
        )
        cleared_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
        )
        cleared_response["rateLimits"]["spendControlReached"] = False

        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(blocked_response, now=NOW),
            now=NOW,
        )
        unavailable = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                unavailable_response,
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )
        cleared = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                cleared_response,
                now=NOW + timedelta(minutes=20),
            ),
            now=NOW + timedelta(minutes=20),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, unavailable, now=NOW + timedelta(minutes=10))
            unavailable_state = core.read_state(state_path)
            core.write_state(state_path, cleared, now=NOW + timedelta(minutes=20))
            cleared_state = core.read_state(state_path)

        self.assertEqual(unavailable_state["status"], "blocked")
        self.assertEqual(unavailable_state["rateLimitReachedType"], "spend_control_reached")
        self.assertEqual(cleared_state["status"], "open")

    def test_malformed_backend_clear_signal_cannot_release_account_latch(self):
        blocked_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
            rate_limit_reached_type="rate_limit_reached",
        )
        malformed_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
        )
        malformed_response["rateLimits"]["rateLimitReachedType"] = {"invalid": True}

        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(blocked_response, now=NOW),
            now=NOW,
        )
        malformed = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                malformed_response,
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, malformed, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["rateLimitReachedType"], "rate_limit_reached")

    def test_omitted_backend_state_cannot_release_account_latch(self):
        blocked_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
            rate_limit_reached_type="rate_limit_reached",
        )
        omitted_response = app_server_rate_limits_response(
            primary_used_percent=40,
            secondary_used_percent=40,
        )
        omitted_response["rateLimits"].pop("rateLimitReachedType")

        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(blocked_response, now=NOW),
            now=NOW,
        )
        omitted = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                omitted_response,
                now=NOW + timedelta(minutes=10),
            ),
            now=NOW + timedelta(minutes=10),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)
            core.write_state(state_path, omitted, now=NOW + timedelta(minutes=10))
            loaded = core.read_state(state_path)

        self.assertFalse(omitted.rate_limit_state_known)
        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(loaded["rateLimitReachedType"], "rate_limit_reached")

    def test_live_guard_rereads_durable_latch_after_open_poll_decision(self):
        blocked = core.parse_codex_usage(
            core.codex_app_server_rate_limits_to_usage(
                app_server_rate_limits_response(secondary_used_percent=99),
                now=NOW,
            ),
            now=NOW,
        )
        short_only_payload = codexbar_payload(used_percent=10)
        del short_only_payload[0]["usage"]["secondary"]
        opened_without_weekly = core.parse_codex_usage(
            short_only_payload,
            now=NOW + timedelta(days=8),
        )
        failures = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_state(state_path, blocked, now=NOW)

            def poller():
                core.write_state(
                    state_path,
                    opened_without_weekly,
                    now=NOW + timedelta(days=8),
                )
                return opened_without_weekly

            result = core.wait_if_blocked(
                state_path,
                poller=poller,
                now_func=lambda: NOW + timedelta(days=8),
                monotonic_func=lambda: 10.0,
                fail_closed_after_seconds=0,
                fail_closed_output=failures.append,
                notify=False,
            )
            loaded = core.read_state(state_path)

        self.assertEqual(result, 2)
        self.assertEqual(loaded["status"], "blocked")
        self.assertEqual(failures, [core.HOOK_FAIL_CLOSED_MESSAGE])

    def test_should_not_block_from_invalid_state_timestamp(self):
        state = {
            "status": "blocked",
            "updatedAt": "not-a-date",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertIsNone(block_until)

    def test_terminal_notice_file_errors_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory_path = Path(temp_dir)
            old_value = os.environ.get("QUOTA_SENTRY_NOTICE_FILE")
            os.environ["QUOTA_SENTRY_NOTICE_FILE"] = str(directory_path)
            try:
                core.emit_terminal_notice("test")
            finally:
                if old_value is None:
                    os.environ.pop("QUOTA_SENTRY_NOTICE_FILE", None)
                else:
                    os.environ["QUOTA_SENTRY_NOTICE_FILE"] = old_value

    def test_terminal_notice_skips_background_process_group(self):
        writes = []

        with mock.patch.object(core.os, "open", return_value=7), \
            mock.patch.object(core.os, "tcgetpgrp", return_value=100), \
            mock.patch.object(core.os, "getpgrp", return_value=200), \
            mock.patch.object(core.os, "write", side_effect=lambda _fd, data: writes.append(data)), \
            mock.patch.object(core.os, "close") as close:
            core.emit_terminal_notice("test")

        self.assertEqual(writes, [])
        close.assert_called_once_with(7)

    def test_terminal_notice_writes_foreground_process_group(self):
        writes = []

        with mock.patch.object(core.os, "open", return_value=7), \
            mock.patch.object(core.os, "tcgetpgrp", return_value=100), \
            mock.patch.object(core.os, "getpgrp", return_value=100), \
            mock.patch.object(core.os, "write", side_effect=lambda _fd, data: writes.append(data)), \
            mock.patch.object(core.os, "close"):
            core.emit_terminal_notice("Quota Sentry: test")

        self.assertEqual(writes, [b"\nQuota Sentry: test\n"])

    def test_wait_if_blocked_is_quiet_by_default(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "windowMinutes": 300,
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        current = {"value": datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)}
        messages = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            def sleeper(seconds):
                current["value"] = current["value"] + timedelta(seconds=seconds)
                if current["value"] >= datetime(2026, 6, 1, 16, 31, 1, tzinfo=timezone.utc):
                    state_path.write_text(json.dumps({"status": "open"}))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("poller should not be called for fresh blocked state"),
                sleeper=sleeper,
                now_func=lambda: current["value"],
                output=messages.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(messages, [])

    def test_wait_if_blocked_polls_after_expired_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "windowMinutes": 300,
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        poll_count = {"value": 0}

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            def poller():
                poll_count["value"] += 1
                observed_at = datetime(2026, 6, 1, 16, 32, 0, tzinfo=timezone.utc)
                short_term = core.QuotaWindow(
                    name="primary",
                    used_percent=40,
                    window_minutes=300,
                    resets_at=datetime(2026, 6, 1, 21, 0, 0, tzinfo=timezone.utc),
                    kind=core.WINDOW_KIND_SHORT_TERM,
                )
                decision = core.QuotaDecision(
                    status="open",
                    reason="fresh quota poll",
                    fail_open=False,
                    short_term_window=short_term,
                    quota_windows=(short_term,),
                    source=core.CODEX_APP_SERVER_SOURCE,
                    source_observed_at=observed_at,
                )
                core.write_state(
                    state_path,
                    decision,
                    now=observed_at,
                )
                return decision

            result = core.wait_if_blocked(
                state_path,
                poller=poller,
                sleeper=lambda _seconds: self.fail("expired state should not sleep"),
                now_func=lambda: datetime(2026, 6, 1, 16, 32, 0, tzinfo=timezone.utc),
                output=lambda _message: self.fail("quiet guard should not write stdout"),
            )

        self.assertEqual(result, 0)
        self.assertEqual(poll_count["value"], 1)

    def test_state_only_guard_does_not_poll_when_cache_is_stale(self):
        state = {
            "status": "open",
            "updatedAt": "2026-06-01T16:00:00Z",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("state-only guard must not call live poller"),
                state_only=True,
                now_func=lambda: NOW,
            )

        self.assertEqual(result, 0)

    def test_checkpoint_guard_fails_closed_when_nonblocked_cache_is_stale(self):
        state = {
            "status": "open",
            "updatedAt": "2026-06-01T16:00:00Z",
        }
        failures = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("cache-only checkpoint must not live-poll"),
                state_only=True,
                require_fresh_state=True,
                now_func=lambda: NOW,
                fail_closed_output=failures.append,
            )

        self.assertEqual(result, 2)
        self.assertEqual(failures, [core.HOOK_STATE_UNAVAILABLE_MESSAGE])

    def test_checkpoint_guard_fails_closed_when_fresh_state_is_unknown(self):
        state = {
            "status": "unknown",
            "updatedAt": "2026-06-01T16:29:59Z",
            "reason": "quota source unavailable",
        }
        failures = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("state-only guard must not live-poll"),
                state_only=True,
                require_fresh_state=True,
                now_func=lambda: NOW,
                fail_closed_output=failures.append,
            )

        self.assertEqual(result, 2)
        self.assertEqual(failures, [core.HOOK_STATE_UNAVAILABLE_MESSAGE])

    def test_state_only_guard_waits_on_fresh_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        current = {"value": datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)}

        def sleeper(seconds):
            current["value"] = current["value"] + timedelta(seconds=seconds)
            if current["value"] >= datetime(2026, 6, 1, 16, 31, 1, tzinfo=timezone.utc):
                state_path.write_text(json.dumps({"status": "open"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("state-only guard must not call live poller"),
                sleeper=sleeper,
                state_only=True,
                now_func=lambda: current["value"],
                notify=False,
            )

        self.assertEqual(result, 0)

    def test_wait_if_blocked_emits_single_wait_notice_without_stdout(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        current = {"value": datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)}
        stdout_messages = []
        notices = []

        def sleeper(seconds):
            current["value"] = current["value"] + timedelta(seconds=seconds)
            if current["value"] >= datetime(2026, 6, 1, 16, 31, 1, tzinfo=timezone.utc):
                state_path.write_text(json.dumps({"status": "open"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("poller should not be called for fresh blocked state"),
                sleeper=sleeper,
                now_func=lambda: current["value"],
                poll_interval_seconds=30,
                output=stdout_messages.append,
                notice=notices.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(stdout_messages, [])
        self.assertEqual(
            notices,
            ["Quota Sentry: waiting for Codex quota reset until 2026-06-01T16:31:01Z."],
        )

    def test_wait_if_blocked_can_emit_single_verbose_message(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        current = {"value": datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)}
        messages = []

        def sleeper(seconds):
            current["value"] = current["value"] + timedelta(seconds=seconds)
            if current["value"] >= datetime(2026, 6, 1, 16, 31, 1, tzinfo=timezone.utc):
                state_path.write_text(json.dumps({"status": "open"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("poller should not be called for fresh blocked state"),
                sleeper=sleeper,
                now_func=lambda: current["value"],
                poll_interval_seconds=30,
                output=messages.append,
                verbose=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            messages,
            ["Quota Sentry: Codex quota guard active until 2026-06-01T16:31:01Z."],
        )

    def test_wait_if_blocked_fails_closed_before_external_hook_timeout(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-07T16:30:00Z",
        }
        current = {"wall": NOW, "monotonic": 100.0}
        failures = []

        def sleeper(seconds):
            current["wall"] = current["wall"] + timedelta(seconds=seconds)
            current["monotonic"] += seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("fresh blocked state must not live-poll"),
                sleeper=sleeper,
                now_func=lambda: current["wall"],
                monotonic_func=lambda: current["monotonic"],
                poll_interval_seconds=30,
                fail_closed_after_seconds=5,
                fail_closed_output=failures.append,
                notify=False,
            )

        self.assertEqual(result, 2)
        self.assertEqual(current["monotonic"], 105.0)
        self.assertEqual(
            failures,
            ["Quota Sentry: quota remains blocked; refusing to fail open at the hook safety deadline."],
        )


class HookInstallTest(unittest.TestCase):
    def test_hook_install_rejects_platform_without_posix_locking(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            core,
            "fcntl",
            None,
        ), mock.patch("sys.stderr", new_callable=StringIO) as stderr:
            hooks_path = Path(temp_dir) / "hooks.json"
            args = cli.build_parser().parse_args(
                ["install-hook", "--hooks-path", str(hooks_path)]
            )
            result = cli.install_hook_command(args)

        self.assertEqual(result, 1)
        self.assertFalse(hooks_path.exists())
        self.assertIn("not supported", stderr.getvalue())

    def test_hook_install_rejects_a_concurrent_installer(self):
        if core.fcntl is None:
            self.skipTest("process file locking is unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            hooks_path = Path(temp_dir) / "hooks.json"
            lock_path = hooks_path.with_suffix(
                hooks_path.suffix + ".quota-sentry-install.lock"
            )
            held_lock = cli.try_acquire_daemon_lock(lock_path)
            self.assertIsNotNone(held_lock)
            try:
                args = cli.build_parser().parse_args(
                    ["install-hook", "--hooks-path", str(hooks_path)]
                )
                with mock.patch("sys.stderr", new_callable=StringIO) as stderr:
                    result = cli.install_hook_command(args)
            finally:
                cli.release_daemon_lock(held_lock)

        self.assertEqual(result, 1)
        self.assertFalse(hooks_path.exists())
        self.assertIn("another hook installation", stderr.getvalue())

    def test_default_hook_path_honors_codex_home(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ, {"CODEX_HOME": temp_dir}
        ):
            args = cli.build_parser().parse_args(["install-hook"])

        self.assertEqual(Path(args.hooks_path), Path(temp_dir) / "hooks.json")

    def test_empty_hooks_file_loads_as_empty_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hooks_path = Path(temp_dir) / "hooks.json"
            hooks_path.write_text("")

            self.assertEqual(cli.read_hooks_config(hooks_path), {})

    def test_merge_hook_config_preserves_existing_hooks(self):
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "echo existing"}],
                    }
                ]
            }
        }

        merged = core.merge_codex_hooks(existing, script_path=Path("/opt/quota-sentry"))

        self.assertEqual(len(merged["hooks"]["SessionStart"]), 2)
        self.assertIn("UserPromptSubmit", merged["hooks"])
        self.assertIn("PreToolUse", merged["hooks"])
        self.assertIn("PostToolUse", merged["hooks"])
        self.assertIn("PreCompact", merged["hooks"])
        session_start_hook = merged["hooks"]["SessionStart"][-1]["hooks"][0]
        session_start_matcher = merged["hooks"]["SessionStart"][-1]["matcher"]
        user_prompt_command = merged["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]
        pre_tool_command = merged["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
        self.assertFalse(session_start_hook["async"])
        self.assertEqual(session_start_matcher, "startup|resume|clear|compact")
        self.assertIn("--read-hook-input", session_start_hook["command"])
        self.assertEqual(
            user_prompt_command,
            "/opt/quota-sentry prompt-guard --read-hook-input "
            "--require-fresh-state "
            f"--fail-closed-after-seconds {core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS}",
        )
        self.assertEqual(
            pre_tool_command,
            "/opt/quota-sentry guard --state-only --no-notify --read-hook-input "
            "--require-fresh-state "
            f"--fail-closed-after-seconds {core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS}",
        )
        self.assertIn("--fail-closed-format block-json", merged["hooks"]["PostToolUse"][-1]["hooks"][0]["command"])
        self.assertIn("--fail-closed-format stop-json", merged["hooks"]["PreCompact"][-1]["hooks"][0]["command"])

    def test_blocking_hooks_outlast_annual_account_window_and_fail_closed_first(self):
        merged = core.merge_codex_hooks({}, script_path=Path("/opt/quota-sentry"))
        minimum_account_wait = 365 * 24 * 60 * 60 + core.DEFAULT_RESET_BUFFER_SECONDS

        self.assertGreater(core.DEFAULT_HOOK_TIMEOUT_SECONDS, minimum_account_wait)
        self.assertGreater(
            core.DEFAULT_HOOK_TIMEOUT_SECONDS,
            core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS,
        )
        self.assertGreaterEqual(
            core.DEFAULT_HOOK_TIMEOUT_SECONDS - core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS,
            core.DEFAULT_HOOK_TIMEOUT_MARGIN_SECONDS,
        )

        for event_name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"):
            hook = merged["hooks"][event_name][-1]["hooks"][0]
            self.assertEqual(hook["timeout"], core.DEFAULT_HOOK_TIMEOUT_SECONDS)
            self.assertIn("--read-hook-input", hook["command"])
            self.assertIn("--require-fresh-state", hook["command"])
            self.assertIn(
                f"--fail-closed-after-seconds {core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS}",
                hook["command"],
            )

    def test_installed_hook_commands_do_not_use_shell_composition(self):
        merged = core.merge_codex_hooks({}, script_path=Path("/opt/quota-sentry"))

        for entries in merged["hooks"].values():
            for entry in entries:
                for hook in entry["hooks"]:
                    command = hook["command"]
                    self.assertFalse(hook["async"])
                    self.assertNotIn(";", command)
                    self.assertNotIn("&&", command)
                    self.assertNotIn("|", command)
                    self.assertNotIn("\n", command)

    def test_user_prompt_hook_is_single_invocation_with_spaced_script_path(self):
        script_path = Path("/Users/example/Open Development/quotaSentry/scripts/quota-sentry")
        merged = core.merge_codex_hooks({}, script_path=script_path)

        command = merged["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]

        self.assertEqual(
            command,
            "'/Users/example/Open Development/quotaSentry/scripts/quota-sentry' prompt-guard "
            "--read-hook-input "
            "--require-fresh-state "
            f"--fail-closed-after-seconds {core.DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS}",
        )
        self.assertEqual(command.count("quota-sentry"), 1)

    def test_hook_trust_updates_cover_only_exact_quota_sentry_hooks(self):
        hooks_path = Path("/Users/example/.codex/hooks.json")
        script_path = Path("/opt/quota-sentry")
        merged = core.merge_codex_hooks({}, script_path=script_path)
        event_names = {
            "SessionStart": "sessionStart",
            "UserPromptSubmit": "userPromptSubmit",
            "PreToolUse": "preToolUse",
            "PostToolUse": "postToolUse",
            "PreCompact": "preCompact",
        }
        event_keys = {
            "sessionStart": "session_start",
            "userPromptSubmit": "user_prompt_submit",
            "preToolUse": "pre_tool_use",
            "postToolUse": "post_tool_use",
            "preCompact": "pre_compact",
        }
        metadata = []
        for index, (config_name, event_name) in enumerate(event_names.items()):
            entry = merged["hooks"][config_name][-1]
            hook = entry["hooks"][0]
            metadata.append(
                {
                    "key": f"{hooks_path}:{event_keys[event_name]}:0:0",
                    "eventName": event_name,
                    "handlerType": "command",
                    "matcher": entry.get("matcher") or None,
                    "command": hook["command"],
                    "sourcePath": str(hooks_path),
                    "currentHash": f"sha256:{index}",
                }
            )
        metadata.append(
            {
                "key": f"{hooks_path}:pre_tool_use:1:0",
                "eventName": "preToolUse",
                "handlerType": "command",
                "command": "echo unrelated",
                "sourcePath": str(hooks_path),
                "currentHash": "sha256:unrelated",
            }
        )

        updates = core.quota_sentry_hook_trust_updates(
            {"data": [{"cwd": "/workspace", "hooks": metadata}]},
            hooks_path=hooks_path,
            script_path=script_path,
        )

        self.assertEqual(len(updates), 5)
        self.assertNotIn(f"{hooks_path}:pre_tool_use:1:0", updates)
        self.assertEqual(
            updates[f"{hooks_path}:user_prompt_submit:0:0"],
            {"trusted_hash": "sha256:1", "enabled": True},
        )

    def test_hook_trust_updates_reject_incomplete_discovery(self):
        hooks_path = Path("/Users/example/.codex/hooks.json")

        with self.assertRaisesRegex(RuntimeError, "missing exact Quota Sentry hooks"):
            core.quota_sentry_hook_trust_updates(
                {"data": [{"cwd": "/workspace", "hooks": []}]},
                hooks_path=hooks_path,
                script_path=Path("/opt/quota-sentry"),
            )

    def test_hook_activation_verifier_requires_trusted_enabled_exact_hooks(self):
        hooks_path = Path("/Users/example/.codex/hooks.json")
        script_path = Path("/opt/quota-sentry")
        merged = core.merge_codex_hooks({}, script_path=script_path)
        event_names = {
            "SessionStart": "sessionStart",
            "UserPromptSubmit": "userPromptSubmit",
            "PreToolUse": "preToolUse",
            "PostToolUse": "postToolUse",
            "PreCompact": "preCompact",
        }
        event_keys = {
            "sessionStart": "session_start",
            "userPromptSubmit": "user_prompt_submit",
            "preToolUse": "pre_tool_use",
            "postToolUse": "post_tool_use",
            "preCompact": "pre_compact",
        }
        metadata = []
        for index, (config_name, event_name) in enumerate(event_names.items()):
            entry = merged["hooks"][config_name][-1]
            hook = entry["hooks"][0]
            metadata.append(
                {
                    "key": f"{hooks_path}:{event_keys[event_name]}:0:0",
                    "eventName": event_name,
                    "handlerType": "command",
                    "matcher": entry.get("matcher") or None,
                    "command": hook["command"],
                    "sourcePath": str(hooks_path),
                    "currentHash": f"sha256:{index}",
                    "enabled": True,
                    "trustStatus": "trusted",
                    "timeoutSec": hook["timeout"],
                }
            )
        result = {"data": [{"cwd": "/workspace", "hooks": metadata}]}

        self.assertEqual(
            core.verify_quota_sentry_hooks_active(result, hooks_path, script_path),
            5,
        )

        metadata[1]["trustStatus"] = "modified"
        with self.assertRaisesRegex(RuntimeError, "not trusted"):
            core.verify_quota_sentry_hooks_active(result, hooks_path, script_path)

        metadata[1]["trustStatus"] = "trusted"
        metadata[2]["matcher"] = "NeverMatches"
        with self.assertRaisesRegex(RuntimeError, "unexpected matcher"):
            core.verify_quota_sentry_hooks_active(result, hooks_path, script_path)

    def test_global_hook_install_persists_codex_trust(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            hooks_path = home / ".codex" / "hooks.json"
            script_path = Path("/opt/quota-sentry")
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                core, "trust_codex_hooks", return_value=5
            ) as trust:
                args = cli.build_parser().parse_args(
                    ["install-hook", "--script-path", str(script_path)]
                )
                result = cli.install_hook_command(args)

        self.assertEqual(result, 0)
        trust.assert_called_once_with(
            hooks_path.resolve(),
            script_path.resolve(),
            cwd=Path.cwd(),
        )

    def test_global_hook_install_fails_when_codex_trust_cannot_be_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            hooks_path = home / ".codex" / "hooks.json"
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                core,
                "trust_codex_hooks",
                side_effect=RuntimeError("simulated trust failure"),
            ), mock.patch("sys.stderr", new_callable=StringIO) as stderr:
                args = cli.build_parser().parse_args(
                    ["install-hook", "--script-path", "/opt/quota-sentry"]
                )
                result = cli.install_hook_command(args)

            hooks_exists = hooks_path.exists()

        self.assertEqual(result, 1)
        self.assertIn("hooks are not active", stderr.getvalue())
        self.assertFalse(hooks_exists)

    def test_failed_global_hook_update_restores_existing_hooks(self):
        existing = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [{"type": "command", "command": "echo existing"}],
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            hooks_path = home / ".codex" / "hooks.json"
            core.write_json_atomic(hooks_path, existing)
            backup_path = hooks_path.with_suffix(".json.bak")
            original_backup = b"preexisting backup\n"
            backup_path.write_bytes(original_backup)
            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                core,
                "trust_codex_hooks",
                side_effect=RuntimeError("simulated trust failure"),
            ):
                args = cli.build_parser().parse_args(
                    ["install-hook", "--script-path", "/opt/quota-sentry"]
                )
                result = cli.install_hook_command(args)
            restored = json.loads(hooks_path.read_text())
            restored_backup = backup_path.read_bytes()

        self.assertEqual(result, 1)
        self.assertEqual(restored, existing)
        self.assertEqual(restored_backup, original_backup)

    def test_failed_global_hook_update_restores_codex_trust_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            config_path = codex_home / "config.toml"
            original_config = b'model = "gpt-5"\n'
            config_path.write_bytes(original_config)

            def fail_after_trust_write(*_args, **_kwargs):
                config_path.write_text('model = "modified"\n')
                raise RuntimeError("simulated post-write verification failure")

            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                core,
                "trust_codex_hooks",
                side_effect=fail_after_trust_write,
            ):
                args = cli.build_parser().parse_args(
                    ["install-hook", "--script-path", "/opt/quota-sentry"]
                )
                result = cli.install_hook_command(args)

            restored_config = config_path.read_bytes()

        self.assertEqual(result, 1)
        self.assertEqual(restored_config, original_config)

    def test_idempotent_global_install_preserves_existing_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            hooks_path = home / ".codex" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            script_path = Path("/opt/quota-sentry")
            core.write_json_atomic(
                hooks_path,
                core.merge_codex_hooks({}, script_path=script_path),
            )
            backup_path = hooks_path.with_suffix(".json.bak")
            backup_path.write_text("original backup\n")

            with mock.patch.object(Path, "home", return_value=home), mock.patch.object(
                core, "trust_codex_hooks", return_value=5
            ):
                args = cli.build_parser().parse_args(
                    ["install-hook", "--script-path", str(script_path)]
                )
                result = cli.install_hook_command(args)

            self.assertEqual(result, 0)
            self.assertEqual(backup_path.read_text(), "original backup\n")


class PollIntervalTest(unittest.TestCase):
    def test_backend_exhaustion_uses_critical_poll_interval(self):
        account = core.QuotaWindow(
            name="account",
            used_percent=100,
            window_minutes=None,
            resets_at=NOW + timedelta(seconds=30),
            kind=core.WINDOW_KIND_ACCOUNT,
        )
        decision = core.QuotaDecision(
            status="blocked",
            reason="backend exhausted",
            used_percent=100,
            quota_windows=(account,),
        )

        interval = core.next_poll_interval_seconds(
            decision,
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )

        self.assertEqual(interval, 30)

    def test_poll_interval_tightens_near_threshold(self):
        base = core.next_poll_interval_seconds(
            core.QuotaDecision(status="open", reason="open", used_percent=84),
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )
        near = core.next_poll_interval_seconds(
            core.QuotaDecision(status="open", reason="open", used_percent=90),
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )
        critical = core.next_poll_interval_seconds(
            core.QuotaDecision(status="open", reason="open", used_percent=94),
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )

        self.assertEqual(base, 300)
        self.assertEqual(near, 60)
        self.assertEqual(critical, 30)

    def test_weekly_advisory_usage_does_not_tighten_poll_interval(self):
        decision = core.QuotaDecision(
            status="open",
            reason="open",
            used_percent=10,
            weekly_window=core.QuotaWindow(
                name="weekly",
                used_percent=99,
                window_minutes=10080,
                resets_at=datetime(2026, 6, 7, 21, 45, 36, tzinfo=timezone.utc),
            ),
        )

        interval = core.next_poll_interval_seconds(
            decision,
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )

        self.assertEqual(interval, 300)

    def test_weekly_hard_block_usage_tightens_poll_interval(self):
        decision = core.QuotaDecision(
            status="open",
            reason="open",
            used_percent=10,
            weekly_window=core.QuotaWindow(
                name="weekly",
                used_percent=99,
                window_minutes=10080,
                resets_at=datetime(2026, 6, 7, 21, 45, 36, tzinfo=timezone.utc),
            ),
            weekly_hard_block_enabled=True,
        )

        interval = core.next_poll_interval_seconds(
            decision,
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )

        self.assertEqual(interval, 30)

    def test_weekly_only_advisory_state_does_not_tighten_poll_interval(self):
        weekly = core.QuotaWindow(
            name="weekly",
            used_percent=99,
            window_minutes=10080,
            resets_at=datetime(2026, 6, 7, 21, 45, 36, tzinfo=timezone.utc),
            kind=core.WINDOW_KIND_WEEKLY,
        )
        decision = core.QuotaDecision(
            status="open",
            reason="weekly advisory",
            used_percent=99,
            weekly_window=weekly,
            quota_windows=(weekly,),
        )

        interval = core.next_poll_interval_seconds(
            decision,
            base_interval_seconds=300,
            near_threshold_percent=85,
            near_interval_seconds=60,
            critical_threshold_percent=93,
            critical_interval_seconds=30,
        )

        self.assertEqual(interval, 300)


class CodexbarFetchTest(unittest.TestCase):
    def test_app_server_rate_limits_response_maps_to_codex_usage_payload(self):
        payload = core.codex_app_server_rate_limits_to_usage(
            app_server_rate_limits_response(),
            now=NOW,
        )

        self.assertEqual(payload[0]["provider"], "codex")
        self.assertEqual(payload[0]["source"], "codex-app-server")
        self.assertEqual(payload[0]["usage"]["primary"]["usedPercent"], 51)
        self.assertEqual(payload[0]["usage"]["primary"]["windowMinutes"], 300)
        self.assertEqual(payload[0]["usage"]["primary"]["resetsAt"], "2026-06-01T21:23:05Z")
        self.assertEqual(payload[0]["usage"]["secondary"]["usedPercent"], 97)
        self.assertEqual(payload[0]["usage"]["secondary"]["windowMinutes"], 10080)
        self.assertEqual(payload[0]["usage"]["windows"][0]["sourceSlot"], "primary")
        self.assertTrue(payload[0]["usage"]["windows"][0]["isDefaultLimit"])

    def test_app_server_multi_bucket_payload_preserves_auxiliary_limits_without_enforcing_them(self):
        response = app_server_rate_limits_response(
            primary_used_percent=50,
            secondary_used_percent=50,
        )
        response["rateLimits"]["limitId"] = "codex"
        response["rateLimitsByLimitId"] = {
            "codex": response["rateLimits"],
            "codex_other": {
                "limitId": "codex_other",
                "limitName": "Codex Other",
                "primary": {
                    "usedPercent": 100,
                    "windowDurationMins": 15,
                    "resetsAt": int(
                        datetime(2026, 6, 1, 17, 0, 0, tzinfo=timezone.utc).timestamp()
                    ),
                },
            },
        }

        payload = core.codex_app_server_rate_limits_to_usage(response, now=NOW)
        decision = core.parse_codex_usage(payload, threshold_percent=95, now=NOW)
        windows = payload[0]["usage"]["windows"]

        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[-1]["limitId"], "codex_other")
        self.assertFalse(windows[-1]["isDefaultLimit"])
        self.assertEqual(decision.status, "open")
        self.assertEqual(len(decision.quota_windows), 3)
        self.assertEqual(decision.quota_windows[-1].limit_id, "codex_other")

    def test_app_server_can_select_canonical_codex_bucket_without_legacy_view(self):
        response = {
            "rateLimits": None,
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 21,
                        "windowDurationMins": 10080,
                        "resetsAt": int(
                            datetime(2026, 6, 7, 21, 45, 36, tzinfo=timezone.utc).timestamp()
                        ),
                    },
                }
            },
        }

        payload = core.codex_app_server_rate_limits_to_usage(response, now=NOW)

        self.assertEqual(payload[0]["usage"]["activeLimitId"], "codex")
        self.assertEqual(payload[0]["usage"]["primary"]["usedPercent"], 21)
        self.assertTrue(payload[0]["usage"]["windows"][0]["isDefaultLimit"])

    def test_app_server_infers_default_limit_id_from_matching_multi_bucket_view(self):
        response = app_server_rate_limits_response()
        response["rateLimitsByLimitId"] = {"codex": response["rateLimits"]}

        payload = core.codex_app_server_rate_limits_to_usage(response, now=NOW)

        self.assertEqual(payload[0]["usage"]["activeLimitId"], "codex")
        self.assertEqual(
            {window["limitId"] for window in payload[0]["usage"]["windows"]},
            {"codex"},
        )

    def test_app_server_promotes_a_sole_replacement_limit_bucket(self):
        response = {
            "rateLimits": None,
            "rateLimitsByLimitId": {
                "codex_other": {
                    "limitId": "codex_other",
                    "primary": {
                        "usedPercent": 100,
                        "resetsAt": int(
                            datetime(2026, 6, 8, 17, 0, 0, tzinfo=timezone.utc).timestamp()
                        ),
                        "windowDurationMins": 10080,
                    },
                }
            },
        }

        payload = core.codex_app_server_rate_limits_to_usage(response, now=NOW)
        decision = core.parse_codex_usage(payload, threshold_percent=95, now=NOW)

        self.assertEqual(payload[0]["usage"]["activeLimitId"], "codex_other")
        self.assertEqual(payload[0]["usage"]["primary"]["usedPercent"], 100)
        self.assertTrue(payload[0]["usage"]["windows"][0]["isDefaultLimit"])
        self.assertEqual(decision.status, "blocked")

    def test_fetch_codex_usage_auto_prefers_app_server(self):
        app_payload = codexbar_payload(used_percent=12)

        with mock.patch.object(core, "fetch_codex_app_server_usage", return_value=app_payload) as app_server, \
            mock.patch.object(core, "fetch_codexbar_usage") as codexbar:
            payload = core.fetch_codex_usage(source="auto")

        self.assertEqual(payload, app_payload)
        app_server.assert_called_once()
        codexbar.assert_not_called()

    def test_fetch_codex_usage_auto_does_not_downgrade_to_codexbar(self):
        with mock.patch.object(
            core,
            "fetch_codex_app_server_usage",
            side_effect=RuntimeError("app-server failed"),
        ), mock.patch.object(core, "fetch_codexbar_usage") as codexbar:
            with self.assertRaisesRegex(RuntimeError, "app-server failed"):
                core.fetch_codex_usage(source="auto")

        codexbar.assert_not_called()

    def test_fetch_codex_usage_can_force_codexbar(self):
        codexbar_payload_value = codexbar_payload(used_percent=44)

        with mock.patch.object(core, "fetch_codex_app_server_usage") as app_server, \
            mock.patch.object(core, "fetch_codexbar_usage", return_value=codexbar_payload_value) as codexbar:
            payload = core.fetch_codex_usage(source="codexbar")

        self.assertEqual(payload, codexbar_payload_value)
        app_server.assert_not_called()
        codexbar.assert_called_once()

    def test_fetch_codexbar_usage_detaches_stdin(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(codexbar_payload()),
                stderr="",
            )

        with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
            payload = core.fetch_codexbar_usage()

        self.assertEqual(payload[0]["provider"], "codex")
        self.assertIs(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertTrue(calls[0][1]["close_fds"])


class ConfigTest(unittest.TestCase):
    def test_default_config_hard_blocks_weekly_at_99_percent(self):
        config = core.read_config(Path("/path/that/does/not/exist/config.json"))

        self.assertEqual(config.weekly_mode, core.WEEKLY_MODE_HARD_BLOCK)
        self.assertEqual(config.weekly_threshold_percent, 99)

    def test_invalid_weekly_mode_falls_back_to_hard_block(self):
        config = core.config_from_payload(
            {"weeklyMode": "invalid", "weeklyThresholdPercent": 100}
        )

        self.assertEqual(config.weekly_mode, core.WEEKLY_MODE_HARD_BLOCK)
        self.assertEqual(config.weekly_threshold_percent, 99)

    def test_config_round_trips_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config = core.QuotaConfig(
                weekly_mode=core.WEEKLY_MODE_HARD_BLOCK,
                weekly_threshold_percent=98,
            )

            core.write_config(config_path, config)
            loaded = core.read_config(config_path)

        self.assertEqual(loaded, config)

    def test_configure_command_persists_weekly_advisory_opt_out(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            args = cli.build_parser().parse_args(
                [
                    "configure",
                    "--config-path",
                    str(config_path),
                    "--weekly-mode",
                    "advisory",
                    "--weekly-threshold-percent",
                    "99",
                ]
            )
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = cli.configure_command(args)

            loaded = core.read_config(config_path)

        self.assertEqual(result, 0)
        self.assertEqual(loaded.weekly_mode, core.WEEKLY_MODE_ADVISORY)
        self.assertEqual(loaded.weekly_threshold_percent, 99)
        self.assertIn("weekly advisory at 99%", stdout.getvalue())


class DaemonStartTest(unittest.TestCase):
    def test_hook_mode_internal_exception_becomes_pre_tool_denial(self):
        stderr = StringIO()
        with mock.patch.object(
            cli, "guard_command", side_effect=RuntimeError("simulated failure")
        ), redirect_stderr(stderr):
            result = cli.main(["guard", "--require-fresh-state"])

        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue().strip(), core.HOOK_INTERNAL_FAILURE_MESSAGE)

    def test_hook_mode_internal_exception_becomes_post_tool_block_json(self):
        stdout = StringIO()
        with mock.patch.object(
            cli, "guard_command", side_effect=RuntimeError("simulated failure")
        ), redirect_stdout(stdout):
            result = cli.main(
                [
                    "guard",
                    "--require-fresh-state",
                    "--fail-closed-format",
                    "block-json",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"decision": "block", "reason": core.HOOK_INTERNAL_FAILURE_MESSAGE},
        )

    def test_wait_for_fresh_quota_state_rejects_fresh_unknown_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            core.write_json_atomic(
                state_path,
                {
                    "status": "unknown",
                    "updatedAt": core.format_timestamp(NOW),
                },
            )

            result = cli.wait_for_fresh_quota_state(
                state_path,
                max_state_age_seconds=420,
                timeout_seconds=0,
                now_func=lambda: NOW,
                monotonic_func=lambda: 0.0,
            )

        self.assertFalse(result)

    def test_start_defaults_to_five_minute_daemon_interval(self):
        args = cli.build_parser().parse_args(["start"])

        self.assertEqual(args.interval_seconds, 300)
        self.assertEqual(args.source, "auto")

    def test_guard_consumes_hook_input_before_waiting(self):
        args = cli.build_parser().parse_args(["guard", "--state-only", "--read-hook-input"])
        hook_input = BytesIO(b"x" * (1024 * 1024))
        fake_stdin = mock.Mock(buffer=hook_input)

        with mock.patch.object(cli.sys, "stdin", fake_stdin), mock.patch.object(
            core, "wait_if_blocked", return_value=0
        ):
            result = cli.guard_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(hook_input.tell(), 1024 * 1024)
        self.assertFalse(args.read_hook_input)

    def test_stop_json_watchdog_returns_valid_compact_stop_output(self):
        args = cli.build_parser().parse_args(
            ["guard", "--state-only", "--fail-closed-format", "stop-json"]
        )

        def fake_wait(*_args, **kwargs):
            kwargs["fail_closed_output"](core.HOOK_FAIL_CLOSED_MESSAGE)
            return kwargs["fail_closed_exit_code"]

        stdout = StringIO()
        with mock.patch.object(core, "wait_if_blocked", side_effect=fake_wait), redirect_stdout(
            stdout
        ):
            result = cli.guard_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"continue": False, "stopReason": core.HOOK_FAIL_CLOSED_MESSAGE},
        )

    def test_block_json_watchdog_returns_valid_post_tool_block_output(self):
        args = cli.build_parser().parse_args(
            ["guard", "--state-only", "--fail-closed-format", "block-json"]
        )

        def fake_wait(*_args, **kwargs):
            kwargs["fail_closed_output"](core.HOOK_FAIL_CLOSED_MESSAGE)
            return kwargs["fail_closed_exit_code"]

        stdout = StringIO()
        with mock.patch.object(core, "wait_if_blocked", side_effect=fake_wait), redirect_stdout(
            stdout
        ):
            result = cli.guard_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"decision": "block", "reason": core.HOOK_FAIL_CLOSED_MESSAGE},
        )

    def test_start_daemon_detaches_stdin(self):
        popen_kwargs = {}
        popen_command = {}

        class FakeProcess:
            pid = 12345

        def fake_popen(command, **kwargs):
            popen_command["value"] = command
            popen_kwargs.update(kwargs)
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temp_dir:
            args = cli.build_parser().parse_args(
                [
                    "start",
                    "--quiet",
                    "--state-dir",
                    temp_dir,
                    "--source",
                    "codex-app-server",
                    "--config-path",
                    str(Path(temp_dir) / "config.json"),
                ]
            )
            with mock.patch.object(cli.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
                cli, "wait_for_registered_daemon", return_value=12345
            ):
                result = cli.start_command(args)

        self.assertEqual(result, 0)
        self.assertIsInstance(popen_command["value"], list)
        self.assertIn("--source", popen_command["value"])
        self.assertIn("codex-app-server", popen_command["value"])
        self.assertIn("--config-path", popen_command["value"])
        self.assertIn("--runtime-fingerprint", popen_command["value"])
        self.assertIn("--config-path-fingerprint", popen_command["value"])
        self.assertIs(popen_kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen_kwargs["start_new_session"])
        self.assertTrue(popen_kwargs["close_fds"])

    def test_start_tolerates_incompatible_daemon_exiting_before_signal(self):
        class FakeProcess:
            pid = 12345

        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            cli.write_pid(core.default_pid_path(state_dir), 222)
            args = cli.build_parser().parse_args(
                ["start", "--quiet", "--state-dir", temp_dir]
            )
            with mock.patch.object(
                cli,
                "is_quota_sentry_daemon",
                side_effect=[False, True],
            ), mock.patch.object(
                cli.os, "kill", side_effect=ProcessLookupError
            ), mock.patch.object(
                cli.subprocess, "Popen", return_value=FakeProcess()
            ), mock.patch.object(
                cli, "wait_for_registered_daemon", return_value=12345
            ):
                result = cli.start_command(args)

        self.assertEqual(result, 0)

    def test_quiet_start_suppresses_existing_daemon_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = core.default_pid_path(Path(temp_dir))
            cli.write_pid(pid_path, os.getpid())
            args = cli.build_parser().parse_args(["start", "--quiet", "--state-dir", temp_dir])
            stdout = StringIO()

            with mock.patch.object(cli, "is_quota_sentry_daemon", return_value=True), redirect_stdout(
                stdout
            ):
                result = cli.start_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_daemon_lock_rejects_a_second_owner(self):
        if core.fcntl is None:
            self.skipTest("process file locking is unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = core.default_daemon_lock_path(Path(temp_dir))
            first = cli.try_acquire_daemon_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                second = cli.try_acquire_daemon_lock(lock_path)
                self.assertIsNone(second)
            finally:
                cli.release_daemon_lock(first)

            third = cli.try_acquire_daemon_lock(lock_path)
            self.assertIsNotNone(third)
            cli.release_daemon_lock(third)

    def test_stop_refuses_to_signal_an_unrelated_reused_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = core.default_pid_path(Path(temp_dir))
            cli.write_pid(pid_path, os.getpid())
            args = cli.build_parser().parse_args(["stop", "--state-dir", temp_dir])
            stdout = StringIO()

            with mock.patch.object(cli, "is_quota_sentry_daemon", return_value=False), mock.patch.object(
                cli.os, "kill"
            ) as kill, redirect_stdout(stdout):
                result = cli.stop_command(args)

        self.assertEqual(result, 0)
        kill.assert_not_called()
        self.assertIn("stale pid removed", stdout.getvalue())

    def test_pid_cleanup_does_not_remove_a_replacement_daemon_pid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = core.default_pid_path(Path(temp_dir))
            cli.write_pid(pid_path, 222)

            cli.remove_pid_if_owned(pid_path, 111)

            self.assertEqual(cli.read_pid(pid_path), 222)

    def test_daemon_identity_requires_matching_state_directory(self):
        command = (
            f"python {cli.script_path()} daemon --state-dir /tmp/other-state "
            "--source codex-app-server"
        )
        completed = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout=command,
            stderr="",
        )

        with mock.patch.object(cli, "is_pid_alive", return_value=True), mock.patch.object(
            cli.subprocess, "run", return_value=completed
        ):
            result = cli.is_quota_sentry_daemon(123, Path("/tmp/expected-state"))

        self.assertFalse(result)

    def test_daemon_configuration_rejects_stale_code_and_unsafe_source(self):
        args = cli.build_parser().parse_args(["start"])
        config_fingerprint = cli.config_path_fingerprint(args.config_path)
        base_command = (
            f"python {cli.script_path()} daemon --state-dir /tmp/state "
            "--threshold-percent 95 --reset-buffer-seconds 60 --source auto "
            "--interval-seconds 300 --near-threshold-percent 85 "
            "--near-interval-seconds 60 --critical-threshold-percent 93 "
            "--critical-interval-seconds 30 --runtime-fingerprint current "
            f"--config-path-fingerprint {config_fingerprint}"
        )

        self.assertTrue(
            cli.daemon_command_is_compatible(base_command, args, "current")
        )
        self.assertFalse(
            cli.daemon_command_is_compatible(
                base_command.replace("--source auto", "--source codexbar"),
                args,
                "current",
            )
        )
        self.assertFalse(
            cli.daemon_command_is_compatible(base_command, args, "new-code")
        )
        self.assertTrue(
            cli.daemon_command_is_compatible(
                base_command.replace("--interval-seconds 300", "--interval-seconds 60"),
                args,
                "current",
            )
        )

        custom_args = cli.build_parser().parse_args(
            ["start", "--config-path", "/tmp/advisory-policy.json"]
        )
        self.assertFalse(
            cli.daemon_command_is_compatible(base_command, custom_args, "current")
        )

    def test_prompt_guard_starts_daemon_quietly_then_uses_state_only_guard(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.object(cli, "start_command", return_value=0) as start, \
            mock.patch.object(cli, "wait_for_fresh_quota_state", return_value=True) as fresh, \
            mock.patch.object(cli, "guard_command", return_value=0) as guard:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 0)
        self.assertTrue(args.quiet)
        self.assertTrue(args.state_only)
        self.assertTrue(args.no_notify)
        start.assert_called_once_with(args)
        fresh.assert_called_once()
        guard.assert_called_once_with(args)

    def test_prompt_guard_emergency_bypass_skips_daemon_and_state_checks(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.dict(os.environ, {"QUOTA_SENTRY_DISABLE": "1"}), mock.patch.object(
            cli, "start_command"
        ) as start, mock.patch.object(
            cli, "wait_for_fresh_quota_state"
        ) as fresh, mock.patch.object(
            cli, "guard_command"
        ) as guard:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 0)
        start.assert_not_called()
        fresh.assert_not_called()
        guard.assert_not_called()

    def test_prompt_guard_fails_closed_when_daemon_start_fails(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.object(cli, "start_command", return_value=7), \
            mock.patch.object(cli, "guard_command") as guard, \
            mock.patch("sys.stderr", new_callable=StringIO) as stderr:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 2)
        self.assertIn("refusing to submit the prompt", stderr.getvalue())
        guard.assert_not_called()

    def test_prompt_guard_fails_closed_when_daemon_does_not_publish_fresh_state(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.object(cli, "start_command", return_value=0), mock.patch.object(
            cli, "wait_for_fresh_quota_state", return_value=False
        ), mock.patch.object(cli, "guard_command") as guard, mock.patch(
            "sys.stderr", new_callable=StringIO
        ) as stderr:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 2)
        self.assertIn("fresh quota state", stderr.getvalue())
        guard.assert_not_called()


class CliStatusTest(unittest.TestCase):
    def test_common_options_are_accepted_after_subcommand(self):
        args = cli.build_parser().parse_args(["poll", "--state-dir", ".quota-sentry-test"])

        self.assertEqual(args.command, "poll")
        self.assertEqual(args.state_dir, ".quota-sentry-test")

    def test_status_text_for_missing_state(self):
        self.assertEqual(cli.status_text({}), "Quota Sentry: no state found")

    def test_poll_command_reports_persisted_latch_instead_of_raw_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = core.default_state_path(Path(temp_dir))
            core.write_json_atomic(
                state_path,
                {
                    "status": "blocked",
                    "usedPercent": 99,
                    "blockedUntil": "2026-06-07T21:45:36Z",
                },
            )
            args = cli.build_parser().parse_args(
                ["poll", "--state-dir", temp_dir]
            )
            stdout = StringIO()
            with mock.patch.object(
                core,
                "poll_once",
                return_value=core.QuotaDecision(status="open", reason="raw source open"),
            ), redirect_stdout(stdout):
                result = cli.poll_command(args)

        self.assertEqual(result, 0)
        self.assertIn("Quota Sentry: blocked", stdout.getvalue())

    def test_status_text_for_blocked_state(self):
        text = cli.status_text(
            {
                "status": "blocked",
                "usedPercent": 97,
                "blockedUntil": "2026-06-01T21:24:05Z",
                "updatedAt": "2026-06-01T16:29:30Z",
            }
        )

        self.assertIn("blocked", text)
        self.assertIn("97%", text)
        self.assertIn("2026-06-01T21:24:05Z", text)

    def test_status_text_for_open_state_is_lean(self):
        text = cli.status_text(
            {
                "status": "open",
                "usedPercent": 14,
                "resetsAt": "2026-06-20T08:41:42Z",
                "updatedAt": "2026-06-20T02:58:10Z",
                "reason": "14% of the 300-minute Codex quota is used",
            }
        )

        self.assertEqual(text, "Quota Sentry: 14% used")

    def test_status_text_for_open_state_includes_weekly_when_available(self):
        text = cli.status_text(
            {
                "status": "open",
                "usedPercent": 14,
                "primary": {"usedPercent": 14, "windowMinutes": 300},
                "weekly": {"usedPercent": 96, "windowMinutes": 10080},
            }
        )

        self.assertEqual(text, "Quota Sentry: 5h 14% used | weekly 96% used")

    def test_status_text_for_weekly_only_state_is_explicit(self):
        text = cli.status_text(
            {
                "status": "open",
                "usedPercent": 5,
                "windows": [
                    {
                        "kind": "weekly",
                        "usedPercent": 5,
                        "windowMinutes": 10080,
                        "isDefaultLimit": True,
                    }
                ],
            }
        )

        self.assertEqual(text, "Quota Sentry: weekly 5% used")

    def test_status_text_preserves_fractional_usage_without_padding(self):
        text = cli.status_text(
            {
                "status": "open",
                "usedPercent": 14.25,
                "windows": [
                    {
                        "kind": "short-term",
                        "usedPercent": 14.25,
                        "windowMinutes": 300,
                        "isDefaultLimit": True,
                    }
                ],
            }
        )

        self.assertEqual(text, "Quota Sentry: 14.25% used")

    def test_status_command_hides_daemon_pid_unless_verbose(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = core.default_state_path(Path(temp_dir))
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"status": "open", "usedPercent": 14}))
            cli.write_pid(core.default_pid_path(Path(temp_dir)), os.getpid())

            quiet_args = cli.build_parser().parse_args(["status", "--state-dir", temp_dir])
            quiet_stdout = StringIO()
            with mock.patch.object(cli, "is_quota_sentry_daemon", return_value=True), redirect_stdout(
                quiet_stdout
            ):
                self.assertEqual(cli.status_command(quiet_args), 0)

            verbose_args = cli.build_parser().parse_args(["status", "--verbose", "--state-dir", temp_dir])
            verbose_stdout = StringIO()
            with mock.patch.object(cli, "is_quota_sentry_daemon", return_value=True), redirect_stdout(
                verbose_stdout
            ):
                self.assertEqual(cli.status_command(verbose_args), 0)

        self.assertEqual(quiet_stdout.getvalue(), "Quota Sentry: 14% used\n")
        self.assertIn("Quota Sentry: daemon pid", verbose_stdout.getvalue())

    def test_status_warns_when_state_is_stale_and_daemon_missing(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:00:00Z",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        warnings = cli.status_health_warnings(
            state,
            daemon_running=False,
            now=datetime(2026, 6, 1, 16, 10, 0, tzinfo=timezone.utc),
            max_state_age_seconds=120,
        )

        self.assertIn("Quota Sentry: warning: state is stale and daemon is not running", warnings)


class AutonomousHarnessTest(unittest.TestCase):
    def test_autonomous_harness_lists_scenarios(self):
        result = subprocess.run(
            ["./scripts/autonomous-test", "--list"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("AT-001 live Codex quota source smoke", result.stdout)
        self.assertIn("AT-007 global hook config", result.stdout)
        self.assertIn("AT-011 auto source never downgrades authority", result.stdout)
        self.assertIn("AT-012 weekly advisory opt-out", result.stdout)
        self.assertIn("AT-013 weekly-only default hard-block", result.stdout)
        self.assertIn("AT-014 auxiliary bucket remains advisory", result.stdout)
        self.assertIn("AT-015 weekly prompt guard waits for authoritative reset", result.stdout)
        self.assertIn("AT-016 hook safety deadline fails closed", result.stdout)
        self.assertIn("AT-017 confirmed block survives source failure", result.stdout)
        self.assertIn("AT-018 valid block wins over malformed sibling", result.stdout)
        self.assertIn("AT-019 backend exhaustion without windows blocks", result.stdout)
        self.assertIn("AT-020 fresh unknown state fails closed", result.stdout)
        self.assertIn("AT-021 omitted backend state cannot clear latch", result.stdout)
        self.assertIn("AT-022 ambiguous replacement buckets fail closed", result.stdout)


if __name__ == "__main__":
    unittest.main()
