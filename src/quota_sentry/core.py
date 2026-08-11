import json
import math
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is process-local.
    fcntl = None


DEFAULT_PROVIDER = "codex"
CODEXBAR_SOURCE = "codexbar"
CODEX_APP_SERVER_SOURCE = "codex-app-server"
AUTO_SOURCE = "auto"
WEEKLY_MODE_ADVISORY = "advisory"
WEEKLY_MODE_HARD_BLOCK = "hard-block"
DEFAULT_WEEKLY_MODE = WEEKLY_MODE_HARD_BLOCK
DEFAULT_USAGE_SOURCE = AUTO_SOURCE
DEFAULT_CODEXBAR_SOURCE = "cli"
DEFAULT_THRESHOLD_PERCENT = 95
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

    @property
    def primary_window(self) -> Optional[QuotaWindow]:
        """Compatibility alias for callers that predate duration-aware windows."""
        return self.short_term_window


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


def default_daemon_lock_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or cache_dir()) / "quota-sentry.daemon.lock"


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


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
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
    weekly_mode = payload.get("weeklyMode")
    if "weeklyMode" in payload and not _is_valid_weekly_mode(weekly_mode):
        return QuotaConfig()
    return QuotaConfig(
        weekly_mode=_valid_weekly_mode(weekly_mode),
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


def _codex_entry(payload: Any) -> Optional[Dict[str, Any]]:
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, dict) and entry.get("provider") == DEFAULT_PROVIDER:
            return entry
    return None


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
    except (TypeError, ValueError):
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
    except (TypeError, ValueError):
        return None
    if not math.isfinite(percent) or percent < 0 or percent > 100:
        return None
    return int(percent) if percent.is_integer() else percent


def _optional_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _parse_quota_window(window: Dict[str, Any]) -> Tuple[Optional[QuotaWindow], Optional[str]]:
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
        ),
        None,
    )


def _parse_quota_windows(
    usage: Dict[str, Any],
) -> Tuple[List[QuotaWindow], List[Tuple[str, bool, str]]]:
    windows: List[QuotaWindow] = []
    errors: List[Tuple[str, bool, str]] = []
    identities = set()
    for candidate in _window_candidates(usage):
        window, error = _parse_quota_window(candidate)
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
    if reached_type is None and spend_control_reached is True:
        reached_type = "spend_control_reached"
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
    return f"{window.used_percent}% of the {duration} Codex quota{bucket} is used"


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
) -> QuotaDecision:
    current_time = now or utc_now()
    config = quota_config or QuotaConfig()
    weekly_hard_block_enabled = config.weekly_mode == WEEKLY_MODE_HARD_BLOCK
    entry = _codex_entry(payload)
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
        "source_observed_at": source_observed_at,
        "canonical_limit_id": canonical_limit_id,
        "account_limit_state_known": account_limit_state_known,
        "rate_limit_state_known": rate_limit_state_known,
        "spend_control_reached": spend_control_reached,
        "rate_limit_reached_type": rate_limit_reached_type,
    }
    quota_windows, window_errors = _parse_quota_windows(usage)
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

    if rate_limit_reached_type is not None:
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
        account_window = QuotaWindow(
            name=WINDOW_KIND_ACCOUNT,
            used_percent=100,
            window_minutes=None,
            resets_at=account_reset,
            kind=WINDOW_KIND_ACCOUNT,
            limit_id=canonical_limit_id,
            source_slot=None,
            is_default_limit=True,
            rate_limit_reached_type=rate_limit_reached_type,
        )
        return _blocked_decision(
            account_window,
            reset_buffer_seconds=reset_buffer_seconds,
            blocked_window=WINDOW_KIND_ACCOUNT,
            short_term_window=short_term_window,
            weekly_window=weekly_window,
            quota_windows=(*quota_windows, account_window),
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            blocked_quota_windows=(account_window,),
            uncertain_blocking_kinds=uncertain_blocking_kinds,
            **decision_context,
        )

    if not quota_windows:
        reason = window_errors[0][2] if window_errors else "quota source returned no quota window"
        return QuotaDecision(
            status="unknown",
            reason=reason,
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            uncertain_blocking_kinds=uncertain_blocking_kinds,
            **decision_context,
        )

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
            quota_windows=quota_windows,
            weekly_hard_block_enabled=weekly_hard_block_enabled,
            blocked_quota_windows=(window for _kind, window in blocked_candidates),
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
        "rateLimitReachedType": window.rate_limit_reached_type,
    }


def _normalized_source(source: Optional[str]) -> Optional[str]:
    if source == CODEX_APP_SERVER_SOURCE:
        return CODEX_APP_SERVER_SOURCE
    if source in {CODEXBAR_SOURCE, "codex-cli"}:
        return CODEXBAR_SOURCE
    return source


def _source_authority(source: Optional[str]) -> int:
    normalized = _normalized_source(source)
    if normalized == CODEX_APP_SERVER_SOURCE:
        return 2
    if normalized == CODEXBAR_SOURCE:
        return 1
    return 0


def _observation_can_supersede_latch(
    previous: Dict[str, Any],
    decision: QuotaDecision,
    candidates: Iterable[QuotaWindow],
) -> bool:
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
        or decision.source_observed_at < previous_observed_at
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
    if isinstance(enforcement, dict) and enforcement.get("status") == "blocked":
        windows = enforcement.get("windows")
        if isinstance(windows, list):
            return [
                dict(window)
                for window in windows
                if isinstance(window, dict)
                and isinstance(window.get("kind"), str)
                and parse_timestamp(window.get("blockedUntil")) is not None
            ]

    if state.get("status") != "blocked":
        return []
    updated_at = parse_timestamp(state.get("updatedAt"))
    blocked_until = parse_timestamp(state.get("blockedUntil"))
    if updated_at is None or blocked_until is None:
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
            "rateLimitReachedType": state.get("rateLimitReachedType"),
        }
    ]


def _merged_enforcement_windows(
    previous_state: Dict[str, Any],
    decision: QuotaDecision,
    *,
    current_time: datetime,
    threshold_percent: int,
    weekly_threshold_percent: int,
) -> List[Dict[str, Any]]:
    active = {
        window["kind"]: window for window in _previous_enforcement_windows(previous_state)
    }
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

    for kind, threshold in (
        (WINDOW_KIND_SHORT_TERM, threshold_percent),
        (WINDOW_KIND_WEEKLY, weekly_threshold_percent),
        (WINDOW_KIND_ACCOUNT, 100),
        (WINDOW_KIND_LONG_TERM, 100),
        (WINDOW_KIND_UNKNOWN, 100),
    ):
        if kind == WINDOW_KIND_WEEKLY and not decision.weekly_hard_block_enabled:
            active.pop(kind, None)
            continue
        if kind == WINDOW_KIND_ACCOUNT:
            previous = active.get(kind)
            previous_reached_type = (
                _optional_string(previous.get("rateLimitReachedType"))
                if previous is not None
                else None
            )
            explicitly_cleared = (
                decision.spend_control_reached is False
                if previous_reached_type == "spend_control_reached"
                else decision.rate_limit_state_known
            )
            if (
                previous is not None
                and decision.account_limit_state_known
                and decision.rate_limit_reached_type is None
                and explicitly_cleared
                and _observation_can_supersede_latch(previous, decision, ())
            ):
                active.pop(kind, None)
            continue
        candidates = observations[kind]
        if kind not in uncertain_kinds and candidates and all(
            window.used_percent < threshold for window in candidates
        ) and (
            kind not in active
            or _observation_can_supersede_latch(active[kind], decision, candidates)
        ):
            active.pop(kind, None)

    reset_buffer_seconds = 0.0
    if decision.resets_at is not None and decision.blocked_until is not None:
        reset_buffer_seconds = max(
            0.0,
            (decision.blocked_until - decision.resets_at).total_seconds(),
        )
    for window in decision.blocked_quota_windows:
        blocked_until = window.resets_at + timedelta(seconds=reset_buffer_seconds)
        previous = active.get(window.kind)
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
            previous=previous,
        )
        existing_until = parse_timestamp(previous.get("blockedUntil")) if previous else None
        if existing_until is None or blocked_until >= existing_until:
            active[window.kind] = candidate
        else:
            previous["lastConfirmedAt"] = format_timestamp(current_time)

    return sorted(active.values(), key=lambda window: str(window.get("kind")))


def state_from_decision(
    decision: QuotaDecision,
    now: Optional[datetime] = None,
    *,
    previous_state: Optional[Dict[str, Any]] = None,
    threshold_percent: int = DEFAULT_THRESHOLD_PERCENT,
    weekly_threshold_percent: int = DEFAULT_WEEKLY_THRESHOLD_PERCENT,
) -> Dict[str, Any]:
    current_time = now or utc_now()
    short_term_state = _window_state(decision.short_term_window)
    payload = {
        "schemaVersion": 4,
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
        "canonicalLimitId": decision.canonical_limit_id,
        "accountLimitStateKnown": decision.account_limit_state_known,
        "rateLimitStateKnown": decision.rate_limit_state_known,
        "spendControlReached": decision.spend_control_reached,
        "rateLimitReachedType": decision.rate_limit_reached_type,
    }

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
) -> None:
    with _state_write_lock(path):
        previous_state = read_state(path)
        payload = state_from_decision(
            decision,
            now=now,
            previous_state=previous_state,
            threshold_percent=threshold_percent,
            weekly_threshold_percent=weekly_threshold_percent,
        )
        write_json_atomic(path, payload)


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
        decision = parse_codex_usage(
            payload,
            threshold_percent=threshold_percent,
            reset_buffer_seconds=reset_buffer_seconds,
            now=current_time,
            quota_config=active_config,
        )
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
    if decision.quota_windows:
        for window in decision.quota_windows:
            if not window.is_default_limit:
                continue
            if window.kind == WINDOW_KIND_SHORT_TERM:
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
) -> int:
    if os.environ.get("QUOTA_SENTRY_DISABLE") == "1":
        return 0

    emitted_wait_message = False
    emitted_notice = False
    waited_once = False
    fail_closed_deadline = None
    if fail_closed_after_seconds is not None:
        fail_closed_deadline = monotonic_func() + max(0.0, fail_closed_after_seconds)

    while True:
        state = read_state(state_path)
        current_time = now_func()
        polled_this_iteration = False
        if (
            require_fresh_state
            and state.get("status") != "blocked"
            and (
                state.get("status") != "open"
                or not state_is_fresh(
                    state,
                    now=current_time,
                    max_state_age_seconds=max_state_age_seconds,
                )
            )
        ):
            if fail_closed_output is not None:
                fail_closed_output(HOOK_STATE_UNAVAILABLE_MESSAGE)
            return fail_closed_exit_code
        if waited_once and state.get("status") != "blocked":
            return 0
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

        block_until = block_until_from_state(state, now=current_time, max_state_age_seconds=max_state_age_seconds)
        if block_until is None:
            if state_only:
                if require_fresh_state and state.get("status") == "blocked":
                    if fail_closed_output is not None:
                        fail_closed_output(HOOK_STATE_UNAVAILABLE_MESSAGE)
                    return fail_closed_exit_code
                return 0
            if not polled_this_iteration:
                poller()
                state = read_state(state_path)
                current_time = now_func()
            block_until = block_until_from_state(
                state,
                now=current_time,
                max_state_age_seconds=max_state_age_seconds,
            )
            if block_until is None:
                return 0

        current_time = now_func()
        if block_until <= current_time:
            return 0

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


def quota_sentry_hook_commands(script_path: Path) -> Dict[str, str]:
    script = shlex.quote(str(script_path))
    fail_closed_option = (
        f"--fail-closed-after-seconds {DEFAULT_HOOK_FAIL_CLOSED_AFTER_SECONDS}"
    )
    cached_guard = (
        f"{script} guard --state-only --no-notify --read-hook-input "
        f"--require-fresh-state {fail_closed_option}"
    )
    return {
        "SessionStart": f"{script} start --quiet --read-hook-input",
        "UserPromptSubmit": (
            f"{script} prompt-guard --read-hook-input --require-fresh-state "
            f"{fail_closed_option}"
        ),
        "PreToolUse": cached_guard,
        "PostToolUse": f"{cached_guard} --fail-closed-format block-json",
        "PreCompact": f"{cached_guard} --fail-closed-format stop-json",
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
    commands = quota_sentry_hook_commands(script_path)

    additions = {
        "SessionStart": hook_entry(
            "startup|resume|clear|compact", commands["SessionStart"], False, 30
        ),
        "UserPromptSubmit": hook_entry(
            "", commands["UserPromptSubmit"], False, DEFAULT_HOOK_TIMEOUT_SECONDS
        ),
        "PreToolUse": hook_entry(
            ".*", commands["PreToolUse"], False, DEFAULT_HOOK_TIMEOUT_SECONDS
        ),
        "PostToolUse": hook_entry(
            ".*", commands["PostToolUse"], False, DEFAULT_HOOK_TIMEOUT_SECONDS
        ),
        "PreCompact": hook_entry(
            ".*", commands["PreCompact"], False, DEFAULT_HOOK_TIMEOUT_SECONDS
        ),
    }

    for event_name, entry in additions.items():
        current_entries = hooks.get(event_name)
        if not isinstance(current_entries, list):
            current_entries = []
        hooks[event_name] = _remove_existing_quota_sentry(current_entries) + [entry]

    return merged


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
    }
    expected_matchers = {
        "sessionStart": "startup|resume|clear|compact",
        "userPromptSubmit": None,
        "preToolUse": ".*",
        "postToolUse": ".*",
        "preCompact": ".*",
    }
    for event_name, metadata in discovered.items():
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
        )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
