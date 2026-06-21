import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
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
):
    return {
        "planType": "plus",
        "rateLimits": {
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

    def test_opens_after_reset_time_has_passed(self):
        decision = core.parse_codexbar_usage(
            codexbar_payload(used_percent=99, resets_at="2026-06-01T16:00:00Z"),
            threshold_percent=95,
            reset_buffer_seconds=60,
            now=NOW,
        )

        self.assertEqual(decision.status, "open")
        self.assertIn("reset time has passed", decision.reason)

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

    def test_should_block_from_state_requires_fresh_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:29:30Z",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertEqual(block_until, datetime(2026, 6, 1, 21, 24, 5, tzinfo=timezone.utc))

    def test_should_not_block_from_stale_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:00:00Z",
            "blockedUntil": "2026-06-01T21:24:05Z",
        }

        block_until = core.block_until_from_state(state, now=NOW, max_state_age_seconds=120)

        self.assertIsNone(block_until)

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
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        now_values = [
            datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 16, 31, 1, tzinfo=timezone.utc),
        ]
        messages = []

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

            result = core.wait_if_blocked(
                state_path,
                poller=lambda: self.fail("poller should not be called for fresh blocked state"),
                sleeper=lambda _seconds: None,
                now_func=lambda: now_values.pop(0),
                output=messages.append,
            )

        self.assertEqual(result, 0)
        self.assertEqual(messages, [])

    def test_wait_if_blocked_polls_after_expired_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        poll_count = {"value": 0}

        def poller():
            poll_count["value"] += 1
            return core.QuotaDecision(status="open", reason="fresh quota poll", fail_open=False)

        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(state))

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

    def test_state_only_guard_waits_on_fresh_blocked_state(self):
        state = {
            "status": "blocked",
            "updatedAt": "2026-06-01T16:30:00Z",
            "blockedUntil": "2026-06-01T16:31:01Z",
        }
        current = {"value": datetime(2026, 6, 1, 16, 30, 0, tzinfo=timezone.utc)}

        def sleeper(seconds):
            current["value"] = current["value"] + timedelta(seconds=seconds)

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


class HookInstallTest(unittest.TestCase):
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
        session_start_hook = merged["hooks"]["SessionStart"][-1]["hooks"][0]
        user_prompt_command = merged["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]
        pre_tool_command = merged["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
        self.assertFalse(session_start_hook["async"])
        self.assertEqual(user_prompt_command, "/opt/quota-sentry prompt-guard")
        self.assertEqual(pre_tool_command, "/opt/quota-sentry guard --state-only --no-notify")

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

        self.assertEqual(command, "'/Users/example/Open Development/quotaSentry/scripts/quota-sentry' prompt-guard")
        self.assertEqual(command.count("quota-sentry"), 1)


class PollIntervalTest(unittest.TestCase):
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

    def test_fetch_codex_usage_auto_prefers_app_server(self):
        app_payload = codexbar_payload(used_percent=12)

        with mock.patch.object(core, "fetch_codex_app_server_usage", return_value=app_payload) as app_server, \
            mock.patch.object(core, "fetch_codexbar_usage") as codexbar:
            payload = core.fetch_codex_usage(source="auto")

        self.assertEqual(payload, app_payload)
        app_server.assert_called_once()
        codexbar.assert_not_called()

    def test_fetch_codex_usage_auto_falls_back_to_codexbar(self):
        codexbar_payload_value = codexbar_payload(used_percent=43)

        with mock.patch.object(core, "fetch_codex_app_server_usage", side_effect=RuntimeError("app-server failed")), \
            mock.patch.object(core, "fetch_codexbar_usage", return_value=codexbar_payload_value) as codexbar:
            payload = core.fetch_codex_usage(source="auto")

        self.assertEqual(payload, codexbar_payload_value)
        codexbar.assert_called_once()

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


class DaemonStartTest(unittest.TestCase):
    def test_start_defaults_to_five_minute_daemon_interval(self):
        args = cli.build_parser().parse_args(["start"])

        self.assertEqual(args.interval_seconds, 300)
        self.assertEqual(args.source, "auto")

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
                ["start", "--quiet", "--state-dir", temp_dir, "--source", "codex-app-server"]
            )
            with mock.patch.object(cli.subprocess, "Popen", side_effect=fake_popen):
                result = cli.start_command(args)

        self.assertEqual(result, 0)
        self.assertIsInstance(popen_command["value"], list)
        self.assertIn("--source", popen_command["value"])
        self.assertIn("codex-app-server", popen_command["value"])
        self.assertIs(popen_kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen_kwargs["start_new_session"])
        self.assertTrue(popen_kwargs["close_fds"])

    def test_quiet_start_suppresses_existing_daemon_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = core.default_pid_path(Path(temp_dir))
            cli.write_pid(pid_path, os.getpid())
            args = cli.build_parser().parse_args(["start", "--quiet", "--state-dir", temp_dir])
            stdout = StringIO()

            with redirect_stdout(stdout):
                result = cli.start_command(args)

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_prompt_guard_starts_daemon_quietly_then_uses_state_only_guard(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.object(cli, "start_command", return_value=0) as start, \
            mock.patch.object(cli, "guard_command", return_value=0) as guard:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 0)
        self.assertTrue(args.quiet)
        self.assertTrue(args.state_only)
        self.assertTrue(args.no_notify)
        start.assert_called_once_with(args)
        guard.assert_called_once_with(args)

    def test_prompt_guard_does_not_guard_when_daemon_start_fails(self):
        args = cli.build_parser().parse_args(["prompt-guard"])

        with mock.patch.object(cli, "start_command", return_value=7), \
            mock.patch.object(cli, "guard_command") as guard:
            result = cli.prompt_guard_command(args)

        self.assertEqual(result, 7)
        guard.assert_not_called()


class CliStatusTest(unittest.TestCase):
    def test_common_options_are_accepted_after_subcommand(self):
        args = cli.build_parser().parse_args(["poll", "--state-dir", ".quota-sentry-test"])

        self.assertEqual(args.command, "poll")
        self.assertEqual(args.state_dir, ".quota-sentry-test")

    def test_status_text_for_missing_state(self):
        self.assertEqual(cli.status_text({}), "Quota Sentry: no state found")

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

    def test_status_command_hides_daemon_pid_unless_verbose(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = core.default_state_path(Path(temp_dir))
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"status": "open", "usedPercent": 14}))
            cli.write_pid(core.default_pid_path(Path(temp_dir)), os.getpid())

            quiet_args = cli.build_parser().parse_args(["status", "--state-dir", temp_dir])
            quiet_stdout = StringIO()
            with redirect_stdout(quiet_stdout):
                self.assertEqual(cli.status_command(quiet_args), 0)

            verbose_args = cli.build_parser().parse_args(["status", "--verbose", "--state-dir", temp_dir])
            verbose_stdout = StringIO()
            with redirect_stdout(verbose_stdout):
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
        self.assertIn("AT-011 auto source falls back to CodexBar", result.stdout)


if __name__ == "__main__":
    unittest.main()
