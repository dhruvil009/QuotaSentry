import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from quota_sentry import core


def format_percent(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def duration_label(window_minutes: Any, kind: Optional[str] = None) -> str:
    if kind == core.WINDOW_KIND_WEEKLY or window_minutes == core.DEFAULT_WEEKLY_WINDOW_MINUTES:
        return "weekly"
    try:
        minutes = int(window_minutes)
    except (TypeError, ValueError):
        return "quota"
    if minutes > 0 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes > 0 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def status_windows(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    windows = state.get("windows")
    if isinstance(windows, list):
        normalized = [
            window
            for window in windows
            if isinstance(window, dict)
            and window.get("usedPercent") is not None
            and window.get("isDefaultLimit") is not False
        ]
        if normalized:
            return normalized

    legacy: list[Dict[str, Any]] = []
    short_term = state.get("shortTerm")
    primary = state.get("primary")
    weekly = state.get("weekly")
    for window in (short_term, primary, weekly):
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        identity = (
            window.get("windowMinutes"),
            window.get("resetsAt"),
            window.get("usedPercent"),
        )
        if any(
            (
                existing.get("windowMinutes"),
                existing.get("resetsAt"),
                existing.get("usedPercent"),
            )
            == identity
            for existing in legacy
        ):
            continue
        legacy.append(window)
    return legacy


def status_text(state: Dict[str, Any]) -> str:
    if not state:
        return "Quota Sentry: no state found"

    status = state.get("status", "unknown")
    used = state.get("usedPercent")
    blocked_until = state.get("blockedUntil")
    updated_at = state.get("updatedAt")
    reason = state.get("reason")

    if status == "open":
        windows = status_windows(state)
        if len(windows) == 1:
            window = windows[0]
            window_used = format_percent(window.get("usedPercent"))
            kind = window.get("kind")
            window_minutes = window.get("windowMinutes")
            if kind == core.WINDOW_KIND_SHORT_TERM or (
                isinstance(window_minutes, int)
                and 0 < window_minutes <= core.DEFAULT_SHORT_TERM_WINDOW_MAX_MINUTES
            ):
                return f"Quota Sentry: {window_used}% used"
            label = duration_label(window_minutes, kind)
            return f"Quota Sentry: {label} {window_used}% used"
        if windows:
            pieces = [
                (
                    f"{duration_label(window.get('windowMinutes'), window.get('kind'))} "
                    f"{format_percent(window.get('usedPercent'))}% used"
                )
                for window in sorted(
                    windows,
                    key=lambda candidate: candidate.get("windowMinutes")
                    if isinstance(candidate.get("windowMinutes"), int)
                    else 10**12,
                )
            ]
            return "Quota Sentry: " + " | ".join(pieces)
        if used is not None:
            return f"Quota Sentry: {format_percent(used)}% used"

    pieces = [f"Quota Sentry: {status}"]
    if used is not None:
        pieces.append(f"{format_percent(used)}% used")
    if blocked_until:
        pieces.append(f"blocked until {blocked_until}")
    if updated_at:
        pieces.append(f"updated {updated_at}")
    if reason:
        pieces.append(str(reason))
    return " | ".join(pieces)


def status_health_warnings(
    state: Dict[str, Any],
    daemon_running: bool,
    now: Optional[datetime] = None,
    max_state_age_seconds: int = core.DEFAULT_MAX_STATE_AGE_SECONDS,
) -> list[str]:
    if not state or daemon_running:
        return []

    current_time = now or core.utc_now()
    updated_at = core.parse_timestamp(state.get("updatedAt"))
    if updated_at is None or current_time - updated_at > timedelta(seconds=max_state_age_seconds):
        return ["Quota Sentry: warning: state is stale and daemon is not running"]
    return []


def resolve_state_dir(value: Optional[str]) -> Path:
    return Path(value).expanduser().resolve() if value else core.cache_dir()


def resolve_config_path(value: Optional[str]) -> Path:
    return Path(value).expanduser().resolve() if value else core.default_config_path()


def script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "quota-sentry"


def runtime_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in (Path(core.__file__).resolve(), Path(__file__).resolve()):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def config_path_fingerprint(value: Optional[str]) -> str:
    config_path = resolve_config_path(value)
    return hashlib.sha256(os.fsencode(config_path)).hexdigest()[:16]


def default_codex_hooks_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "hooks.json"


def consume_hook_input(args: argparse.Namespace) -> None:
    if not getattr(args, "read_hook_input", False):
        return
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    while stream.read(1024 * 1024):
        pass
    args.read_hook_input = False


def wait_for_fresh_quota_state(
    state_path: Path,
    *,
    max_state_age_seconds: int,
    timeout_seconds: float,
    sleeper=time.sleep,
    now_func=core.utc_now,
    monotonic_func=time.monotonic,
) -> bool:
    deadline = monotonic_func() + max(0.0, timeout_seconds)
    while True:
        state = core.read_state(state_path)
        if state.get("status") == "blocked":
            return True
        if state.get("status") == "open" and core.state_is_fresh(
            state,
            now=now_func(),
            max_state_age_seconds=max_state_age_seconds,
        ):
            return True
        remaining = deadline - monotonic_func()
        if remaining <= 0:
            return False
        sleeper(min(0.1, remaining))


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _command_option(command: str, option: str) -> Optional[str]:
    match = re.search(rf"(?:^|\s){re.escape(option)}\s+(\S+)", command)
    return match.group(1) if match else None


def daemon_command_is_compatible(
    command: str,
    args: argparse.Namespace,
    expected_runtime_fingerprint: str,
) -> bool:
    if _command_option(command, "--runtime-fingerprint") != expected_runtime_fingerprint:
        return False
    if _command_option(command, "--config-path-fingerprint") != config_path_fingerprint(
        args.config_path
    ):
        return False

    running_source = _command_option(command, "--source")
    requested_source = args.source
    if requested_source in {core.AUTO_SOURCE, core.CODEX_APP_SERVER_SOURCE}:
        if running_source not in {core.AUTO_SOURCE, core.CODEX_APP_SERVER_SOURCE}:
            return False
    elif running_source != requested_source:
        return False

    comparisons = (
        ("--threshold-percent", args.threshold_percent, lambda running, requested: running <= requested),
        ("--reset-buffer-seconds", args.reset_buffer_seconds, lambda running, requested: running >= requested),
        ("--interval-seconds", args.interval_seconds, lambda running, requested: running <= requested),
        (
            "--near-threshold-percent",
            args.near_threshold_percent,
            lambda running, requested: running <= requested,
        ),
        (
            "--near-interval-seconds",
            args.near_interval_seconds,
            lambda running, requested: running <= requested,
        ),
        (
            "--critical-threshold-percent",
            args.critical_threshold_percent,
            lambda running, requested: running <= requested,
        ),
        (
            "--critical-interval-seconds",
            args.critical_interval_seconds,
            lambda running, requested: running <= requested,
        ),
    )
    for option, requested_value, predicate in comparisons:
        raw_value = _command_option(command, option)
        try:
            running_value = int(raw_value) if raw_value is not None else None
        except ValueError:
            return False
        if running_value is None or not predicate(running_value, requested_value):
            return False
    return True


def is_quota_sentry_daemon(
    pid: int,
    state_dir: Path,
    *,
    expected_args: Optional[argparse.Namespace] = None,
    expected_runtime_fingerprint: Optional[str] = None,
) -> bool:
    if not is_pid_alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = result.stdout.strip()
    expected_state_marker = (
        f"--state-dir {state_dir.expanduser().resolve()} --threshold-percent"
    )
    is_daemon = (
        result.returncode == 0
        and str(script_path()) in command
        and " daemon " in f" {command} "
        and expected_state_marker in command
    )
    if not is_daemon:
        return False
    if expected_args is None or expected_runtime_fingerprint is None:
        return True
    return daemon_command_is_compatible(
        command,
        expected_args,
        expected_runtime_fingerprint,
    )


def read_pid(pid_path: Path) -> Optional[int]:
    try:
        return int(pid_path.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def read_hooks_config(hooks_path: Path) -> Dict[str, Any]:
    if not hooks_path.exists():
        return {}
    text = hooks_path.read_text()
    if not text.strip():
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Codex hooks config must be a JSON object")
    return payload


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n")


def remove_pid_if_owned(pid_path: Path, pid: int) -> None:
    if read_pid(pid_path) != pid:
        return
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def try_acquire_daemon_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    if core.fcntl is None:
        return handle
    try:
        core.fcntl.flock(handle.fileno(), core.fcntl.LOCK_EX | core.fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return handle


def release_daemon_lock(handle) -> None:
    if handle is None:
        return
    try:
        if core.fcntl is not None:
            core.fcntl.flock(handle.fileno(), core.fcntl.LOCK_UN)
    finally:
        handle.close()


def wait_for_registered_daemon(
    pid_path: Path,
    process,
    *,
    timeout_seconds: float = core.DEFAULT_DAEMON_START_TIMEOUT_SECONDS,
    expected_args: Optional[argparse.Namespace] = None,
    expected_runtime_fingerprint: Optional[str] = None,
) -> Optional[int]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        registered_pid = read_pid(pid_path)
        if registered_pid and is_quota_sentry_daemon(
            registered_pid,
            pid_path.parent,
            expected_args=expected_args,
            expected_runtime_fingerprint=expected_runtime_fingerprint,
        ):
            return registered_pid
        if process.poll() is not None or time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def percent_value(value: str) -> int:
    try:
        percent = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 100") from exc
    if percent < 1 or percent > 100:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 100")
    return percent


def poll_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    state_path = core.default_state_path(state_dir)
    core.poll_once(
        state_path,
        threshold_percent=args.threshold_percent,
        reset_buffer_seconds=args.reset_buffer_seconds,
        source=args.source,
        config_path=resolve_config_path(args.config_path),
    )
    print(status_text(core.read_state(state_path)))
    return 0


def daemon_command(args: argparse.Namespace) -> int:
    if core.fcntl is None:
        print(
            "Quota Sentry: daemon enforcement requires POSIX file locking; "
            "this platform is not supported.",
            file=sys.stderr,
        )
        return 1
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    lock_handle = try_acquire_daemon_lock(core.default_daemon_lock_path(state_dir))
    if lock_handle is None:
        return 0
    own_pid = os.getpid()
    write_pid(pid_path, own_pid)
    state_path = core.default_state_path(state_dir)

    keep_running = True

    def stop(_signum, _frame):
        nonlocal keep_running
        keep_running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while keep_running:
            decision = core.poll_once(
                state_path,
                threshold_percent=args.threshold_percent,
                reset_buffer_seconds=args.reset_buffer_seconds,
                source=args.source,
                config_path=resolve_config_path(args.config_path),
            )
            persisted_state = core.read_state(state_path)
            print(status_text(persisted_state), flush=True)
            sleep_seconds = core.next_poll_interval_seconds(
                decision,
                base_interval_seconds=args.interval_seconds,
                near_threshold_percent=args.near_threshold_percent,
                near_interval_seconds=args.near_interval_seconds,
                critical_threshold_percent=args.critical_threshold_percent,
                critical_interval_seconds=args.critical_interval_seconds,
            )
            if persisted_state.get("status") != "open":
                sleep_seconds = min(
                    sleep_seconds,
                    max(1, args.critical_interval_seconds),
                )
            for _ in range(sleep_seconds):
                if not keep_running:
                    break
                time.sleep(1)
        return 0
    finally:
        remove_pid_if_owned(pid_path, own_pid)
        release_daemon_lock(lock_handle)


def start_command(args: argparse.Namespace) -> int:
    consume_hook_input(args)
    if core.fcntl is None:
        if not args.quiet:
            print(
                "Quota Sentry: daemon enforcement requires POSIX file locking; "
                "this platform is not supported.",
                file=sys.stderr,
            )
        return 1
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    existing_pid = read_pid(pid_path)
    current_runtime_fingerprint = runtime_fingerprint()
    current_config_path_fingerprint = config_path_fingerprint(args.config_path)
    if existing_pid and is_quota_sentry_daemon(
        existing_pid,
        state_dir,
        expected_args=args,
        expected_runtime_fingerprint=current_runtime_fingerprint,
    ):
        if not args.quiet:
            print(f"Quota Sentry: daemon already running with pid {existing_pid}")
        return 0
    if existing_pid and is_quota_sentry_daemon(existing_pid, state_dir):
        try:
            os.kill(existing_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            if not args.quiet:
                print(
                    f"Quota Sentry: could not stop incompatible daemon pid "
                    f"{existing_pid}: {exc}",
                    file=sys.stderr,
                )
            return 1
        deadline = time.monotonic() + core.DEFAULT_DAEMON_STOP_TIMEOUT_SECONDS
        while is_pid_alive(existing_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if is_pid_alive(existing_pid):
            if not args.quiet:
                print(
                    f"Quota Sentry: incompatible daemon pid {existing_pid} did not stop",
                    file=sys.stderr,
                )
            return 1
        remove_pid_if_owned(pid_path, existing_pid)
    if existing_pid:
        remove_pid_if_owned(pid_path, existing_pid)

    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = core.default_log_path(state_dir)
    command = [
        str(script_path()),
        "daemon",
        "--state-dir",
        str(state_dir),
        "--threshold-percent",
        str(args.threshold_percent),
        "--reset-buffer-seconds",
        str(args.reset_buffer_seconds),
        "--source",
        args.source,
        "--interval-seconds",
        str(args.interval_seconds),
        "--near-threshold-percent",
        str(args.near_threshold_percent),
        "--near-interval-seconds",
        str(args.near_interval_seconds),
        "--critical-threshold-percent",
        str(args.critical_threshold_percent),
        "--critical-interval-seconds",
        str(args.critical_interval_seconds),
        "--runtime-fingerprint",
        current_runtime_fingerprint,
        "--config-path-fingerprint",
        current_config_path_fingerprint,
    ]
    if args.config_path:
        command.extend(["--config-path", str(resolve_config_path(args.config_path))])
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    registered_pid = wait_for_registered_daemon(
        pid_path,
        process,
        expected_args=args,
        expected_runtime_fingerprint=current_runtime_fingerprint,
    )
    if registered_pid is None:
        if process.poll() is None:
            process.terminate()
        if not args.quiet:
            print("Quota Sentry: daemon failed to register", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"Quota Sentry: daemon started with pid {registered_pid}")
        print(f"Quota Sentry: log {log_path}")
    return 0


def stop_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    pid = read_pid(pid_path)
    if not pid:
        print("Quota Sentry: daemon is not running")
        return 0
    if not is_quota_sentry_daemon(pid, state_dir):
        remove_pid_if_owned(pid_path, pid)
        print("Quota Sentry: stale pid removed")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid_if_owned(pid_path, pid)
        print(f"Quota Sentry: stopped daemon pid {pid}")
        return 0
    except OSError as exc:
        print(f"Quota Sentry: could not stop daemon pid {pid}: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + core.DEFAULT_DAEMON_STOP_TIMEOUT_SECONDS
    while is_pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if is_pid_alive(pid):
        print(f"Quota Sentry: daemon pid {pid} did not stop", file=sys.stderr)
        return 1
    remove_pid_if_owned(pid_path, pid)
    print(f"Quota Sentry: stopped daemon pid {pid}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    state = core.read_state(core.default_state_path(state_dir))
    print(status_text(state))
    pid = read_pid(core.default_pid_path(state_dir))
    daemon_running = bool(pid and is_quota_sentry_daemon(pid, state_dir))
    if daemon_running and args.verbose:
        print(f"Quota Sentry: daemon pid {pid}")
    for warning in status_health_warnings(state, daemon_running=daemon_running):
        print(warning)
    return 0


def guard_command(args: argparse.Namespace) -> int:
    consume_hook_input(args)
    state_dir = resolve_state_dir(args.state_dir)
    state_path = core.default_state_path(state_dir)

    def poller():
        return core.poll_once(
            state_path,
            threshold_percent=args.threshold_percent,
            reset_buffer_seconds=args.reset_buffer_seconds,
            source=args.source,
            config_path=resolve_config_path(args.config_path),
        )

    if args.fail_closed_format == "stop-json":
        fail_closed_output = lambda message: print(
            json.dumps({"continue": False, "stopReason": message})
        )
        fail_closed_exit_code = 0
    elif args.fail_closed_format == "block-json":
        fail_closed_output = lambda message: print(
            json.dumps({"decision": "block", "reason": message})
        )
        fail_closed_exit_code = 0
    else:
        fail_closed_output = lambda message: print(message, file=sys.stderr)
        fail_closed_exit_code = 2

    return core.wait_if_blocked(
        state_path,
        poller=poller,
        max_state_age_seconds=args.max_state_age_seconds,
        poll_interval_seconds=args.interval_seconds,
        verbose=args.verbose,
        notify=not args.no_notify,
        state_only=args.state_only,
        fail_closed_after_seconds=args.fail_closed_after_seconds,
        fail_closed_output=fail_closed_output,
        fail_closed_exit_code=fail_closed_exit_code,
        require_fresh_state=args.require_fresh_state,
    )


def prompt_guard_command(args: argparse.Namespace) -> int:
    consume_hook_input(args)
    if os.environ.get("QUOTA_SENTRY_DISABLE") == "1":
        return 0
    args.quiet = True
    try:
        start_result = start_command(args)
    except Exception as exc:
        print(
            "Quota Sentry: daemon startup failed; refusing to submit the prompt: "
            f"{exc}",
            file=sys.stderr,
        )
        return 2
    if start_result != 0:
        print(
            "Quota Sentry: daemon startup failed; refusing to submit the prompt.",
            file=sys.stderr,
        )
        return 2

    state_path = core.default_state_path(resolve_state_dir(args.state_dir))
    if not wait_for_fresh_quota_state(
        state_path,
        max_state_age_seconds=args.max_state_age_seconds,
        timeout_seconds=args.startup_wait_seconds,
    ):
        print(
            "Quota Sentry: daemon did not publish fresh quota state; refusing to submit the prompt.",
            file=sys.stderr,
        )
        return 2

    args.state_only = True
    args.no_notify = True
    return guard_command(args)


def install_hook_command(args: argparse.Namespace) -> int:
    if core.fcntl is None:
        print(
            "Quota Sentry: hook enforcement requires POSIX file locking; "
            "this platform is not supported.",
            file=sys.stderr,
        )
        return 1
    hooks_path = Path(args.hooks_path).expanduser().resolve()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    install_lock = try_acquire_daemon_lock(
        hooks_path.with_suffix(hooks_path.suffix + ".quota-sentry-install.lock")
    )
    if install_lock is None:
        print("Quota Sentry: another hook installation is already running", file=sys.stderr)
        return 1

    default_hooks_path = default_codex_hooks_path().resolve()
    codex_config_path = hooks_path.parent / "config.toml"
    backup_path = hooks_path.with_suffix(hooks_path.suffix + ".bak")
    hooks_existed = False
    hooks_before = None
    codex_config_existed = False
    codex_config_before = None
    backup_existed = False
    backup_before = None
    snapshot_complete = False
    try:
        hooks_existed = hooks_path.exists()
        hooks_before = hooks_path.read_bytes() if hooks_existed else None
        codex_config_existed = codex_config_path.exists()
        codex_config_before = (
            codex_config_path.read_bytes() if codex_config_existed else None
        )
        backup_existed = backup_path.exists()
        backup_before = backup_path.read_bytes() if backup_existed else None
        snapshot_complete = True

        existing = read_hooks_config(hooks_path)
        selected_script_path = (
            Path(args.script_path).expanduser().resolve()
            if args.script_path
            else script_path()
        )
        merged = core.merge_codex_hooks(existing, selected_script_path)
        if merged != existing:
            if hooks_path.exists() and hooks_path.read_text().strip():
                core.write_json_atomic(backup_path, existing)
                print(f"Quota Sentry: backed up existing hooks to {backup_path}")
            core.write_json_atomic(hooks_path, merged)
        if hooks_path == default_hooks_path:
            trusted_count = core.trust_codex_hooks(
                hooks_path,
                selected_script_path,
                cwd=Path.cwd(),
            )
            print(f"Quota Sentry: trusted and enabled {trusted_count} Codex hooks")
    except Exception as exc:
        if snapshot_complete:
            try:
                if hooks_existed and hooks_before is not None:
                    core.write_bytes_atomic(hooks_path, hooks_before)
                else:
                    hooks_path.unlink(missing_ok=True)
                if backup_existed and backup_before is not None:
                    core.write_bytes_atomic(backup_path, backup_before)
                else:
                    backup_path.unlink(missing_ok=True)
                if hooks_path == default_hooks_path:
                    if codex_config_existed and codex_config_before is not None:
                        core.write_bytes_atomic(codex_config_path, codex_config_before)
                    else:
                        codex_config_path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                print(
                    f"Quota Sentry: installation failed and rollback also failed: {rollback_exc}",
                    file=sys.stderr,
                )
                return 1
        print(
            "Quota Sentry: Codex could not install and trust the hook definitions; "
            "hooks are not active and the previous hook and trust configuration "
            f"was restored: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        release_daemon_lock(install_lock)

    print(f"Quota Sentry: installed Codex hooks in {hooks_path}")
    print("Quota Sentry: restart every running Codex process to load the new hook definitions.")
    return 0


def configure_command(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config_path)
    current = core.read_config(config_path)
    weekly_mode = args.weekly_mode or current.weekly_mode
    weekly_threshold_percent = (
        args.weekly_threshold_percent
        if args.weekly_threshold_percent is not None
        else current.weekly_threshold_percent
    )
    config = core.QuotaConfig(
        weekly_mode=weekly_mode,
        weekly_threshold_percent=weekly_threshold_percent,
    )
    core.write_config(config_path, config)
    print(
        "Quota Sentry: weekly "
        f"{config.weekly_mode} at {config.weekly_threshold_percent}%"
    )
    print(f"Quota Sentry: config {config_path}")
    return 0


def fail_closed_hook_result(args: argparse.Namespace, message: str) -> int:
    output_format = getattr(args, "fail_closed_format", "exit-2")
    if output_format == "stop-json":
        print(json.dumps({"continue": False, "stopReason": message}))
        return 0
    if output_format == "block-json":
        print(json.dumps({"decision": "block", "reason": message}))
        return 0
    print(message, file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quota-sentry")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=None)
    common.add_argument("--config-path", default=None)
    common.add_argument("--threshold-percent", type=int, default=core.DEFAULT_THRESHOLD_PERCENT)
    common.add_argument("--reset-buffer-seconds", type=int, default=core.DEFAULT_RESET_BUFFER_SECONDS)
    common.add_argument("--read-hook-input", action="store_true", help=argparse.SUPPRESS)
    common.add_argument("--require-fresh-state", action="store_true", help=argparse.SUPPRESS)
    common.add_argument(
        "--source",
        choices=[core.AUTO_SOURCE, core.CODEX_APP_SERVER_SOURCE, core.CODEXBAR_SOURCE],
        default=core.DEFAULT_USAGE_SOURCE,
        help="Quota source for live polling. Default: auto.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    poll_parser = subparsers.add_parser("poll", parents=[common])
    poll_parser.set_defaults(func=poll_command)

    daemon_parser = subparsers.add_parser("daemon", parents=[common])
    daemon_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    daemon_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    daemon_parser.add_argument("--near-interval-seconds", type=int, default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS)
    daemon_parser.add_argument(
        "--critical-threshold-percent",
        type=int,
        default=core.DEFAULT_CRITICAL_THRESHOLD_PERCENT,
    )
    daemon_parser.add_argument(
        "--critical-interval-seconds",
        type=int,
        default=core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS,
    )
    daemon_parser.add_argument("--runtime-fingerprint", default=None, help=argparse.SUPPRESS)
    daemon_parser.add_argument(
        "--config-path-fingerprint", default=None, help=argparse.SUPPRESS
    )
    daemon_parser.set_defaults(func=daemon_command)

    start_parser = subparsers.add_parser("start", parents=[common])
    start_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    start_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    start_parser.add_argument("--near-interval-seconds", type=int, default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS)
    start_parser.add_argument(
        "--critical-threshold-percent",
        type=int,
        default=core.DEFAULT_CRITICAL_THRESHOLD_PERCENT,
    )
    start_parser.add_argument(
        "--critical-interval-seconds",
        type=int,
        default=core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS,
    )
    start_parser.add_argument("--quiet", action="store_true")
    start_parser.set_defaults(func=start_command)

    stop_parser = subparsers.add_parser("stop", parents=[common])
    stop_parser.set_defaults(func=stop_command)

    status_parser = subparsers.add_parser("status", parents=[common])
    status_parser.add_argument("--verbose", action="store_true")
    status_parser.set_defaults(func=status_command)

    guard_parser = subparsers.add_parser("guard", parents=[common])
    guard_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    guard_parser.add_argument("--max-state-age-seconds", type=int, default=core.DEFAULT_MAX_STATE_AGE_SECONDS)
    guard_parser.add_argument("--verbose", action="store_true")
    guard_parser.add_argument("--no-notify", action="store_true")
    guard_parser.add_argument("--state-only", action="store_true")
    guard_parser.add_argument("--fail-closed-after-seconds", type=float, default=None)
    guard_parser.add_argument(
        "--fail-closed-format",
        choices=["exit-2", "stop-json", "block-json"],
        default="exit-2",
        help=argparse.SUPPRESS,
    )
    guard_parser.set_defaults(func=guard_command)

    prompt_guard_parser = subparsers.add_parser("prompt-guard", parents=[common])
    prompt_guard_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    prompt_guard_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    prompt_guard_parser.add_argument(
        "--near-interval-seconds",
        type=int,
        default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS,
    )
    prompt_guard_parser.add_argument(
        "--critical-threshold-percent",
        type=int,
        default=core.DEFAULT_CRITICAL_THRESHOLD_PERCENT,
    )
    prompt_guard_parser.add_argument(
        "--critical-interval-seconds",
        type=int,
        default=core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS,
    )
    prompt_guard_parser.add_argument(
        "--max-state-age-seconds",
        type=int,
        default=core.DEFAULT_MAX_STATE_AGE_SECONDS,
    )
    prompt_guard_parser.add_argument(
        "--startup-wait-seconds",
        type=float,
        default=(
            core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS
            + core.DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS
            + 5
        ),
        help=argparse.SUPPRESS,
    )
    prompt_guard_parser.add_argument("--verbose", action="store_true")
    prompt_guard_parser.add_argument("--no-notify", action="store_true")
    prompt_guard_parser.add_argument("--fail-closed-after-seconds", type=float, default=None)
    prompt_guard_parser.add_argument(
        "--fail-closed-format",
        choices=["exit-2", "stop-json", "block-json"],
        default="exit-2",
        help=argparse.SUPPRESS,
    )
    prompt_guard_parser.set_defaults(func=prompt_guard_command)

    install_parser = subparsers.add_parser("install-hook", parents=[common])
    install_parser.add_argument("--hooks-path", default=str(default_codex_hooks_path()))
    install_parser.add_argument("--script-path", default=None)
    install_parser.set_defaults(func=install_hook_command)

    configure_parser = subparsers.add_parser("configure", parents=[common])
    configure_parser.add_argument(
        "--weekly-mode",
        choices=[core.WEEKLY_MODE_ADVISORY, core.WEEKLY_MODE_HARD_BLOCK],
        default=None,
    )
    configure_parser.add_argument("--weekly-threshold-percent", type=percent_value, default=None)
    configure_parser.set_defaults(func=configure_command)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception:
        if args.command in {"guard", "prompt-guard"} and getattr(
            args, "require_fresh_state", False
        ):
            return fail_closed_hook_result(args, core.HOOK_INTERNAL_FAILURE_MESSAGE)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
