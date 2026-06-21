#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${TMPDIR:-/tmp}/quota-sentry-demo-state"

rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR"

write_state() {
  local status="$1"
  local used_percent="$2"
  python3 - "$STATE_DIR/state.json" "$status" "$used_percent" <<'PY'
import json
import sys
from datetime import datetime, timedelta, timezone

path, status, used_percent = sys.argv[1], sys.argv[2], int(sys.argv[3])
now = datetime.now(timezone.utc).replace(microsecond=0)
resets_at = now + timedelta(minutes=37)
blocked_until = resets_at + timedelta(seconds=60) if status == "blocked" else None

payload = {
    "status": status,
    "reason": f"{used_percent}% of the 300-minute Codex quota is used",
    "usedPercent": used_percent,
    "windowMinutes": 300,
    "resetsAt": resets_at.isoformat().replace("+00:00", "Z"),
    "blockedUntil": blocked_until.isoformat().replace("+00:00", "Z") if blocked_until else None,
    "failOpen": False,
    "updatedAt": now.isoformat().replace("+00:00", "Z"),
}

with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

run() {
  printf "\n$ %s\n" "$*"
  "$@"
  sleep 0.8
}

cd "$ROOT"
clear

printf "Quota Sentry demo\n"
printf "Synthetic quota state only. No live Codex quota is used.\n"
sleep 1

write_state open 84
run ./scripts/quota-sentry status --state-dir "$STATE_DIR"

write_state open 94
run ./scripts/quota-sentry status --state-dir "$STATE_DIR"

write_state blocked 97
run ./scripts/quota-sentry status --state-dir "$STATE_DIR"

printf "\n$ ./scripts/quota-sentry install-hook\n"
printf "Quota Sentry installs global Codex hooks that read cached state before new prompt and tool activity.\n"
sleep 1.2

printf "\nResult: new Codex activity waits near the threshold; stale or missing quota data fails open.\n"
sleep 2
