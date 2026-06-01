# Quota Sentry

Quota Sentry is a local Codex plugin/helper that watches the Codex 5-hour usage window via `codexbar` and blocks future Codex lifecycle activity once the configured threshold is reached.

## Current Scope

- Codex only.
- Uses `codexbar usage --provider codex --source cli --format json`.
- Monitors the 5-hour window (`windowMinutes: 300`) by default.
- Blocks at `usedPercent >= 95` until `resetsAt` plus a 60-second buffer.
- Fails open when `codexbar` is missing, quota JSON is unavailable, or state is stale.

The background daemon does not interrupt an already-running model request. It polls every minute and writes state; the synchronous `guard` command is what sleeps when invoked from a Codex hook or wrapper.

## Commands

Run from this plugin root:

```bash
./scripts/quota-sentry poll
./scripts/quota-sentry start
./scripts/quota-sentry status
./scripts/quota-sentry guard
./scripts/quota-sentry stop
```

`guard` keeps stdout/stderr quiet by default because Codex surfaces hook output back into the TUI after long waits. When it starts waiting, it writes one notice directly to the controlling terminal instead:

```text
Quota Sentry: waiting for Codex quota reset until <timestamp>.
```

Use `./scripts/quota-sentry guard --verbose` only when running it manually and you want a captured wait message too. Use `--no-notify` to suppress the terminal notice.

Install global Codex hooks:

```bash
./scripts/quota-sentry install-hook
```

That command merges Quota Sentry hooks into `~/.codex/hooks.json` and writes a `.bak` backup if a hooks file already exists. Restart Codex after installing hooks if the current session does not pick them up.

## Hook Model

Quota Sentry intentionally does not rely on plugin-local hooks. Current Codex builds expose global hooks, but plugin-scoped hooks are not a reliable runtime surface yet. The installer writes absolute script paths into `~/.codex/hooks.json`.

Installed hooks:

- `SessionStart`: starts the background daemon.
- `UserPromptSubmit`: runs `guard` before a new prompt is accepted.
- `PreToolUse`: runs `guard` before tool execution, using the daemon's latest state.

## State

Default state lives under:

```text
~/.cache/quota-sentry/
```

Files:

- `state.json`: latest quota decision.
- `quota-sentry.pid`: daemon pid.
- `quota-sentry.log`: daemon output.

## Bypass

Set this environment variable to bypass synchronous blocking:

```bash
export QUOTA_SENTRY_DISABLE=1
```

## Development

Run tests:

```bash
PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v
```

Run autonomous E2E tests:

```bash
./scripts/autonomous-test
```

The autonomous harness runs one live `codexbar` smoke poll, then uses synthetic `codexbar` binaries for quota-edge scenarios. It writes a report under `.quota-sentry-runs/`.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for development workflow, test expectations, and hook-safety guidance.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
