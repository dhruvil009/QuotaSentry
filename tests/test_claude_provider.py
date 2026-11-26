import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest import mock

from quota_sentry import cli, core


NOW = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)
FIVE_RESET = "2026-11-26T06:59:59.689985+00:00"
WEEK_RESET = "2026-11-27T04:59:59.690004+00:00"
TOKEN = "fake-access-token-TEST-ONLY-SECRET"


def claude_body(five=41, week=24, *, five_reset=FIVE_RESET, week_reset=WEEK_RESET, extra=None):
    body = {
        "five_hour": None if five is None else {"utilization": five, "resets_at": five_reset},
        "seven_day": None if week is None else {"utilization": week, "resets_at": week_reset},
        "seven_day_oauth_apps": None,
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "extra_usage": {"is_enabled": False, "monthly_limit": None,
                        "used_credits": None, "utilization": None},
    }
    if extra:
        body.update(extra)
    return body


def claude_payload(five=41, week=24, *, observed=NOW, **kwargs):
    return core.claude_usage_to_usage(claude_body(five, week, **kwargs), now=observed)


def publish_claude(path, five=41, week=24, *, observed=NOW, quota_config=None, **kwargs):
    return core.poll_once(
        path,
        fetcher=lambda: claude_payload(five, week, observed=observed, **kwargs),
        now=observed,
        quota_config=quota_config or core.QuotaConfig(),
        provider=core.CLAUDE_PROVIDER,
    )


def fail_claude(path, *, observed):
    def broken():
        raise RuntimeError("usage endpoint unavailable")

    return core.poll_once(path, fetcher=broken, now=observed,
                          quota_config=core.QuotaConfig(), provider=core.CLAUDE_PROVIDER)


def credentials(expires_at=NOW + timedelta(hours=4), scopes=("user:inference", "user:profile")):
    return {"claudeAiOauth": {
        "accessToken": TOKEN,
        "refreshToken": "refresh-TEST-ONLY",
        "expiresAt": int(expires_at.timestamp() * 1000),
        "scopes": list(scopes),
        "subscriptionType": "pro",
    }}


def window(payload, limit_id):
    for candidate in payload[0]["usage"]["windows"]:
        if candidate["limitId"] == limit_id:
            return candidate
    raise AssertionError(f"missing {limit_id} window")


class ClaudeUsageNormalizationTest(unittest.TestCase):
    def test_named_windows_map_to_five_hour_and_weekly_durations(self):
        payload = claude_payload(41, 24)

        self.assertEqual(payload[0]["provider"], core.CLAUDE_PROVIDER)
        self.assertEqual(payload[0]["source"], core.CLAUDE_USAGE_API_SOURCE)
        five = window(payload, "five_hour")
        week = window(payload, "seven_day")
        self.assertEqual((five["windowMinutes"], five["usedPercent"], five["isDefaultLimit"]),
                         (300, 41, True))
        self.assertEqual((week["windowMinutes"], week["usedPercent"], week["isDefaultLimit"]),
                         (10080, 24, True))
        self.assertEqual(core.parse_timestamp(five["resetsAt"]),
                         datetime(2026, 11, 26, 6, 59, 59, 689985, tzinfo=timezone.utc))

    def test_model_scoped_weekly_windows_are_advisory(self):
        payload = claude_payload(41, 24, extra={
            "seven_day_opus": {"utilization": 100, "resets_at": WEEK_RESET}})

        opus = window(payload, "seven_day_opus")
        self.assertFalse(opus["isDefaultLimit"])
        decision = core.parse_codex_usage(payload, now=NOW, provider=core.CLAUDE_PROVIDER)
        self.assertEqual(decision.status, "open")

    def test_window_without_reset_time_is_a_current_reading(self):
        payload = claude_payload(0, 24, five_reset=None)

        self.assertEqual(window(payload, "five_hour")["resetsAt"],
                         payload[0]["usage"]["updatedAt"])

    def test_non_object_response_is_rejected(self):
        with self.assertRaises(ValueError):
            core.claude_usage_to_usage(["not", "an", "object"], now=NOW)


class ClaudePolicyTest(unittest.TestCase):
    def test_five_hour_at_ninety_percent_blocks_until_reported_reset(self):
        decision = core.parse_codex_usage(claude_payload(90, 24), now=NOW,
                                          provider=core.CLAUDE_PROVIDER)

        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.blocked_window, core.WINDOW_KIND_SHORT_TERM)
        self.assertEqual(decision.blocked_limit_id, "five_hour")
        self.assertEqual(decision.resets_at,
                         datetime(2026, 11, 26, 6, 59, 59, 689985, tzinfo=timezone.utc))
        self.assertIn("Claude quota", decision.reason)

    def test_five_hour_below_ninety_percent_is_open(self):
        decision = core.parse_codex_usage(claude_payload(89, 24), now=NOW,
                                          provider=core.CLAUDE_PROVIDER)

        self.assertEqual(decision.status, "open")

    def test_weekly_blocks_at_default_ninety_nine_percent(self):
        blocked = core.parse_codex_usage(claude_payload(10, 99), now=NOW,
                                         provider=core.CLAUDE_PROVIDER)
        open_decision = core.parse_codex_usage(claude_payload(10, 98), now=NOW,
                                               provider=core.CLAUDE_PROVIDER)

        self.assertEqual((blocked.status, blocked.blocked_window),
                         ("blocked", core.WINDOW_KIND_WEEKLY))
        self.assertEqual(open_decision.status, "open")

    def test_weekly_advisory_opt_out_does_not_block(self):
        decision = core.parse_codex_usage(
            claude_payload(10, 100), now=NOW, provider=core.CLAUDE_PROVIDER,
            quota_config=core.QuotaConfig(weekly_mode=core.WEEKLY_MODE_ADVISORY))

        self.assertEqual(decision.status, "open")

    def test_missing_utilization_is_unknown(self):
        payload = core.claude_usage_to_usage(
            claude_body(41, 24, extra={"five_hour": {"utilization": None,
                                                     "resets_at": FIVE_RESET}}),
            now=NOW)

        decision = core.parse_codex_usage(payload, now=NOW, provider=core.CLAUDE_PROVIDER)
        self.assertEqual(decision.status, "unknown")


class ClaudeStateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.json"
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def test_poll_records_claude_provider_source_and_context(self):
        publish_claude(self.path, 41, 24)
        state = core.read_state(self.path)

        self.assertEqual(state["provider"], core.CLAUDE_PROVIDER)
        self.assertEqual(state["status"], "open")
        self.assertEqual(state["source"], core.CLAUDE_USAGE_API_SOURCE)
        self.assertRegex(state["sourceContext"], r"^[0-9a-f]{64}$")

        codex_path = Path(self.directory.name) / "codex-state.json"
        core.poll_once(codex_path, fetcher=lambda: [{
            "provider": "codex", "source": core.CODEX_APP_SERVER_SOURCE,
            "usage": {"updatedAt": core.format_timestamp(NOW), "activeLimitId": "codex",
                      "windows": [{"usedPercent": 10, "windowMinutes": 300,
                                   "resetsAt": "2026-11-26T05:00:00Z", "limitId": "codex",
                                   "isDefaultLimit": True}]}}],
            now=NOW, quota_config=core.QuotaConfig())
        codex_state = core.read_state(codex_path)
        self.assertNotIn("provider", codex_state)
        self.assertNotEqual(codex_state["sourceContext"], state["sourceContext"])

    def test_confirmed_block_survives_failed_and_older_reads(self):
        publish_claude(self.path, 95, 24)
        self.assertEqual(core.read_state(self.path)["status"], "blocked")

        fail_claude(self.path, observed=NOW + timedelta(minutes=1))
        self.assertEqual(core.read_state(self.path)["status"], "blocked")

        publish_claude(self.path, 20, 24, observed=NOW - timedelta(minutes=5))
        self.assertEqual(core.read_state(self.path)["status"], "blocked")

        publish_claude(self.path, 20, 24, observed=NOW + timedelta(minutes=2))
        self.assertEqual(core.read_state(self.path)["status"], "open")

    def test_reported_reset_time_passing_does_not_release(self):
        publish_claude(self.path, 95, 24)

        fail_claude(self.path, observed=datetime(2026, 11, 26, 7, 30, tzinfo=timezone.utc))

        self.assertEqual(core.read_state(self.path)["status"], "blocked")

    def test_reading_without_reset_time_after_reset_releases(self):
        publish_claude(self.path, 95, 24)

        publish_claude(self.path, 0, 24, five_reset=None,
                       observed=datetime(2026, 11, 26, 7, 5, tzinfo=timezone.utc))

        self.assertEqual(core.read_state(self.path)["status"], "open")

    def test_cached_observation_rebuilds_claude_decision(self):
        publish_claude(self.path, 41, 24)

        observation = core.cached_observation(core.read_state(self.path), now=NOW)

        self.assertIsNotNone(observation)
        self.assertEqual(observation.status, "open")
        self.assertEqual({window.limit_id for window in observation.quota_windows},
                         {"five_hour", "seven_day"})


class ClaudeHookWaitTest(unittest.TestCase):
    def test_hook_waiter_holds_until_fresh_claude_reading_below_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            publish_claude(path, 95, 24)
            ticks = []

            def sleeper(seconds):
                ticks.append(seconds)
                if len(ticks) == 1:
                    fail_claude(path, observed=NOW + timedelta(seconds=1))
                elif len(ticks) == 2:
                    publish_claude(path, 20, 24, observed=NOW + timedelta(seconds=2))
                else:
                    self.fail("fresh Claude reading below threshold did not release the waiter")

            result = core.wait_if_blocked(
                path, poller=lambda: self.fail("hook live-polled the quota source"),
                sleeper=sleeper, state_only=True, notify=False,
                now_func=lambda: NOW + timedelta(seconds=len(ticks)))

            self.assertEqual(result, 0)
            self.assertEqual(len(ticks), 2)


RENEWED = "fake-renewed-token-TEST-ONLY"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT = "https://platform.claude.com/oauth/code/callback"


class _AnthropicHandler(BaseHTTPRequestHandler):
    def _record(self, body=None):
        self.server.requests.append({
            "method": self.command, "path": self.path, "body": body,
            "headers": {k.lower(): v for k, v in self.headers.items()}})

    def _send(self, status, headers, body):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        accepted = {f"Bearer {token}" for token in self.server.accepted_tokens}
        if accepted and self.headers.get("Authorization") not in accepted:
            self._send(401, {"Content-Type": "application/json"}, b'{"error": "BODY-TEST-ONLY"}')
            return
        self._send(*self.server.usage_reply)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._record(json.loads(self.rfile.read(length) or b"{}"))
        status, payload = self.server.token_reply
        self._send(status, {"Content-Type": "application/json"}, json.dumps(payload).encode())

    def log_message(self, *_args):
        return


class AnthropicFixture:
    """Loopback stand-ins for Anthropic's usage and sign-in endpoints."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _AnthropicHandler)
        self.server.requests = []
        self.server.accepted_tokens = set()
        self.server.usage_reply = (200, {"Content-Type": "application/json"},
                                   json.dumps(claude_body(41, 24)).encode())
        self.server.token_reply = (200, {"access_token": RENEWED,
                                         "refresh_token": "refresh-RENEWED",
                                         "expires_in": 28800, "scope": "user:profile"})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config_home = tempfile.TemporaryDirectory()
        self.environ = {
            "QUOTA_SENTRY_CLAUDE_USAGE_URL": f"http://127.0.0.1:{port}/api/oauth/usage",
            "QUOTA_SENTRY_CLAUDE_TOKEN_URL": f"http://127.0.0.1:{port}/v1/oauth/token",
            "XDG_CONFIG_HOME": self.config_home.name,
        }
        self.login_path = Path(self.config_home.name) / "quota-sentry" / "claude-credentials.json"

    def save_login(self, stored):
        core.write_claude_credentials(self.login_path, stored)

    def requests(self, method):
        return [request for request in self.server.requests if request["method"] == method]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.config_home.cleanup()


class ClaudeSignInFileTest(unittest.TestCase):
    def test_reads_only_quota_sentrys_own_login_file(self):
        with tempfile.TemporaryDirectory() as config_home:
            environ = {"XDG_CONFIG_HOME": config_home}
            core.write_claude_credentials(core.claude_credentials_path(environ), credentials())
            with mock.patch.object(core.subprocess, "run") as run:
                stored = core.read_claude_credentials(environ=environ)

        run.assert_not_called()
        self.assertEqual(stored["claudeAiOauth"]["accessToken"], TOKEN)
        self.assertEqual(core.claude_credentials_path(environ),
                         Path(config_home) / "quota-sentry" / "claude-credentials.json")

    def test_claude_codes_own_login_is_never_read(self):
        with tempfile.TemporaryDirectory() as config_home, \
                tempfile.TemporaryDirectory() as claude_dir:
            Path(claude_dir, ".credentials.json").write_text(json.dumps(credentials()))
            with mock.patch.object(core.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "quota-sentry login --provider claude"):
                    core.read_claude_credentials(environ={
                        "XDG_CONFIG_HOME": config_home, "CLAUDE_CONFIG_DIR": claude_dir})

        run.assert_not_called()

    def test_login_file_is_readable_only_by_owner(self):
        with tempfile.TemporaryDirectory() as config_home:
            path = core.claude_credentials_path({"XDG_CONFIG_HOME": config_home})
            core.write_claude_credentials(path, credentials())

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), credentials())

    def test_access_token_requires_unexpired_profile_scoped_login(self):
        self.assertEqual(core.claude_access_token(credentials(), now=NOW), TOKEN)

        failures = [
            credentials(expires_at=NOW - timedelta(seconds=1)),
            credentials(scopes=("user:inference",)),
            {"claudeAiOauth": {"accessToken": ""}},
            {},
        ]
        for stored in failures:
            with self.subTest(stored=list(stored)):
                with self.assertRaises(RuntimeError) as raised:
                    core.claude_access_token(stored, now=NOW)
                self.assertNotIn("SECRET", str(raised.exception))


class ClaudeSignInFlowTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AnthropicFixture()

    def tearDown(self):
        self.fixture.close()

    def test_sign_in_link_uses_claude_oauth_with_pkce(self):
        start = core.claude_oauth_start()

        url = urllib.parse.urlparse(start["url"])
        params = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "claude.com", "/cai/oauth/authorize"))
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(start["verifier"].encode("ascii")).digest()).rstrip(b"=").decode()
        self.assertEqual(params, {
            "code": "true", "client_id": CLIENT_ID, "response_type": "code",
            "redirect_uri": REDIRECT, "scope": "user:profile", "code_challenge": challenge,
            "code_challenge_method": "S256", "state": start["state"]})
        self.assertTrue(43 <= len(start["verifier"]) <= 128)

    def test_exchange_turns_pasted_code_into_login(self):
        start = core.claude_oauth_start()

        stored = core.claude_oauth_exchange(
            f"auth-code#{start['state']}", verifier=start["verifier"], state=start["state"],
            environ=self.fixture.environ, now=NOW)

        [request] = self.fixture.requests("POST")
        self.assertEqual(request["path"], "/v1/oauth/token")
        self.assertEqual(request["body"], {
            "grant_type": "authorization_code", "code": "auth-code", "redirect_uri": REDIRECT,
            "client_id": CLIENT_ID, "code_verifier": start["verifier"],
            "state": start["state"]})
        self.assertEqual(stored["claudeAiOauth"], {
            "accessToken": RENEWED, "refreshToken": "refresh-RENEWED",
            "expiresAt": int((NOW.timestamp() + 28800) * 1000), "scopes": ["user:profile"]})

    def test_exchange_refuses_code_from_another_sign_in(self):
        start = core.claude_oauth_start()

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            core.claude_oauth_exchange("auth-code#another-state", verifier=start["verifier"],
                                       state=start["state"], environ=self.fixture.environ)

        self.assertEqual(self.fixture.requests("POST"), [])

    def test_exchange_refuses_incomplete_code(self):
        start = core.claude_oauth_start()

        with self.assertRaisesRegex(RuntimeError, "#"):
            core.claude_oauth_exchange("auth-code", verifier=start["verifier"],
                                       state=start["state"], environ=self.fixture.environ)

    def test_refused_exchange_is_reported_without_response_text(self):
        self.fixture.server.token_reply = (401, {"error": "SECRET-BODY-TEST-ONLY"})
        start = core.claude_oauth_start()

        with self.assertRaises(RuntimeError) as raised:
            core.claude_oauth_exchange(f"auth-code#{start['state']}", verifier=start["verifier"],
                                       state=start["state"], environ=self.fixture.environ)

        self.assertIn("401", str(raised.exception))
        self.assertNotIn("SECRET", str(raised.exception))


class ClaudeUsageFetchTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AnthropicFixture()
        self.state_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.fixture.close()
        self.state_dir.cleanup()

    def fetch(self):
        return core.fetch_claude_usage_api(
            now=NOW, environ=self.fixture.environ, state_dir=Path(self.state_dir.name))

    def test_fetch_sends_own_login_to_usage_endpoint(self):
        self.fixture.save_login(credentials())
        self.fixture.server.accepted_tokens = {TOKEN}

        payload = self.fetch()

        self.assertEqual(window(payload, "five_hour")["usedPercent"], 41)
        [request] = self.fixture.requests("GET")
        self.assertEqual(request["path"], "/api/oauth/usage")
        self.assertEqual(request["headers"]["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(request["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(self.fixture.requests("POST"), [])

    def test_fetch_renews_an_expiring_login_first_and_saves_it(self):
        self.fixture.save_login(credentials(expires_at=NOW + timedelta(minutes=2)))
        self.fixture.server.accepted_tokens = {RENEWED}

        payload = self.fetch()

        self.assertEqual(window(payload, "five_hour")["usedPercent"], 41)
        [renewal] = self.fixture.requests("POST")
        self.assertEqual(renewal["body"], {
            "grant_type": "refresh_token", "refresh_token": "refresh-TEST-ONLY",
            "client_id": CLIENT_ID, "scope": "user:inference user:profile"})
        saved = json.loads(self.fixture.login_path.read_text())["claudeAiOauth"]
        self.assertEqual((saved["accessToken"], saved["refreshToken"]),
                         (RENEWED, "refresh-RENEWED"))
        self.assertEqual(stat.S_IMODE(self.fixture.login_path.stat().st_mode), 0o600)

    def test_rejected_usage_token_is_renewed_once_then_retried(self):
        self.fixture.save_login(credentials())
        self.fixture.server.accepted_tokens = {RENEWED}

        payload = self.fetch()

        self.assertEqual(window(payload, "seven_day")["usedPercent"], 24)
        self.assertEqual(len(self.fixture.requests("POST")), 1)
        self.assertEqual(len(self.fixture.requests("GET")), 2)

    def test_still_rejected_after_renewal_is_unavailable_without_leaking(self):
        self.fixture.save_login(credentials())
        self.fixture.server.accepted_tokens = {"never-accepted"}

        with self.assertRaises(RuntimeError) as raised:
            self.fetch()

        self.assertIn("401", str(raised.exception))
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertNotIn("BODY-TEST-ONLY", str(raised.exception))
        self.assertEqual(len(self.fixture.requests("POST")), 1)

    def test_failed_renewal_keeps_previous_login_file(self):
        self.fixture.save_login(credentials(expires_at=NOW - timedelta(seconds=1)))
        before = self.fixture.login_path.read_bytes()
        self.fixture.server.token_reply = (400, {"error": "invalid_grant"})

        with self.assertRaisesRegex(RuntimeError, "refused"):
            self.fetch()

        self.assertEqual(self.fixture.login_path.read_bytes(), before)
        self.assertEqual(self.fixture.requests("GET"), [])

    def test_default_endpoints_are_anthropic(self):
        self.assertEqual(core.claude_usage_url(environ={}),
                         "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(core.claude_token_url(environ={}),
                         "https://platform.claude.com/v1/oauth/token")

    def test_endpoint_overrides_must_stay_on_loopback(self):
        with self.assertRaises(ValueError):
            core.claude_usage_url(environ={
                "QUOTA_SENTRY_CLAUDE_USAGE_URL": "https://example.com/api/oauth/usage"})
        with self.assertRaises(ValueError):
            core.claude_token_url(environ={
                "QUOTA_SENTRY_CLAUDE_TOKEN_URL": "https://example.com/v1/oauth/token"})

    def test_rate_limited_endpoint_is_not_asked_again_until_retry_after(self):
        self.fixture.save_login(credentials())
        self.fixture.server.usage_reply = (429, {"Retry-After": "120"}, b"")

        with self.assertRaises(RuntimeError):
            self.fetch()
        with self.assertRaisesRegex(RuntimeError, "rate-limited"):
            self.fetch()

        self.assertEqual(len(self.fixture.requests("GET")), 1)
        held = core.read_claude_usage_hold(Path(self.state_dir.name))
        self.assertEqual(held["until"], NOW + timedelta(seconds=120))

    def test_forbidden_usage_read_holds_five_minutes_like_claude_code(self):
        self.fixture.save_login(credentials())
        self.fixture.server.usage_reply = (403, {}, b"")

        with self.assertRaises(RuntimeError):
            self.fetch()
        with self.assertRaisesRegex(RuntimeError, "rate-limited"):
            self.fetch()

        self.assertEqual(len(self.fixture.requests("GET")), 1)
        held = core.read_claude_usage_hold(Path(self.state_dir.name))
        self.assertEqual(held["until"], NOW + timedelta(minutes=5))

    def test_rejected_login_is_not_retried_within_the_hour(self):
        self.fixture.save_login(credentials())
        self.fixture.server.accepted_tokens = {"never-accepted"}

        with self.assertRaises(RuntimeError):
            self.fetch()
        with self.assertRaisesRegex(RuntimeError, "login quota-sentry|quota-sentry login"):
            self.fetch()

        self.assertEqual((len(self.fixture.requests("GET")), len(self.fixture.requests("POST"))),
                         (2, 1))
        held = core.read_claude_usage_hold(Path(self.state_dir.name))
        self.assertEqual(held["until"], NOW + timedelta(hours=1))

    def test_failed_renewal_is_not_retried_within_the_hour(self):
        self.fixture.save_login(credentials(expires_at=NOW - timedelta(seconds=1)))
        self.fixture.server.token_reply = (400, {"error": "invalid_grant"})

        with self.assertRaises(RuntimeError):
            self.fetch()
        with self.assertRaises(RuntimeError):
            self.fetch()

        self.assertEqual(len(self.fixture.requests("POST")), 1)

    def test_a_new_sign_in_clears_a_hold_for_the_old_login(self):
        self.fixture.save_login(credentials())
        self.fixture.server.accepted_tokens = {"never-accepted"}
        with self.assertRaises(RuntimeError):
            self.fetch()

        fresh = credentials()
        fresh["claudeAiOauth"]["accessToken"] = "fake-new-login-token-TEST-ONLY"
        self.fixture.save_login(fresh)
        self.fixture.server.accepted_tokens = {"fake-new-login-token-TEST-ONLY"}

        payload = self.fetch()
        self.assertEqual(window(payload, "five_hour")["usedPercent"], 41)


class ClaudeLoginCommandTest(unittest.TestCase):
    def setUp(self):
        self.fixture = AnthropicFixture()

    def tearDown(self):
        self.fixture.close()

    def run_cli(self, argv, pasted=None):
        output = StringIO()
        start = {"url": "https://claude.com/cai/oauth/authorize?state=known",
                 "verifier": "v" * 64, "state": "known-state"}
        with mock.patch.dict(os.environ, self.fixture.environ), \
                mock.patch.object(core, "claude_oauth_start", return_value=start), \
                mock.patch("builtins.input", return_value=pasted or ""), \
                redirect_stdout(output), redirect_stderr(output):
            result = cli.main(argv)
        return result, output.getvalue()

    def test_login_saves_a_private_login_without_printing_tokens(self):
        result, output = self.run_cli(["login", "--provider", "claude"],
                                      pasted="auth-code#known-state")

        self.assertEqual(result, 0)
        saved = json.loads(self.fixture.login_path.read_text())["claudeAiOauth"]
        self.assertEqual(saved["accessToken"], RENEWED)
        self.assertEqual(stat.S_IMODE(self.fixture.login_path.stat().st_mode), 0o600)
        self.assertIn("https://claude.com/cai/oauth/authorize", output)
        self.assertNotIn(RENEWED, output)
        self.assertNotIn("refresh-RENEWED", output)

    def test_login_is_only_for_claude(self):
        result, output = self.run_cli(["login"])

        self.assertEqual(result, 2)
        self.assertFalse(self.fixture.login_path.exists())

    def test_logout_removes_the_login_file(self):
        self.fixture.save_login(credentials())

        result, _output = self.run_cli(["logout", "--provider", "claude"])

        self.assertEqual(result, 0)
        self.assertFalse(self.fixture.login_path.exists())


class ClaudeHookMergeTest(unittest.TestCase):
    SCRIPT = Path("/opt/Quota Sentry/scripts/quota-sentry")

    def entries(self, merged, event):
        found = []
        for entry in merged["hooks"].get(event, []):
            for hook in entry["hooks"]:
                if hook.get("command") == str(self.SCRIPT):
                    found.append((entry, hook))
        return found

    def test_claude_hooks_cover_codex_checkpoints_with_exec_form(self):
        merged = core.merge_claude_hooks({}, self.SCRIPT)

        self.assertEqual(set(merged["hooks"]), {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
            "PostToolBatch", "PreCompact"})
        [(start_entry, start)] = self.entries(merged, "SessionStart")
        self.assertEqual(start_entry["matcher"], "startup|resume|clear|compact")
        self.assertEqual(start["args"], ["start", "--quiet", "--read-hook-input",
                                         "--provider", "claude"])
        self.assertEqual(start["timeout"], 30)
        self.assertNotIn("statusMessage", start)

        [(_, prompt)] = self.entries(merged, "UserPromptSubmit")
        self.assertEqual(prompt["args"], ["prompt-guard", "--read-hook-input", "--hook-mode",
                                          "--recover-daemon", "--provider", "claude"])
        guard_args = ["guard", "--state-only", "--no-notify", "--read-hook-input",
                      "--hook-mode", "--recover-daemon", "--provider", "claude"]
        for event, matcher in (("PreToolUse", "*"), ("PostToolUse", "*"),
                               ("PostToolBatch", None), ("PreCompact", None)):
            with self.subTest(event=event):
                [(entry, hook)] = self.entries(merged, event)
                self.assertEqual(entry.get("matcher"), matcher)
                self.assertEqual(hook["args"], guard_args)
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolBatch",
                      "PreCompact"):
            with self.subTest(event=event):
                [(_, hook)] = self.entries(merged, event)
                self.assertEqual(hook["type"], "command")
                self.assertEqual(hook["timeout"], core.DEFAULT_HOOK_TIMEOUT_SECONDS)
                self.assertEqual(hook["statusMessage"], "Quota Sentry: quota checkpoint")
                self.assertNotIn("onFailure", hook)
                self.assertNotIn("async", hook)

    def test_merge_preserves_other_settings_and_replaces_previous_install(self):
        existing = {
            "env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            "permissions": {"allow": ["Read"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]},
                    {"matcher": "*", "hooks": [{"type": "command",
                                                "command": "/old place/quota-sentry",
                                                "args": ["guard"]}]},
                ],
                "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
            },
        }

        merged = core.merge_claude_hooks(existing, self.SCRIPT)

        self.assertEqual(merged["env"], existing["env"])
        self.assertEqual(merged["permissions"], existing["permissions"])
        self.assertEqual(merged["hooks"]["Stop"], existing["hooks"]["Stop"])
        commands = [hook["command"] for entry in merged["hooks"]["PreToolUse"]
                    for hook in entry["hooks"]]
        self.assertEqual(commands, ["other.sh", str(self.SCRIPT)])
        self.assertEqual(core.merge_claude_hooks(merged, self.SCRIPT), merged)
        self.assertEqual(existing["hooks"]["PreToolUse"][1]["hooks"][0]["command"],
                         "/old place/quota-sentry", "merge mutated its input")

    def test_notification_preference_can_omit_status_text(self):
        merged = core.merge_claude_hooks({}, self.SCRIPT, notify_on_block=False)

        [(_, hook)] = self.entries(merged, "PreToolUse")
        self.assertNotIn("statusMessage", hook)


class ClaudeCliTest(unittest.TestCase):
    def test_claude_provider_uses_its_own_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": directory}):
                self.assertEqual(cli.resolve_state_dir(None, core.CLAUDE_PROVIDER),
                                 Path(directory) / "quota-sentry" / "claude")
                self.assertEqual(cli.resolve_state_dir(None),
                                 Path(directory) / "quota-sentry")

    def daemon_line(self, state_dir, *extra):
        return " ".join([
            str(cli.script_path()), "daemon", "--state-dir", str(state_dir),
            "--threshold-percent", "90", "--reset-buffer-seconds", "60", "--source", "auto",
            "--interval-seconds", "300", "--near-threshold-percent", "85",
            "--near-interval-seconds", "60", "--critical-threshold-percent", "93",
            "--critical-interval-seconds", "30", "--runtime-fingerprint", "abc",
            "--config-path-fingerprint", cli.config_path_fingerprint(None), *extra])

    def test_daemon_compatibility_requires_matching_provider(self):
        claude_args = cli.build_parser().parse_args(["start", "--provider", "claude"])
        codex_args = cli.build_parser().parse_args(["start"])

        self.assertFalse(cli.daemon_command_is_compatible(
            self.daemon_line("/s"), claude_args, "abc"))
        self.assertTrue(cli.daemon_command_is_compatible(
            self.daemon_line("/s", "--provider", "claude"), claude_args, "abc"))
        self.assertTrue(cli.daemon_command_is_compatible(
            self.daemon_line("/s"), codex_args, "abc"))
        self.assertFalse(cli.daemon_command_is_compatible(
            self.daemon_line("/s", "--provider", "claude"), codex_args, "abc"))

    def test_claude_start_launches_provider_daemon_in_claude_state_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
                    mock.patch.object(cli.subprocess, "Popen") as popen, \
                    mock.patch.object(cli, "wait_for_registered_daemon", return_value=4242):
                args = cli.build_parser().parse_args(["start", "--quiet", "--provider", "claude"])
                self.assertEqual(cli.start_command(args), 0)

            command = popen.call_args.args[0]
            self.assertEqual(command[1], "daemon")
            self.assertEqual(command[command.index("--state-dir") + 1],
                             str(Path(directory) / "quota-sentry" / "claude"))
            self.assertEqual(command[command.index("--provider") + 1], "claude")
            self.assertEqual(command[command.index("--min-poll-spacing-seconds") + 1], "60")

    def test_codex_daemon_command_line_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": directory}), \
                    mock.patch.object(cli.subprocess, "Popen") as popen, \
                    mock.patch.object(cli, "wait_for_registered_daemon", return_value=4242):
                args = cli.build_parser().parse_args(["start", "--quiet"])
                self.assertEqual(cli.start_command(args), 0)

            command = popen.call_args.args[0]
            self.assertNotIn("--provider", command)
            self.assertNotIn("--min-poll-spacing-seconds", command)

    def test_claude_daemon_waits_out_the_read_spacing_even_when_woken(self):
        self.assertEqual(cli.poll_spacing_wait_seconds(None, 100.0, 60), 0)
        self.assertEqual(cli.poll_spacing_wait_seconds(100.0, 130.0, 60), 30)
        self.assertEqual(cli.poll_spacing_wait_seconds(100.0, 175.0, 60), 0)
        self.assertEqual(cli.poll_spacing_wait_seconds(100.0, 101.0, 0), 0)

    def test_claude_recovery_bootstrap_carries_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            args = cli.build_parser().parse_args([
                "guard", "--hook-mode", "--recover-daemon", "--provider", "claude",
                "--state-dir", directory])
            with mock.patch.object(cli.subprocess, "Popen") as popen:
                cli.request_daemon_recovery(args)

            command = popen.call_args.args[0]
            self.assertEqual(command[1], "start")
            self.assertEqual(command[command.index("--provider") + 1], "claude")
            self.assertTrue(Path(directory, "refresh.request").exists())

    def test_provider_rejects_sources_of_other_providers(self):
        for argv in (["poll", "--provider", "claude", "--source", "codexbar"],
                     ["poll", "--provider", "claude", "--source", "codex-app-server"],
                     ["poll", "--source", core.CLAUDE_USAGE_API_SOURCE]):
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
                    cli.main(argv)
                self.assertEqual(raised.exception.code, 2)


class ClaudeInstallerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.settings = root / "settings.json"
        self.config = root / "config.json"
        self.original = (b'{\n  "env": {\n    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"\n  },\n'
                         b'  "alwaysThinkingEnabled": true\n}\n')
        self.settings.write_bytes(self.original)
        self.settings.chmod(0o644)

    def tearDown(self):
        self.directory.cleanup()

    def install(self, *extra):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = cli.main(["install-hook", "--provider", "claude",
                               "--hooks-path", str(self.settings),
                               "--script-path", "/opt/Quota Sentry/scripts/quota-sentry",
                               "--config-path", str(self.config), *extra])
        return result, output.getvalue()

    def test_install_merges_hooks_and_keeps_backup_order_and_mode(self):
        result, _output = self.install()

        self.assertEqual(result, 0)
        installed = json.loads(self.settings.read_text())
        self.assertEqual(list(installed)[:2], ["env", "alwaysThinkingEnabled"])
        self.assertEqual(installed["env"], {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        self.assertIn("PostToolBatch", installed["hooks"])
        self.assertEqual(Path(str(self.settings) + ".bak").read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o644)

        before = self.settings.read_bytes()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_default_target_is_claude_settings_not_codex_hooks(self):
        root = Path(self.directory.name)
        claude_dir, codex_dir = root / "claude-config", root / "codex-home"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir),
                                          "CODEX_HOME": str(codex_dir)}):
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                result = cli.main(["install-hook", "--provider", "claude",
                                   "--script-path", "/opt/Quota Sentry/scripts/quota-sentry",
                                   "--config-path", str(self.config)])

        self.assertEqual(result, 0)
        self.assertIn("PreToolUse", json.loads((claude_dir / "settings.json").read_text())["hooks"])
        self.assertFalse((codex_dir / "hooks.json").exists())

    def test_install_refuses_unreadable_settings_without_writing(self):
        self.settings.write_bytes(b"{ not json")

        result, _output = self.install()

        self.assertEqual(result, 1)
        self.assertEqual(self.settings.read_bytes(), b"{ not json")
        self.assertFalse(Path(str(self.settings) + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
