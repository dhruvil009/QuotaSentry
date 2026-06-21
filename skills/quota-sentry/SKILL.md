---
name: quota-sentry
description: Use when managing Quota Sentry, checking Codex quota guard state, starting the background quota monitor, or installing the global Codex hook.
---

# Quota Sentry

Quota Sentry is a local Codex quota guard.

## Behavior

- Uses `codex app-server --stdio` by default, reading `account/rateLimits/read`.
- Falls back to CodexBar when `--source auto` cannot use the app-server path.
- Watches the 5-hour Codex window (`windowMinutes: 300`).
- Starts blocking when `usedPercent >= 95`.
- Waits until `resetsAt` plus a 60-second buffer.
- Fails open if usage data is unavailable.

## Important Constraint

The background daemon observes quota and writes state every five minutes by default, tightening its cadence near the quota threshold. Actual blocking requires a synchronous guard path to run from a global Codex hook or wrapper. Do not claim that the daemon can interrupt an already-running model request.

Installed Codex hooks must only read cached daemon state and must not invoke a live quota source from the hook process. `SessionStart` should run `start --quiet` synchronously because Codex 0.140.0 skips async hooks. `UserPromptSubmit` should use `prompt-guard`; `PreToolUse` should use `guard --state-only --no-notify`.

## Commands

Run from the plugin root:

```bash
./scripts/quota-sentry poll
./scripts/quota-sentry start
./scripts/quota-sentry status
./scripts/quota-sentry guard
./scripts/quota-sentry stop
./scripts/quota-sentry install-hook
./scripts/autonomous-test
```

Use `install-hook` to merge global hooks into `~/.codex/hooks.json`. Restart Codex if the current session does not pick them up.

Use `./scripts/autonomous-test` for the E2E harness. It performs one live Codex quota-source smoke poll and uses fake `codex` and `codexbar` binaries for quota-edge scenarios so it does not burn quota through repeated real prompts.

`poll`, `start`, and `guard` accept `--source auto|codex-app-server|codexbar`. Default `auto` uses Codex app-server first and falls back to CodexBar.

`guard` keeps stdout/stderr quiet by default to avoid flooding Codex hook context after long waits. It still writes one wait notice directly to the controlling terminal when waiting starts unless `--no-notify` is set. Use `guard --verbose` only for manual debugging, `guard --no-notify` to suppress the terminal notice, and `guard --state-only` when a hook must not perform a live quota-source poll.

## Bypass

Set `QUOTA_SENTRY_DISABLE=1` to bypass blocking.
