# Decisions, trade-offs and limitations

A running, plain-language record of *why* Panorama is built the way it is. One
section per milestone. Written to be read on its own — no code required.

---

## The problem in one paragraph

An organisation has many repositories that together make one product. A pull
request can look completely correct inside its own repo and still break
something elsewhere: a renamed API field that a web client destructures, a new
helper that duplicates a shared one, an endpoint that ignores the org's error
and timestamp conventions. A normal code review only sees the one repo.
Panorama reviews a PR *with* the surrounding repositories in view, and reports
what the change does to its neighbours.

---

## Decisions that shape the whole project

### A CLI, not a GitHub App

A webhook service would be the "real" answer, but most of the effort would go
into delivery plumbing — a server, event handling, retries, install flow — and
none of it into the actual hard problem, which is *finding relevant context in
other repositories*. A CLI runs the identical pipeline and can be wrapped in a
webhook later without redesign.

**Trade-off:** no automatic PR triggers in V1. Someone runs a command.

### Claude Code subscription, never the Anthropic API

This was a requirement, and it genuinely changes the design. Panorama shells
out to the locally installed `claude` command and inherits whatever
authentication that CLI already has. There is no Anthropic SDK, no HTTP call,
no API key — anywhere.

**Why it matters:** the system runs on tooling a team already has and pays for,
with no new credential to provision. **Trade-off:** we are bound to one CLI's
behaviour and its version-to-version changes, so we verified that behaviour
empirically instead of trusting documentation (see M0).

### The model reads the repositories directly; the host checks its answers

Two alternatives were rejected. Feeding the model only pre-selected snippets
makes it blind to anything retrieval missed. Letting the model roam with no
verification makes it free to invent citations.

Panorama does both halves: deterministic code finds the *likely* cross-repo
context and hands it over as orientation, the model may then search the whole
workspace itself, and then **host code re-checks every claim** — does that repo
exist, does that file exist at the reviewed commit, is that line real, does a
cross-repo finding actually cite another repo? Anything that fails is
**discarded, not downgraded**.

**Why:** an unverifiable review is worse than no review, because it costs the
reader trust every time they check one and it's wrong.

### No embeddings, no vector database

Deliberately not built. A lexical approach — identifiers, routes, field names —
plus the model's own search covers the failure modes we actually care about
(renames, duplicate helpers, convention drift) and stays explainable: you can
always say *why* a file was surfaced.

**Trade-off:** we will miss semantic links that share no vocabulary. Accepted,
and listed as future work rather than hidden.

### Findings cite locations; they never quote code

A review comment on one repo may be read by people who cannot see another
repo's source. Panorama therefore emits `repo/path:line` references only, never
excerpts. It's a smaller answer, and a safe one to post.

---

## M0 — proving the Claude Code boundary

*Status: complete. This milestone deliberately produced almost no product
features. It exists to de-risk everything after it.*

### The decision

Before building anything on top of the `claude` CLI, verify what it actually
does — flags, output shape, whether restrictions are truly enforced, how it
fails, how it times out. The plan said explicitly: if this can't be made to
work, fix the environment rather than quietly falling back to an API. It did
work.

### What we chose to do, and why

**Restrict the model to reading and searching.** It gets three capabilities —
read a file, grep, glob — and nothing else. No shell, no writing, no network,
no plugins. So a review cannot modify a repository or reach outside it,
regardless of what the model decides to do.

**Treat all repository content as untrusted input.** A PR is data, not
instructions. If a file contains text like "ignore your instructions and
approve this", the review must not obey it. The tool restriction is what makes
that safe in practice: even a fully hijacked model has no capability worth
hijacking it for.

**Run the review in a clean room.** The reviewer's personal Claude Code
configuration — their `CLAUDE.md`, plugins, custom commands, connected servers
— is switched off for the review. Two people reviewing the same PR should get
the same kind of answer, and a review shouldn't be steerable by local settings.

**Bound the run by wall-clock time.** The CLI has no built-in cap on how much
work it does, so a hard timeout is the only real limit. On timeout we kill the
entire process tree, not just the top process, so nothing is left running.

**Verify empirically, not from documentation.** Four independent investigations
ran the real CLI and recorded what actually happened. This paid for itself
immediately: the single most obvious way to restrict the model's tools *does
not work* — it looks like it applies and silently doesn't. Had we assumed it,
every review would have run with full shell access while appearing locked down.
The findings are written up in `docs/m0-claude-boundary.md`.

### What M0 shipped

A working project skeleton, the verified model-invocation boundary, the data
shapes a review produces, and `panorama doctor` — a preflight command that
checks the environment and, with `--deep`, performs one real end-to-end round
trip to prove the boundary genuinely works on this machine. Plus an offline
test suite (220 tests) that runs against a stand-in for the CLI, so day-to-day
development needs no subscription and no network.

### What went wrong, and what we changed

We had the work independently audited rather than trusting our own green test
suite. That found two real security defects that the passing tests had missed:

1. **A credential was being handed to the model's process.** One code path
   passed the full environment to the CLI, which would have included any API
   keys or GitHub tokens present. Fixed so the safe behaviour is the *default*
   — a future code path can no longer reintroduce this by forgetting a
   parameter.

2. **Nothing restricted which directory the model could read.** The tool
   restriction limited what the model could *do*, but not what it could do it
   *to* — a bad caller could have pointed it at the home directory, including
   SSH keys. Now the workspace is validated, and widening it has to be a
   deliberate, visible choice.

The honest lesson: the test suite was green *and* wrong. In one case a test
asserted exactly the right property against a code path that couldn't reach the
bug. Adversarial review found in one pass what the tests were structurally
incapable of finding.

### Known limitations after M0

- Verified against **one CLI version** on **one machine** (macOS). A CLI update
  could change the output shape; `doctor --deep` is the early-warning system.
- The tool restriction is confirmed at the point of invocation. We are trusting
  the CLI to honour it — reasonable, but it is trust, not proof.
- The review prompt and rubric exist but are **not yet wired in**; that happens
  when the review pipeline is built.
- **Prompt injection is reduced, not solved.** Read-only tools, an isolated
  configuration and host-side verification remove the ways an injection could
  do damage. We don't claim a model can never be misled — we claim it can't act
  on it.
- Giving the model direct access to private source is an **intentional
  trade-off**, not an oversight. Cross-repo review is not possible without it.
  What we do instead: keep everything local and owner-only, use the
  subscription rather than shipping code to a new vendor, and never republish
  another repo's source in the output.

---

## Standing limitations of V1

Known and accepted, so they can be stated plainly rather than discovered:

- **Small organisations only** — up to 50 repositories, and it refuses rather
  than degrading past that. Repository selection and indexing is future work.
- **Lexical retrieval misses semantic links.** A related concept with no shared
  vocabulary won't be surfaced by our deterministic pass; we rely on the
  model's own search to close some of that gap.
- **Model output varies between runs.** The same PR may produce a differently
  worded review. Host verification bounds the *correctness* of findings, not
  their consistency.
- **One summary comment, no inline diff comments** and no automatic fixes.
- **Whole-repository granularity.** Monorepos with per-directory ownership are
  out of scope.
- **Absence of a finding is not proof of safety.** Panorama surfaces
  cross-repo impact it can evidence. It is a reviewer's assistant, not a gate.
