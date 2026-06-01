---
name: quota-sentry
description: Use when managing Quota Sentry, checking Codex quota guard state, starting the background quota monitor, or installing the global Codex hook.
---

# Quota Sentry

Quota Sentry is a local Codex quota guard.

## Behavior

- Uses `codexbar usage --provider codex --source cli --format json`.
- Watches the 5-hour Codex window (`windowMinutes: 300`).
- Starts blocking when `usedPercent >= 95`.
- Waits until `resetsAt` plus a 60-second buffer.
- Fails open if usage data is unavailable.

## Important Constraint

The background daemon only observes quota and writes state every minute. Actual blocking requires the synchronous `guard` command to run from a global Codex hook or wrapper. Do not claim that the daemon can interrupt an already-running model request.

## Commands

Run from the plugin root:

```bash
./scripts/quota-sentry poll
./scripts/quota-sentry start
./scripts/quota-sentry status
./scripts/quota-sentry guard
./scripts/quota-sentry stop
./scripts/quota-sentry install-hook
```

Use `install-hook` to merge global hooks into `~/.codex/hooks.json`. Restart Codex if the current session does not pick them up.

## Bypass

Set `QUOTA_SENTRY_DISABLE=1` to bypass blocking.
