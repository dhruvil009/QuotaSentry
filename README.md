# Quota Sentry

Quota Sentry is a local quota guard for agent harnesses. The `v0.1.x` line supports Codex by watching the Codex 5-hour usage window via `codexbar` and blocking future Codex lifecycle activity once the configured threshold is reached.

## Current Scope

- Codex only in `v0.1.x`; additional harness adapters are welcome.
- Uses `codexbar usage --provider codex --source cli --format json`.
- Monitors the 5-hour window (`windowMinutes: 300`) by default.
- Blocks at `usedPercent >= 95` until `resetsAt` plus a 60-second buffer.
- Fails open when `codexbar` is missing, quota JSON is unavailable, or state is stale.

The background daemon does not interrupt an already-running model request. It polls every five minutes by default, tightens its cadence near the quota threshold, and writes state; the synchronous `guard` command is what sleeps when invoked from a Codex hook or wrapper.

Quota Sentry is intentionally conservative for public use: missing tools, malformed quota data, stale state, and unknown quota windows fail open instead of blocking the user.

## Commands

Run from this plugin root:

```bash
./scripts/quota-sentry poll
./scripts/quota-sentry start
./scripts/quota-sentry status
./scripts/quota-sentry guard
./scripts/quota-sentry stop
```

`status` is intentionally terse for normal use, for example `Quota Sentry: 14% used`. It warns when the saved quota state is stale and the background daemon is not running. Use `status --verbose` to include daemon details. The synchronous `guard` command still self-heals by polling before deciding whether to block unless it is run with `--state-only`.

`guard` keeps stdout/stderr quiet by default because Codex surfaces hook output back into the TUI after long waits. When it starts waiting, it writes one notice directly to the controlling terminal instead:

```text
Quota Sentry: waiting for Codex quota reset until <timestamp>.
```

Use `./scripts/quota-sentry guard --verbose` only when running it manually and you want a captured wait message too. Use `--no-notify` to suppress the terminal notice. Use `--state-only` for hook paths that must only read cached daemon state and must not invoke `codexbar`.

Daemon cadence is configurable:

```bash
./scripts/quota-sentry start --interval-seconds 300
./scripts/quota-sentry start --near-threshold-percent 85 --near-interval-seconds 60
./scripts/quota-sentry start --critical-threshold-percent 93 --critical-interval-seconds 30
```

Install global Codex hooks:

```bash
./scripts/quota-sentry install-hook
```

That command merges Quota Sentry hooks into `~/.codex/hooks.json` and writes a `.bak` backup if a hooks file already exists. Restart Codex after installing hooks if the current session does not pick them up.

## Hook Model

Quota Sentry intentionally does not rely on plugin-local hooks. Current Codex builds expose global hooks, but plugin-scoped hooks are not a reliable runtime surface yet. The installer writes absolute script paths into `~/.codex/hooks.json`.

Installed hooks:

- `SessionStart`: starts the background daemon.
- `UserPromptSubmit`: ensures the daemon is running, then runs live `guard` before a new prompt is accepted.
- `PreToolUse`: runs `guard --state-only --no-notify` before tool execution, using only the daemon's latest cached state.

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

For clean clones without installed Codex hooks, the global hook scenario is skipped by default. Use `./scripts/autonomous-test --skip-live --require-global-hook` when you specifically need to verify that this checkout is installed in `~/.codex/hooks.json`.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for development workflow, test expectations, hook-safety guidance, AI-generated code expectations, and guidance for adding other harnesses such as Claude Code or OpenCode.

## License

Apache License 2.0. See [LICENSE](./LICENSE).
