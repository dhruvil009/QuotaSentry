import base64
import hashlib
import ipaddress
import json
import math
import os
import queue
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is process-local.
    fcntl = None


DEFAULT_PROVIDER = "codex"
CLAUDE_PROVIDER = "claude"
PROVIDERS = (DEFAULT_PROVIDER, CLAUDE_PROVIDER)
CODEXBAR_SOURCE = "codexbar"
CODEX_APP_SERVER_SOURCE = "codex-app-server"
CLAUDE_USAGE_API_SOURCE = "claude-usage-api"
AUTO_SOURCE = "auto"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_URL_OVERRIDE_ENV = "QUOTA_SENTRY_CLAUDE_USAGE_URL"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
# Claude's public sign-in client and endpoints, as used by Claude Code 2.1.332.
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_REDIRECT_URL = "https://platform.claude.com/oauth/code/callback"
CLAUDE_TOKEN_URL_OVERRIDE_ENV = "QUOTA_SENTRY_CLAUDE_TOKEN_URL"
# Reading plan usage needs only the profile scope.
CLAUDE_OAUTH_SCOPES = ("user:profile",)
CLAUDE_CREDENTIALS_FILENAME = "claude-credentials.json"
CLAUDE_TOKEN_RENEW_MARGIN_SECONDS = 300
CLAUDE_USAGE_HOLD_FILENAME = "claude-usage.hold"
DEFAULT_CLAUDE_USAGE_TIMEOUT_SECONDS = 15
# Claude Code's own pacing for this endpoint: a reading is reused for 60 s,
# 403/429 hold for Retry-After or 5 minutes (at most an hour), and a rejected
# login is not retried for an hour.
CLAUDE_MIN_POLL_SPACING_SECONDS = 60
CLAUDE_RATE_LIMIT_HOLD_DEFAULT_SECONDS = 300
CLAUDE_RATE_LIMIT_HOLD_MAX_SECONDS = 3600
CLAUDE_AUTH_REJECTED_HOLD_SECONDS = 3600
# Claude names its plan windows; the names carry the durations.
CLAUDE_DEFAULT_WINDOWS = (("five_hour", 300), ("seven_day", 10080))
WEEKLY_MODE_ADVISORY = "advisory"
WEEKLY_MODE_HARD_BLOCK = "hard-block"
DEFAULT_WEEKLY_MODE = WEEKLY_MODE_HARD_BLOCK
DEFAULT_USAGE_SOURCE = AUTO_SOURCE
DEFAULT_CODEXBAR_SOURCE = "cli"
DEFAULT_THRESHOLD_PERCENT = 90
DEFAULT_WINDOW_MINUTES = 300
DEFAULT_WEEKLY_WINDOW_MINUTES = 10080
DEFAULT_WEEKLY_WINDOW_TOLERANCE_MINUTES = 60
DEFAULT_SHORT_TERM_WINDOW_MAX_MINUTES = 1440
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
DEFAULT_DAEMON_START_TIMEOUT_SECONDS = 5
DEFAULT_DAEMON_STOP_TIMEOUT_SECONDS = 5
DEFAULT_HOOK_TIMEOUT_SECONDS = 366 * 24 * 60 * 60
DEFAULT_HOOK_TIMEOUT_MARGIN_SECONDS = 10 * 60
DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS = (
    DEFAULT_HOOK_TIMEOUT_SECONDS - DEFAULT_HOOK_TIMEOUT_MARGIN_SECONDS
)
HOOK_FAIL_CLOSED_MESSAGE = (
    "Quota Sentry: quota remains blocked; refusing to fail open at the hook safety deadline."
)
HOOK_STATE_UNAVAILABLE_MESSAGE = (
    "Quota Sentry: cached quota state is unavailable or stale; refusing to fail open."
)
HOOK_INTERNAL_FAILURE_MESSAGE = (
    "Quota Sentry: internal hook failure; refusing to continue."
)
WINDOW_KIND_SHORT_TERM = "short-term"
WINDOW_KIND_WEEKLY = "weekly"
WINDOW_KIND_ACCOUNT = "account"
WINDOW_KIND_LONG_TERM = "long-term"
WINDOW_KIND_UNKNOWN = "unknown"


Percent = Union[int, float]
_STATE_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class QuotaConfig:
    weekly_mode: str = DEFAULT_WEEKLY_MODE
    weekly_threshold_percent: int = DEFAULT_WEEKLY_THRESHOLD_PERCENT
    exempt_cwds: Tuple[str, ...] = ()
    notify_on_block: bool = True


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: Percent
    window_minutes: Optional[int]
    resets_at: datetime
    kind: str = WINDOW_KIND_UNKNOWN
    limit_id: Optional[str] = None
    limit_name: Optional[str] = None
    source_slot: Optional[str] = None
    is_default_limit: bool = True
    rate_limit_reached_type: Optional[str] = None
    provider: str = DEFAULT_PROVIDER


@dataclass(frozen=True)
class QuotaDecision:
    status: str
    reason: str
    used_percent: Optional[Percent] = None
    window_minutes: Optional[int] = None
    resets_at: Optional[datetime] = None
    blocked_until: Optional[datetime] = None
    fail_open: bool = True
    short_term_window: Optional[QuotaWindow] = None
    weekly_window: Optional[QuotaWindow] = None
    quota_windows: Tuple[QuotaWindow, ...] = ()
    blocked_window: Optional[str] = None
    blocked_limit_id: Optional[str] = None
    weekly_hard_block_enabled: bool = False
    blocked_quota_windows: Tuple[QuotaWindow, ...] = ()
    uncertain_blocking_kinds: Tuple[str, ...] = ()
    source: Optional[str] = None
    source_observed_at: Optional[datetime] = None
    canonical_limit_id: Optional[str] = None
    account_limit_state_known: bool = False
    rate_limit_state_known: bool = False
    spend_control_reached: Optional[bool] = None
    rate_limit_reached_type: Optional[str] = None
    source_context: Optional[str] = None

    @property
    def primary_window(self) -> Optional[QuotaWindow]:
        """Compatibility alias for callers that predate duration-aware windows."""
        return self.short_term_window


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


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


def default_daemon_lock_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.daemon.lock"


def default_refresh_request_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "refresh.request"


def request_refresh(path: Path) -> None:
    try:
        write_bytes_atomic(path, b"refresh\n")
    except OSError:
        pass


def consume_refresh_request(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_bytes_atomic(path: Path, payload: bytes, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


@contextmanager
def _state_write_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_WRITE_LOCK:
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_valid_weekly_mode(value: Any) -> bool:
    return isinstance(value, str) and value in {
        WEEKLY_MODE_ADVISORY,
        WEEKLY_MODE_HARD_BLOCK,
    }


def _valid_weekly_mode(value: Any) -> str:
    if _is_valid_weekly_mode(value):
        return value
    return DEFAULT_WEEKLY_MODE


def _valid_percent(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        percent = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if percent < 1 or percent > 100:
        return default
    return percent


def _valid_exempt_cwds(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        dict.fromkeys(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )


def is_cwd_exempt(cwd: Path, exempt_cwds: Iterable[str]) -> bool:
    try:
        resolved_cwd = cwd.expanduser().resolve()
    except (OSError, RuntimeError):
        return False

    for value in exempt_cwds:
        if not isinstance(value, str):
            continue

        exempt_path = Path(value).expanduser()
        if not exempt_path.is_absolute():
            continue

        try:
            resolved_exempt = exempt_path.resolve()
        except (OSError, RuntimeError):
            continue

        if resolved_cwd == resolved_exempt or resolved_exempt in resolved_cwd.parents:
            return True

    return False


def config_from_payload(payload: Any) -> QuotaConfig:
    if not isinstance(payload, dict):
        return QuotaConfig()
    weekly_mode = payload.get("weeklyMode")
    if "weeklyMode" in payload and not _is_valid_weekly_mode(weekly_mode):
        return QuotaConfig()
    return QuotaConfig(
        weekly_mode=_valid_weekly_mode(weekly_mode),
        weekly_threshold_percent=_valid_percent(
            payload.get("weeklyThresholdPercent"),
            DEFAULT_WEEKLY_THRESHOLD_PERCENT,
        ),
        exempt_cwds=_valid_exempt_cwds(payload.get("exemptCwds")),
        notify_on_block=payload.get("notifyOnBlock") is not False,
    )


def config_to_payload(config: QuotaConfig) -> Dict[str, Any]:
    payload = {
        "weeklyMode": _valid_weekly_mode(config.weekly_mode),
        "weeklyThresholdPercent": _valid_percent(
            config.weekly_threshold_percent,
            DEFAULT_WEEKLY_THRESHOLD_PERCENT,
        ),
    }
    exempt_cwds = _valid_exempt_cwds(list(config.exempt_cwds))
    if exempt_cwds:
        payload["exemptCwds"] = list(exempt_cwds)
    if config.notify_on_block is False:
        payload["notifyOnBlock"] = False
    return payload


def read_config(path: Optional[Path] = None) -> QuotaConfig:
    return read_valid_config(path) or QuotaConfig()


def read_valid_config(path: Optional[Path] = None) -> Optional[QuotaConfig]:
    config_path = path or default_config_path()
    try:
        payload = json.loads(config_path.read_text())
        if not isinstance(payload, dict):
            return None
        if "weeklyMode" in payload and not _is_valid_weekly_mode(payload["weeklyMode"]):
            return None
        threshold = payload.get("weeklyThresholdPercent", DEFAULT_WEEKLY_THRESHOLD_PERCENT)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 100:
            return None
        return config_from_payload(payload)
    except (UnicodeError, ValueError, RecursionError, OSError):
        return None


def write_config(path: Path, config: QuotaConfig) -> None:
    write_json_atomic(path, config_to_payload(config))


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


def _provider_entry(payload: Any, provider: str = DEFAULT_PROVIDER) -> Optional[Dict[str, Any]]:
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("provider") == provider:
            return entry
    return None


def _codex_entry(payload: Any) -> Optional[Dict[str, Any]]:
    return _provider_entry(payload, DEFAULT_PROVIDER)


def _provider_label(provider: str) -> str:
    return "Claude" if provider == CLAUDE_PROVIDER else "Codex"


def _window_candidates(usage: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    normalized = usage.get("windows")
    if isinstance(normalized, list) and normalized:
        for window in normalized:
            if isinstance(window, dict):
                yield window
        return

    # CodexBar and older Quota Sentry payloads only expose positional slots.
    # Preserve the slot as metadata; duration determines policy.
    for source_slot in ("primary", "secondary", "tertiary"):
        window = usage.get(source_slot)
        if not isinstance(window, dict):
            continue
        candidate = dict(window)
        candidate.setdefault("sourceSlot", source_slot)
        candidate.setdefault("isDefaultLimit", True)
        yield candidate


def _window_minutes(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return minutes if minutes > 0 else None


def _window_kind(window_minutes: Optional[int]) -> str:
    if (
        window_minutes is not None
        and abs(window_minutes - DEFAULT_WEEKLY_WINDOW_MINUTES)
        <= DEFAULT_WEEKLY_WINDOW_TOLERANCE_MINUTES
    ):
        return WINDOW_KIND_WEEKLY
    if window_minutes is None:
        return WINDOW_KIND_UNKNOWN
    if window_minutes <= DEFAULT_SHORT_TERM_WINDOW_MAX_MINUTES:
        return WINDOW_KIND_SHORT_TERM
    return WINDOW_KIND_LONG_TERM


def _percent(value: Any) -> Optional[Percent]:
    if value is None or isinstance(value, bool):
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(percent) or percent < 0 or percent > 100:
        return None
    return int(percent) if percent.is_integer() else percent


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _parse_quota_window(
    window: Dict[str, Any], provider: str = DEFAULT_PROVIDER,
) -> Tuple[Optional[QuotaWindow], Optional[str]]:
    used = _percent(window.get("usedPercent"))
    resets_at = parse_timestamp(window.get("resetsAt"))
    window_minutes = _window_minutes(window.get("windowMinutes"))

    if used is None:
        return None, "quota window is missing or has invalid usedPercent"
    if resets_at is None:
        return None, "quota window has missing or invalid resetsAt"

    kind = _window_kind(window_minutes)
    return (
        QuotaWindow(
            name=kind,
            used_percent=used,
            window_minutes=window_minutes,
            resets_at=resets_at,
            kind=kind,
            limit_id=_optional_string(window.get("limitId")),
            limit_name=_optional_string(window.get("limitName")),
            source_slot=_optional_string(window.get("sourceSlot")),
            is_default_limit=window.get("isDefaultLimit") is not False,
            rate_limit_reached_type=_optional_string(window.get("rateLimitReachedType")),
            provider=provider,
        ),
        None,
    )


def _parse_quota_windows(
    usage: Dict[str, Any], provider: str = DEFAULT_PROVIDER,
) -> Tuple[List[QuotaWindow], List[Tuple[str, bool, str]]]:
    windows: List[QuotaWindow] = []
    errors: List[Tuple[str, bool, str]] = []
    identities = set()
    for candidate in _window_candidates(usage):
        window, error = _parse_quota_window(candidate, provider)
        if error or window is None:
            errors.append(
                (
                    _window_kind(_window_minutes(candidate.get("windowMinutes"))),
                    candidate.get("isDefaultLimit") is not False,
                    error or "invalid quota window",
                )
            )
            continue
        identity = (
            window.limit_id,
            window.source_slot,
            window.window_minutes,
            window.resets_at,
            window.used_percent,
        )
        if identity in identities:
            continue
        identities.add(identity)
        windows.append(window)
    return windows, errors


def _backend_limit_state(
    usage: Dict[str, Any],
) -> Tuple[bool, Optional[str], bool, Optional[bool]]:
    rate_state_known = False
    reached_type: Optional[str] = None
    if "rateLimitReachedType" in usage:
        value = usage.get("rateLimitReachedType")
        if value is None:
            rate_state_known = True
        elif _optional_string(value) is not None:
            rate_state_known = True
            reached_type = value
    else:
        for candidate in _window_candidates(usage):
            if candidate.get("isDefaultLimit") is False:
                continue
            if "rateLimitReachedType" not in candidate:
                continue
            value = candidate.get("rateLimitReachedType")
            if value is None:
                rate_state_known = True
            elif _optional_string(value) is not None:
                rate_state_known = True
                reached_type = reached_type or value

    spend_value = usage.get("spendControlReached")
    spend_control_reached = spend_value if isinstance(spend_value, bool) else None
    account_state_known = rate_state_known or spend_control_reached is not None
    return (
        account_state_known,
        reached_type,
        rate_state_known,
        spend_control_reached,
    )


def _window_state(window: Optional[QuotaWindow]) -> Optional[Dict[str, Any]]:
    if window is None:
        return None
    return {
        "kind": window.kind,
        "usedPercent": window.used_percent,
        "windowMinutes": window.window_minutes,
        "resetsAt": format_timestamp(window.resets_at),
        "limitId": window.limit_id,
        "limitName": window.limit_name,
        "sourceSlot": window.source_slot,
        "isDefaultLimit": window.is_default_limit,
        "rateLimitReachedType": window.rate_limit_reached_type,
    }


def _preferred_window(windows: Iterable[QuotaWindow]) -> Optional[QuotaWindow]:
    candidates = list(windows)
    if not candidates:
        return None
    exact_five_hour = [
        window for window in candidates if window.window_minutes == DEFAULT_WINDOW_MINUTES
    ]
    if exact_five_hour:
        return exact_five_hour[0]
    short_term = [window for window in candidates if window.kind == WINDOW_KIND_SHORT_TERM]
    if short_term:
        return max(short_term, key=lambda window: window.window_minutes or 0)
    weekly = [window for window in candidates if window.kind == WINDOW_KIND_WEEKLY]
    if weekly:
        return weekly[0]
    return candidates[0]


def _window_reason(window: QuotaWindow) -> str:
    if window.kind == WINDOW_KIND_ACCOUNT and window.rate_limit_reached_type:
        return (
            "Codex reports an active backend quota block "
            f"({window.rate_limit_reached_type})"
        )
    duration = (
        f"{window.window_minutes}-minute"
        if window.window_minutes is not None
        else "unknown-duration"
    )
    bucket = f" ({window.limit_id})" if window.limit_id else ""
    label = _provider_label(window.provider)
    return f"{window.used_percent}% of the {duration} {label} quota{bucket} is used"


def _blocked_decision(
    window: QuotaWindow,
    reset_buffer_seconds: int,
    blocked_window: str,
    short_term_window: Optional[QuotaWindow],
    weekly_window: Optional[QuotaWindow],
    quota_windows: Iterable[QuotaWindow],
    weekly_hard_block_enabled: bool,
    blocked_quota_windows: Iterable[QuotaWindow],
    uncertain_blocking_kinds: Iterable[str] = (),
    source: Optional[str] = None,
    source_observed_at: Optional[datetime] = None,
    canonical_limit_id: Optional[str] = None,
    account_limit_state_known: bool = False,
    rate_limit_state_known: bool = False,
    spend_control_reached: Optional[bool] = None,
    rate_limit_reached_type: Optional[str] = None,
    source_context: Optional[str] = None,
) -> QuotaDecision:
    blocked_until = window.resets_at + timedelta(seconds=reset_buffer_seconds)
    return QuotaDecision(
        status="blocked",
        reason=_window_reason(window),
        used_percent=window.used_percent,
        window_minutes=window.window_minutes,
        resets_at=window.resets_at,
        blocked_until=blocked_until,
        fail_open=False,
        short_term_window=short_term_window,
        weekly_window=weekly_window,
        quota_windows=tuple(quota_windows),
        blocked_window=blocked_window,
        blocked_limit_id=window.limit_id,
        weekly_hard_block_enabled=weekly_hard_block_enabled,
        blocked_quota_windows=tuple(blocked_quota_windows),
        uncertain_blocking_kinds=tuple(uncertain_blocking_kinds),
        source=source,
        source_observed_at=source_observed_at,
        source_context=source_context,
        canonical_limit_id=canonical_limit_id,
        account_limit_state_known=account_limit_state_known,
        rate_limit_state_known=rate_limit_state_known,
        spend_control_reached=spend_control_reached,
        rate_limit_reached_type=rate_limit_reached_type,
    )


def parse_codex_usage(
    payload: Any,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    now: Optional[datetime] = None,
    quota_config: Optional[QuotaConfig] = None,
    provider: str = DEFAULT_PROVIDER,
) -> QuotaDecision:
    current_time = now or utc_now()
    config = quota_config or QuotaConfig()
    weekly_hard_block_enabled = config.weekly_mode == WEEKLY_MODE_HARD_BLOCK
    entry = _provider_entry(payload, provider)
    if not entry:
        return QuotaDecision(
            status="unknown",
            reason="quota source returned no provider entries",
            weekly_hard_block_enabled=weekly_hard_block_enabled,
        )

    source = _optional_string(entry.get("source"))

    error = entry.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "unknown provider error"
        return QuotaDecision(
            status="unknown",
            reason=str(message),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            source=source,
        )

    usage = entry.get("usage")
    if not isinstance(usage, dict):
        return QuotaDecision(
            status="unknown",
            reason="quota source returned no usage object",
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            source=source,
        )

    source_observed_at = parse_timestamp(usage.get("updatedAt"))
    canonical_limit_id = _optional_string(usage.get("activeLimitId"))
    (
        account_limit_state_known,
        rate_limit_reached_type,
        rate_limit_state_known,
        spend_control_reached,
    ) = _backend_limit_state(usage)
    decision_context = {
        "source": source,
        "source_context": _optional_string(entry.get("sourceContext")),
        "source_observed_at": source_observed_at,
        "canonical_limit_id": canonical_limit_id,
        "account_limit_state_known": account_limit_state_known,
        "rate_limit_state_known": rate_limit_state_known,
        "spend_control_reached": spend_control_reached,
        "rate_limit_reached_type": rate_limit_reached_type,
    }
    quota_windows, window_errors = _parse_quota_windows(usage, provider)
    blocking_policy_error_entries = [
        (kind, error)
        for kind, is_default_limit, error in window_errors
        if is_default_limit
        and (
            kind == WINDOW_KIND_SHORT_TERM
            or (kind == WINDOW_KIND_WEEKLY and weekly_hard_block_enabled)
            or kind in {WINDOW_KIND_LONG_TERM, WINDOW_KIND_UNKNOWN}
        )
    ]
    uncertain_blocking_kinds = tuple(
        sorted({kind for kind, _error in blocking_policy_error_entries})
    )
    default_windows = [window for window in quota_windows if window.is_default_limit]
    short_term_windows = [
        window for window in default_windows if window.kind == WINDOW_KIND_SHORT_TERM
    ]
    weekly_windows = [
        window for window in default_windows if window.kind == WINDOW_KIND_WEEKLY
    ]
    short_term_window = _preferred_window(short_term_windows)
    weekly_window = _preferred_window(weekly_windows)

    account_windows = []
    if rate_limit_reached_type is not None or spend_control_reached is True:
        future_resets = [
            window.resets_at
            for window in default_windows
            if window.resets_at > current_time
        ]
        account_reset = (
            max(future_resets)
            if future_resets
            else current_time + timedelta(seconds=DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)
        )
        reasons = []
        if rate_limit_reached_type is not None:
            reasons.append(rate_limit_reached_type)
        if spend_control_reached is True:
            reasons.append("spend_control_reached")
        account_windows = [QuotaWindow(
            name=WINDOW_KIND_ACCOUNT, used_percent=100, window_minutes=None,
            resets_at=account_reset, kind=WINDOW_KIND_ACCOUNT,
            limit_id=canonical_limit_id, rate_limit_reached_type=reason,
            provider=provider,
        ) for reason in reasons]

    blocked_candidates: List[Tuple[str, QuotaWindow]] = []
    threshold_reached_windows: List[QuotaWindow] = []
    for window in short_term_windows:
        if window.used_percent >= threshold_percent:
            threshold_reached_windows.append(window)
            if window.resets_at > current_time:
                blocked_candidates.append((WINDOW_KIND_SHORT_TERM, window))
    if weekly_hard_block_enabled:
        for window in weekly_windows:
            if window.used_percent >= config.weekly_threshold_percent:
                threshold_reached_windows.append(window)
                if window.resets_at > current_time:
                    blocked_candidates.append((WINDOW_KIND_WEEKLY, window))
    for window in default_windows:
        if (
            window.kind in {WINDOW_KIND_LONG_TERM, WINDOW_KIND_UNKNOWN}
            and window.used_percent >= 100
        ):
            threshold_reached_windows.append(window)
            if window.resets_at > current_time:
                blocked_candidates.append((window.kind, window))
    blocked_candidates.extend((WINDOW_KIND_ACCOUNT, window) for window in account_windows)

    if blocked_candidates:
        blocked_window, blocked_quota_window = max(
            blocked_candidates,
            key=lambda candidate: candidate[1].resets_at,
        )
        return _blocked_decision(
            blocked_quota_window,
            reset_buffer_seconds=reset_buffer_seconds,
            blocked_window=blocked_window,
            short_term_window=short_term_window,
            weekly_window=weekly_window,
            quota_windows=(*quota_windows, *account_windows),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            blocked_quota_windows=(window for _kind, window in blocked_candidates),
            uncertain_blocking_kinds=uncertain_blocking_kinds,
            **decision_context,
        )

    if not quota_windows:
        reason = window_errors[0][2] if window_errors else "quota source returned no quota window"
        return QuotaDecision(
            status="unknown", reason=reason,
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            uncertain_blocking_kinds=uncertain_blocking_kinds,
            **decision_context,
        )

    if blocking_policy_error_entries:
        return QuotaDecision(
            status="unknown",
            reason=blocking_policy_error_entries[0][1],
            short_term_window=short_term_window,
            weekly_window=weekly_window,
            quota_windows=tuple(quota_windows),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            uncertain_blocking_kinds=uncertain_blocking_kinds,
            **decision_context,
        )

    unknown_window = next(
        (window for window in default_windows if window.kind == WINDOW_KIND_UNKNOWN),
        None,
    )
    if unknown_window is not None:
        return QuotaDecision(
            status="unknown",
            reason=(
                "canonical Codex quota window has no usable duration; "
                "cannot select an enforcement policy"
            ),
            used_percent=unknown_window.used_percent,
            window_minutes=unknown_window.window_minutes,
            resets_at=unknown_window.resets_at,
            short_term_window=short_term_window,
            weekly_window=weekly_window,
            quota_windows=tuple(quota_windows),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            **decision_context,
        )

    if not default_windows:
        return QuotaDecision(
            status="unknown",
            reason=(
                "quota source returned quota windows but did not identify the canonical "
                "Codex limit"
            ),
            used_percent=quota_windows[0].used_percent,
            window_minutes=quota_windows[0].window_minutes,
            resets_at=quota_windows[0].resets_at,
            fail_open=True,
            quota_windows=tuple(quota_windows),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            **decision_context,
        )

    representative = _preferred_window(default_windows)
    if representative is None:
        return QuotaDecision(
            status="unknown",
            reason="quota source returned no canonical quota window",
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            **decision_context,
        )

    recognized_windows = short_term_windows + weekly_windows
    fail_open = not recognized_windows
    if threshold_reached_windows:
        return QuotaDecision(
            status="unknown",
            reason=(
                "quota remains above its enforcement threshold after the reported reset time; "
                "awaiting authoritative refreshed quota data"
            ),
            used_percent=representative.used_percent,
            window_minutes=representative.window_minutes,
            resets_at=representative.resets_at,
            short_term_window=short_term_window,
            weekly_window=weekly_window,
            quota_windows=tuple(quota_windows),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            **decision_context,
        )
    if fail_open:
        reason = (
            "quota source returned only unfamiliar long-term windows; "
            "enforcement failed open"
        )
    elif not short_term_windows and weekly_windows and not weekly_hard_block_enabled:
        reason = "weekly Codex quota is advisory"
    else:
        reason = _window_reason(representative)

    return QuotaDecision(
        status="open",
        reason=reason,
        used_percent=representative.used_percent,
        window_minutes=representative.window_minutes,
        resets_at=representative.resets_at,
        fail_open=fail_open,
        short_term_window=short_term_window,
        weekly_window=weekly_window,
        quota_windows=tuple(quota_windows),
        weekly_hard_block_enabled=weekly_hard_block_enabled,
        **decision_context,
    )


def parse_codexbar_usage(
    payload: Any,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    now: Optional[datetime] = None,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    quota_config: Optional[QuotaConfig] = None,
) -> QuotaDecision:
    # Kept as a compatibility entry point for callers from the CodexBar-only era.
    # Window classification is now duration-aware; window_minutes is intentionally ignored.
    del window_minutes
    return parse_codex_usage(
        payload,
        threshold_percent=threshold_percent,
        reset_buffer_seconds=reset_buffer_seconds,
        now=now,
        quota_config=quota_config,
    )


def _blocked_latch_from_window(
    window: QuotaWindow,
    *,
    blocked_until: datetime,
    confirmed_at: datetime,
    source: Optional[str],
    source_observed_at: Optional[datetime],
    source_context: Optional[str] = None,
    threshold_percent: int = 100,
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    previous_confirmed_at = previous.get("confirmedAt") if isinstance(previous, dict) else None
    return {
        "kind": window.kind,
        "limitId": window.limit_id,
        "windowMinutes": window.window_minutes,
        "usedPercent": window.used_percent,
        "resetsAt": format_timestamp(window.resets_at),
        "blockedUntil": format_timestamp(blocked_until),
        "reason": _window_reason(window),
        "confirmedAt": previous_confirmed_at or format_timestamp(confirmed_at),
        "lastConfirmedAt": format_timestamp(confirmed_at),
        "source": source,
        "sourceObservedAt": format_timestamp(source_observed_at),
        "sourceContext": source_context,
        "rateLimitReachedType": window.rate_limit_reached_type,
        "thresholdPercent": threshold_percent,
    }


def _normalized_source(source: Optional[str]) -> Optional[str]:
    if source == CODEX_APP_SERVER_SOURCE:
        return CODEX_APP_SERVER_SOURCE
    if source == CLAUDE_USAGE_API_SOURCE:
        return CLAUDE_USAGE_API_SOURCE
    if source in {CODEXBAR_SOURCE, "codex-cli"}:
        return CODEXBAR_SOURCE
    return source


def _source_authority(source: Optional[str]) -> int:
    normalized = _normalized_source(source)
    if normalized in {CODEX_APP_SERVER_SOURCE, CLAUDE_USAGE_API_SOURCE}:
        return 2
    if normalized == CODEXBAR_SOURCE:
        return 1
    return 0


def _observation_can_supersede_latch(
    previous: Dict[str, Any],
    decision: QuotaDecision,
    candidates: Iterable[QuotaWindow],
) -> bool:
    previous_context = _optional_string(previous.get("sourceContext"))
    if previous_context is not None and decision.source_context != previous_context:
        return False
    previous_source = _optional_string(previous.get("source"))
    current_source = decision.source
    previous_authority = _source_authority(previous_source)
    current_authority = _source_authority(current_source)
    if previous_authority == 0:
        if current_authority < _source_authority(CODEX_APP_SERVER_SOURCE):
            return False
    elif current_authority < previous_authority:
        return False

    previous_observed_at = parse_timestamp(previous.get("sourceObservedAt"))
    if previous_observed_at is not None and (
        decision.source_observed_at is None
        or decision.source_observed_at <= previous_observed_at
    ):
        return False

    previous_limit_id = _optional_string(previous.get("limitId"))
    if previous_limit_id is None:
        return True
    candidate_list = list(candidates)
    if candidate_list:
        return any(window.limit_id == previous_limit_id for window in candidate_list)
    return decision.canonical_limit_id == previous_limit_id


def _previous_enforcement_windows(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    enforcement = state.get("enforcement")
    enforcement_blocked = isinstance(enforcement, dict) and enforcement.get("status") == "blocked"
    if enforcement_blocked:
        windows = enforcement.get("windows")
        if isinstance(windows, list):
            retained = [
                dict(window)
                for window in windows
                if isinstance(window, dict)
                and _optional_string(window.get("kind")) is not None
            ]
            if retained:
                return retained

    if state.get("status") != "blocked" and not enforcement_blocked:
        return []
    kind = state.get("blockedWindow")
    if not isinstance(kind, str):
        kind = _window_kind(_window_minutes(state.get("windowMinutes")))
    return [
        {
            "kind": kind,
            "limitId": state.get("blockedLimitId"),
            "windowMinutes": state.get("windowMinutes"),
            "usedPercent": state.get("usedPercent"),
            "resetsAt": state.get("resetsAt"),
            "blockedUntil": state.get("blockedUntil"),
            "reason": state.get("reason") or "previously confirmed Codex quota block",
            "confirmedAt": state.get("updatedAt"),
            "lastConfirmedAt": state.get("updatedAt"),
            "source": state.get("source"),
            "sourceObservedAt": state.get("sourceObservedAt"),
            "sourceContext": state.get("sourceContext"),
            "rateLimitReachedType": state.get("rateLimitReachedType"),
        }
    ]


def _latch_identity(window: Dict[str, Any]) -> Tuple[Any, ...]:
    kind = _optional_string(window.get("kind"))
    condition = None
    if kind == WINDOW_KIND_ACCOUNT:
        condition = ("spend" if window.get("rateLimitReachedType") == "spend_control_reached"
                     else "rate")
    return (kind, _optional_string(window.get("limitId")),
            _window_minutes(window.get("windowMinutes")),
            _optional_string(window.get("sourceContext")), condition)


def _retained_policy_config(
    state: Dict[str, Any], windows: Iterable[Dict[str, Any]] = (),
) -> QuotaConfig:
    snapshot = state.get("observation")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    retained = [*_previous_enforcement_windows(state), *windows]
    thresholds = [window.get("thresholdPercent") for window in retained
                  if window.get("kind") == WINDOW_KIND_WEEKLY]
    thresholds.append(snapshot.get("weeklyThresholdPercent"))
    valid_thresholds = [value for value in thresholds
                        if type(value) is int and 1 <= value <= 100]
    weekly_blocked = any(window.get("kind") == WINDOW_KIND_WEEKLY for window in retained)
    mode = (WEEKLY_MODE_ADVISORY if snapshot.get("weeklyHardBlockEnabled") is False
            and not weekly_blocked else WEEKLY_MODE_HARD_BLOCK)
    return QuotaConfig(weekly_mode=mode,
                       weekly_threshold_percent=min(valid_thresholds)
                       if valid_thresholds else DEFAULT_WEEKLY_THRESHOLD_PERCENT)


def _merged_enforcement_windows(
    previous_state: Dict[str, Any],
    decision: QuotaDecision,
    *,
    current_time: datetime,
    threshold_percent: int,
    weekly_threshold_percent: int,
) -> List[Dict[str, Any]]:
    active = {_latch_identity(window): window
              for window in _previous_enforcement_windows(previous_state)}
    uncertain_kinds = set(decision.uncertain_blocking_kinds)
    observations: Dict[str, List[QuotaWindow]] = {
        WINDOW_KIND_SHORT_TERM: [],
        WINDOW_KIND_WEEKLY: [],
        WINDOW_KIND_ACCOUNT: [],
        WINDOW_KIND_LONG_TERM: [],
        WINDOW_KIND_UNKNOWN: [],
    }
    for window in decision.quota_windows:
        if window.is_default_limit and window.kind in observations:
            observations[window.kind].append(window)

    thresholds = {WINDOW_KIND_SHORT_TERM: threshold_percent,
                  WINDOW_KIND_WEEKLY: weekly_threshold_percent}
    for key, previous in list(active.items()):
        kind = previous["kind"]
        threshold = thresholds.get(kind, 100)
        if kind == WINDOW_KIND_WEEKLY and not decision.weekly_hard_block_enabled:
            active.pop(key, None)
            continue
        if kind == WINDOW_KIND_ACCOUNT:
            explicitly_cleared = (
                decision.spend_control_reached is False
                if previous.get("rateLimitReachedType") == "spend_control_reached"
                else decision.rate_limit_state_known and decision.rate_limit_reached_type is None
            )
            if explicitly_cleared and _observation_can_supersede_latch(previous, decision, ()):
                active.pop(key, None)
            continue
        duration = _window_minutes(previous.get("windowMinutes"))
        limit_id = _optional_string(previous.get("limitId"))
        candidates = [window for window in observations.get(kind, ())
                      if window.window_minutes == duration
                      and (limit_id is None or window.limit_id == limit_id)]
        if kind not in uncertain_kinds and candidates and all(
            window.used_percent < threshold for window in candidates
        ) and _observation_can_supersede_latch(previous, decision, candidates):
            active.pop(key, None)

    reset_buffer_seconds = 0.0
    if decision.resets_at is not None and decision.blocked_until is not None:
        reset_buffer_seconds = max(
            0.0,
            (decision.blocked_until - decision.resets_at).total_seconds(),
        )
    for window in decision.blocked_quota_windows:
        blocked_until = window.resets_at + timedelta(seconds=reset_buffer_seconds)
        key = _latch_identity({"kind": window.kind, "limitId": window.limit_id,
                               "windowMinutes": window.window_minutes,
                               "sourceContext": decision.source_context,
                               "rateLimitReachedType": window.rate_limit_reached_type})
        previous = active.get(key)
        if previous is not None and not _observation_can_supersede_latch(
            previous,
            decision,
            (window,),
        ):
            continue
        candidate = _blocked_latch_from_window(
            window,
            blocked_until=blocked_until,
            confirmed_at=current_time,
            source=decision.source,
            source_observed_at=decision.source_observed_at,
            source_context=decision.source_context,
            threshold_percent=thresholds.get(window.kind, 100),
            previous=previous,
        )
        existing_until = parse_timestamp(previous.get("blockedUntil")) if previous else None
        if existing_until is not None and blocked_until < existing_until:
            candidate["blockedUntil"] = format_timestamp(existing_until)
        active[key] = candidate

    return sorted(active.values(), key=lambda window: str(_latch_identity(window)))


def state_from_decision(
    decision: QuotaDecision,
    now: Optional[datetime] = None,
    *,
    previous_state: Optional[Dict[str, Any]] = None,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    weekly_threshold_percent: int = DEFAULT_WEEKLY_THRESHOLD_PERCENT,
    provider: str = DEFAULT_PROVIDER,
) -> Dict[str, Any]:
    current_time = now or utc_now()
    short_term_state = _window_state(decision.short_term_window)
    payload = {
        "schemaVersion": 5,
        "status": decision.status,
        "sourceStatus": decision.status,
        "sourceReason": decision.reason,
        "sourceFailOpen": decision.fail_open,
        "reason": decision.reason,
        "usedPercent": decision.used_percent,
        "windowMinutes": decision.window_minutes,
        "resetsAt": format_timestamp(decision.resets_at),
        "blockedUntil": format_timestamp(decision.blocked_until),
        "failOpen": decision.fail_open,
        "updatedAt": format_timestamp(current_time),
        "shortTerm": short_term_state,
        "primary": short_term_state,
        "weekly": _window_state(decision.weekly_window),
        "windows": [_window_state(window) for window in decision.quota_windows],
        "blockedWindow": decision.blocked_window,
        "blockedLimitId": decision.blocked_limit_id,
        "weeklyHardBlockEnabled": decision.weekly_hard_block_enabled,
        "source": decision.source,
        "sourceObservedAt": format_timestamp(decision.source_observed_at),
        "sourceContext": decision.source_context,
        "canonicalLimitId": decision.canonical_limit_id,
        "accountLimitStateKnown": decision.account_limit_state_known,
        "rateLimitStateKnown": decision.rate_limit_state_known,
        "spendControlReached": decision.spend_control_reached,
        "rateLimitReachedType": decision.rate_limit_reached_type,
        "observation": {
            "version": 1,
            "status": decision.status,
            "source": decision.source,
            "sourceContext": decision.source_context,
            "observedAt": format_timestamp(decision.source_observed_at),
            "canonicalLimitId": decision.canonical_limit_id,
            "windows": [_window_state(window) for window in decision.quota_windows
                        if window.kind != WINDOW_KIND_ACCOUNT],
            "uncertainBlockingKinds": list(decision.uncertain_blocking_kinds),
            "rateLimitStateKnown": decision.rate_limit_state_known,
            "rateLimitReachedType": decision.rate_limit_reached_type,
            "spendControlReached": decision.spend_control_reached,
            "weeklyHardBlockEnabled": decision.weekly_hard_block_enabled,
            "thresholdPercent": threshold_percent,
            "weeklyThresholdPercent": weekly_threshold_percent,
        },
    }
    # Codex state keeps its original shape; other providers record themselves.
    if provider != DEFAULT_PROVIDER:
        payload["provider"] = provider

    enforcement_windows = _merged_enforcement_windows(
        previous_state or {},
        decision,
        current_time=current_time,
        threshold_percent=threshold_percent,
        weekly_threshold_percent=weekly_threshold_percent,
    )
    if not enforcement_windows:
        payload["enforcement"] = {"status": "open", "windows": []}
        return payload

    selected = max(
        enforcement_windows,
        key=lambda window: parse_timestamp(window.get("blockedUntil"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    payload.update(
        {
            "status": "blocked",
            "reason": selected.get("reason"),
            "usedPercent": selected.get("usedPercent"),
            "windowMinutes": selected.get("windowMinutes"),
            "resetsAt": selected.get("resetsAt"),
            "blockedUntil": selected.get("blockedUntil"),
            "failOpen": False,
            "blockedWindow": selected.get("kind"),
            "blockedLimitId": selected.get("limitId"),
            "rateLimitReachedType": selected.get("rateLimitReachedType"),
            "enforcement": {
                "status": "blocked",
                "blockedUntil": selected.get("blockedUntil"),
                "windows": enforcement_windows,
            },
        }
    )
    return payload


def write_state(
    path: Path,
    decision: QuotaDecision,
    now: Optional[datetime] = None,
    *,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    weekly_threshold_percent: int = DEFAULT_WEEKLY_THRESHOLD_PERCENT,
    provider: str = DEFAULT_PROVIDER,
) -> None:
    with _state_write_lock(path):
        previous_state = read_state(path)
        payload = state_from_decision(
            decision,
            now=now,
            previous_state=previous_state,
            threshold_percent=threshold_percent,
            weekly_threshold_percent=weekly_threshold_percent,
            provider=provider,
        )
        write_json_atomic(path, payload)


def read_state(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
        return payload if isinstance(payload, dict) else {}
    except (UnicodeError, ValueError, RecursionError, OSError):
        return {}


def cached_observation(
    state: Dict[str, Any],
    *,
    now: datetime,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
    quota_config: Optional[QuotaConfig] = None,
    threshold_percent: Optional[int] = None,
) -> Optional[QuotaDecision]:
    """Recover a source decision, not an effective open flag, for latch merging."""
    provider = state.get("provider", DEFAULT_PROVIDER)
    if not isinstance(provider, str) or provider not in PROVIDERS:
        return None
    snapshot = state.get("observation")
    if (type(state.get("schemaVersion")) is not int or state.get("schemaVersion") != 5
            or not isinstance(snapshot, dict)
            or type(snapshot.get("version")) is not int or snapshot.get("version") != 1
            or not isinstance(snapshot.get("status"), str)
            or snapshot.get("status") not in {"open", "blocked", "unknown"}
            or not state_is_fresh(state, now=now, max_state_age_seconds=max_state_age_seconds)):
        return None
    source = snapshot.get("source")
    observed_at = parse_timestamp(snapshot.get("observedAt"))
    if (not isinstance(source, str) or _source_authority(source) == 0
            or observed_at is None or observed_at > now
            or now - observed_at > timedelta(seconds=max_state_age_seconds)):
        return None
    context = snapshot.get("sourceContext")
    if context is not None and (not isinstance(context, str) or not context):
        return None
    windows = snapshot.get("windows")
    uncertain = snapshot.get("uncertainBlockingKinds")
    if (not isinstance(windows, list) or not all(isinstance(w, dict) for w in windows)
            or not isinstance(uncertain, list)
            or not all(isinstance(kind, str) and kind in {
                WINDOW_KIND_SHORT_TERM, WINDOW_KIND_WEEKLY, WINDOW_KIND_ACCOUNT,
                WINDOW_KIND_LONG_TERM, WINDOW_KIND_UNKNOWN} for kind in uncertain)):
        return None
    for window in windows:
        parsed, error = _parse_quota_window(window, provider)
        if error or parsed is None or parsed.kind != window.get("kind"):
            return None
        if not isinstance(window.get("isDefaultLimit"), bool):
            return None
    for key in ("thresholdPercent", "weeklyThresholdPercent"):
        value = snapshot.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            return None
    if (not isinstance(snapshot.get("weeklyHardBlockEnabled"), bool)
            or not isinstance(snapshot.get("rateLimitStateKnown"), bool)
            or (snapshot.get("spendControlReached") is not None
                and not isinstance(snapshot.get("spendControlReached"), bool))):
        return None
    if (state.get("source") != source or state.get("sourceContext") != context
            or state.get("sourceObservedAt") != snapshot.get("observedAt")
            or state.get("sourceStatus") != snapshot.get("status")):
        return None
    canonical = snapshot.get("canonicalLimitId")
    if canonical is not None and (not isinstance(canonical, str) or not canonical):
        return None
    usage = {"updatedAt": format_timestamp(observed_at), "activeLimitId": canonical,
             "windows": windows}
    if snapshot["rateLimitStateKnown"]:
        reached = snapshot.get("rateLimitReachedType")
        if reached is not None and (not isinstance(reached, str) or not reached):
            return None
        usage["rateLimitReachedType"] = reached
    if snapshot.get("spendControlReached") is not None:
        usage["spendControlReached"] = snapshot["spendControlReached"]
    payload = [{"provider": provider, "source": source,
                "sourceContext": context, "usage": usage}]
    try:
        decision = parse_codex_usage(
            payload,
            now=observed_at,
            threshold_percent=snapshot["thresholdPercent"],
            quota_config=QuotaConfig(
                weekly_mode=(WEEKLY_MODE_HARD_BLOCK if snapshot["weeklyHardBlockEnabled"]
                             else WEEKLY_MODE_ADVISORY),
                weekly_threshold_percent=snapshot["weeklyThresholdPercent"],
            ),
            provider=provider,
        )
        if decision.status != snapshot["status"]:
            return None
        if quota_config is not None or threshold_percent is not None:
            decision = parse_codex_usage(
                payload, now=observed_at,
                threshold_percent=(min(threshold_percent, snapshot["thresholdPercent"])
                                   if threshold_percent is not None
                                   else snapshot["thresholdPercent"]),
                quota_config=quota_config or QuotaConfig(
                    weekly_mode=(WEEKLY_MODE_HARD_BLOCK if snapshot["weeklyHardBlockEnabled"]
                                 else WEEKLY_MODE_ADVISORY),
                    weekly_threshold_percent=snapshot["weeklyThresholdPercent"]),
                provider=provider,
            )
    except (TypeError, ValueError, OverflowError):
        return None
    # Normalized window metadata contains nulls that were absent upstream.
    # Preserve the producer's explicit backend-knowledge flags, not those nulls.
    return replace(
        decision, uncertain_blocking_kinds=tuple(uncertain),
        rate_limit_state_known=snapshot["rateLimitStateKnown"],
        account_limit_state_known=(snapshot["rateLimitStateKnown"]
                                   or snapshot.get("spendControlReached") is not None),
    )


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
        return current_time + timedelta(seconds=DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)
    del max_state_age_seconds
    if blocked_until <= current_time:
        return current_time + timedelta(seconds=DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)
    return blocked_until


def state_is_fresh(
    state: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
) -> bool:
    updated_at = parse_timestamp(state.get("updatedAt"))
    if updated_at is None:
        return False
    age = (now or utc_now()) - updated_at
    return timedelta(0) <= age <= timedelta(seconds=max_state_age_seconds)


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


def _app_server_window_to_usage(
    window: Any,
    *,
    source_slot: str,
    limit_id: Optional[str],
    limit_name: Optional[str],
    is_default_limit: bool,
    rate_limit_reached_type: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not isinstance(window, dict):
        return None
    return {
        "usedPercent": window.get("usedPercent"),
        "windowMinutes": window.get("windowDurationMins"),
        "resetsAt": _format_unix_timestamp(window.get("resetsAt")),
        "sourceSlot": source_slot,
        "limitId": limit_id,
        "limitName": limit_name,
        "isDefaultLimit": is_default_limit,
        "rateLimitReachedType": rate_limit_reached_type,
    }


def _snapshot_has_windows(snapshot: Any) -> bool:
    return isinstance(snapshot, dict) and any(
        isinstance(snapshot.get(source_slot), dict)
        for source_slot in ("primary", "secondary", "tertiary")
    )


def _snapshot_window_fingerprint(snapshot: Any) -> Tuple[Any, ...]:
    if not isinstance(snapshot, dict):
        return ()
    return tuple(
        json.dumps(snapshot.get(source_slot), sort_keys=True, separators=(",", ":"))
        if isinstance(snapshot.get(source_slot), dict)
        else None
        for source_slot in ("primary", "secondary", "tertiary")
    )


def _app_server_snapshot_windows(
    snapshot: Dict[str, Any],
    *,
    fallback_limit_id: Optional[str],
    is_default_limit: bool,
) -> List[Dict[str, Any]]:
    limit_id = _optional_string(snapshot.get("limitId")) or fallback_limit_id
    limit_name = _optional_string(snapshot.get("limitName"))
    reached_type = _optional_string(snapshot.get("rateLimitReachedType"))
    windows: List[Dict[str, Any]] = []
    for source_slot in ("primary", "secondary", "tertiary"):
        mapped = _app_server_window_to_usage(
            snapshot.get(source_slot),
            source_slot=source_slot,
            limit_id=limit_id,
            limit_name=limit_name,
            is_default_limit=is_default_limit,
            rate_limit_reached_type=reached_type,
        )
        if mapped is not None:
            if "rateLimitReachedType" not in snapshot:
                mapped.pop("rateLimitReachedType", None)
            windows.append(mapped)
    return windows


def codex_app_server_rate_limits_to_usage(
    result: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    rate_limits = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    by_limit_id = result.get("rateLimitsByLimitId") if isinstance(result, dict) else None
    if not isinstance(by_limit_id, dict):
        by_limit_id = {}

    default_snapshot = (
        rate_limits
        if _snapshot_has_windows(rate_limits)
        or any(
            key in rate_limits
            for key in (
                "limitId",
                "rateLimitReachedType",
                "spendControlReached",
            )
        )
        else {}
    )
    default_limit_id = _optional_string(default_snapshot.get("limitId"))
    snapshot_items = [
        (mapped_limit_id, snapshot)
        for mapped_limit_id, snapshot in by_limit_id.items()
        if isinstance(snapshot, dict)
    ]
    fallback_item: Optional[Tuple[str, Dict[str, Any]]] = None
    if isinstance(by_limit_id.get("codex"), dict):
        fallback_item = ("codex", by_limit_id["codex"])
    elif len(snapshot_items) == 1:
        fallback_item = snapshot_items[0]
    if not default_snapshot and fallback_item is not None:
        fallback_id, candidate = fallback_item
        if isinstance(candidate, dict):
            default_snapshot = candidate
            default_limit_id = _optional_string(candidate.get("limitId")) or fallback_id

    default_fingerprint = _snapshot_window_fingerprint(default_snapshot)
    if default_snapshot and default_limit_id is None:
        for mapped_limit_id, snapshot in by_limit_id.items():
            if (
                isinstance(snapshot, dict)
                and _snapshot_window_fingerprint(snapshot) == default_fingerprint
            ):
                default_limit_id = (
                    _optional_string(snapshot.get("limitId"))
                    or _optional_string(mapped_limit_id)
                )
                break
    windows = _app_server_snapshot_windows(
        default_snapshot,
        fallback_limit_id=default_limit_id,
        is_default_limit=True,
    )

    for mapped_limit_id, snapshot in by_limit_id.items():
        if not isinstance(snapshot, dict):
            continue
        snapshot_limit_id = _optional_string(snapshot.get("limitId")) or _optional_string(mapped_limit_id)
        if (
            snapshot_limit_id == default_limit_id
            or (
                default_fingerprint
                and _snapshot_window_fingerprint(snapshot) == default_fingerprint
            )
        ):
            continue
        windows.extend(
            _app_server_snapshot_windows(
                snapshot,
                fallback_limit_id=snapshot_limit_id,
                is_default_limit=False,
            )
        )

    plan_type = (
        result.get("planType")
        if isinstance(result, dict)
        else None
    ) or default_snapshot.get("planType")

    usage: Dict[str, Any] = {
        "updatedAt": format_timestamp(now or utc_now()),
        "loginMethod": plan_type,
        "activeLimitId": default_limit_id,
        "windows": windows,
    }
    if "rateLimitReachedType" in default_snapshot:
        usage["rateLimitReachedType"] = default_snapshot.get("rateLimitReachedType")
    if "spendControlReached" in default_snapshot:
        usage["spendControlReached"] = default_snapshot.get("spendControlReached")
    for window in windows:
        if not window.get("isDefaultLimit"):
            continue
        source_slot = window.get("sourceSlot")
        if isinstance(source_slot, str):
            usage[source_slot] = window

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
    if source in {AUTO_SOURCE, CODEX_APP_SERVER_SOURCE}:
        return fetch_codex_app_server_usage()
    if source == CODEXBAR_SOURCE:
        return fetch_codexbar_usage()
    raise ValueError(f"unsupported quota source: {source}")


def claude_usage_to_usage(result: Any, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Normalize Anthropic's plan-usage response into Quota Sentry's usage payload."""
    if not isinstance(result, dict):
        raise ValueError("Claude usage response was not a JSON object")
    observed = format_timestamp(now or utc_now())
    defaults = dict(CLAUDE_DEFAULT_WINDOWS)
    windows: List[Dict[str, Any]] = []
    for name, value in result.items():
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        if name in defaults:
            minutes, is_default = defaults[name], True
        elif name.startswith("seven_day_"):
            # Model- or surface-scoped weekly rows never gate all Claude work.
            minutes, is_default = DEFAULT_WEEKLY_WINDOW_MINUTES, False
        else:
            continue
        resets_at = value.get("resets_at")
        if resets_at is None:
            # Claude reports no reset time for a window that has not started;
            # its utilization is still the current reading.
            resets_at = observed
        windows.append({
            "usedPercent": value.get("utilization"),
            "windowMinutes": minutes,
            "resetsAt": resets_at,
            "sourceSlot": name,
            "limitId": name,
            "isDefaultLimit": is_default,
        })
    return [{
        "provider": CLAUDE_PROVIDER,
        "source": CLAUDE_USAGE_API_SOURCE,
        "usage": {"updatedAt": observed, "activeLimitId": None, "windows": windows},
    }]


def claude_config_dir(environ: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def claude_credentials_path(environ: Optional[Dict[str, str]] = None) -> Path:
    """Quota Sentry's own Claude sign-in file. Claude Code's login is never read."""
    env = os.environ if environ is None else environ
    root = env.get("XDG_CONFIG_HOME")
    base = Path(root) / "quota-sentry" if root else Path.home() / ".config" / "quota-sentry"
    return base / CLAUDE_CREDENTIALS_FILENAME


def read_claude_credentials(
    *, environ: Optional[Dict[str, str]] = None, path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read Quota Sentry's own Claude sign-in file. Never reads the Keychain."""
    login_path = path or claude_credentials_path(environ)
    try:
        text = login_path.read_text()
    except FileNotFoundError:
        raise RuntimeError(
            "no Quota Sentry Claude sign-in; run `quota-sentry login --provider claude`")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"could not read the Quota Sentry Claude sign-in ({type(exc).__name__})")
    try:
        stored = json.loads(text.strip())
    except (ValueError, RecursionError):
        raise RuntimeError("the Quota Sentry Claude sign-in file is not readable JSON")
    if not isinstance(stored, dict):
        raise RuntimeError("the Quota Sentry Claude sign-in file is not a JSON object")
    return stored


def write_claude_credentials(path: Path, stored: Dict[str, Any]) -> None:
    """Atomically write the sign-in file, readable only by its owner."""
    write_bytes_atomic(path, (json.dumps(stored, indent=2) + "\n").encode("utf-8"), mode=0o600)


def _claude_oauth(stored: Any) -> Dict[str, Any]:
    oauth = stored.get("claudeAiOauth") if isinstance(stored, dict) else None
    return oauth if isinstance(oauth, dict) else {}


def claude_access_token(stored: Any, now: Optional[datetime] = None) -> str:
    oauth = stored.get("claudeAiOauth") if isinstance(stored, dict) else None
    if not isinstance(oauth, dict):
        raise RuntimeError("the Claude sign-in has no claude.ai login")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("the Claude sign-in has no access token")
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        if expires_at / 1000 <= (now or utc_now()).timestamp():
            raise RuntimeError("the Claude sign-in token has expired and was not renewed")
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and "user:profile" not in scopes:
        raise RuntimeError("the Claude sign-in lacks the user:profile scope")
    return token


def _loopback_override(env: Dict[str, str], name: str, default: str) -> str:
    override = env.get(name)
    if not override:
        return default
    host = urllib.parse.urlparse(override).hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError(f"{name} must point to a loopback address")
    return override


def claude_usage_url(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return _loopback_override(env, CLAUDE_USAGE_URL_OVERRIDE_ENV, CLAUDE_USAGE_URL)


def claude_token_url(environ: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return _loopback_override(env, CLAUDE_TOKEN_URL_OVERRIDE_ENV, CLAUDE_OAUTH_TOKEN_URL)


def claude_oauth_start() -> Dict[str, str]:
    """Build a PKCE sign-in link whose final page shows a code to paste back."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    query = urllib.parse.urlencode([
        ("code", "true"),
        ("client_id", CLAUDE_OAUTH_CLIENT_ID),
        ("response_type", "code"),
        ("redirect_uri", CLAUDE_OAUTH_REDIRECT_URL),
        ("scope", " ".join(CLAUDE_OAUTH_SCOPES)),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
    ], quote_via=urllib.parse.quote)
    return {"url": f"{CLAUDE_OAUTH_AUTHORIZE_URL}?{query}", "verifier": verifier, "state": state}


class ClaudeSignInRejected(RuntimeError):
    """Anthropic refused the sign-in or its renewal; a new login is needed."""


class ClaudeRateLimited(RuntimeError):
    def __init__(self, message: str, retry_after: Optional[str] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _claude_token_request(
    body: Dict[str, Any], *, environ: Optional[Dict[str, str]], timeout_seconds: int,
) -> Dict[str, Any]:
    request = urllib.request.Request(
        claude_token_url(environ), method="POST", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "quota-sentry"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After") if error.headers else None
        error.close()
        if status == 429:
            raise ClaudeRateLimited("Claude sign-in endpoint rate-limited Quota Sentry",
                                    retry_after)
        if status in {400, 401, 403}:
            raise ClaudeSignInRejected(
                f"Anthropic refused the Quota Sentry Claude sign-in (HTTP {status}); "
                "run `quota-sentry login --provider claude` again")
        raise RuntimeError(f"Claude sign-in endpoint returned HTTP {status}")
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"Claude sign-in endpoint unreachable ({type(reason).__name__})")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise RuntimeError("Claude sign-in endpoint returned unreadable JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) \
            or not payload["access_token"]:
        raise RuntimeError("Claude sign-in endpoint returned no access token")
    return payload


def _stored_from_token_response(
    payload: Dict[str, Any], *, previous_refresh: Optional[str],
    requested_scopes: Iterable[str], now: datetime,
) -> Dict[str, Any]:
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = int((now.timestamp() + expires_in) * 1000)
    scope = payload.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope.strip() else list(requested_scopes)
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous_refresh
    return {"claudeAiOauth": {"accessToken": payload["access_token"], "refreshToken": refresh,
                              "expiresAt": expires_at, "scopes": scopes}}


def claude_oauth_exchange(
    pasted: str, *, verifier: str, state: str, environ: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None, timeout_seconds: int = DEFAULT_CLAUDE_USAGE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Exchange the code shown after sign-in (`code#state`) for a Quota Sentry login."""
    code, separator, pasted_state = (pasted or "").strip().partition("#")
    if not code or not separator or not pasted_state:
        raise RuntimeError("that is not the full code; copy all of it, including the # in the middle")
    if not secrets.compare_digest(pasted_state, state):
        raise RuntimeError("that code does not match this sign-in; start `quota-sentry login` again")
    payload = _claude_token_request({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URL, "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "code_verifier": verifier, "state": state,
    }, environ=environ, timeout_seconds=timeout_seconds)
    return _stored_from_token_response(payload, previous_refresh=None,
                                       requested_scopes=CLAUDE_OAUTH_SCOPES,
                                       now=now or utc_now())


def renew_claude_login(
    stored: Dict[str, Any], *, environ: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None, timeout_seconds: int = DEFAULT_CLAUDE_USAGE_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    oauth = _claude_oauth(stored)
    refresh = oauth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh:
        raise ClaudeSignInRejected(
            "the Quota Sentry Claude sign-in cannot be renewed; "
            "run `quota-sentry login --provider claude` again")
    scopes = oauth.get("scopes")
    scopes = [item for item in scopes if isinstance(item, str)] if isinstance(scopes, list) else []
    scopes = scopes or list(CLAUDE_OAUTH_SCOPES)
    payload = _claude_token_request({
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": CLAUDE_OAUTH_CLIENT_ID, "scope": " ".join(scopes),
    }, environ=environ, timeout_seconds=timeout_seconds)
    return _stored_from_token_response(payload, previous_refresh=refresh,
                                       requested_scopes=scopes, now=now or utc_now())


def _claude_login_fingerprint(stored: Any) -> str:
    token = _claude_oauth(stored).get("accessToken")
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def _claude_needs_renewal(stored: Any, now: datetime) -> bool:
    expires_at = _claude_oauth(stored).get("expiresAt")
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return False
    return expires_at / 1000 - now.timestamp() <= CLAUDE_TOKEN_RENEW_MARGIN_SECONDS


@contextmanager
def _claude_login_lock(path: Path):
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _renew_and_save_claude_login(
    path: Path, seen: Dict[str, Any], *, force: bool, environ: Optional[Dict[str, str]],
    now: datetime, timeout_seconds: int,
) -> Dict[str, Any]:
    with _claude_login_lock(path):
        latest = read_claude_credentials(path=path)
        if _claude_login_fingerprint(latest) != _claude_login_fingerprint(seen):
            return latest  # Another Quota Sentry process already renewed or re-signed-in.
        if not force and not _claude_needs_renewal(latest, now):
            return latest
        renewed = renew_claude_login(latest, environ=environ, now=now,
                                     timeout_seconds=timeout_seconds)
        write_claude_credentials(path, renewed)
        return renewed


def _claude_usage_hold_path(state_dir: Optional[Path]) -> Optional[Path]:
    return state_dir / CLAUDE_USAGE_HOLD_FILENAME if state_dir is not None else None


def read_claude_usage_hold(state_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    path = _claude_usage_hold_path(state_dir)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    until = parse_timestamp(payload.get("until")) if isinstance(payload, dict) else None
    if until is None or not isinstance(payload.get("login"), str):
        return None
    return {"until": until, "kind": payload.get("kind"), "login": payload["login"]}


def _write_claude_usage_hold(state_dir: Optional[Path], *, until: datetime, kind: str,
                             login: str) -> None:
    path = _claude_usage_hold_path(state_dir)
    if path is None:
        return
    try:
        write_json_atomic(path, {"until": format_timestamp(until), "kind": kind, "login": login})
    except OSError:
        pass


def _claude_rate_limit_hold_seconds(retry_after: Optional[str]) -> int:
    try:
        seconds = int(str(retry_after).strip())
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        seconds = CLAUDE_RATE_LIMIT_HOLD_DEFAULT_SECONDS
    return min(seconds, CLAUDE_RATE_LIMIT_HOLD_MAX_SECONDS)


def _claude_usage_get(url: str, token: str, timeout_seconds: int) -> bytes:
    request = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "quota-sentry",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        retry_after = error.headers.get("Retry-After") if error.headers else None
        error.close()
        if status in {403, 429}:
            raise ClaudeRateLimited(f"Claude usage endpoint answered HTTP {status}", retry_after)
        if status == 401:
            raise ClaudeSignInRejected("Anthropic rejected the Quota Sentry Claude sign-in (HTTP 401)")
        raise RuntimeError(f"Claude usage endpoint returned HTTP {status}")
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"Claude usage endpoint unreachable ({type(reason).__name__})")


def fetch_claude_usage_api(
    *,
    state_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    environ: Optional[Dict[str, str]] = None,
    credentials_path: Optional[Path] = None,
    timeout_seconds: int = DEFAULT_CLAUDE_USAGE_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """Read Claude plan usage from Anthropic with Quota Sentry's own sign-in."""
    current_time = now or utc_now()
    url = claude_usage_url(environ)
    path = credentials_path or claude_credentials_path(environ)
    stored = read_claude_credentials(path=path)
    hold = read_claude_usage_hold(state_dir)
    if (hold is not None and current_time < hold["until"]
            and hold["login"] == _claude_login_fingerprint(stored)):
        if hold["kind"] == "auth_rejected":
            raise RuntimeError(
                "Anthropic rejected the Quota Sentry Claude sign-in; not asking again until "
                f"{format_timestamp(hold['until'])} unless you run `quota-sentry login --provider claude`")
        raise RuntimeError(
            f"Claude usage endpoint rate-limited Quota Sentry until {format_timestamp(hold['until'])}")

    def hold_for(kind: str, seconds: int, login: Dict[str, Any]) -> datetime:
        until = current_time + timedelta(seconds=seconds)
        _write_claude_usage_hold(state_dir, until=until, kind=kind,
                                 login=_claude_login_fingerprint(login))
        return until

    renewed = False
    try:
        if _claude_needs_renewal(stored, current_time):
            stored = _renew_and_save_claude_login(path, stored, force=False, environ=environ,
                                                  now=current_time, timeout_seconds=timeout_seconds)
            renewed = True
        token = claude_access_token(stored, now=current_time)
        try:
            body = _claude_usage_get(url, token, timeout_seconds)
        except ClaudeSignInRejected:
            if renewed:
                raise
            stored = _renew_and_save_claude_login(path, stored, force=True, environ=environ,
                                                  now=current_time, timeout_seconds=timeout_seconds)
            body = _claude_usage_get(url, claude_access_token(stored, now=current_time),
                                     timeout_seconds)
    except ClaudeRateLimited as limited:
        until = hold_for("rate_limited", _claude_rate_limit_hold_seconds(limited.retry_after),
                         stored)
        raise RuntimeError(f"{limited}; rate-limited until {format_timestamp(until)}")
    except ClaudeSignInRejected as rejected:
        # `stored` is the login that was last rejected (renewed or not).
        hold_for("auth_rejected", CLAUDE_AUTH_REJECTED_HOLD_SECONDS, stored)
        raise RuntimeError(str(rejected))
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise RuntimeError("Claude usage endpoint returned unreadable JSON")
    return claude_usage_to_usage(result, now=current_time)


def fetch_quota_usage(
    provider: str = DEFAULT_PROVIDER,
    source: str = DEFAULT_USAGE_SOURCE,
    *,
    state_dir: Optional[Path] = None,
) -> Any:
    if provider == CLAUDE_PROVIDER:
        if source in {AUTO_SOURCE, CLAUDE_USAGE_API_SOURCE}:
            return fetch_claude_usage_api(state_dir=state_dir)
        raise ValueError(f"unsupported Claude quota source: {source}")
    return fetch_codex_usage(source=source)


def _source_context(provider: str, decision: QuotaDecision, config_path: Optional[Path]) -> str:
    config = str((config_path or default_config_path()).expanduser().resolve())
    if provider == CLAUDE_PROVIDER:
        context = {
            "claudeLogin": str(claude_credentials_path().expanduser().resolve()),
            "config": config,
            "source": _normalized_source(decision.source),
        }
    else:
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        context = {
            "codexHome": str(codex_home.expanduser().resolve()),
            "config": config,
            "source": _normalized_source(decision.source),
        }
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode("utf-8")).hexdigest()


def poll_once(
    state_path: Path,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    reset_buffer_seconds: int = DEFAULT_RESET_BUFFER_SECONDS,
    fetcher: Optional[Callable[[], Any]] = None,
    source: str = DEFAULT_USAGE_SOURCE,
    config_path: Optional[Path] = None,
    quota_config: Optional[QuotaConfig] = None,
    now: Optional[datetime] = None,
    provider: str = DEFAULT_PROVIDER,
) -> QuotaDecision:
    current_time = now or utc_now()
    active_config = (quota_config or read_valid_config(config_path)
                     or _retained_policy_config(read_state(state_path)))
    try:
        payload = (fetcher() if fetcher is not None
                   else fetch_quota_usage(provider, source, state_dir=state_path.parent))
        decision = parse_codex_usage(
            payload,
            threshold_percent=threshold_percent,
            reset_buffer_seconds=reset_buffer_seconds,
            now=current_time,
            quota_config=active_config,
            provider=provider,
        )
        decision = replace(decision, source_context=_source_context(provider, decision, config_path))
    except Exception as exc:
        decision = QuotaDecision(
            status="unknown",
            reason=f"quota fetch failed: {exc}",
            weekly_hard_block_enabled=(
                active_config.weekly_mode == WEEKLY_MODE_HARD_BLOCK
            ),
        )
    write_state(
        state_path,
        decision,
        now=current_time,
        threshold_percent=threshold_percent,
        weekly_threshold_percent=active_config.weekly_threshold_percent,
        provider=provider,
    )
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

    used_values: List[Optional[Percent]] = []
    has_short_term_window = decision.short_term_window is not None
    if decision.quota_windows:
        for window in decision.quota_windows:
            if not window.is_default_limit:
                continue
            if window.kind == WINDOW_KIND_SHORT_TERM:
                has_short_term_window = True
                used_values.append(window.used_percent)
            elif window.kind in {
                WINDOW_KIND_ACCOUNT,
                WINDOW_KIND_LONG_TERM,
                WINDOW_KIND_UNKNOWN,
            }:
                used_values.append(window.used_percent)
            elif (
                window.kind == WINDOW_KIND_WEEKLY
                and decision.weekly_hard_block_enabled
            ):
                used_values.append(window.used_percent)
    else:
        used_values.append(decision.used_percent)
        if decision.short_term_window is not None:
            used_values.append(decision.short_term_window.used_percent)
        if decision.weekly_hard_block_enabled and decision.weekly_window is not None:
            used_values.append(decision.weekly_window.used_percent)
    effective_base_interval = (
        min(base_interval, near_interval) if has_short_term_window else base_interval
    )
    numeric_used_values = [used for used in used_values if used is not None]
    if not numeric_used_values:
        return effective_base_interval
    used = max(numeric_used_values)
    if used >= critical_threshold_percent:
        return min(effective_base_interval, critical_interval)
    if used >= near_threshold_percent:
        return min(effective_base_interval, near_interval)
    return effective_base_interval


def wait_if_blocked(
    state_path: Path,
    poller: Callable[[], QuotaDecision],
    sleeper: Callable[[float], None] = time.sleep,
    now_func: Callable[[], datetime] = utc_now,
    monotonic_func: Callable[[], float] = time.monotonic,
    max_state_age_seconds: int = DEFAULT_MAX_STATE_AGE_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    output: Callable[[str], None] = print,
    verbose: bool = False,
    notice: Callable[[str], None] = emit_terminal_notice,
    notify: bool = True,
    state_only: bool = False,
    fail_closed_after_seconds: Optional[float] = None,
    fail_closed_output: Optional[Callable[[str], None]] = None,
    fail_closed_exit_code: int = 2,
    require_fresh_state: bool = False,
    recovery: Optional[Callable[[], None]] = None,
    bypass: Optional[Callable[[], bool]] = None,
    policy_config: Optional[Callable[[], Optional[QuotaConfig]]] = None,
    threshold_percent: Optional[int] = None,
) -> int:
    if os.environ.get("QUOTA_SENTRY_DISABLE") == "1":
        return 0

    emitted_wait_message = False
    emitted_notice = False
    retained: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    last_valid_config = None
    fail_closed_deadline = None
    if fail_closed_after_seconds is not None:
        fail_closed_deadline = monotonic_func() + max(0.0, fail_closed_after_seconds)

    while True:
        state = read_state(state_path)
        current_time = now_func()
        polled_this_iteration = False
        saved_blocked_until = parse_timestamp(state.get("blockedUntil"))
        if (
            not state_only
            and state.get("status") == "blocked"
            and saved_blocked_until is not None
            and saved_blocked_until <= current_time
        ):
            poller()
            polled_this_iteration = True
            state = read_state(state_path)
            current_time = now_func()

        if not state_only and not retained and state.get("status") != "blocked" and not polled_this_iteration:
            poller()
            state = read_state(state_path)
            current_time = now_func()

        # A cache read cannot revoke evidence already observed by this invocation.
        for window in _previous_enforcement_windows(state):
            key = _latch_identity(window)
            if key not in retained:
                retained[key] = window
        current_config = None
        if policy_config is not None:
            try:
                current_config = policy_config()
            except Exception:
                pass
            if current_config is not None:
                last_valid_config = current_config
        effective_config = (current_config or last_valid_config
                            or _retained_policy_config(state, retained.values()))
        observation = cached_observation(
            state, now=current_time, max_state_age_seconds=max_state_age_seconds,
            quota_config=effective_config if policy_config is not None else None,
            threshold_percent=threshold_percent)
        if observation is not None:
            snapshot = state["observation"]
            short_threshold = (min(threshold_percent, snapshot["thresholdPercent"])
                               if threshold_percent is not None else snapshot["thresholdPercent"])
            weekly_threshold = (effective_config.weekly_threshold_percent if policy_config is not None
                                else snapshot["weeklyThresholdPercent"])
            for window in _merged_enforcement_windows(
                    {}, observation, current_time=current_time,
                    threshold_percent=short_threshold, weekly_threshold_percent=weekly_threshold):
                key = _latch_identity(window)
                if key not in retained:
                    retained[key] = window
            remaining = {}
            for window in retained.values():
                observed = parse_timestamp(window.get("sourceObservedAt"))
                if (observed is not None and observation.source_observed_at is not None
                        and observation.source_observed_at <= observed):
                    remaining[_latch_identity(window)] = window
                    continue
                merged = _merged_enforcement_windows(
                    {"enforcement": {"status": "blocked", "windows": [window]}},
                    observation, current_time=current_time,
                    threshold_percent=short_threshold,
                    weekly_threshold_percent=weekly_threshold,
                )
                for candidate in merged:
                    remaining[_latch_identity(candidate)] = candidate
            retained = remaining

        if policy_config is not None:
            if current_config is not None and current_config.weekly_mode == WEEKLY_MODE_ADVISORY:
                retained = {key: window for key, window in retained.items()
                            if window.get("kind") != WINDOW_KIND_WEEKLY}
        if bypass is not None:
            try:
                if bypass():
                    return 0
            except Exception:
                pass
        if recovery is not None and (retained or observation is None or state.get("status") != "open"
                or not state_is_fresh(state, now=current_time,
                                      max_state_age_seconds=max_state_age_seconds)):
            try:
                recovery()
            except Exception:
                pass
        if not retained:
            return 0
        selected = max(retained.values(), key=lambda w: parse_timestamp(w.get("blockedUntil"))
                       or datetime.min.replace(tzinfo=timezone.utc))
        block_until = block_until_from_state(
            {"status": "blocked", "updatedAt": selected.get("lastConfirmedAt"),
             "blockedUntil": selected.get("blockedUntil")}, now=current_time)
        if block_until is None:
            block_until = current_time + timedelta(seconds=DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)

        seconds = float(
            max(
                1,
                min(
                    poll_interval_seconds,
                    DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS,
                    int((block_until - current_time).total_seconds()),
                ),
            )
        )
        if fail_closed_deadline is not None:
            remaining_guard_seconds = fail_closed_deadline - monotonic_func()
            if remaining_guard_seconds <= 0:
                if fail_closed_output is not None:
                    fail_closed_output(HOOK_FAIL_CLOSED_MESSAGE)
                return fail_closed_exit_code
            seconds = min(seconds, remaining_guard_seconds)
        if notify and not emitted_notice:
            label = _provider_label(state.get("provider", DEFAULT_PROVIDER))
            notice(f"Quota Sentry: waiting for {label} quota reset until {format_timestamp(block_until)}.")
            emitted_notice = True
        if verbose and not emitted_wait_message:
            label = _provider_label(state.get("provider", DEFAULT_PROVIDER))
            output(f"Quota Sentry: {label} quota guard active until {format_timestamp(block_until)}.")
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


def blocking_hook_entry(matcher: str, command: str, timeout_seconds: int,
                        notify_on_block: bool = True) -> Dict[str, Any]:
    entry = hook_entry(matcher, command, False, timeout_seconds)
    if notify_on_block:
        entry["hooks"][0]["statusMessage"] = "Quota Sentry: quota checkpoint"
    return entry


def quota_sentry_hook_commands(script_path: Path) -> Dict[str, str]:
    script = shlex.quote(str(script_path))
    cached_guard = (
        f"{script} guard --state-only --no-notify --read-hook-input "
        "--hook-mode --recover-daemon"
    )
    return {
        "SessionStart": f"{script} start --quiet --read-hook-input",
        "UserPromptSubmit": (
            f"{script} prompt-guard --read-hook-input --hook-mode --recover-daemon"
        ),
        "PreToolUse": cached_guard,
        "PostToolUse": cached_guard,
        "PreCompact": cached_guard,
        "PreModelRequest": cached_guard,
        "PostModelRequest": cached_guard,
    }


def _remove_existing_quota_sentry(
    entries: List[Dict[str, Any]], script_path: Path,
) -> List[Dict[str, Any]]:
    def is_quota_sentry(hook):
        command = hook.get("command") if isinstance(hook, dict) else None
        if not isinstance(command, str):
            return False
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        return bool(words and (Path(words[0]).name == "quota-sentry"
                               or words[0] == str(script_path)))

    filtered = []
    for entry in entries:
        hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
        if not isinstance(hooks, list):
            filtered.append(entry)
            continue
        remaining = [hook for hook in hooks if not is_quota_sentry(hook)]
        if len(remaining) == len(hooks):
            filtered.append(entry)
        elif remaining:
            filtered.append({**entry, "hooks": remaining})
    return filtered


def merge_codex_hooks(existing: Dict[str, Any], script_path: Path,
                      notify_on_block: bool = True) -> Dict[str, Any]:
    merged = json.loads(json.dumps(existing or {}))
    hooks = merged.setdefault("hooks", {})
    commands = quota_sentry_hook_commands(script_path)

    additions = {
        "SessionStart": hook_entry(
            "startup|resume|clear|compact", commands["SessionStart"], False, 30
        ),
        "UserPromptSubmit": blocking_hook_entry(
            "", commands["UserPromptSubmit"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
        "PreToolUse": blocking_hook_entry(
            ".*", commands["PreToolUse"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
        "PostToolUse": blocking_hook_entry(
            ".*", commands["PostToolUse"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
        "PreCompact": blocking_hook_entry(
            ".*", commands["PreCompact"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
        "PreModelRequest": blocking_hook_entry(
            "", commands["PreModelRequest"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
        "PostModelRequest": blocking_hook_entry(
            "", commands["PostModelRequest"], DEFAULT_HOOK_TIMEOUT_SECONDS, notify_on_block
        ),
    }

    for event_name, entry in additions.items():
        current_entries = hooks.get(event_name)
        if not isinstance(current_entries, list):
            current_entries = []
        hooks[event_name] = _remove_existing_quota_sentry(current_entries, script_path) + [entry]

    return merged


CLAUDE_HOOK_STATUS_MESSAGE = "Quota Sentry: quota checkpoint"
CLAUDE_SESSION_START_TIMEOUT_SECONDS = 30
CLAUDE_HOOK_MATCHERS = {
    "SessionStart": "startup|resume|clear|compact",
    "UserPromptSubmit": None,
    "PreToolUse": "*",
    "PostToolUse": "*",
    # PostToolBatch runs once per tool batch, before Claude's next model request.
    "PostToolBatch": None,
    "PreCompact": None,
}


def claude_quota_sentry_hook_args() -> Dict[str, List[str]]:
    provider = ["--provider", CLAUDE_PROVIDER]
    cached_guard = ["guard", "--state-only", "--no-notify", "--read-hook-input",
                    "--hook-mode", "--recover-daemon", *provider]
    return {
        "SessionStart": ["start", "--quiet", "--read-hook-input", *provider],
        "UserPromptSubmit": ["prompt-guard", "--read-hook-input", "--hook-mode",
                             "--recover-daemon", *provider],
        "PreToolUse": cached_guard,
        "PostToolUse": list(cached_guard),
        "PostToolBatch": list(cached_guard),
        "PreCompact": list(cached_guard),
    }


def _is_claude_quota_sentry_hook(hook: Any, script_path: Path) -> bool:
    command = hook.get("command") if isinstance(hook, dict) else None
    if not isinstance(command, str) or not command:
        return False
    if "args" in hook:
        program = command
    else:
        try:
            words = shlex.split(command)
        except ValueError:
            return False
        if not words:
            return False
        program = words[0]
    return Path(program).name == "quota-sentry" or program == str(script_path)


def merge_claude_hooks(existing: Dict[str, Any], script_path: Path,
                       notify_on_block: bool = True) -> Dict[str, Any]:
    """Merge Quota Sentry hooks into Claude Code settings, leaving everything else intact."""
    merged = json.loads(json.dumps(existing or {}))
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        merged["hooks"] = hooks
    for event_name, args in claude_quota_sentry_hook_args().items():
        hook: Dict[str, Any] = {"type": "command", "command": str(script_path), "args": args}
        if event_name == "SessionStart":
            hook["timeout"] = CLAUDE_SESSION_START_TIMEOUT_SECONDS
        else:
            hook["timeout"] = DEFAULT_HOOK_TIMEOUT_SECONDS
            if notify_on_block:
                hook["statusMessage"] = CLAUDE_HOOK_STATUS_MESSAGE
        entry: Dict[str, Any] = {}
        if CLAUDE_HOOK_MATCHERS[event_name] is not None:
            entry["matcher"] = CLAUDE_HOOK_MATCHERS[event_name]
        entry["hooks"] = [hook]

        current_entries = hooks.get(event_name)
        if not isinstance(current_entries, list):
            current_entries = []
        kept = []
        for current in current_entries:
            entry_hooks = current.get("hooks") if isinstance(current, dict) else None
            if not isinstance(entry_hooks, list):
                kept.append(current)
                continue
            remaining = [item for item in entry_hooks
                         if not _is_claude_quota_sentry_hook(item, script_path)]
            if len(remaining) == len(entry_hooks):
                kept.append(current)
            elif remaining:
                kept.append({**current, "hooks": remaining})
        hooks[event_name] = kept + [entry]
    return merged


def claude_hooks_installed(settings: Any, script_path: Path, notify_on_block: bool = True) -> bool:
    """Check that exactly one expected Quota Sentry hook exists for every Claude event."""
    expected = merge_claude_hooks({}, script_path, notify_on_block=notify_on_block)["hooks"]
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    for event_name, [entry] in expected.items():
        found = [
            (current, item)
            for current in hooks.get(event_name) or []
            if isinstance(current, dict) and isinstance(current.get("hooks"), list)
            for item in current["hooks"]
            if _is_claude_quota_sentry_hook(item, script_path)
        ]
        if len(found) != 1:
            return False
        current, item = found[0]
        if item != entry["hooks"][0] or current.get("matcher") != entry.get("matcher"):
            return False
    return True


def _exact_quota_sentry_hook_metadata(
    hooks_list_result: Any,
    hooks_path: Path,
    script_path: Path,
) -> Dict[str, Dict[str, Any]]:
    event_names = {
        "SessionStart": "sessionStart",
        "UserPromptSubmit": "userPromptSubmit",
        "PreToolUse": "preToolUse",
        "PostToolUse": "postToolUse",
        "PreCompact": "preCompact",
        "PreModelRequest": "preModelRequest",
        "PostModelRequest": "postModelRequest",
    }
    commands = quota_sentry_hook_commands(script_path)
    expected = {event_names[name]: command for name, command in commands.items()}
    expected_source = hooks_path.expanduser().resolve()
    discovered: Dict[str, Dict[str, Any]] = {}

    entries = hooks_list_result.get("data") if isinstance(hooks_list_result, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Codex hooks/list returned no hook entries")

    for entry in entries:
        metadata_items = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(metadata_items, list):
            continue
        for metadata in metadata_items:
            if not isinstance(metadata, dict):
                continue
            event_name = metadata.get("eventName")
            command = metadata.get("command")
            source_path = metadata.get("sourcePath")
            if (
                event_name not in expected
                or command != expected[event_name]
                or metadata.get("handlerType") != "command"
                or not isinstance(source_path, str)
                or Path(source_path).expanduser().resolve() != expected_source
            ):
                continue
            key = metadata.get("key")
            current_hash = metadata.get("currentHash")
            if not isinstance(key, str) or not isinstance(current_hash, str):
                raise RuntimeError(f"Codex returned incomplete metadata for {event_name}")
            if event_name in discovered:
                raise RuntimeError(f"Codex discovered duplicate Quota Sentry {event_name} hooks")
            discovered[event_name] = metadata

    missing = sorted(set(expected) - set(discovered))
    if missing:
        raise RuntimeError(
            "Codex hooks/list is missing exact Quota Sentry hooks: " + ", ".join(missing)
        )

    return discovered


def quota_sentry_hook_trust_updates(
    hooks_list_result: Any,
    hooks_path: Path,
    script_path: Path,
) -> Dict[str, Dict[str, Any]]:
    discovered = _exact_quota_sentry_hook_metadata(
        hooks_list_result,
        hooks_path,
        script_path,
    )
    return {
        metadata["key"]: {
            "trusted_hash": metadata["currentHash"],
            "enabled": True,
        }
        for metadata in discovered.values()
    }


def verify_quota_sentry_hooks_active(
    hooks_list_result: Any,
    hooks_path: Path,
    script_path: Path,
    notify_on_block: bool = True,
) -> int:
    discovered = _exact_quota_sentry_hook_metadata(
        hooks_list_result,
        hooks_path,
        script_path,
    )
    expected_timeouts = {
        "sessionStart": 30,
        "userPromptSubmit": DEFAULT_HOOK_TIMEOUT_SECONDS,
        "preToolUse": DEFAULT_HOOK_TIMEOUT_SECONDS,
        "postToolUse": DEFAULT_HOOK_TIMEOUT_SECONDS,
        "preCompact": DEFAULT_HOOK_TIMEOUT_SECONDS,
        "preModelRequest": DEFAULT_HOOK_TIMEOUT_SECONDS,
        "postModelRequest": DEFAULT_HOOK_TIMEOUT_SECONDS,
    }
    expected_matchers = {
        "sessionStart": "startup|resume|clear|compact",
        "userPromptSubmit": None,
        "preToolUse": ".*",
        "postToolUse": ".*",
        "preCompact": ".*",
        "preModelRequest": None,
        "postModelRequest": None,
    }
    for event_name, metadata in discovered.items():
        if metadata.get("async", False) is not False:
            raise RuntimeError(f"Quota Sentry {event_name} hook is asynchronous")
        expected_status = ("Quota Sentry: quota checkpoint"
                           if notify_on_block and event_name != "sessionStart" else None)
        if metadata.get("statusMessage") != expected_status:
            raise RuntimeError(f"Quota Sentry {event_name} hook has unexpected status message")
        if metadata.get("enabled") is not True:
            raise RuntimeError(f"Quota Sentry {event_name} hook is disabled")
        if metadata.get("trustStatus") not in {"trusted", "managed"}:
            raise RuntimeError(f"Quota Sentry {event_name} hook is not trusted")
        if metadata.get("timeoutSec") != expected_timeouts[event_name]:
            raise RuntimeError(
                f"Quota Sentry {event_name} hook has unexpected timeout "
                f"{metadata.get('timeoutSec')!r}"
            )
        if metadata.get("matcher") != expected_matchers[event_name]:
            raise RuntimeError(
                f"Quota Sentry {event_name} hook has unexpected matcher "
                f"{metadata.get('matcher')!r}"
            )
    return len(discovered)


def trust_codex_hooks(
    hooks_path: Path,
    script_path: Path,
    *,
    cwd: Path,
    timeout_seconds: int = DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS,
    notify_on_block: bool = True,
) -> int:
    process = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
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
                "id": "quota-sentry-trust-init",
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
        initialize_response = _read_json_response(
            output_queue, "quota-sentry-trust-init", deadline
        )
        if "error" in initialize_response:
            raise RuntimeError(
                f"codex app-server initialize failed: {initialize_response['error']}"
            )

        _write_json_line(process.stdin, {"method": "initialized"})
        _write_json_line(
            process.stdin,
            {
                "id": "quota-sentry-hooks-list",
                "method": "hooks/list",
                "params": {"cwds": [str(cwd.expanduser().resolve())]},
            },
        )
        hooks_response = _read_json_response(
            output_queue, "quota-sentry-hooks-list", deadline
        )
        if "error" in hooks_response:
            raise RuntimeError(f"Codex hooks/list failed: {hooks_response['error']}")
        trust_updates = quota_sentry_hook_trust_updates(
            hooks_response.get("result"),
            hooks_path=hooks_path,
            script_path=script_path,
        )

        _write_json_line(
            process.stdin,
            {
                "id": "quota-sentry-hooks-trust",
                "method": "config/batchWrite",
                "params": {
                    "edits": [
                        {
                            "keyPath": "hooks.state",
                            "value": trust_updates,
                            "mergeStrategy": "upsert",
                        }
                    ],
                    "reloadUserConfig": True,
                },
            },
        )
        trust_response = _read_json_response(
            output_queue, "quota-sentry-hooks-trust", deadline
        )
        if "error" in trust_response:
            raise RuntimeError(f"Codex hook trust write failed: {trust_response['error']}")

        _write_json_line(
            process.stdin,
            {
                "id": "quota-sentry-hooks-verify",
                "method": "hooks/list",
                "params": {"cwds": [str(cwd.expanduser().resolve())]},
            },
        )
        verify_response = _read_json_response(
            output_queue, "quota-sentry-hooks-verify", deadline
        )
        if "error" in verify_response:
            raise RuntimeError(f"Codex hook verification failed: {verify_response['error']}")
        return verify_quota_sentry_hooks_active(
            verify_response.get("result"),
            hooks_path=hooks_path,
            script_path=script_path,
            notify_on_block=notify_on_block,
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
