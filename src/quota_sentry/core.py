import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


DEFAULT_PROVIDER = "codex"
DEFAULT_SOURCE = "cli"
DEFAULT_THRESHOLD_PERCENT = 95
DEFAULT_WINDOW_MINUTES = 300
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_MAX_STATE_AGE_SECONDS = 180
DEFAULT_RESET_BUFFER_SECONDS = 60
DEFAULT_CODEXBAR_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class QuotaDecision:
    status: str
    reason: str
    used_percent: Optional[int] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[datetime] = None
    blocked_until: Optional[datetime] = None
    fail_open: bool = True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
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


def default_state_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "state.json"


def default_pid_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.pid"


def default_log_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.log"


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


def parse_codexbar_usage(
    payload: Any,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    now: Optional[datetime] = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> QuotaDecision:
    current_time = now or utc_now()
    entry = _codex_entry(payload)
    if not entry:
        return QuotaDecision(status="unknown", reason="codexbar returned no provider entries")

    error = entry.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "unknown provider error"
        return QuotaDecision(status="unknown", reason=str(message))

    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return QuotaDecision(status="unknown", reason="codexbar returned no usage object")

    window = _five_hour_window(usage, window_minutes)
    if not window:
        return QuotaDecision(status="unknown", reason="codexbar returned no quota window")

    used_percent = window.get("usedPercent")
    resets_at = parse_timestamp(window.get("resetsAt"))
    actual_window_minutes = window.get("windowMinutes")

    if used_percent is None:
        return QuotaDecision(status="unknown", reason="quota window is missing usedPercent")
    if resets_at is None:
        return QuotaDecision(status="unknown", reason="quota window is missing resetsAt")

    try:
        used = int(used_percent)
    except (TypeError, ValueError):
        return QuotaDecision(status="unknown", reason="quota window has invalid usedPercent")

    if resets_at <= current_time:
        return QuotaDecision(
            status="open",
            reason="quota reset time has passed",
            used_percent=used,
            window_minutes=actual_window_minutes,
            resets_at=resets_at,
            fail_open=False,
        )

    if used >= threshold_percent:
        blocked_until = resets_at + timedelta(seconds=reset_buffer_seconds)
        return QuotaDecision(
            status="blocked",
            reason=f"{used}% of the {actual_window_minutes}-minute Codex quota is used",
            used_percent=used,
            window_minutes=actual_window_minutes,
            resets_at=resets_at,
            blocked_until=blocked_until,
            fail_open=False,
        )

    return QuotaDecision(
        status="open",
        reason=f"{used}% of the {actual_window_minutes}-minute Codex quota is used",
        used_percent=used,
        window_minutes=actual_window_minutes,
        resets_at=resets_at,
        fail_open=False,
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


def fetch_codexbar_usage(timeout_seconds: int = DEFAULT_CODEXBAR_TIMEOUT_SECONDS) -> Any:
    command = [
        "codexbar",
        "usage",
        "--provider",
        DEFAULT_PROVIDER,
        "--source",
        DEFAULT_SOURCE,
        "--format",
        "json",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    output = completed.stdout or completed.stderr
    if completed.returncode != 0 and not output.strip():
        raise RuntimeError(f"codexbar exited with {completed.returncode}")
    return extract_json(output)


def poll_once(
    state_path: Path,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    fetcher: Callable[[], Any] = fetch_codexbar_usage,
    now: Optional[datetime] = None,
) -> QuotaDecision:
    current_time = now or utc_now()
    try:
        payload = fetcher()
        decision = parse_codexbar_usage(
            payload,
            threshold_percent=threshold_percent,
            reset_buffer_seconds=reset_buffer_seconds,
            now=current_time,
        )
    except Exception as exc:
        decision = QuotaDecision(status="unknown", reason=f"codexbar fetch failed: {exc}")
    write_state(state_path, decision, now=current_time)
    return decision


def wait_if_blocked(
    state_path: Path,
    poller: Callable[[], QuotaDecision],
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], datetime] = utc_now,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    output: Callable[[str], None] = print,
    verbose: bool = False,
) -> int:
    if os.environ.get("QUOTA_SENTRY_DISABLE") == "1":
        return 0

    emitted_wait_message = False
    while True:
        state = read_state(state_path)
        current_time = now_func()
        if state.get("status") == "blocked":
            saved_blocked_until = parse_timestamp(state.get("blockedUntil"))
            if saved_blocked_until is not None and saved_blocked_until <= current_time:
                return 0

        block_until = block_until_from_state(state, now=current_time, max_state_age_seconds=max_state_age_seconds)
        if block_until is None:
            decision = poller()
            if decision.status != "blocked" or decision.blocked_until is None:
                return 0
            block_until = decision.blocked_until

        current_time = now_func()
        if block_until <= current_time:
            return 0

        seconds = max(1, min(poll_interval_seconds, int((block_until - current_time).total_seconds())))
        if verbose and not emitted_wait_message:
            output(f"Quota Sentry: Codex quota guard active until {format_timestamp(block_until)}.")
            emitted_wait_message = True
        sleeper(seconds)


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
    start_command = f"{script} start"
    guard_command = f"{script} guard"

    additions = {
        "SessionStart": hook_entry("startup|clear|compact", start_command, True, 30),
        "UserPromptSubmit": hook_entry("", guard_command, False, 21600),
        "PreToolUse": hook_entry(".*", guard_command, False, 21600),
    }

    for event_name, entry in additions.items():
        current_entries = hooks.get(event_name)
        if not isinstance(current_entries, list):
            current_entries = []
        hooks[event_name] = _remove_existing_quota_sentry(current_entries) + [entry]

    return merged
