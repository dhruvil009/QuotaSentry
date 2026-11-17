import importlib.util
import importlib.machinery
import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from quota_sentry import cli, core


NOW = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


def usage(used=98, weekly=40, *, observed=NOW, limit_id="codex"):
    return [{
        "provider": "codex",
        "source": "codex-app-server",
        "usage": {
            "updatedAt": core.format_timestamp(observed),
            "activeLimitId": limit_id,
            "rateLimitReachedType": None,
            "spendControlReached": False,
            "windows": [
                {"usedPercent": used, "windowMinutes": 300,
                 "resetsAt": "2026-11-15T23:00:00Z", "limitId": limit_id,
                 "isDefaultLimit": True},
                {"usedPercent": weekly, "windowMinutes": 10080,
                 "resetsAt": "2026-11-20T20:00:00Z", "limitId": limit_id,
                 "isDefaultLimit": True},
            ],
        },
    }]


def publish(path, *, used=98, weekly=40, observed=NOW, limit_id="codex", quota_config=None):
    core.poll_once(path, fetcher=lambda: usage(
        used, weekly, observed=observed, limit_id=limit_id), now=observed,
        quota_config=quota_config or core.QuotaConfig())


class RetainedWaitTest(unittest.TestCase):
    def _loss_then_release(self, corrupt):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish(path)
            ticks = []

            def sleeper(seconds):
                ticks.append(seconds)
                if len(ticks) == 1:
                    corrupt(path)
                elif len(ticks) == 2:
                    publish(path, used=20, observed=NOW + timedelta(seconds=2))
                else:
                    self.fail("valid matching authoritative release did not wake waiter")

            try:
                result = core.wait_if_blocked(
                    path, poller=lambda: self.fail("cache hook live-polled"),
                    sleeper=sleeper, state_only=True, notify=False,
                    now_func=lambda: NOW + timedelta(seconds=len(ticks)))
            except (AttributeError, UnicodeError, TypeError) as error:
                self.fail("recoverable cache corruption escaped guard: " + str(error))
            self.assertEqual(result, 0)
            self.assertEqual(len(ticks), 2,
                             "waiter released on unavailable evidence before source recovery")

    def test_retained_block_survives_deleted_cache(self):
        self._loss_then_release(lambda path: path.unlink())

    def test_retained_block_survives_malformed_json(self):
        self._loss_then_release(lambda path: path.write_text("{"))

    def test_retained_block_survives_invalid_utf8(self):
        self._loss_then_release(lambda path: path.write_bytes(b"\xff"))

    def test_retained_block_survives_excessive_json_nesting(self):
        self._loss_then_release(lambda path: path.write_text("[" * 1200 + "0" + "]" * 1200))

    def test_malformed_normalized_window_cannot_terminate_waiter(self):
        for field, value in (("usedPercent", 10 ** 400),
                             ("resetsAt", "0001-01-01T00:00:00+01:00"),
                             ("resetsAt", "9999-12-31T23:59:59Z")):
            def corrupt(path, field=field, value=value):
                path.unlink()
                publish(path, used=20, observed=NOW + timedelta(seconds=1))
                state = core.read_state(path)
                window = state["observation"]["windows"][0]
                window[field] = value
                if value == "9999-12-31T23:59:59Z":
                    window["usedPercent"] = 99
                    state["sourceStatus"] = state["observation"]["status"] = "blocked"
                core.write_json_atomic(path, state)
            with self.subTest(field=field, value=value):
                self._loss_then_release(corrupt)

    def test_retained_block_survives_non_object_json(self):
        for value in ([], None, 42, "open"):
            with self.subTest(value=value):
                self._loss_then_release(lambda path: path.write_text(json.dumps(value)))

    def test_bare_open_flag_is_not_release_evidence(self):
        self._loss_then_release(lambda path: path.write_text('{"status":"open"}'))

    def test_unrelated_canonical_window_cannot_release_retained_block(self):
        def unrelated(path):
            path.unlink()
            publish(path, used=20, limit_id="another-account-bucket",
                    observed=NOW + timedelta(seconds=1))
        self._loss_then_release(unrelated)

    def test_stale_open_snapshot_cannot_release_retained_block(self):
        def stale(path):
            path.unlink()
            publish(path, used=20, observed=NOW - timedelta(hours=1))
        self._loss_then_release(stale)

    def test_another_local_source_context_cannot_release_retained_block(self):
        def different_context(path):
            path.unlink()
            publish(path, used=20, observed=NOW + timedelta(seconds=1))
            state = core.read_state(path)
            state["sourceContext"] = state["observation"]["sourceContext"] = "other-context"
            core.write_json_atomic(path, state)
        self._loss_then_release(different_context)

    def test_equal_source_observation_is_not_a_new_release(self):
        def same_observation(path):
            context = core.read_state(path).get("sourceContext")
            path.unlink()
            decision = core.parse_codex_usage(usage(20), now=NOW)
            core.write_state(path, replace(decision, source_context=context),
                             now=NOW + timedelta(seconds=1))
        self._loss_then_release(same_observation)

    def test_wrong_typed_observation_cannot_release_retained_block(self):
        for key, value in (("source", []), ("status", []),
                           ("version", True),
                           ("weeklyHardBlockEnabled", "false"),
                           ("thresholdPercent", True), ("spendControlReached", 1)):
            def invalid(path, key=key, value=value):
                path.unlink()
                publish(path, used=20, observed=NOW + timedelta(seconds=1))
                state = core.read_state(path)
                state["observation"][key] = value
                core.write_json_atomic(path, state)
            with self.subTest(key=key):
                self._loss_then_release(invalid)

    def test_all_retained_windows_must_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish(path, weekly=99)
            ticks = []

            def sleeper(seconds):
                ticks.append(seconds)
                path.unlink()
                publish(path, used=20, weekly=99 if len(ticks) == 1 else 20,
                        observed=NOW + timedelta(seconds=len(ticks)))
                if len(ticks) > 2:
                    self.fail("all matching windows released but guard stayed held")

            result = core.wait_if_blocked(
                path, poller=lambda: self.fail("cache hook live-polled"),
                sleeper=sleeper, state_only=True, notify=False,
                now_func=lambda: NOW + timedelta(seconds=len(ticks)))
            self.assertEqual(result, 0)
            self.assertEqual(len(ticks), 2)

    def test_stale_advisory_policy_cannot_hide_new_weekly_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish(path)
            ticks = []

            def sleeper(seconds):
                ticks.append(seconds)
                path.unlink()
                publish(path, used=20, weekly=99 if len(ticks) == 1 else 20,
                        observed=NOW + timedelta(seconds=len(ticks)),
                        quota_config=core.QuotaConfig(weekly_mode="advisory"))
                if len(ticks) > 2:
                    self.fail("fresh open data did not release")

            result = core.wait_if_blocked(
                path, poller=lambda: self.fail("cache hook live-polled"),
                sleeper=sleeper, state_only=True, notify=False,
                now_func=lambda: NOW + timedelta(seconds=len(ticks)),
                policy_config=lambda: core.QuotaConfig())
            self.assertEqual(result, 0)
            self.assertEqual(len(ticks), 2,
                             "cached advisory policy hid the currently enforced weekly quota")

    def test_default_hook_does_not_relax_a_stricter_daemon_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            ticks = []

            def produce(used, observed):
                core.poll_once(path, fetcher=lambda: usage(used, observed=observed),
                               threshold_percent=80, now=observed,
                               quota_config=core.QuotaConfig())

            produce(85, NOW)

            def sleeper(seconds):
                ticks.append(seconds)
                produce(85 if len(ticks) == 1 else 20,
                        NOW + timedelta(seconds=len(ticks)))
                if len(ticks) > 2:
                    self.fail("matching below-threshold evidence did not release")

            core.wait_if_blocked(
                path, poller=lambda: self.fail("unexpected source poll"),
                state_only=True, notify=False, sleeper=sleeper,
                now_func=lambda: NOW + timedelta(seconds=len(ticks)),
                threshold_percent=90, policy_config=lambda: core.QuotaConfig())
            self.assertEqual(len(ticks), 2,
                             "default hook weakened the daemon's confirmed 80% policy")


class CacheShapeTest(unittest.TestCase):
    def test_boolean_weekly_threshold_does_not_create_a_one_percent_limit(self):
        config = core.config_from_payload({"weeklyThresholdPercent": True})
        decision = core.parse_codex_usage(usage(20, weekly=40), now=NOW, quota_config=config)
        self.assertEqual(decision.status, "open")

    def test_normalized_window_null_does_not_invent_backend_clear_evidence(self):
        payload = usage(20)
        del payload[0]["usage"]["rateLimitReachedType"]
        del payload[0]["usage"]["spendControlReached"]
        decision = core.parse_codex_usage(payload, now=NOW)
        self.assertFalse(decision.account_limit_state_known)
        cached = core.cached_observation(core.state_from_decision(decision, now=NOW), now=NOW)
        self.assertIsNotNone(cached)
        self.assertFalse(cached.account_limit_state_known,
                         "normalization invented a backend-clear assertion")
        self.assertFalse(cached.rate_limit_state_known)

    def test_invalid_cache_shapes_are_unavailable_not_exceptions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for payload in (b"[]", b"null", b"42", b'"open"', b"\xff"):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    try:
                        result = core.read_state(path)
                    except UnicodeError as error:
                        self.fail("cache decoding failure escaped: " + str(error))
                    self.assertEqual(result, {})


class HookModeTest(unittest.TestCase):
    def _args(self, directory):
        args = cli.build_parser().parse_args([
            "guard", "--state-only", "--no-notify", "--state-dir", directory,
            "--config-path", str(Path(directory) / "config.json")])
        args.hook_mode = True
        args.recover_daemon = False
        return args

    def test_refresh_failure_before_unlatched_wait_is_quiet_success(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self._args(directory)
            output = StringIO()
            with mock.patch.object(core, "request_refresh", side_effect=OSError("disk full")), \
                    redirect_stdout(output), redirect_stderr(output):
                try:
                    result = cli.guard_command(args)
                except OSError as error:
                    self.fail("unlatched health error escaped hook: " + str(error))
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), "")

    def test_hook_mode_does_not_turn_refresh_failure_into_lost_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish(path)
            args = self._args(directory)
            args.fail_closed_after_seconds = 0
            ticks = []
            real_wait = core.wait_if_blocked

            def sleeper(seconds):
                ticks.append(seconds)
                publish(path, used=20, observed=NOW + timedelta(seconds=1))
                if len(ticks) > 1:
                    self.fail("authoritative release did not complete the original wait")

            def invoke(*values, **kwargs):
                kwargs.update(sleeper=sleeper,
                              now_func=lambda: NOW + timedelta(seconds=len(ticks)))
                return real_wait(*values, **kwargs)

            with mock.patch.object(core, "request_refresh", side_effect=OSError("disk full")), \
                    mock.patch.object(core, "wait_if_blocked", side_effect=invoke):
                try:
                    result = cli.guard_command(args)
                except OSError as error:
                    self.fail("refresh error bypassed the retained wait: " + str(error))
            self.assertEqual(result, 0)
            self.assertEqual(len(ticks), 1)

    def test_invalid_config_cannot_turn_unlatched_health_into_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self._args(directory)
            Path(args.config_path).write_bytes(b"\xff")
            self.assertEqual(cli.guard_command(args), 0)

    def test_dynamic_explicit_cwd_exemption_releases_only_its_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish(path)
            args = self._args(directory)
            ticks = []
            real_wait = core.wait_if_blocked

            def sleeper(seconds):
                ticks.append(seconds)
                core.write_json_atomic(Path(args.config_path),
                                       {"exemptCwds": [str(Path.cwd())]})
                if len(ticks) > 1:
                    self.fail("explicit exemption did not release this invocation")

            def invoke(*values, **kwargs):
                kwargs.update(sleeper=sleeper, now_func=lambda: NOW)
                return real_wait(*values, **kwargs)

            with mock.patch.object(core, "wait_if_blocked", side_effect=invoke):
                self.assertEqual(cli.guard_command(args), 0)
            self.assertEqual(len(ticks), 1)
            self.assertEqual(core.read_state(path)["status"], "blocked")

    def test_parser_can_select_explicit_hook_mode_without_live_source(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            try:
                args = cli.build_parser().parse_args([
                    "guard", "--hook-mode", "--recover-daemon", "--state-only"])
            except SystemExit:
                self.fail("candidate hook mode is not a supported invocation")
        self.assertTrue(args.hook_mode)
        self.assertTrue(args.state_only)


class HookProcessTest(unittest.TestCase):
    def test_actual_hook_process_retains_cache_failures_and_resumes_quietly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            refresh = root / "refresh.request"
            config = root / "config.json"
            config.write_text('{"weeklyMode":"hard-block"}')
            script = Path(__file__).resolve().parents[1] / "scripts/quota-sentry"
            env = {"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
                   "XDG_CACHE_HOME": str(root / "cache"),
                   "XDG_CONFIG_HOME": str(root / "config"), "PATH": "/usr/bin:/bin"}

            def produce(used):
                now = core.utc_now()
                payload = usage(used, observed=now)
                windows = payload[0]["usage"]["windows"]
                windows[0]["resetsAt"] = core.format_timestamp(now + timedelta(hours=3))
                windows[1]["resetsAt"] = core.format_timestamp(now + timedelta(days=7))
                with mock.patch.dict(os.environ, env):
                    core.poll_once(state, fetcher=lambda: payload,
                                   now=now, config_path=config, quota_config=core.QuotaConfig())

            def await_refresh_after(previous=0):
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), "actual hook exited before validated release")
                    if refresh.exists() and refresh.stat().st_mtime_ns > previous:
                        return refresh.stat().st_mtime_ns
                    time.sleep(0.025)
                self.fail("actual hook stopped rereading its cache")

            produce(98)
            process = subprocess.Popen([
                sys.executable, str(script), "guard", "--hook-mode", "--state-only",
                "--no-notify", "--read-hook-input", "--interval-seconds", "1",
                "--state-dir", str(root), "--config-path", str(config)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=root)
            try:
                process.stdin.write(b'{}\n')
                process.stdin.close()
                process.stdin = None
                seen = await_refresh_after()
                for corrupted in (b"{", b"\xff", b"[]", b'{"status":"open"}'):
                    state.write_bytes(corrupted)
                    seen = await_refresh_after(seen)
                state.unlink()
                await_refresh_after(seen)
                produce(20)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual((stdout, stderr), (b"", b""))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


class DetachedRecoveryTest(unittest.TestCase):
    def test_bootstrap_inherits_recovery_lock_until_its_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = cli.build_parser().parse_args([
                "guard", "--state-dir", directory, "--recover-daemon"])
            child = None
            popen = subprocess.Popen

            def spawn(_command, **kwargs):
                nonlocal child
                child = popen([sys.executable, "-c",
                    "import pathlib,sys,time; root=pathlib.Path(sys.argv[1]); "
                    "(root/'ready').touch(); "
                    "exec(\"while not (root/'release').exists(): time.sleep(0.01)\")",
                    directory], **kwargs)
                return child

            try:
                with mock.patch.object(cli.subprocess, "Popen", side_effect=spawn):
                    cli.request_daemon_recovery(args)
                deadline = time.monotonic() + 3
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / "ready").exists())
                handle = cli.try_acquire_daemon_lock(root / "recovery.lock")
                if handle is not None:
                    cli.release_daemon_lock(handle)
                    self.fail("parent released the inherited bootstrap lock too early")
                (root / "release").touch()
                self.assertEqual(child.wait(timeout=3), 0)
                handle = cli.try_acquire_daemon_lock(root / "recovery.lock")
                self.assertIsNotNone(handle)
                cli.release_daemon_lock(handle)
            finally:
                (root / "release").touch()
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)

    def test_missing_daemon_bootstrap_is_detached_and_coalesced(self):
        with tempfile.TemporaryDirectory() as directory:
            args = cli.build_parser().parse_args([
                "guard", "--state-dir", directory, "--recover-daemon"])
            children = []

            def spawn(command, **kwargs):
                children.append((command, kwargs))
                return mock.Mock()

            if not hasattr(cli, "request_daemon_recovery"):
                self.fail("detached quota recovery is not implemented")
            with mock.patch.object(cli.subprocess, "Popen", side_effect=spawn):
                cli.request_daemon_recovery(args)
                cli.request_daemon_recovery(args)
            self.assertEqual(len(children), 1, "repeated guards spawned duplicate bootstraps")
            command, options = children[0]
            self.assertIn("start", command)
            self.assertIs(options["stdin"], cli.subprocess.DEVNULL)
            self.assertIs(options["stdout"], cli.subprocess.DEVNULL)
            self.assertIs(options["stderr"], cli.subprocess.DEVNULL)
            self.assertTrue(options["start_new_session"])
            self.assertTrue(options["close_fds"])

    def test_reading_invalid_configuration_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for payload in (b"\xff", b"[]", b"null", b"{}", b'{"weeklyMode":[]}'):
                path.write_bytes(payload)
                self.assertEqual(core.read_config(path).weekly_mode, "hard-block")


class ModelHookConfigurationTest(unittest.TestCase):
    def test_custom_script_filename_merges_idempotently_without_removing_audit(self):
        script = Path("/opt/quota-sentry-custom")
        audit = {"type": "command", "command": "/opt/audit --label quota-sentry"}
        original = {"hooks": {"PreModelRequest": [{"hooks": [audit]}]}}
        first = core.merge_codex_hooks(original, script)
        second = core.merge_codex_hooks(first, script)
        self.assertEqual(second, first, "reinstall duplicated a custom script hook")
        self.assertIn(audit, [handler for group in second["hooks"]["PreModelRequest"]
                              for handler in group["hooks"]])

    def test_existing_autonomous_installer_scenario_accepts_candidate_safely(self):
        path = Path(__file__).resolve().parents[1] / "scripts/autonomous-test"
        loader = importlib.machinery.SourceFileLoader("quota_sentry_autonomous_test", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"HOME": str(root / "home"), "CODEX_HOME": str(root / "codex"),
                           "XDG_CACHE_HOME": str(root / "cache"),
                           "XDG_CONFIG_HOME": str(root / "config")}
            with mock.patch.dict(os.environ, environment):
                harness = module.Harness(root, skip_live=True, require_global_hook=False)
                result = module.ScenarioResult("AT-006", "isolated temporary hook installer")
                try:
                    harness.scenario_hook_installer_temp_merge(result)
                except module.AutonomousTestError as error:
                    self.fail("autonomous check rejects the approved hook policy: " + str(error))

    def test_model_rounds_get_synchronous_cache_only_checkpoints(self):
        merged = core.merge_codex_hooks({}, Path("/opt/quota-sentry"))
        for name in ("PreModelRequest", "PostModelRequest"):
            with self.subTest(event=name):
                self.assertIn(name, merged["hooks"], "model sampling has no quota checkpoint")
                if name not in merged["hooks"]:
                    continue
                hook = merged["hooks"][name][0]["hooks"][0]
                self.assertIs(hook["async"], False)
                self.assertIn("--state-only", hook["command"])
                self.assertIn("--hook-mode", hook["command"])
                self.assertIn("--recover-daemon", hook["command"])
                self.assertNotIn("--fail-closed", hook["command"])
                self.assertEqual(hook["timeout"], 31622400)

    def test_candidate_installer_does_not_generate_normal_denial_watchdogs(self):
        merged = core.merge_codex_hooks({}, Path("/opt/quota-sentry"))
        for name, entries in merged["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    if name != "SessionStart":
                        self.assertIn("--hook-mode", hook["command"])
                        self.assertNotIn("--fail-closed", hook["command"])

    def test_notification_preference_is_a_real_quota_sentry_setting(self):
        config = core.config_from_payload({"notifyOnBlock": False})
        self.assertIs(getattr(config, "notify_on_block", None), False)
        self.assertIs(core.config_to_payload(config).get("notifyOnBlock"), False)

    def test_custom_status_can_be_omitted_without_disabling_hooks(self):
        try:
            merged = core.merge_codex_hooks({}, Path("/opt/quota-sentry"), notify_on_block=False)
        except TypeError:
            self.fail("notification preference cannot be applied at installation")
        self.assertEqual(len(merged["hooks"]), 7)
        for entries in merged["hooks"].values():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertFalse(hook.get("statusMessage"))
                    self.assertIs(hook["async"], False)


if __name__ == "__main__":
    unittest.main()
