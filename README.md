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

> **Loom walkthrough:** _add link here_ — a short demo, the architecture and
> major decisions, known limitations, what's next, and how AI tools were used.

This README is the submission's written companion to that video. It covers
setup and run instructions (below), and mirrors the video's architecture,
decisions, limitations, next-steps, and AI-use notes further down so they can
be read without watching.

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

## Trying it on the local fixtures

Panorama ships a mock four-repository organisation so the core review works
with no GitHub access and no cloning. Build it, then review one of the seeded
pull requests:

```bash
uv run panorama fixtures bootstrap
uv run panorama review --local acme-api --head p1-rename        # Markdown
uv run panorama review --local acme-api --head p1-rename --json # machine-readable
```

Each review runs the full pipeline: intake → workspace → deterministic
retrieval → Claude review → host evidence validation → reference-only report.
Every finding cites `repo/path:line`; findings whose evidence the host cannot
verify on disk are discarded and counted, never silently softened.

The four seeded pull requests are:

| Branch | Repo | Seeded defect |
|---|---|---|
| `p1-rename` | acme-api | renames a response field a sibling client consumes |
| `p2-local-validator` | acme-web | reimplements a helper that already exists in a shared repo |
| `p3-endpoint-conventions` | acme-api | ignores org error-envelope and timestamp conventions |
| `p4-docs-cleanup` | acme-api | docs-only control that should raise nothing cross-repo |

## Evaluation

The fixtures were reviewed against a real Claude Code subscription, twice each,
to record category- and evidence-level outcomes. (Exact model wording is not
recorded — it varies between runs; the *categories and cited repositories* are
what matter.)

| PR | Expected | Result (both passes) |
|---|---|---|
| P1 | contract break vs the consumer | `contract_break`, high, citing **acme-web** |
| P2 | duplicate of a shared helper | `duplicate_logic`, medium, citing **acme-shared** |
| P3 | convention violation | `convention` (×2), citing **acme-contracts** |
| P4 | no cross-repo impact | no findings; verdict `comment` |

Across both passes: **zero findings were discarded** (every citation the model
produced resolved to a real, in-bounds line), and the P4 control produced no
finding either time — no fabricated cross-repository impact. The only run-to-run
variance observed was on P3, where one pass additionally surfaced a low-severity
`duplicate_logic` finding (also correctly cited); the core convention findings
were stable. No prompt or retrieval tuning was required.

## Reviewing a GitHub pull request

Panorama can review a real pull request through `gh` (which must be installed
and signed in — `gh auth login`):

```bash
uv run panorama review owner/repo#123          # or a full PR URL
uv run panorama review owner/repo#123 --json
uv run panorama review owner/repo#123 --post   # also post the review as a comment
```

This runs the same pipeline as a local review. Panorama normalizes the PR (a
fork boundary is fine), clones the organisation's repositories into a private
workspace under `~/.panorama/workspaces/<owner>/` (`0700`, locked so two runs
never collide), checks the PR's own repository out at the exact reviewed commit,
and then retrieves, reviews, validates, and renders exactly as it does locally.

With `--post`, Panorama publishes the review as a **single** comment tagged with
a hidden marker: run it again and that comment is *updated*, not duplicated. It
re-reads the head SHA immediately before posting and aborts if the branch moved,
and screens the outgoing body for secrets one last time. Posting needs write
access (`--post` only); a read-only token still reviews fine.

Two boundaries worth knowing: Panorama never reads, stores, or prints a GitHub
token — `gh` owns that credential — and organisations larger than 50
repositories are out of scope (it fails before cloning rather than degrading).

## Seeding a live demo

To show the whole thing end to end on real GitHub, seed the mock organisation as
private repositories in an account you control:

```bash
uv run panorama demo --github <your-user-or-org>
```

This **creates private repositories** (`acme-api`, `acme-web`, `acme-shared`,
`acme-contracts`) and opens a pull request for each seeded change — the same
P1–P4 defects the offline evaluation uses. It always confirms before writing
(pass `--yes` to skip the prompt, `--recreate` to replace existing repos). Then
review a seeded PR live, optionally posting the result back:

```bash
uv run panorama review <your-user-or-org>/acme-api#1 --post
```

## Architecture

One pipeline runs for every review, regardless of where the PR came from:

```
panorama review <PR>
  intake     PR metadata + diff             -> normalized PullRequest
  workspace  clone/fetch every sibling repo -> PR repo pinned at head SHA (0700, locked)
  retrieve   diff -> generic signals        -> bounded git-grep across siblings + org map + convention docs
  review     local claude CLI, Read/Grep/Glob only, sandboxed -> JSON review
  validate   repo/path/line/SHA + foreign-evidence + secret screen -> discard unsupported findings
  deliver    Markdown / --json / idempotent --post
```

A diagram of the same flow lives in `docs/architecture.mmd` (paste into
<https://mermaid.live> or any Mermaid preview). The load-bearing ideas:

- **Two sources, one shape.** A local fixture branch and a GitHub PR both
  normalize to the same `PullRequest`, so retrieval, review, validation, and
  delivery never learn where the PR came from — the GitHub path only changes how
  the repositories land on disk.
- **Retrieval orients, the model confirms, the host verifies.** Deterministic
  code finds likely cross-repo context; Claude reviews with that context;
  then host code re-checks every citation against the real files. A finding
  whose evidence doesn't resolve is **discarded, never downgraded**.
- **The Claude boundary is the whole security story.** The only AI integration
  is the local `claude` CLI on its subscription — no SDK, no HTTP, no API key.
  It runs read/search-only, with the user's personal config, plugins, and MCP
  servers switched off, scoped to the workspace, under a wall-clock timeout.
  What was empirically verified about that boundary is written up in
  `docs/m0-claude-boundary.md`.
- **Reference-only by construction.** The output schema has no field for a
  source excerpt, so a finding can only ever be a `repo/path:line` pointer —
  safe to post on a repo whose readers can't see the cited repo's source.

## Major decisions

The full reasoning, milestone by milestone, is in `DECISIONS.md`. In brief:

- **A CLI, not a GitHub App.** The hard problem is *finding cross-repo
  context*, not delivery plumbing; the same pipeline wraps in a webhook later.
  The assignment explicitly permits a CLI that can become a bot.
- **The model reads the repositories directly, and the host checks its answers.**
  Rather than feeding the model only pre-selected snippets (blind to whatever
  retrieval missed) or letting it roam unverified (free to invent citations), it
  does both — and every citation is re-checked against the real files, with
  failures **discarded, never downgraded**.
- **Lexical retrieval, not embeddings.** Explainable — every surfaced repo is
  justified by which signal matched which line — with no index to build or drift.
- **Findings cite locations; they never quote code.** Reference-only output is
  safe to post on a repo whose readers can't see the cited repo's source.

## Known limitations

- **Small organisations only** — up to 50 repositories; it fails before cloning
  rather than degrading past that.
- **Lexical retrieval misses purely semantic links** — related code that shares
  no vocabulary may not be surfaced; the model's own workspace search closes
  some of that gap.
- **Model output varies between runs.** Host validation bounds the *correctness*
  of findings, not their run-to-run *consistency*.
- **Giving the model read access to private source is an intentional
  trade-off** — mitigated by read-only sandboxing, owner-only `0700` storage,
  prompt-injection treatment, and reference-only output, not eliminated.
- **One summary comment**, no inline diff comments, no automatic fixes. Absence
  of a finding is not proof of safety — Panorama is a reviewer's assistant, not
  a merge gate.

## What I'd build next

- Configurable repository selection instead of cloning the whole org.
- Stronger semantic retrieval to catch links with no shared vocabulary.
- A GitHub App / webhook delivery path for automatic PR triggers.
- Inline line-level review comments, not just one summary comment.
- Permission-aware evidence, so a cited location respects who can see which repo.

## How I used AI tools

AI was used throughout development as a coding partner — scaffolding modules,
writing tests, and drafting docs — while I directed the architecture, the
milestone sequencing, and every security boundary, and reviewed all of it. One
milestone (M0) was deliberately handed to an adversarial AI review that found
two real security defects a green test suite had missed. In the shipped
product, the *only* runtime AI dependency is the local Claude Code CLI that
performs the review itself.

## Status

Feature-complete V1. Implemented: `panorama doctor`, `panorama fixtures
bootstrap`, `panorama review` end to end for a local fixture (`--local`) and a
GitHub PR (`owner/repo#n` or URL) — with `--json` and an idempotent `--post` —
and `panorama demo --github` to seed a live private demo. See `v1plan.md` for
the build order and `DECISIONS.md` for the reasoning behind the major choices.
