"""Focused review counterexamples; all quota input and filesystem state are local."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from quota_sentry import cli, core


NOW = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
OMITTED = object()


class ReviewRegressionTest(unittest.TestCase):
    def test_daemon_with_signal_permission_denied_is_not_treated_as_dead(self):
        with mock.patch.object(cli.os, "kill", side_effect=PermissionError(1, "not permitted")):
            self.assertTrue(cli.is_pid_alive(12345))
        with mock.patch.object(cli.os, "kill", side_effect=ProcessLookupError(3, "not found")):
            self.assertFalse(cli.is_pid_alive(12345))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "codex"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "QUOTA_SENTRY_DISABLE": "0",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.state = self.root / "state.json"
        self.config = self.root / "policy.json"
        self.clock = NOW
        core.write_config(self.config, core.QuotaConfig())

    def publish(self, windows=(), *, observed=0, written=None,
                reached=OMITTED, spend=OMITTED):
        """Use the stock adapter and poller, replacing only the source fetch."""
        snapshot = {"limitId": "codex"}
        for slot, (minutes, used, reset_seconds) in zip(
                ("primary", "secondary", "tertiary"), windows):
            snapshot[slot] = {
                "usedPercent": used,
                "windowDurationMins": minutes,
                "resetsAt": int((NOW + timedelta(seconds=reset_seconds)).timestamp()),
            }
        if reached is not OMITTED:
            snapshot["rateLimitReachedType"] = reached
        if spend is not OMITTED:
            snapshot["spendControlReached"] = spend
        observed_at = NOW + timedelta(seconds=observed)
        payload = core.codex_app_server_rate_limits_to_usage(
            {"rateLimits": snapshot}, now=observed_at)
        decision = core.poll_once(
            self.state, fetcher=lambda: payload, config_path=self.config,
            now=NOW + timedelta(seconds=observed if written is None else written))
        return decision, core.read_state(self.state)

    def wait_through(self, steps, **kwargs):
        """Every scripted transition must be observed before the same wait exits."""
        completed = []

        def sleeper(_seconds):
            if len(completed) >= len(steps):
                self.fail("waiter did not release after the final authoritative clearance")
            offset, transition = steps[len(completed)]
            self.clock = NOW + timedelta(seconds=offset)
            transition()
            completed.append(offset)

        result = core.wait_if_blocked(
            self.state,
            poller=lambda: self.fail("cache-only guard attempted a source poll"),
            state_only=True, notify=False, sleeper=sleeper,
            now_func=lambda: self.clock, **kwargs)
        self.assertEqual(result, 0)
        self.assertEqual(len(completed), len(steps),
                         "waiter released before all retained conditions were cleared")

    def test_malformed_policy_does_not_clear_producer_weekly_latch(self):
        core.write_config(self.config, core.QuotaConfig(weekly_threshold_percent=95))
        self.assertEqual(self.publish(((10080, 95, 604800),))[1]["status"], "blocked")
        self.config.write_text("{")
        _, state = self.publish(((10080, 95, 604800),), observed=1)
        self.assertEqual(state["status"], "blocked",
                         "invalid policy silently raised the effective threshold to 99%")

    def test_malformed_policy_does_not_release_retained_weekly_wait(self):
        core.write_config(self.config, core.QuotaConfig(weekly_threshold_percent=95))
        self.publish(((10080, 95, 604800),))
        args = cli.build_parser().parse_args([
            "guard", "--config-path", str(self.config), "--state-dir", str(self.root)])

        def corrupt_policy():
            self.config.write_text("{")
            self.publish(((10080, 95, 604800),), observed=1)

        def clear():
            core.write_config(self.config, core.QuotaConfig(weekly_threshold_percent=95))
            self.publish(((10080, 20, 604800),), observed=2)

        self.wait_through([(1, corrupt_policy), (2, clear)],
                          policy_config=lambda: cli.hook_policy_config(args),
                          threshold_percent=90)

    def test_equal_source_timestamp_cannot_clear_durable_latch(self):
        self.publish(((300, 98, 10800),))
        _, state = self.publish(((300, 20, 10800),), observed=0, written=1)
        self.assertEqual(state["status"], "blocked",
                         "a new file timestamp is not newer source evidence")

    def test_shortened_reset_preserves_newest_block_source_watermark(self):
        self.publish(((300, 98, 10800),))
        self.publish(((300, 98, 3600),), observed=2)
        _, state = self.publish(((300, 20, 10800),), observed=1, written=3)
        self.assertEqual(state["status"], "blocked",
                         "T+1 clearance superseded the reconfirmed T+2 block")

    def test_shortened_reset_cannot_release_wait_on_intermediate_old_observation(self):
        self.publish(((300, 98, 10800),))
        self.wait_through([
            (2, lambda: self.publish(((300, 98, 3600),), observed=2)),
            (3, lambda: self.publish(((300, 20, 10800),), observed=1, written=3)),
            (4, lambda: self.publish(((300, 20, 10800),), observed=4)),
        ])

    def test_account_clear_does_not_forget_omitted_blocked_weekly_window(self):
        self.publish(((300, 98, 10800), (10080, 99, 604800)),
                     reached="rate_limit_reached")
        self.wait_through([
            (1, lambda: self.publish(((300, 20, 10800),), observed=1, reached=None)),
            (2, lambda: self.publish(((300, 20, 10800), (10080, 20, 604800)),
                                    observed=2, reached=None)),
        ])

    def test_rate_clear_does_not_forget_simultaneous_spend_block(self):
        self.publish(((300, 20, 10800),), reached="rate_limit_reached", spend=True)
        self.wait_through([
            (1, lambda: self.publish(((300, 20, 10800),), observed=1, reached=None)),
            (2, lambda: self.publish(((300, 20, 10800),), observed=2,
                                    reached=None, spend=False)),
        ])

    def test_same_kind_window_omission_does_not_clear_producer_latch(self):
        self.publish(((300, 98, 10800), (60, 95, 3600)))
        _, state = self.publish(((60, 20, 3600),), observed=1)
        self.assertEqual(state["status"], "blocked",
                         "the omitted 300-minute block was cleared by a 60-minute window")

    def test_same_kind_windows_each_require_matching_release_in_waiter(self):
        self.publish(((300, 98, 10800), (60, 95, 3600)))
        self.wait_through([
            (1, lambda: self.publish(((60, 20, 3600),), observed=1)),
            (2, lambda: self.publish(((300, 20, 10800), (60, 20, 3600)), observed=2)),
        ])

    def test_legacy_top_level_block_survives_empty_or_invalid_enforcement_list(self):
        for windows in ([], [{"kind": None}]):
            with self.subTest(windows=windows):
                self.state.unlink(missing_ok=True)
                _, state = self.publish(((300, 98, 10800),))
                # Fault injection: retain the old top-level block, not a schema-5 observation.
                state.pop("observation", None)
                state["schemaVersion"] = 4
                state["enforcement"] = {"status": "blocked", "windows": windows}
                core.write_json_atomic(self.state, state)
                self.clock = NOW
                self.wait_through([
                    (1, lambda: self.publish(((300, 20, 10800),), observed=1)),
                ])

    def test_legacy_top_level_block_without_enforcement_can_recover(self):
        _, state = self.publish(((300, 98, 10800),))
        state.pop("observation", None)
        state.pop("enforcement", None)
        state["schemaVersion"] = 4
        core.write_json_atomic(self.state, state)
        self.wait_through([
            (1, lambda: self.publish(((300, 20, 10800),), observed=1)),
        ])

    def test_backend_block_without_percentage_windows_round_trips(self):
        _, state = self.publish(reached="workspace_member_usage_limit_reached")
        self.assertEqual(state["status"], "blocked")
        recovered = core.cached_observation(state, now=NOW)
        self.assertIsNotNone(recovered,
                             "the producer's synthetic account window is not malformed")
        self.assertEqual(recovered.status, "blocked")
        self.assertEqual(recovered.rate_limit_reached_type,
                         "workspace_member_usage_limit_reached")

    def test_explicit_backend_clear_without_percentage_windows_round_trips(self):
        self.publish(reached="workspace_member_usage_limit_reached")
        _, state = self.publish(observed=1, reached=None)
        self.assertEqual(state["enforcement"]["status"], "open")
        self.assertEqual(state["sourceStatus"], "unknown")
        recovered = core.cached_observation(state, now=NOW + timedelta(seconds=1))
        self.assertIsNotNone(recovered,
                             "unknown percentage health must preserve explicit backend evidence")
        self.assertTrue(recovered.rate_limit_state_known)
        self.assertIsNone(recovered.rate_limit_reached_type)

    def test_backend_only_wait_releases_on_matching_explicit_clear(self):
        self.publish(reached="workspace_member_usage_limit_reached")
        self.wait_through([
            (1, lambda: self.publish(observed=1, reached=None)),
        ])

    def test_mixed_handler_merge_preserves_unrelated_model_audit(self):
        for event in ("PreModelRequest", "PostModelRequest"):
            with self.subTest(event=event):
                audit = {"type": "command", "command": "/opt/audit-model", "async": False}
                hooks = {"hooks": {event: [{"hooks": [
                    {"type": "command", "command": "/old/quota-sentry guard"}, audit,
                ]}]}}
                path = self.root / "hooks.json"
                core.write_json_atomic(path, hooks)
                merged = core.merge_codex_hooks(
                    json.loads(path.read_text()), self.root / "scripts/quota-sentry")
                core.write_json_atomic(path, merged)
                handlers = [handler for entry in core.read_state(path)["hooks"][event]
                            for handler in entry["hooks"]]
                self.assertIn(audit, handlers,
                              "replacing a Quota Sentry handler deleted the unrelated audit")

    def test_fresh_malformed_observation_requests_real_refresh_marker(self):
        for malformed in ([], None, {"version": "wrong-type"}):
            with self.subTest(observation=malformed):
                self.state.unlink(missing_ok=True)
                marker = core.default_refresh_request_path(self.root)
                marker.unlink(missing_ok=True)
                _, state = self.publish(((300, 20, 10800),))
                state["observation"] = malformed
                core.write_json_atomic(self.state, state)
                args = cli.build_parser().parse_args([
                    "guard", "--hook-mode", "--state-only", "--no-notify",
                    "--state-dir", str(self.root), "--config-path", str(self.config),
                ])
                result = core.wait_if_blocked(
                    self.state, poller=lambda: self.fail("unexpected live quota poll"),
                    state_only=True, notify=False, now_func=lambda: NOW,
                    sleeper=lambda _: self.fail("unlatched health must not block"),
                    recovery=lambda: cli.request_daemon_recovery(args))
                self.assertEqual(result, 0)
                self.assertTrue(marker.exists(),
                                "unavailable normalized observation did not request recovery")


if __name__ == "__main__":
    unittest.main()
