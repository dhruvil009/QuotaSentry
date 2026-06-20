import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from quota_sentry import core


def status_text(state: Dict[str, Any]) -> str:
    if not state:
        return "Quota Sentry: no state found"

    status = state.get("status", "unknown")
    used = state.get("usedPercent")
    blocked_until = state.get("blockedUntil")
    updated_at = state.get("updatedAt")
    reason = state.get("reason")

    if status == "open" and used is not None:
        return f"Quota Sentry: {used}% used"

    pieces = [f"Quota Sentry: {status}"]
    if used is not None:
        pieces.append(f"{used}% used")
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


def script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "quota-sentry"


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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


def poll_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    decision = core.poll_once(
        core.default_state_path(state_dir),
        threshold_percent=args.threshold_percent,
        reset_buffer_seconds=args.reset_buffer_seconds,
    )
    print(status_text(core.state_from_decision(decision)))
    return 0


def daemon_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    write_pid(pid_path, os.getpid())
    state_path = core.default_state_path(state_dir)

    keep_running = True

    def stop(_signum, _frame):
        nonlocal keep_running
        keep_running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while keep_running:
        decision = core.poll_once(
            state_path,
            threshold_percent=args.threshold_percent,
            reset_buffer_seconds=args.reset_buffer_seconds,
        )
        print(status_text(core.state_from_decision(decision)), flush=True)
        sleep_seconds = core.next_poll_interval_seconds(
            decision,
            base_interval_seconds=args.interval_seconds,
            near_threshold_percent=args.near_threshold_percent,
            near_interval_seconds=args.near_interval_seconds,
            critical_threshold_percent=args.critical_threshold_percent,
            critical_interval_seconds=args.critical_interval_seconds,
        )
        for _ in range(sleep_seconds):
            if not keep_running:
                break
            time.sleep(1)

    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass
    return 0


def start_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    existing_pid = read_pid(pid_path)
    if existing_pid and is_pid_alive(existing_pid):
        if not args.quiet:
            print(f"Quota Sentry: daemon already running with pid {existing_pid}")
        return 0

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
    ]
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    write_pid(pid_path, process.pid)
    if not args.quiet:
        print(f"Quota Sentry: daemon started with pid {process.pid}")
        print(f"Quota Sentry: log {log_path}")
    return 0


def stop_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    pid_path = core.default_pid_path(state_dir)
    pid = read_pid(pid_path)
    if not pid:
        print("Quota Sentry: daemon is not running")
        return 0
    if not is_pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        print("Quota Sentry: stale pid removed")
        return 0
    os.kill(pid, signal.SIGTERM)
    print(f"Quota Sentry: stopped daemon pid {pid}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    state = core.read_state(core.default_state_path(state_dir))
    print(status_text(state))
    pid = read_pid(core.default_pid_path(state_dir))
    daemon_running = bool(pid and is_pid_alive(pid))
    if daemon_running and args.verbose:
        print(f"Quota Sentry: daemon pid {pid}")
    for warning in status_health_warnings(state, daemon_running=daemon_running):
        print(warning)
    return 0


def guard_command(args: argparse.Namespace) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    state_path = core.default_state_path(state_dir)

    def poller():
        return core.poll_once(
            state_path,
            threshold_percent=args.threshold_percent,
            reset_buffer_seconds=args.reset_buffer_seconds,
        )

    return core.wait_if_blocked(
        state_path,
        poller=poller,
        max_state_age_seconds=args.max_state_age_seconds,
        poll_interval_seconds=args.interval_seconds,
        verbose=args.verbose,
        notify=not args.no_notify,
        state_only=args.state_only,
    )


def prompt_guard_command(args: argparse.Namespace) -> int:
    args.quiet = True
    start_result = start_command(args)
    if start_result != 0:
        return start_result

    args.state_only = True
    args.no_notify = True
    return guard_command(args)


def install_hook_command(args: argparse.Namespace) -> int:
    hooks_path = Path(args.hooks_path).expanduser().resolve()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_hooks_config(hooks_path)
    if hooks_path.exists() and hooks_path.read_text().strip():
        backup_path = hooks_path.with_suffix(hooks_path.suffix + ".bak")
        backup_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
        print(f"Quota Sentry: backed up existing hooks to {backup_path}")

    selected_script_path = Path(args.script_path).expanduser().resolve() if args.script_path else script_path()
    merged = core.merge_codex_hooks(existing, selected_script_path)
    hooks_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    print(f"Quota Sentry: installed Codex hooks in {hooks_path}")
    print("Quota Sentry: restart Codex for hook discovery if this session does not pick them up.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quota-sentry")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", default=None)
    common.add_argument("--threshold-percent", type=int, default=core.DEFAULT_THRESHOLD_PERCENT)
    common.add_argument("--reset-buffer-seconds", type=int, default=core.DEFAULT_RESET_BUFFER_SECONDS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    poll_parser = subparsers.add_parser("poll", parents=[common])
    poll_parser.set_defaults(func=poll_command)

    daemon_parser = subparsers.add_parser("daemon", parents=[common])
    daemon_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    daemon_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    daemon_parser.add_argument("--near-interval-seconds", type=int, default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS)
    daemon_parser.add_argument("--critical-threshold-percent", type=int, default=core.DEFAULT_CRITICAL_THRESHOLD_PERCENT)
    daemon_parser.add_argument("--critical-interval-seconds", type=int, default=core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)
    daemon_parser.set_defaults(func=daemon_command)

    start_parser = subparsers.add_parser("start", parents=[common])
    start_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    start_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    start_parser.add_argument("--near-interval-seconds", type=int, default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS)
    start_parser.add_argument("--critical-threshold-percent", type=int, default=core.DEFAULT_CRITICAL_THRESHOLD_PERCENT)
    start_parser.add_argument("--critical-interval-seconds", type=int, default=core.DEFAULT_CRITICAL_POLL_INTERVAL_SECONDS)
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
    guard_parser.set_defaults(func=guard_command)

    prompt_guard_parser = subparsers.add_parser("prompt-guard", parents=[common])
    prompt_guard_parser.add_argument("--interval-seconds", type=int, default=core.DEFAULT_POLL_INTERVAL_SECONDS)
    prompt_guard_parser.add_argument("--near-threshold-percent", type=int, default=core.DEFAULT_NEAR_THRESHOLD_PERCENT)
    prompt_guard_parser.add_argument("--near-interval-seconds", type=int, default=core.DEFAULT_NEAR_POLL_INTERVAL_SECONDS)
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
    prompt_guard_parser.add_argument("--verbose", action="store_true")
    prompt_guard_parser.add_argument("--no-notify", action="store_true")
    prompt_guard_parser.set_defaults(func=prompt_guard_command)

    install_parser = subparsers.add_parser("install-hook", parents=[common])
    install_parser.add_argument("--hooks-path", default=str(Path.home() / ".codex" / "hooks.json"))
    install_parser.add_argument("--script-path", default=None)
    install_parser.set_defaults(func=install_hook_command)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
