---
description: Check, configure, start, or install the local Codex quota guard
argument-hint: [status|configure|start|stop|poll|install-hook]
allowed-tools: [Bash, Read]
---

# Quota Sentry

The user invoked this command with: $ARGUMENTS

## Instructions

Use this command to help the user manage Quota Sentry from the plugin root.

1. Confirm `codex` is available:

```bash
command -v codex
```

2. If the user asks for status, run:

```bash
./scripts/quota-sentry status
```

3. If the user asks to refresh quota state once, run:

```bash
./scripts/quota-sentry poll
```

4. If the user asks to start monitoring, run:

```bash
./scripts/quota-sentry start
```

5. If the user asks to stop monitoring, run:

```bash
./scripts/quota-sentry stop
```

6. If the user asks to install enforcement, run:

```bash
./scripts/quota-sentry install-hook
```

After hook installation, tell the user to restart every running Codex process. Existing processes retain their loaded hook registry, and editing the hook file is not a reliable hot-reload mechanism.

7. Weekly hard-blocking is already the default. If the user asks to change or explicitly persist its threshold, run:

```bash
./scripts/quota-sentry configure --weekly-mode hard-block --weekly-threshold-percent 99
```

If the user asks to opt out of weekly enforcement, run:

```bash
./scripts/quota-sentry configure --weekly-mode advisory
```

8. If the user asks to run autonomous tests, run:

```bash
./scripts/autonomous-test
```

`poll`, `start`, and `guard` accept `--source auto|codex-app-server|codexbar`. Default `auto` uses only the authoritative Codex app-server and requires no CodexBar installation.

Weekly usage hard-blocks at `99%` by default, even when no config file exists. `configure --weekly-mode advisory` is an explicit opt-out. Persisted policy lives in `~/.config/quota-sentry/config.json` and does not require changing installed hook commands.

Quota policy follows window duration and canonical limit identity, not Codex's `primary` or `secondary` slot names. Short-term windows up to 24 hours use the normal guard; seven-day windows use weekly policy; unfamiliar long-term canonical windows block at `100%`; durationless or canonically ambiguous data fails closed; auxiliary buckets remain advisory only when a canonical bucket is known.

Installed hooks fail closed when cached source state is unknown, stale, or missing. Manual status and poll commands may still report `unknown` for diagnosis.

Once an enforced block is confirmed, keep it latched through source failures, stale state, malformed sibling windows, and expired estimated reset timestamps. Release it only after an authoritative below-threshold snapshot for that policy window or an explicit policy opt-out.

`guard` should keep stdout/stderr quiet in hooks. It writes one wait notice directly to the controlling terminal when waiting starts. Use `./scripts/quota-sentry guard --verbose` only for manual debugging.

Current hook model:

- `SessionStart` runs `start --quiet` synchronously; the command returns after spawning the detached daemon.
- `UserPromptSubmit` runs `prompt-guard`, which starts the daemon quietly, requires fresh cached state, and waits when blocked.
- `PreToolUse` and `PostToolUse` run cache-only guards around supported local tools.
- `PreCompact` guards model-backed compaction.

Installed hook commands consume Codex's JSON stdin immediately. Blocking hooks use a 366-day host timeout and an internal watchdog ten minutes earlier so short-term, weekly, and long-lived account blocks do not reach Codex's ordinary timeout and fail open.
