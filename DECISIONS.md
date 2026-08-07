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

## S1 — the local fixture organisation

### The decision

The graded question is how well Panorama uses *cross-repository* context, and a
small local organisation can demonstrate that with no GitHub, no cloning, and
no network. So the first thing built after the boundary is the mock org itself:
four intentionally-coupled repositories and the four seed changes that couple
them.

### What S1 shipped

`panorama fixtures bootstrap` turns checked-in source data into four real git
repositories under `.panorama/demo-org/` — `acme-api`, `acme-web`,
`acme-shared`, `acme-contracts` — each on a `main` branch, with four seed
branches carrying the defects the review has to catch: a response-field rename
that breaks a consumer, a duplicated URL validator, an endpoint that ignores
the org's error/timestamp conventions, and a documentation-only control that
must *not* produce a cross-repo finding. Each defect is visible through a plain
`git diff main <branch>`.

### Why it is shaped this way

- **The fixtures are data; the bootstrap is generic.** The command knows
  nothing about repo names, field names, branch names, or expected findings —
  it just materialises whatever the data tree describes. That keeps constraint
  #2 (no fixture specifics in production code) true by construction, and a test
  asserts the production modules contain none of those tokens.
- **Real git, not a directory of files.** Branches and SHAs are what later
  milestones read to build a normalized `PullRequest`, so the fixtures are the
  same kind of object a real PR is — a diff between two commits.
- **Built under `.panorama/`, gitignored.** No nested `.git` is ever committed
  to the outer repository; the mock org is a reproducible build artifact, not
  checked-in history.
- **Idempotent and safe.** Re-running rebuilds cleanly by reclaiming its own
  prior checkouts, but it refuses to delete a directory that is not
  recognisably a previous bootstrap unless `--force` is given.

## S2 — local pull-request intake

### What S2 shipped

`panorama review --local <repo> --base main --head <branch>` normalizes a local
git branch into the `PullRequest` shape that every later stage reads. Both
intake sources (this one and the GitHub source later) produce that identical
shape, so retrieval, review, and delivery never learn where a PR came from.

### Decisions worth recording

- **A local PR's title and body come from its head commit**, not a separate PR
  description — there is no PR object locally. It is an honest stand-in, and a
  real GitHub PR (whose title can differ from any commit) will fill these from
  the API. Owner/repo are likewise read from the checkout's own directory
  layout rather than invented.
- **The diff is merge-base (three-dot) based**, matching how a real PR diff is
  computed: it shows what the branch introduced since it diverged, so unrelated
  commits landing on the base afterwards never leak into the review.
- **An empty diff is a valid pull request, not an error.** A no-op PR
  normalizes cleanly with an empty diff; an unknown ref or a non-git directory
  is the failure, and it fails with a specific message.
- **Intake is strictly read-only** — only `git rev-parse`, `git diff`, and
  `git log`. The default summary never prints the diff body, keeping the
  "don't dump repository source to stdout" discipline even for our own
  fixtures; `--json` is the explicit way to get the full bundle.

## S3 — the workspace view

### What S3 shipped

A read-only `Workspace` over the bootstrapped fixture directory: it enumerates
the four repositories at their immutable HEAD SHAs, resolves a `repo/path`
reference while proving it stays inside its repository, and generates an *org
map* — a compact orientation document listing each repo's description, README
summary, top-level paths, and any organisation convention documents.

### Decisions worth recording

- **The org map is derived entirely from generic content patterns**, never from
  fixture knowledge. Descriptions come from a `package.json`, summaries from the
  README's first paragraph, and convention documents from generic engineering
  names (`CONVENTIONS.md`, `ARCHITECTURE.md`, `docs/standards/**`, `openapi/**`,
  …). The code contains no repo, field, or document names — a test enforces
  that — so it will work unchanged on a real organisation.
- **Containment is the foundation the evidence validator will stand on.**
  `resolve_within` follows symlinks and normalizes `..` *before* testing that a
  path is inside its repository, so a reference can never escape via traversal
  or a symlink. It deliberately does not assert the file exists — that is a
  separate check the S5 validator layers on top.
- **A repository with no convention documents surfaces none** rather than
  guessing, and summaries are length-capped, so the org map stays honest and
  bounded regardless of repository size.

---

## S4 — deterministic cross-repository retrieval

### What S4 shipped

The step that turns a diff into *orientation*: a small, ranked set of generic
signals pulled from the PR diff, the sibling-repository lines those signals
match (found with bounded `git grep`), and a relevance ranking of the siblings
themselves — plus the convention documents discovered on the neighbours. This
is what the review runs against, before any model is involved.

### Decisions worth recording

- **Signals are generic shapes, not known names.** The diff is read for
  public-looking fields (`name:`), route literals, added function/class names,
  and other changed identifiers — each weighted by how contract-like it is. A
  stopword list of language keywords and ubiquitous builtins, plus
  generated/lockfile/binary filtering, keeps the noise out. There is no fixture
  repo name, field name, or expected finding anywhere in the code; it keys only
  off the diff and the workspace.
- **Siblings are ranked by *distinct* matched signals, discounted by how widely
  each signal spreads.** A token that matches one repository is discriminating;
  one that matches everywhere is nearly worthless. Dividing each signal's weight
  by the number of repositories it hits (an IDF-like discount) is what pushes
  the true consumer to the top instead of whichever repo is simply largest.
- **Everything is capped and explainable.** Retrieval is orientation, not an
  index: a fixed number of signals searched, hits per signal per repo, and total
  hits, each hit carrying a `repo/path:line` and a small context window. Every
  surfaced repository can be justified by which signal matched which line — no
  embeddings, no parser, nothing opaque.
- **The polarity fix.** A token first seen as a weak identifier and later
  upgraded to a higher-weight field was silently losing its earlier add/remove
  record. Polarity now accumulates across every sighting, so a removed-then-
  reshaped field still reports as both — information the review step will lean on
  to reason about who still consumes the old shape.

### A limitation this surfaced

On the convention-violation PR (P3), the intended neighbour ties on score with
other siblings rather than leading outright — the violation is about *absent*
convention adherence, which leaves a weaker lexical footprint than a rename or a
duplicated helper. Retrieval still pulls the right repo and its convention
documents into context; ranking it first is left to the model and the
convention-doc channel rather than forced with fixture-specific tuning.

---

## S5 — review, host validation, and reference-only rendering

### What S5 shipped

The walking skeleton: `panorama review --local <repo> --head <branch>` now runs
end to end — intake, workspace, retrieval, a real Claude review, host validation,
and a rendered report. Three new pieces sit between retrieval and the user: a
prompt assembler that hands the model the diff plus deterministic leads, a
host-side validator that re-checks every finding against ground truth, and a
reference-only renderer.

### Decisions worth recording

- **The model finds; the host verifies; failing findings are discarded, never
  downgraded.** This was always the design intent, and S5 makes it real. Each
  finding must clear four independent checks: its text passes secret screening,
  at least one piece of evidence resolves to a real in-bounds line on disk, a
  cross-repository finding cites at least one repository *outside* the pull
  request, and any pull-request location it names matches a line the diff
  actually changed. A finding that fails any check is dropped and counted in the
  report — never quietly softened to a lower severity, because a review the
  reader cannot trust is worse than a shorter one.
- **Silence is always explained.** Zero surviving findings renders as an explicit
  "no supported cross-repository impact detected", and the discard count and any
  retrieval truncation are always stated. An empty report would be
  indistinguishable from a tool that did nothing.
- **The host will not assert a disposition it cannot support.** If every finding
  is discarded, the rendered verdict falls back to a neutral "comment" even if
  the model asked for "request changes" — the host has nothing left to justify a
  stronger call.
- **Source quotation is prevented structurally, not by detection.** The schema
  has no field to hold a source excerpt and the prompt forbids quoting, so the
  renderer prints only `repo/path:line`. The one residual risk — a credential a
  model might echo inside a sentence — is caught by host secret-shape screening.
  We deliberately did *not* build a general "is this quoted source code?"
  detector: it would be fuzzy and would discard real findings for false reasons.
- **The prompt states the trust boundary first and last.** The rubric leads, the
  untrusted diff and leads sit in the middle clearly fenced, and a reminder that
  repository text carries no authority closes the prompt — so the boundary is
  the last thing read before the model answers. Retrieval leads are labelled as
  leads to confirm, not evidence, matching the "a search hit is not proof" rule.

### A limitation this surfaced

In local mode the pull request's own repository is presented at its main `HEAD`,
not checked out at the branch under review. The change itself is fully conveyed
by the diff, and every *sibling* is at its real `HEAD`, so cross-repository
findings — the graded ones — validate correctly. The gap is only that evidence
pointing *into the PR's own repo* is checked against main rather than the branch.
A detached checkout at the immutable head SHA is deliberately deferred to S8
(real multi-repo workspace) rather than front-loaded here.

---

## S6 — real evaluation on the fixtures

### What S6 did

Ran the four seeded pull requests through the whole pipeline against a real
Claude Code subscription, twice each, and recorded the category- and
evidence-level outcomes (never the exact model wording, which varies). This was
deliberately scheduled *before* any GitHub plumbing: if review quality were
poor, that had to surface while there was still time to fix retrieval, not after
sinking effort into cloning and posting.

### What it showed

- **All four met their exit criteria on the first pass, with no tuning.** P1
  produced a `contract_break` citing the consumer repo; P2 a `duplicate_logic`
  citing the shared-helper repo; P3 two `convention` findings citing the
  contracts repo; P4 — the control — produced no finding and a neutral verdict.
  The plan budgeted up to two tuning iterations; none were needed.
- **Zero findings were discarded across eight runs.** Every citation the model
  produced resolved to a real, in-bounds line in the workspace. That is the
  outcome the whole design aims at: deterministic retrieval orients the model,
  the prompt insists a lead is not evidence until the file is opened, and the
  host validator would have caught any invention — and had nothing to catch.
- **The false-positive control held both times.** A docs-only change produced no
  cross-repository finding, which is the result that most easily goes wrong when
  a reviewer feels obliged to say something.

### Variance, reported honestly

Model output is not deterministic. The one run-to-run difference observed was on
P3, where one pass surfaced an extra low-severity `duplicate_logic` finding
(itself correctly cited) that the other pass did not. Severities and the core
findings were otherwise stable. Host validation bounds the *correctness* of what
is shown, not its *consistency* between runs — two runs may legitimately differ
in which true findings they include.

### How it was run

The subscription used for evaluation lives under a non-default Claude Code
config directory. Rather than weaken the runner's environment allowlist (which
is security-relevant and was minimised in M0), the evaluation used a local shim
named `claude` on `PATH` that selects that config directory and execs the real
binary. Nothing about this ships: production invokes `claude` directly with the
machine's default subscription, exactly as the README describes.

---

## S7 — GitHub pull-request intake

### What S7 shipped

A second pull-request *source*. `GitHubPullRequestSource` turns an `owner/repo#n`
reference or a GitHub PR URL into the exact same `PullRequest` shape the local
source produces, using `gh` and nothing else. This is the local-first bet paying
off: GitHub becomes an input to a pipeline already proven end to end, not a
prerequisite for finding out whether the idea works.

### Decisions worth recording

- **`gh` owns the GitHub credential, so it is run with the normal environment.**
  This is the deliberate opposite of the `claude` child, which is stripped to a
  three-name allowlist precisely so it can never see a credential. `gh` is the
  tool the credential belongs to; Panorama never reads, stores, or prints a
  token, and it never echoes raw `gh` stderr — a failure becomes a host-authored,
  actionable message, because stderr is the most likely place a token hint or an
  operator path would surface.
- **Two REST reads via `gh api`, not a clone and not `gh pr diff`.** The
  pull-request object gives reliable base/head SHAs and refs; the same object
  requested with an `Accept: …diff` header gives the unified diff. Both resolve a
  fork PR without ever fetching the fork, which keeps intake cheap and side-effect
  free — no working tree, no checkout — and defers all cloning to S8 where it
  belongs.
- **Intake lands now; cross-repository review waits for the workspace.** A GitHub
  PR cannot be reviewed across its siblings until those siblings are cloned into a
  workspace (S8). Rather than pretend, the CLI's GitHub path normalizes the pull
  request and stops there, saying so. The `PullRequest` it produces is
  byte-for-byte the shape the local pipeline already consumes, so S8 adds a
  workspace and nothing else changes.

### How it was verified

Offline tests drive a fake `gh` through success, a fork PR, a `gh` failure (with
a planted token proving stderr never leaks), a missing binary, non-JSON output,
and missing SHAs. The field assumptions were then confirmed against the real
`gh` on a live public pull request — base/head SHAs, refs, URL, title, and diff
all normalized as expected. No private repositories were needed: intake reads any
accessible PR, and seeding the private demo org is a later, separate milestone.

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
