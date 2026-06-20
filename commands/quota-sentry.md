---
description: Check, start, or install the local Codex quota guard
argument-hint: [status|start|stop|poll|install-hook]
allowed-tools: [Bash, Read]
---

# Quota Sentry

The user invoked this command with: $ARGUMENTS

## Instructions

Use this command to help the user manage Quota Sentry from the plugin root.

1. Confirm `codexbar` is available:

```bash
command -v codexbar
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

6. If the user asks to install enforcement, explain that plugin-local hooks are not reliable yet, then run:

```bash
./scripts/quota-sentry install-hook
```

After hook installation, tell the user to restart Codex if the current session does not pick up the new global hooks.

7. If the user asks to run autonomous tests, run:

```bash
./scripts/autonomous-test
```

`guard` should keep stdout/stderr quiet in hooks. It writes one wait notice directly to the controlling terminal when waiting starts. Use `./scripts/quota-sentry guard --verbose` only for manual debugging.

Current hook model:

- `SessionStart` runs `start --quiet` synchronously; the command returns after spawning the detached daemon.
- `UserPromptSubmit` runs `prompt-guard`, which starts the daemon quietly and then checks cached state without terminal notices.
- `PreToolUse` runs `guard --state-only --no-notify` so tool hooks only read cached state and never invoke `codexbar`.
