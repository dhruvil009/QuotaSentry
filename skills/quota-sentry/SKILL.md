---
name: quota-sentry
description: Use when managing Quota Sentry, checking Codex or Claude Code quota guard state, starting the background quota monitor, or installing its hooks.
---

# Quota Sentry

Quota Sentry is a local quota guard for Codex and Claude Code. The behavior below describes Codex; see Claude Code at the end.

Daemon enforcement supports macOS and Linux. Do not install it as an enforcement mechanism on Windows because it requires POSIX process and file locking.

## Behavior

- Uses `codex app-server --stdio` by default, reading `account/rateLimits/read`.
- Keeps `auto` pinned to Codex app-server authority; it never downgrades to CodexBar.
- Retains explicit `--source codexbar` only as an optional compatibility and test adapter.
- Reads both the canonical bucket and `rateLimitsByLimitId` when available.
- Classifies quota policy by duration, not the `primary` or `secondary` source slot.
- Guards canonical short-term windows up to 24 hours and starts blocking at `usedPercent >= 90`, preserving 10% of the window.
- Hard-blocks the seven-day window (`windowMinutes: 10080`) at `usedPercent >= 99` by default.
- Hard-blocks explicit backend exhaustion signals even when no percentage window is returned.
- Blocks unfamiliar long-term canonical windows at `100%` as a schema-drift fail-safe. Durationless or canonically ambiguous data becomes unavailable; auxiliary buckets remain advisory only when a canonical bucket is known.
- Supports explicit weekly advisory mode as an opt-out from enforcement.
- Uses `resetsAt` plus a 60-second buffer as an initial wait estimate.
- Latches a confirmed block until Codex reports that policy window below threshold.
- In installed hooks, holds a confirmed block through unknown, stale, or missing source state; without one, requests daemon recovery and continues.

## Important Constraint

The background daemon observes quota every five minutes for weekly-only state, every minute when a short-term window is present, and at most every 30 seconds for recovery, near-critical usage, or a retained block. Actual blocking requires a synchronous guard path to run from a global Codex hook or wrapper. Do not claim that the daemon can interrupt an already-running model request.

Installed Codex hooks consume hook stdin immediately and read cached daemon state. `SessionStart` runs `start --quiet` synchronously. `UserPromptSubmit` uses `prompt-guard`; `PreToolUse`, `PostToolUse`, `PreCompact`, `PreModelRequest`, and `PostModelRequest` use cache-only guards. They preserve a retained block through telemetry failure, but request daemon recovery and continue when stale, unknown, or missing state has no retained block.

The guard cannot interrupt a model request already in flight, and Codex documents specialized or hosted tool paths as possible exceptions to local tool hooks. Do not claim stronger enforcement than the host lifecycle provides.

Installed hooks never deny: they wait, then return quiet success after a validated release. The 366-day host timeout is finite, so killing, disabling, skipping, or timing out a hook can let Codex continue.

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

`guard` keeps stdout/stderr quiet by default to avoid flooding Codex hook context after long waits. It still writes one wait notice directly to the controlling terminal when waiting starts unless `--no-notify` is set. Use `guard --verbose` only for manual debugging, `guard --no-notify` to suppress the terminal notice, and `guard --state-only` when a hook must not perform a live quota-source poll. Installed blocking hooks allow 366 days and do not use a watchdog denial.

## Claude Code

Every command takes `--provider claude`. Run `./scripts/quota-sentry login --provider claude` once, then `start` and `install-hook` with the same flag; the hooks go into `~/.claude/settings.json`. The README's Claude Code section covers its source, sign-in and read pacing.

## Bypass

Launch or restart Codex or Claude Code with `QUOTA_SENTRY_DISABLE=1` to bypass blocking.
