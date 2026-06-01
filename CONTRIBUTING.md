# Contributing to Quota Sentry

Thanks for improving Quota Sentry. This project is a local Codex quota guard, so changes should preserve two priorities: avoid wasting quota and avoid noisy Codex hook output.

## Development Setup

Clone the repository, then run commands from the repository root.

```bash
PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v
```

No package install is required for the core test suite; the project currently uses the Python standard library.

## Testing

Use the focused unit suite for normal development:

```bash
PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v
```

Use the autonomous E2E harness before changing hook behavior, daemon behavior, or quota parsing:

```bash
./scripts/autonomous-test
```

The autonomous harness performs one live `codexbar` smoke check and uses synthetic `codexbar` binaries for quota-edge scenarios. If you need to avoid the live check, run:

```bash
./scripts/autonomous-test --skip-live
```

## Hook Safety

Quota Sentry hooks should not flood stdout or stderr. Codex may surface hook output back into the TUI after a long wait, so keep hook output quiet unless a command is explicitly run in a manual/debug mode.

Expected behavior:

- `guard` keeps stdout and stderr quiet by default.
- `guard` may write one wait notice directly to the controlling terminal.
- `guard --verbose` is for manual debugging only.
- `QUOTA_SENTRY_DISABLE=1` bypasses blocking.

## Pull Request Checklist

Before opening or merging a change:

- Run the unit suite.
- Run `./scripts/autonomous-test --skip-live` for logic-only changes.
- Run full `./scripts/autonomous-test` for changes that touch `codexbar` parsing, hooks, daemon lifecycle, or user-visible wait behavior.
- Keep generated `.quota-sentry-runs/` artifacts out of commits.
- Update `README.md`, `docs/Autonomous_Testing_Plan.md`, or plugin skill/command docs when behavior changes.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
