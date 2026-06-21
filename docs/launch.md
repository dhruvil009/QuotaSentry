# Quota Sentry Launch Package

This is the working checklist for the first public push. Launch only after the README, demo GIF, and GitHub metadata are ready.

## Positioning

One-line pitch:

> Quota Sentry is a local circuit breaker for Codex quota: it watches your 5-hour usage window and pauses new Codex activity before you burn through the limit.

Shorter social hook:

> Stop burning the last few percent of your Codex quota by accident.

Plain-language description:

> Quota Sentry runs a local daemon, watches Codex rate-limit state, and installs global Codex hooks that wait before new prompt or tool activity when the 5-hour window is at the configured threshold.

GitHub description:

> Local circuit breaker for Codex quota. Watches the 5-hour usage window and pauses new Codex activity near the limit.

GitHub topics:

```text
codex
openai-codex
cli
quota
rate-limit
developer-tools
agent-tools
hooks
python
```

## Pre-Launch Checklist

- [ ] Render `docs/demo/quota-sentry-demo.tape` to `docs/assets/quota-sentry-demo.gif`.
- [ ] Add the rendered GIF near the top of `README.md`.
- [ ] Confirm the Mermaid architecture diagram renders on GitHub.
- [ ] Run `PYTHONPATH=src python3 -m unittest tests/test_quota_sentry.py -v`.
- [ ] Run `./scripts/autonomous-test --skip-live`.
- [ ] Update the GitHub repo description and topics.
- [ ] Ask 10-20 trusted Codex users to sanity-check the README and install path.
- [ ] Prepare a launch-day list of people to notify for feedback. Do not ask for HN upvotes.

## Show HN

Title:

```text
Show HN: Quota Sentry - a local circuit breaker for Codex quota
```

URL:

```text
https://github.com/dhruvil009/QuotaSentry
```

First comment draft:

```text
I built Quota Sentry after running into a boring but expensive problem: long Codex sessions make it easy to keep submitting prompts and tool calls when the 5-hour quota window is almost spent.

It runs locally, reads Codex quota state from `codex app-server --stdio` with CodexBar as an optional fallback, and installs global Codex hooks that pause new Codex activity once usage crosses the configured threshold. It fails open when quota data is missing or stale, and the hook paths only read cached state so they stay quiet and predictable.

The current version is intentionally narrow: Codex only, local state only, no hosted service, no account data sent anywhere. I would especially like feedback on the hook model and whether this should grow into a generic quota guard for Claude Code, OpenCode, and similar agent CLIs.
```

HN response posture:

- Be precise and modest.
- Acknowledge that the daemon cannot interrupt an already-running model request.
- Emphasize fail-open behavior and local-only state.
- Invite critique on the hook model and packaging path.

## Reddit

Use one or two targeted subreddits on launch day. Do not spray every subreddit at once.

### r/codex or Agent Tooling Communities

```text
I built Quota Sentry, a local guard that watches the Codex 5-hour quota window and pauses new Codex prompt/tool activity near the limit.

It runs locally, uses `codex app-server --stdio` by default, falls back to CodexBar if configured, and installs global hooks that read cached quota state. It fails open if quota data is unavailable or stale.

Repo: https://github.com/dhruvil009/QuotaSentry

I would like feedback from people who run long Codex sessions: is the hook behavior conservative enough, and what install path would you trust?
```

### r/commandline

```text
I built a small Python CLI called Quota Sentry. It is a local circuit breaker for Codex quota: a background daemon watches the 5-hour usage window, and Codex hooks pause new activity once usage crosses a threshold.

It is intentionally boring infrastructure: local state, quiet hooks, fail-open behavior, and a manual bypass.

Repo: https://github.com/dhruvil009/QuotaSentry
```

## X / Bluesky Thread

Post the GIF first, link in a reply.

```text
I built Quota Sentry because long Codex sessions make it too easy to burn through the last few percent of a quota window.

It is a local circuit breaker: watch the 5-hour Codex quota, then pause new prompt/tool activity near the limit.
```

```text
How it works:

- daemon polls Codex quota state
- state is cached locally
- global Codex hooks read the cache
- hooks wait only when the state is fresh and blocked
- missing/stale quota data fails open
```

```text
Current scope is narrow on purpose: Codex only, local only, no hosted service.

I am looking for feedback from people running long Codex sessions or juggling quota across agent CLIs.
```

Reply with:

```text
Repo: https://github.com/dhruvil009/QuotaSentry
```

## Week-One Follow-Up

- [ ] Package the CLI if launch feedback says clone-and-run is too much friction.
- [ ] Open issues for Claude Code and OpenCode adapter investigation.
- [ ] Submit to relevant developer-tool newsletters only after the README and install path are proven.
- [ ] If the repo clears 50 stars or meaningful usage, submit to relevant awesome lists.
