---
name: quota-sentry
description: Use when managing Quota Sentry, checking Codex quota guard state, starting the background quota monitor, or installing the global Codex hook.
---

# Quota Sentry

Quota Sentry is a local Codex quota guard.

Daemon enforcement supports macOS and Linux. Do not install it as an enforcement mechanism on Windows because `v0.1.x` requires POSIX process and file locking.

## Behavior

- Uses `codex app-server --stdio` by default, reading `account/rateLimits/read`.
- Keeps `auto` pinned to Codex app-server authority; it never downgrades to CodexBar.
- Retains explicit `--source codexbar` only as an optional compatibility and test adapter.
- Reads both the canonical bucket and `rateLimitsByLimitId` when available.
- Classifies quota policy by duration, not the `primary` or `secondary` source slot.
- Guards canonical short-term windows up to 24 hours and starts blocking at `usedPercent >= 95`.
- Hard-blocks the seven-day window (`windowMinutes: 10080`) at `usedPercent >= 99` by default.
- Hard-blocks explicit backend exhaustion signals even when no percentage window is returned.
- Blocks unfamiliar long-term canonical windows at `100%` as a schema-drift fail-safe. Durationless or canonically ambiguous data fails closed; auxiliary buckets remain advisory only when a canonical bucket is known.
- Supports explicit weekly advisory mode as an opt-out from enforcement.
- Uses `resetsAt` plus a 60-second buffer as an initial wait estimate.
- Latches a confirmed block until Codex reports that policy window below threshold.
- Fails closed in installed hooks when source state is unknown, stale, or missing.

## Important Constraint

The background daemon observes quota and writes state every five minutes by default, tightening its cadence near the quota threshold. Actual blocking requires a synchronous guard path to run from a global Codex hook or wrapper. Do not claim that the daemon can interrupt an already-running model request.

Installed Codex hooks consume hook stdin immediately and read cached daemon state. `SessionStart` runs `start --quiet` synchronously. `UserPromptSubmit` uses `prompt-guard`; `PreToolUse`, `PostToolUse`, and `PreCompact` use cache-only guards and fail closed when cached state is stale or missing.

The guard cannot interrupt a model request already in flight, and Codex documents specialized or hosted tool paths as possible exceptions to local tool hooks. Do not claim stronger enforcement than the host lifecycle provides.

A `PostToolUse` denial rejects or replaces the completed tool result but can return feedback to the model. The long host timeout is therefore the primary protection against reaching that fallback during a normal quota block.

## Commands

Run from the plugin root:

```bash
./scripts/quota-sentry poll
./scripts/quota-sentry start
./scripts/quota-sentry status
./scripts/quota-sentry guard
./scripts/quota-sentry stop
./scripts/quota-sentry install-hook
./scripts/quota-sentry configure --weekly-mode hard-block --weekly-threshold-percent 99
./scripts/autonomous-test
```

Use `install-hook` to atomically merge global hooks into `$CODEX_HOME/hooks.json` or `~/.codex/hooks.json`, persist exact-hash trust, and verify activation. A failed trust or activation check restores both hook and trust configuration. Restart every running Codex process after any hook update.

Use `./scripts/autonomous-test` for the E2E harness. It performs one live Codex quota-source smoke poll and uses fake `codex` and `codexbar` binaries for quota-edge scenarios so it does not burn quota through repeated real prompts.

`poll`, `start`, and `guard` accept `--source auto|codex-app-server|codexbar`. Default `auto` uses only the authoritative Codex app-server and requires no CodexBar installation.

Weekly usage hard-blocks at `99%` by default without requiring a config file. Use `configure --weekly-mode hard-block --weekly-threshold-percent <percent>` to change or explicitly persist that policy, or `configure --weekly-mode advisory` to opt out and retain status-only weekly behavior. Config lives at `~/.config/quota-sentry/config.json`; installed hook commands should remain unchanged.

Do not infer policy from source position. A weekly-only response may legitimately place the seven-day window in `primary`; classify it by its seven-day duration and apply the resolved weekly policy.

`guard` keeps stdout/stderr quiet by default to avoid flooding Codex hook context after long waits. It still writes one wait notice directly to the controlling terminal when waiting starts unless `--no-notify` is set. Use `guard --verbose` only for manual debugging, `guard --no-notify` to suppress the terminal notice, and `guard --state-only` when a hook must not perform a live quota-source poll. Installed blocking hooks allow 366 days and trigger an internal fail-closed watchdog ten minutes before Codex's timeout.

## Bypass

Launch or restart Codex with `QUOTA_SENTRY_DISABLE=1` to bypass blocking.
