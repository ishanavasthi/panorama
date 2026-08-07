# Panorama

Panorama reviews a GitHub pull request using evidence from **sibling
repositories in the same organisation** — a rename that breaks a client, a
helper that duplicates one that already exists elsewhere, an endpoint that
ignores an org convention. A normal review only sees the one repo; Panorama
looks at the neighbours too, and cites exactly where.

It is a CLI, not a GitHub App: you run `panorama review ...` against a PR (or
a local fixture) and get a Markdown report with `repo/path:line` references,
never a copied excerpt from another repository.

**All review reasoning runs through the local Claude Code CLI (`claude`), on
your existing Claude Code subscription.** Panorama has no Anthropic API
integration of any kind — no SDK, no HTTP call, no API key. If `claude` isn't
installed and signed in, Panorama has no fallback. See `CLAUDE.md` for the
full list of constraints this project is built against.

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- `git` >= 2.25
- [`gh`](https://cli.github.com/), authenticated (`gh auth login`) — only
  needed once GitHub PR review lands; local fixture review doesn't use it
- [Claude Code](https://claude.com/claude-code), signed in with your
  subscription (`claude` on `PATH`)

## Setup

```bash
uv sync
uv run panorama doctor --deep
```

`doctor --deep` checks Python, git, `gh` and its auth, the `claude` CLI and
its subscription auth, the private workspace directory, and — as one live,
cheap round trip — that Panorama's sandboxed `claude` invocation can actually
read a file and return structured output. A plain `uv run panorama doctor`
skips that last round trip and reports everything else.

Both commands are read-only: they never request, read, or configure an
Anthropic API key. See `docs/m0-claude-boundary.md` for what the `claude`
invocation looks like and what was verified about it.

## Status

Early build. `panorama doctor` is implemented; `review`, `fixtures` and
`demo` are not yet wired up. See `v1plan.md` for the build order and
`DECISIONS.md` for the reasoning behind the major choices.
