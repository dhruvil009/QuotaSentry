import json
import os
import queue
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


DEFAULT_PROVIDER = "codex"
CODEXBAR_SOURCE = "codexbar"
CODEX_APP_SERVER_SOURCE = "codex-app-server"
AUTO_SOURCE = "auto"
WEEKLY_MODE_ADVISORY = "advisory"
WEEKLY_MODE_HARD_BLOCK = "hard-block"
DEFAULT_USAGE_SOURCE = AUTO_SOURCE
DEFAULT_CODEXBAR_SOURCE = "cli"
DEFAULT_THRESHOLD_PERCENT = 95
DEFAULT_WINDOW_MINUTES = 300
DEFAULT_WEEKLY_WINDOW_MINUTES = 10080
DEFAULT_WEEKLY_THRESHOLD_PERCENT = 99
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_NEAR_POLL_INTERVAL_SECONDS = 60
DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS = 30
DEFAULT_NEAR_THRESHOLD_PERCENT = 85
DEFAULT_CRITICAL_THRESHOLD_PERCENT = 93
DEFAULT_MAX_STATE_AGE_SECONDS = 420
DEFAULT_RESET_BUFFER_SECONDS = 60
DEFAULT_CODEXBAR_TIMEOUT_SECONDS = 30
DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class QuotaConfig:
    weekly_mode: str = WEEKLY_MODE_ADVISORY
    weekly_threshold_percent: int = DEFAULT_WEEKLY_THRESHOLD_PERCENT


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: int
    window_minutes: Optional[int]
    resets_at: datetime


@dataclass(frozen=True)
class QuotaDecision:
    status: str
    reason: str
    used_percent: Optional[int] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[datetime] = None
    blocked_until: Optional[datetime] = None
    fail_open: bool = True
    primary_window: Optional[QuotaWindow] = None
    weekly_window: Optional[QuotaWindow] = None
    blocked_window: Optional[str] = None
    weekly_hard_block_enabled: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    if root:
        return Path(root) / "quota-sentry"
    return Path.home() / ".cache" / "quota-sentry"


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "quota-sentry"
    return Path.home() / ".config" / "quota-sentry"


def default_config_path(config_root: Optional[Path] = None) -> Path:
    return (config_root or config_dir()) / "config.json"


def default_state_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "state.json"


def default_pid_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.pid"


def default_log_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.log"


def _valid_weekly_mode(value: Any) -> str:
    if isinstance(value, str) and value in {WEEKLY_MODE_ADVISORY, WEEKLY_MODE_HARD_BLOCK}:
        return value
    return WEEKLY_MODE_ADVISORY


def _valid_percent(value: Any, default: int) -> int:
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return default
    if percent < 1 or percent > 100:
        return default
    return percent


def config_from_payload(payload: Any) -> QuotaConfig:
    if not isinstance(payload, dict):
        return QuotaConfig()
    return QuotaConfig(
        weekly_mode=_valid_weekly_mode(payload.get("weeklyMode")),
        weekly_threshold_percent=_valid_percent(
            payload.get("weeklyThresholdPercent"),
            DEFAULT_WEEKLY_THRESHOLD_PERCENT,
        ),
    )


def config_to_payload(config: QuotaConfig) -> Dict[str, Any]:
    return {
        "weeklyMode": _valid_weekly_mode(config.weekly_mode),
        "weeklyThresholdPercent": _valid_percent(
            config.weekly_threshold_percent,
            DEFAULT_WEEKLY_THRESHOLD_PERCENT,
        ),
    }


def read_config(path: Optional[Path] = None) -> QuotaConfig:
    config_path = path or default_config_path()
    try:
        return config_from_payload(json.loads(config_path.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return QuotaConfig()


def write_config(path: Path, config: QuotaConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(config_to_payload(config), indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)


def emit_terminal_notice(message: str) -> None:
    notice_file = os.environ.get("QUOTA_SENTRY_NOTICE_FILE")
    if notice_file:
        try:
            path = Path(notice_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(message + "\n")
        except OSError:
            return
        return

    flags = os.O_WRONLY | getattr(os, "O_NOCTTY", 0)
    fd: Optional[int] = None
    try:
        fd = os.open("/dev/tty", flags)
        if os.tcgetpgrp(fd) != os.getpgrp():
            return
        os.write(fd, ("\n" + message + "\n").encode())
    except OSError:
        return
    finally:
        if fd is not None:
            os.close(fd)


def _codex_entry(payload: Any) -> Optional[Dict[str, Any]]:
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("provider") == DEFAULT_PROVIDER:
            return entry
    for entry in entries:
        if isinstance(entry, dict):
            return entry
    return None


def _window_candidates(usage: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for name in ("primary", "secondary", "tertiary"):
        window = usage.get(name)
        if isinstance(window, dict):
            yield name, window


def _five_hour_window(usage: Dict[str, Any], window_minutes: int) -> Optional[Dict[str, Any]]:
    for _name, window in _window_candidates(usage):
        if window.get("windowMinutes") == window_minutes:
            return window
    primary = usage.get("primary")
    return primary if isinstance(primary, dict) else None


def _window_by_minutes(usage: Dict[str, Any], window_minutes: int) -> Optional[Dict[str, Any]]:
    for _name, window in _window_candidates(usage):
        if window.get("windowMinutes") == window_minutes:
            return window
    return None


def _parse_quota_window(window: Dict[str, Any], name: str) -> Tuple[Optional[QuotaWindow], Optional[str]]:
    used_percent = window.get("usedPercent")
    resets_at = parse_timestamp(window.get("resetsAt"))
    actual_window_minutes = window.get("windowMinutes")

    if used_percent is None:
        return None, "quota window is missing usedPercent"
    if resets_at is None:
        return None, "quota window has missing or invalid resetsAt"

    try:
        used = int(used_percent)
    except (TypeError, ValueError):
        return None, "quota window has invalid usedPercent"

    return (
        QuotaWindow(
            name=name,
            used_percent=used,
            window_minutes=actual_window_minutes,
            resets_at=resets_at,
        ),
        None,
    )


def _window_state(window: Optional[QuotaWindow]) -> Optional[Dict[str, Any]]:
    if window is None:
        return None
    return {
        "usedPercent": window.used_percent,
        "windowMinutes": window.window_minutes,
        "resetsAt": format_timestamp(window.resets_at),
    }


def _blocked_decision(
    window: QuotaWindow,
    reset_buffer_seconds: int,
    blocked_window: str,
    primary_window: Optional[QuotaWindow],
    weekly_window: Optional[QuotaWindow],
    weekly_hard_block_enabled: bool,
) -> QuotaDecision:
    blocked_until = window.resets_at + timedelta(seconds=reset_buffer_seconds)
    return QuotaDecision(
        status="blocked",
        reason=f"{window.used_percent}% of the {window.window_minutes}-minute Codex quota is used",
        used_percent=window.used_percent,
        window_minutes=window.window_minutes,
        resets_at=window.resets_at,
        blocked_until=blocked_until,
        fail_open=False,
        primary_window=primary_window,
        weekly_window=weekly_window,
        blocked_window=blocked_window,
        weekly_hard_block_enabled=weekly_hard_block_enabled,
    )


def parse_codexbar_usage(
    payload: Any,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    now: Optional[datetime] = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    quota_config: Optional[QuotaConfig] = None,
) -> QuotaDecision:
    current_time = now or utc_now()
    config = quota_config or QuotaConfig()
    weekly_hard_block_enabled = config.weekly_mode == WEEKLY_MODE_HARD_BLOCK
    entry = _codex_entry(payload)
    if not entry:
        return QuotaDecision(status="unknown", reason="quota source returned no provider entries")

    error = entry.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "unknown provider error"
        return QuotaDecision(status="unknown", reason=str(message))

    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return QuotaDecision(status="unknown", reason="quota source returned no usage object")

    window = _five_hour_window(usage, window_minutes)
    if not window:
        return QuotaDecision(status="unknown", reason="quota source returned no quota window")

    primary_window, primary_error = _parse_quota_window(window, "primary")
    if primary_error or primary_window is None:
        return QuotaDecision(status="unknown", reason=primary_error or "quota source returned no quota window")

    weekly_window = None
    weekly_source_window = _window_by_minutes(usage, DEFAULT_WEEKLY_WINDOW_MINUTES)
    if weekly_source_window:
        weekly_window, _weekly_error = _parse_quota_window(weekly_source_window, "weekly")

    blocked_candidates: List[Tuple[str, QuotaWindow]] = []
    if primary_window.resets_at > current_time and primary_window.used_percent >= threshold_percent:
        blocked_candidates.append(("primary", primary_window))
    if (
        weekly_hard_block_enabled
        and weekly_window is not None
        and weekly_window.resets_at > current_time
        and weekly_window.used_percent >= config.weekly_threshold_percent
    ):
        blocked_candidates.append(("weekly", weekly_window))

    if blocked_candidates:
        blocked_window, blocked_quota_window = max(
            blocked_candidates,
            key=lambda candidate: candidate[1].resets_at,
        )
        return _blocked_decision(
            blocked_quota_window,
            reset_buffer_seconds=reset_buffer_seconds,
            blocked_window=blocked_window,
            primary_window=primary_window,
            weekly_window=weekly_window,
            weekly_hard_block_enabled=weekly_hard_block_enabled,
        )

    if primary_window.resets_at <= current_time:
        return QuotaDecision(
            status="open",
            reason="quota reset time has passed",
            used_percent=primary_window.used_percent,
            window_minutes=primary_window.window_minutes,
            resets_at=primary_window.resets_at,
            fail_open=False,
            primary_window=primary_window,
            weekly_window=weekly_window,
            weekly_hard_block_enabled=weekly_hard_block_enabled,
        )

    return QuotaDecision(
        status="open",
        reason=f"{primary_window.used_percent}% of the {primary_window.window_minutes}-minute Codex quota is used",
        used_percent=primary_window.used_percent,
        window_minutes=primary_window.window_minutes,
        resets_at=primary_window.resets_at,
        fail_open=False,
        primary_window=primary_window,
        weekly_window=weekly_window,
        weekly_hard_block_enabled=weekly_hard_block_enabled,
    )


def state_from_decision(decision: QuotaDecision, now: Optional[datetime] = None) -> Dict[str, Any]:
    current_time = now or utc_now()
    return {
        "status": decision.status,
        "reason": decision.reason,
        "usedPercent": decision.used_percent,
        "windowMinutes": decision.window_minutes,
        "resetsAt": format_timestamp(decision.resets_at),
        "blockedUntil": format_timestamp(decision.blocked_until),
        "failOpen": decision.fail_open,
        "updatedAt": format_timestamp(current_time),
        "primary": _window_state(decision.primary_window),
        "weekly": _window_state(decision.weekly_window),
        "blockedWindow": decision.blocked_window,
        "weeklyHardBlockEnabled": decision.weekly_hard_block_enabled,
    }


def write_state(path: Path, decision: QuotaDecision, now: Optional[datetime] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state_from_decision(decision, now=now), indent=2, sort_keys=True) + "\n")
    temp_path.replace(path)


def read_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def block_until_from_state(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
) -> Optional[datetime]:
    if state.get("status") != "blocked":
        return None
    current_time = now or utc_now()
    updated_at = parse_timestamp(state.get("updatedAt"))
    blocked_until = parse_timestamp(state.get("blockedUntil"))
    if not updated_at or not blocked_until:
        return None
    if current_time - updated_at > timedelta(seconds=max_state_age_seconds):
        return None
    if blocked_until <= current_time:
        return None
    return blocked_until


def extract_json(text: str) -> Any:
    stripped = text.strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character not in "[{":
            continue
        try:
            payload, _end = decoder.raw_decode(stripped[index:])
            return payload
        except json.JSONDecodeError:
            continue
    raise ValueError("codexbar output did not contain JSON")


def _format_unix_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return format_timestamp(datetime.fromtimestamp(seconds, timezone.utc))


def _app_server_window_to_usage(window: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(window, dict):
        return None
    return {
        "usedPercent": window.get("usedPercent"),
        "windowMinutes": window.get("windowDurationMins"),
        "resetsAt": _format_unix_timestamp(window.get("resetsAt")),
    }


def codex_app_server_rate_limits_to_usage(
    result: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    rate_limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(rate_limits, dict):
        rate_limits = {}

    usage: Dict[str, Any] = {
        "updatedAt": format_timestamp(now or utc_now()),
        "loginMethod": result.get("planType") if isinstance(result, dict) else None,
    }
    for name in ("primary", "secondary", "tertiary"):
        mapped = _app_server_window_to_usage(rate_limits.get(name))
        if mapped is not None:
            usage[name] = mapped

    return [
        {
            "provider": DEFAULT_PROVIDER,
            "source": CODEX_APP_SERVER_SOURCE,
            "usage": usage,
        }
    ]


def _write_json_line(stdin: Any, payload: Dict[str, Any]) -> None:
    stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stdin.flush()


def _read_json_response(
    output_queue: "queue.Queue[Optional[str]]",
    request_id: str,
    deadline: float,
) -> Dict[str, Any]:
    while time.monotonic() < deadline:
        timeout = max(0.1, deadline - time.monotonic())
        try:
            line = output_queue.get(timeout=min(0.5, timeout))
        except queue.Empty:
            continue
        if line is None:
            raise RuntimeError(f"codex app-server closed before {request_id}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == request_id:
            return message
    raise TimeoutError(f"codex app-server timed out waiting for {request_id}")


def _enqueue_output_lines(stdout: Any, output_queue: "queue.Queue[Optional[str]]") -> None:
    try:
        if stdout is not None:
            for line in stdout:
                output_queue.put(line)
    finally:
        output_queue.put(None)


def fetch_codex_app_server_usage(
    timeout_seconds: int = DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS,
) -> Any:
    command = ["codex", "app-server", "--stdio"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,
        close_fds=True,
    )
    output_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    reader = threading.Thread(
        target=_enqueue_output_lines,
        args=(process.stdout, output_queue),
        daemon=True,
    )
    reader.start()

    try:
        if process.stdin is None:
            raise RuntimeError("codex app-server stdin unavailable")

        deadline = time.monotonic() + timeout_seconds
        _write_json_line(
            process.stdin,
            {
                "id": "quota-sentry-init",
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "quota-sentry",
                        "title": "Quota Sentry",
                        "version": "0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                        "mcpServerOpenaiFormElicitation": False,
                        "optOutNotificationMethods": [],
                    },
                },
            },
        )
        initialize_response = _read_json_response(output_queue, "quota-sentry-init", deadline)
        if "error" in initialize_response:
            raise RuntimeError(f"codex app-server initialize failed: {initialize_response['error']}")

        _write_json_line(process.stdin, {"method": "initialized"})
        _write_json_line(
            process.stdin,
            {
                "id": "quota-sentry-rate-limits",
                "method": "account/rateLimits/read",
            },
        )
        rate_limits_response = _read_json_response(output_queue, "quota-sentry-rate-limits", deadline)
        if "error" in rate_limits_response:
            raise RuntimeError(f"codex app-server rate limit read failed: {rate_limits_response['error']}")

        result = rate_limits_response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("codex app-server returned no rate limit payload")
        return codex_app_server_rate_limits_to_usage(result)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def fetch_codexbar_usage(timeout_seconds: int = DEFAULT_CODEXBAR_TIMEOUT_SECONDS) -> Any:
    command = [
        "codexbar",
        "usage",
        "--provider",
        DEFAULT_PROVIDER,
        "--source",
        DEFAULT_CODEXBAR_SOURCE,
        "--format",
        "json",
    ]
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        start_new_session=True,
        close_fds=True,
    )
    output = completed.stdout or completed.stderr
    if completed.returncode != 0 and not output.strip():
        raise RuntimeError(f"codexbar exited with {completed.returncode}")
    return extract_json(output)


def fetch_codex_usage(source: str = DEFAULT_USAGE_SOURCE) -> Any:
    if source == CODEX_APP_SERVER_SOURCE:
        return fetch_codex_app_server_usage()
    if source == CODEXBAR_SOURCE:
        return fetch_codexbar_usage()
    if source != AUTO_SOURCE:
        raise ValueError(f"unsupported quota source: {source}")

    try:
        return fetch_codex_app_server_usage()
    except Exception as app_server_exc:
        try:
            return fetch_codexbar_usage()
        except Exception as codexbar_exc:
            raise RuntimeError(
                f"codex app-server failed: {app_server_exc}; codexbar fallback failed: {codexbar_exc}"
            ) from codexbar_exc


def poll_once(
    state_path: Path,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    fetcher: Optional[Callable[[], Any]] = None,
    source: str = DEFAULT_USAGE_SOURCE,
    config_path: Optional[Path] = None,
    quota_config: Optional[QuotaConfig] = None,
    now: Optional[datetime] = None,
) -> QuotaDecision:
    current_time = now or utc_now()
    active_config = quota_config or read_config(config_path)
    try:
        payload = fetcher() if fetcher is not None else fetch_codex_usage(source=source)
        decision = parse_codexbar_usage(
            payload,
            threshold_percent=threshold_percent,
            reset_buffer_seconds=reset_buffer_seconds,
            now=current_time,
            quota_config=active_config,
        )
    except Exception as exc:
        decision = QuotaDecision(status="unknown", reason=f"quota fetch failed: {exc}")
    write_state(state_path, decision, now=current_time)
    return decision


def next_poll_interval_seconds(
    decision: QuotaDecision,
    base_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    near_threshold_percent: int = DEFAULT_NEAR_THRESHOLD_PERCENT,
    near_interval_seconds: int = DEFAULT_NEAR_POLL_INTERVAL_SECONDS,
    critical_threshold_percent: int = DEFAULT_CRITICAL_THRESHOLD_PERCENT,
    critical_interval_seconds: int = DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS,
) -> int:
    base_interval = max(1, int(base_interval_seconds))
    near_interval = max(1, int(near_interval_seconds))
    critical_interval = max(1, int(critical_interval_seconds))

    used_values = [decision.used_percent]
    if decision.primary_window is not None:
        used_values.append(decision.primary_window.used_percent)
    if decision.weekly_hard_block_enabled and decision.weekly_window is not None:
        used_values.append(decision.weekly_window.used_percent)
    numeric_used_values = [used for used in used_values if used is not None]
    if not numeric_used_values:
        return base_interval
    used = max(numeric_used_values)
    if used >= critical_threshold_percent:
        return min(base_interval, critical_interval)
    if used >= near_threshold_percent:
        return min(base_interval, near_interval)
    return base_interval


def wait_if_blocked(
    state_path: Path,
    poller: Callable[[], QuotaDecision],
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], datetime] = utc_now,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    output: Callable[[str], None] = print,
    verbose: bool = False,
    notice: Callable[[str], None] = emit_terminal_notice,
    notify: bool = True,
    state_only: bool = False,
) -> int:
    if os.environ.get("QUOTA_SENTRY_DISABLE") == "1":
        return 0

    emitted_wait_message = False
    emitted_notice = False
    waited_once = False
    while True:
        state = read_state(state_path)
        current_time = now_func()
        if waited_once and state.get("status") == "blocked":
            saved_blocked_until = parse_timestamp(state.get("blockedUntil"))
            if saved_blocked_until is not None and saved_blocked_until <= current_time:
                return 0

        block_until = block_until_from_state(state, now=current_time, max_state_age_seconds=max_state_age_seconds)
        if block_until is None:
            if state_only:
                return 0
            decision = poller()
            if decision.status != "blocked" or decision.blocked_until is None:
                return 0
            block_until = decision.blocked_until

        current_time = now_func()
        if block_until <= current_time:
            return 0

        seconds = max(1, min(poll_interval_seconds, int((block_until - current_time).total_seconds())))
        if notify and not emitted_notice:
            notice(f"Quota Sentry: waiting for Codex quota reset until {format_timestamp(block_until)}.")
            emitted_notice = True
        if verbose and not emitted_wait_message:
            output(f"Quota Sentry: Codex quota guard active until {format_timestamp(block_until)}.")
            emitted_wait_message = True
        sleeper(seconds)
        waited_once = True


def hook_entry(matcher: str, command: str, async_value: bool, timeout_seconds: int) -> Dict[str, Any]:
    return {
        "matcher": matcher,
        "hooks": [
            {
                "type": "command",
                "command": command,
                "async": async_value,
                "timeout": timeout_seconds,
            }
        ],
    }


def _remove_existing_quota_sentry(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered = []
    for entry in entries:
        hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
        serialized = json.dumps(hooks)
        if "quota-sentry" not in serialized and "Quota Sentry" not in serialized:
            filtered.append(entry)
    return filtered


def merge_codex_hooks(existing: Dict[str, Any], script_path: Path) -> Dict[str, Any]:
    merged = json.loads(json.dumps(existing or {}))
    hooks = merged.setdefault("hooks", {})
    script = shlex.quote(str(script_path))
    start_command = f"{script} start --quiet"
    user_prompt_command = f"{script} prompt-guard"
    pre_tool_command = f"{script} guard --state-only --no-notify"

    additions = {
        "SessionStart": hook_entry("startup|clear|compact", start_command, False, 30),
        "UserPromptSubmit": hook_entry("", user_prompt_command, False, 21600),
        "PreToolUse": hook_entry(".*", pre_tool_command, False, 21600),
    }

    for event_name, entry in additions.items():
        current_entries = hooks.get(event_name)
        if not isinstance(current_entries, list):
            current_entries = []
        hooks[event_name] = _remove_existing_quota_sentry(current_entries) + [entry]

    return merged
