# Contributing to Quota Sentry

Thanks for improving Quota Sentry. The first supported harness is Codex, but the project is meant to grow into a set of quota guards for other agent harnesses such as Claude Code, OpenCode, and similar local CLIs.

## Project Direction

Quota Sentry should stay small, local, and conservative:

- Avoid wasting paid or rate-limited quota.
- Fail open when quota data is missing, malformed, stale, or unavailable.
- Keep hooks and wrappers quiet unless the user explicitly asks for debug output.
- Preserve a clear manual bypass for every harness integration.
- Keep harness-specific behavior isolated so new adapters do not regress Codex.

## Development Setup

Clone the repository, then run commands from the repository root.

```bash
PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v
```

No package install is required for the core test suite; the project currently uses the Python standard library.

## Adding Harness Support

Codex support uses `codex app-server --stdio` as the primary source and keeps CodexBar as an optional fallback adapter. New harness integrations should be added as isolated adapters, scripts, commands, or plugin surfaces rather than by hardcoding another harness into the Codex path.

When adding support for a harness such as Claude Code or OpenCode:

- Document the harness version, quota source, hook or wrapper entrypoint, and install/uninstall steps.
- Use structured quota data when available; avoid scraping terminal text unless there is no stable alternative.
- Make missing credentials, missing binaries, unknown quota windows, and parse errors fail open.
- Keep stdout/stderr quiet in hook execution paths.
- Emit at most one human-readable wait notice through the terminal path when blocking begins.
- Add a bypass equivalent to `QUOTA_SENTRY_DISABLE=1`.
- Add synthetic E2E coverage so contributors can test edge cases without consuming real quota.
- Include one real smoke path only when it is safe, bounded, and clearly optional.

## Testing

Use the focused unit suite for normal development:

```bash
PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v
```

Use the autonomous E2E harness before changing hook behavior, daemon behavior, or quota parsing:

```bash
./scripts/autonomous-test
```

The autonomous harness performs one live Codex quota-source smoke check and uses synthetic `codex` and `codexbar` binaries for quota-edge scenarios. If you need to avoid the live check, run:

```bash
./scripts/autonomous-test --skip-live
```

On a clean clone, the global Codex hook check is skipped unless this checkout is installed in `~/.codex/hooks.json`. Maintainers can force that check with:

```bash
./scripts/autonomous-test --skip-live --require-global-hook
```

## Hook Safety

Quota Sentry hooks should not flood stdout or stderr. Codex may surface hook output back into the TUI after a long wait, so keep hook output quiet unless a command is explicitly run in a manual/debug mode.

Expected behavior:

- `guard` keeps stdout and stderr quiet by default.
- `guard` may write one wait notice directly to the controlling terminal.
- `guard --verbose` is for manual debugging only.
- `QUOTA_SENTRY_DISABLE=1` bypasses blocking.
- Installed Codex hook paths must not perform live quota-source polling.
- Installed Codex hook paths must not open or write to `/dev/tty`; use `--no-notify` or `prompt-guard` for hook mode.
- Installed hook commands must be single commands, not shell-composed chains with `;`, `&&`, or pipes.
- Installed Codex hooks should be synchronous commands; Codex 0.140.0 skips async hooks.

For other harnesses, preserve the same shape: cache-only hook path, explicit debug mode, manual-only terminal notices, and a documented bypass.

## AI-Generated Code

AI-generated code is allowed. Contributors are still responsible for the result.

If a PR includes AI-assisted code:

- Say so in the PR description.
- Explain what was generated and what was manually reviewed or changed.
- Run the relevant unit and autonomous tests.
- Include test screenshots, terminal screenshots, or concise log excerpts for user-visible hook and terminal behavior.
- Open the PR as a draft until the behavior is tested and the PR description is complete.

AI output that is not understood, tested, and documented is not ready to merge.

## Pull Request Checklist

Before opening or merging a change:

- Run the unit suite.
- Run `./scripts/autonomous-test --skip-live` for logic-only changes.
- Run full `./scripts/autonomous-test` for changes that touch quota-source adapters, hooks, daemon lifecycle, or user-visible wait behavior.
- For new harnesses, include synthetic E2E tests for below-threshold, blocked, reset, stale-state, missing-binary, and malformed-quota cases.
- Keep generated `.quota-sentry-runs/` artifacts out of commits.
- Do not commit local paths, personal tokens, credentials, cookies, or provider account data.
- Update `README.md`, autonomous testing docs, or plugin skill/command docs when behavior changes.
- Make the PR description clear enough that a reviewer can reproduce the tested behavior.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
