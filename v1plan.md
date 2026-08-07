# Panorama V1 — ordered delivery plan

Supersedes the milestone **ordering** in `IMPLEMENT.md`. That document remains
the authority on scope, architecture, data shapes and constraints; this one
decides what gets built when, and what gets dropped first if time runs short.

## Why this differs from IMPLEMENT.md

`IMPLEMENT.md` sequences GitHub intake (M2) and repository cloning (M3) ahead
of retrieval (M4) and review (M5). That front-loads plumbing and integrates the
graded core last.

The assignment is evaluated on *"how effectively it uses cross-repository
context"* — that is retrieval and review. It also states that a CLI is
acceptable and that the demonstration should run on your own mock multi-repo
scenario. **A local fixture organisation needs no `gh`, no cloning, no fork
handling.** So the entire core demo can be built and evaluated before any
GitHub code exists.

Local-first therefore reaches a working end-to-end review far earlier, and
turns GitHub into a second PR *source* feeding a pipeline already proven to
work — rather than a prerequisite for finding out whether the idea works at
all.

---

## The cut line

> **After S6 the project is complete and demonstrable.**

Everything from S7 onward widens the input surface from local fixtures to live
GitHub. If time runs out at the cut line, the deliverable is a focused, working
V1 with recorded evaluation results — not a half-built larger system. That is
the explicitly preferred outcome.

Below the cut line, drop in this order: live `--post` → `demo --github`
seeding → GitHub intake. Never drop: fixture evaluation (S6), host evidence
validation (S5), or the Loom (S11).

---

## Sequence

### S0 — Repo legibility · 0.25h
Minimal README: prerequisites, `uv sync`, `uv run panorama doctor --deep`, and
a plain statement that review reasoning runs through the local Claude Code
subscription rather than a direct Anthropic API. Track `CLAUDE.md` so the
constraints ship with the code.
**Exit:** a fresh clone can be set up and verified from the README alone.

### S1 — Fixture organisation · 2.0h
Four plain source repos: `acme-api`, `acme-web`, `acme-shared`,
`acme-contracts`. `panorama fixtures bootstrap` builds real local git repos
under `.panorama/demo-org/` with branches P1–P4. No nested `.git` committed.
**Exit:** each seeded defect is visible through a local `git diff`.

### S2 — Local PR intake · 1.0h
`LocalPullRequestSource` only. `panorama review --local <repo> --base main
--head <branch>` produces the normalized `PullRequest` model, with real base
and head SHAs read from the local repo.
**Exit:** unit tests for local normalization, empty diffs, unknown refs.

### S3 — Workspace view · 1.0h
Treat the bootstrapped fixture directory as the workspace. Generate the org map
(repo names, README/manifest summaries, top-level paths, convention documents).
Reuse the 0700 permissions and `--add-dir` containment already built in M0.
No cloning, no locking, no fork handling yet.
**Exit:** the workspace exposes all four repos at known SHAs; the org map is
generated from repository content, with no fixture names in the code.

### S4 — Deterministic retrieval · 2.5h
Parse the diff into ranked generic signals (changed identifiers, public-looking
fields, routes, added function/class names). Stopword and generated-file
filtering. Bounded `git grep` across siblings. Hit windows. Convention-document
discovery.
**Exit:** an offline test ranks P1's web consumer and P2's shared helper into
context — with zero fixture names, field names or expected findings in the
retrieval code.

### S5 — Review, validation, rendering · 3.0h
Wire `prompts.py` into `ClaudeRunner` (built and verified in M0). Host-side
evidence validator: repo is an allowed workspace child, path stays inside it,
file exists at the run's SHA, line in bounds, cross-repo categories cite a
foreign repo, PR locations match changed lines. Failing findings are
**discarded, never downgraded**. Reference-only Markdown renderer that reports
the discard count and any truncation.
**Exit:** fake-`claude` tests cover success, malformed-then-repaired, invalid
citation discarded, CLI error, timeout. Empty result renders as an explicit
"no supported cross-repository impact detected".

> ### ★ Walking skeleton complete
> `uv run panorama review --local acme-api --head p1-rename` produces a real,
> validated, cross-repository review from end to end.

### S6 — Real evaluation · 1.5h
Run P1–P4 against the actual Claude Code subscription. Record category- and
evidence-level outcomes (never exact model wording). Inspect P4 for fabricated
findings. Tune prompt or retrieval **at most twice**.

Deliberately early: if review quality is poor, that must surface now — while
there is still time to fix retrieval — not after GitHub plumbing is sunk.
**Exit:** P1 `contract_break` citing `acme-web`; P2 `duplicate_logic` citing
`acme-shared`; P3 `convention` citing `acme-contracts` or a consumer; P4 zero
high-severity cross-repo findings. Variance reported honestly.

--- **cut line** ---

### S7 — GitHub PR intake · 1.5h
`GitHubPullRequestSource` via `gh`. PR URL and `owner/repo#number` parsing,
metadata and diff retrieval, SHA capture, fork PRs via the pull ref.
**Exit:** a real PR normalizes into the same `PullRequest` the local source
produces, and flows through the existing pipeline unchanged.

### S8 — Real multi-repo workspace · 2.0h
`gh repo list` with the 50-repo ceiling (fail before cloning). Clone/fetch
siblings into `~/.panorama/workspaces/<owner>/` at 0700, no submodules, no
project command execution. Workspace lock. Detached checkout at the immutable
head SHA.
**Exit:** a live private PR reviews end to end.

### S9 — Delivery · 1.5h
`--json`. Idempotent `--post`: create or update the single comment marked
`<!-- panorama:v1 -->`. Re-check the head SHA before posting and abort if the
PR moved. Secret-pattern screening on model text. Clean exit codes.
**Exit:** a real private PR comment is created, then updated rather than
duplicated on a second run.

### S10 — Live demo seeding and hardening · 1.5h
`demo --github <owner>` with explicit confirmation before creating private
repos. Large, binary, generated and lockfile diffs. Missing auth. Stale and
force-pushed PRs.
**Exit:** the seeded private demo runs from a clean state.

### S11 — Handoff · 3.0h
Full README, architecture and decision record, limitations, AI-use note.
Record and upload the Loom: live demo, architecture, major decisions, known
limitations, what comes next, how AI tools were used.

---

## Budget

| Block | Hours |
|---|---:|
| M0 (complete) | — |
| S0–S6 — working local V1 with recorded evaluation | 11.25 |
| S7–S10 — live GitHub | 6.5 |
| S11 — docs and Loom | 3.0 |
| **Total remaining** | **20.75** |

S11 is reserved, not squeezed: the Loom is a required deliverable and is
protected ahead of everything below the cut line.

## Standing rules

- No fixture names, field names, PR numbers or expected findings in production
  code. Test expectations are not review logic.
- Findings cite `repo/path:line`. Never quote another repository's source.
- The only AI integration is the local `claude` CLI on subscription auth.
- Commit and push at each S-boundary and at each coherent unit within one.
- Deferred and not to be built: GitHub App, webhooks, queues, inline diff
  comments, automatic patches, embeddings, vector stores, a second LLM
  verification pass, monorepo path scoping.
