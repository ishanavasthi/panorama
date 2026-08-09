# Panorama V2 — plan

V1 answered the assignment: a CLI that reviews a pull request with its sibling
repositories in view, cites `repo/path:line`, and verifies every citation
host-side before showing it. It is feature-complete and demonstrable.

V2 turns that into a system someone would actually leave running. Three things
change, in this order:

1. **It becomes measurable.** V1's entire evidence base is 4 fixture PRs run
   twice, scored by hand. Every quality decision after that point is a guess.
2. **It becomes a better reviewer.** Retrieval is one lexical channel. The
   biggest wins available are structural — who actually depends on whom, what a
   repo actually exports, what changes together historically.
3. **It stops needing a human to run it.** V1 reviews when you type a command.

This document is authoritative on **ordering and exit criteria**. `CHECKLIST.md`
tracks what is done. `DECISIONS.md` continues to record trade-offs *after* they
are made. `IMPLEMENT.md` and `v1plan.md` remain the record of V1 and are not
edited.

---

## Constraints carried forward from V1

These are still non-negotiable, and V2 makes two of them *harder*, not easier.

1. **Claude Code subscription only.** No Anthropic SDK, no HTTP endpoint, no API
   key, no credential passed to a child process. Confirmed for V2. This is the
   single most consequential constraint on the delivery design — see the
   architecture decision below.
2. **No hard-coded findings.** Production code contains no fixture repo names,
   field names, PR numbers, or expected categories. V2 adds a dependency graph
   and a symbol index, which makes this *easier to violate by accident* — see
   "How the dependency graph avoids fixture knowledge".
3. **Reference-only output.** `repo/path:line`, never a source excerpt.
4. **Read/search only for the model.** `Read`, `Grep`, `Glob`. Nothing else.
5. **Credentials stay with the tools that own them.** `gh` holds GitHub auth.
6. **Repository content is untrusted data.** V2 raises the stakes here: an
   autonomous watcher that posts comments is a bigger prize for an injected
   instruction than a CLI a human runs and reads.

### New constraint for V2

7. **The cache can never make a review wrong.** V2 introduces persistent state.
   That state may only influence *which repositories and files get looked at*.
   Every citation is still validated against the live checkout at the reviewed
   SHA, on every run, with no cache in the path. A stale or corrupted cache must
   be able to produce a *worse* review, never an *unsupported* one. This is what
   keeps the V1 guarantee intact while adding state.

---

## The central architectural decision: subscription-only forces a pull model

The obvious way to review PRs automatically is a GitHub App: a webhook endpoint,
an event handler, a queue. V1 deferred it as "delivery plumbing". V2 cannot
build it *at all* in the usual shape, and the reason is worth stating plainly.

A hosted service has no Claude Code subscription. The subscription is an
interactive credential belonging to a machine and a person. You cannot put it in
a serverless function, and passing it to one would violate constraint #1 and #5
simultaneously. So the options are:

| Option | Verdict |
|---|---|
| Hosted GitHub App + webhooks | **Impossible** under constraint #1. Requires an API key server-side. |
| GitHub Actions **self-hosted runner** on the developer's machine | Possible. Needs runner registration, a workflow file in every repo, and `claude` authenticated as the runner's user. Heavy setup, per-repo opt-in. |
| **Local polling watcher** (`panorama watch`) | **Chosen.** No inbound network, no public URL, no tunnel, no runner registration, works behind NAT, one command, and the state it needs is exactly the local cache V2 is already building. |

**Decision: V2 delivers automation as `panorama watch <owner>`** — a long-lived
local process that polls the GitHub API for open pull requests, reviews the ones
that are new or whose head SHA moved, and optionally posts. The self-hosted
runner path is documented as an alternative for teams who want it, but is not
built.

**Trade-off:** reviews are delayed by the poll interval rather than instant, and
automation only runs while someone's machine is up. That is an honest
consequence of the subscription constraint, not a shortcoming we can engineer
away. In exchange there is no server to operate, no secret to store anywhere,
and no inbound attack surface at all.

---

## Retrieval: from one channel to four, fused

V1 retrieval is a single lexical pass: extract generic signals from the diff,
`git grep` siblings, rank by distinct matched signals with an IDF-like discount.
It works, and S4 already recorded its weakness — on the convention-violation
case (P3) the correct neighbour only *ties* for top rank, because a violation is
about an *absent* convention and leaves almost no lexical footprint.

V2 keeps that channel unchanged and adds three structural ones, then fuses.

### Channel A — lexical (existing, unchanged)

`retrieval.extract_signals` + `git grep`. Strong on renames and duplicated
helpers. Weak on absence, and on any link that shares no vocabulary.

### Channel B — manifest dependency graph

Parse every repository's own package manifest to learn two things: the package
name it **declares for itself**, and the package names it **depends on**. An
edge exists when repo X depends on a package name that repo Y declares as its
own. A repo on the receiving end of an edge from the PR repo is a candidate
consumer *by construction*, regardless of vocabulary overlap.

This is the channel that fixes P3: the contracts repo is a declared dependency
even though the diff never mentions it.

**How the dependency graph avoids fixture knowledge.** The tempting
implementation — match a dependency string against a workspace directory name —
would smuggle in an assumption that repo directories are named after packages.
Instead, edges resolve **declared name to declared name**: read every sibling's
manifest, build `declared_package_name -> repo`, and resolve the PR repo's
dependency list through that map. No repo-name string matching anywhere, and it
works on real orgs where `github.com/acme/api-server` publishes `@acme/api`.

### Channel C — symbol index

Per repository, at its HEAD SHA: the set of **exported** symbols with their
`path:line`, and the set of **imported** external symbols. Cached, keyed by SHA.

This turns "does anyone consume this field" from a fuzzy grep into a lookup, and
makes rename detection exact — a symbol present in the removed side of the diff
and present in a sibling's *import* set is a contract break with a precise
citation, not a lexical coincidence.

**Regex-based, not tree-sitter.** Language packs (below) use anchored regexes
over export/import syntax. Reasons: zero new dependencies, consistent with V1's
"no parser framework" posture, and the eval harness will tell us empirically
whether recall is good enough. If V2.7's measurement shows regex recall is the
binding constraint, tree-sitter becomes a justified, *measured* upgrade rather
than a speculative one.

### Channel D — co-change coupling (stretch)

Mine each repository's git log for signals that two repos change together:
same-author commits within a short window, and commit messages or branch names
that reference another repository or a shared ticket key. Produces a per-pair
prior that improves as the org's history grows and captures coupling that
neither text nor manifests express.

Marked **stretch** because it is the least certain of the four and the corpus of
fixture repos has shallow synthetic history, which is exactly the condition
under which it will look better than it is. It must be measured on a real org
before it is trusted.

### Fusion: reciprocal rank fusion, not weighted sum

Each channel produces its own ranking of sibling repositories. They are combined
with **reciprocal rank fusion** — each repo's score is the sum of `1/(k + rank)`
across the channels that ranked it.

**Why RRF over a tuned weighted sum:** a weighted sum needs per-channel weights,
and the only data available to fit them is an 18-case fixture corpus. Weights
fitted on 18 cases are overfitting with extra steps. RRF has one constant
(`k=60`, the standard value), needs no fitting, is robust when one channel is
silent, and stays fully explainable — the report can say "ranked #1 by
dependency edge, #3 by lexical overlap, not seen by symbol index".

**Trade-off:** RRF discards the *magnitude* of a channel's confidence, only
using order. A channel that is overwhelmingly certain cannot express that. This
is a deliberate trade of some ceiling for a lot of robustness, and the eval
harness makes it a testable choice rather than a permanent one.

---

## Scale: manifest-first candidate selection

V1 clones every repository in the org and refuses above 50. That ceiling is a
symptom of cloning everything, not a real limit on the idea.

V2 splits provisioning into two phases:

1. **Metadata phase.** Fetch only each repository's manifest and default-branch
   SHA via `gh api` contents reads — no clone. Build the dependency graph from
   that. Cheap enough for hundreds of repos.
2. **Selection phase.** Rank candidates using the dependency graph plus the
   cached symbol index from previous runs, take the top N (default 15,
   configurable), and full-clone only those — blobless
   (`--filter=blob:none`) with the working tree materialised on demand.

**Effect:** the 50-repo ceiling is replaced by a *clone budget*, and Panorama
becomes usable on an org where cloning everything is absurd.

**Trade-off:** selection can be wrong. A relevant sibling with no manifest edge
and no cached symbol overlap will not be cloned, and therefore cannot be found —
a *silent* recall loss, which is the worst kind. Mitigations: the report always
states how many repos were considered versus cloned, `--all-repos` forces the V1
behaviour, and the eval harness measures selection recall explicitly (does the
labelled target repo survive selection?) as a separate metric from ranking.

---

## Feedback: fingerprints and suppression

A reviewer that repeats a rejected finding every run gets muted by its readers.
V2 gives findings identity and remembers dismissals.

- **Fingerprint** = stable hash of `category` + normalized title + the sorted
  set of evidence locations at *path* granularity (not line — lines drift).
- **Dismissal** is read from the PR thread: a 👎 reaction on Panorama's comment,
  or a reply containing an explicit dismiss directive.
- **Suppression** is per-owner, stored in the local cache. A suppressed finding
  is dropped and *counted in the report*, exactly like a validation discard —
  silence is always explained.

**Trade-off:** dismissal signals are read from repository content, which is
untrusted data (constraint #6). A dismiss directive can therefore only ever
*reduce* what Panorama says — it can never add a finding, change a severity, or
alter a citation. The blast radius of a forged dismissal is one suppressed
finding, and `panorama suppressions list|clear` makes the state visible and
reversible.

---

## Milestones

Ordering rule: **measurement before mechanism.** Nothing in the retrieval track
starts until there is a baseline number to beat.

Estimates assume focused work. The **cut line sits after V2.7** — at that point
Panorama is measurably better than V1, fully tested, and nothing is
half-finished. Everything after it is additive.

### V2.1 — Evaluation harness and baseline · 2.5h

`panorama eval` over labelled cases in `evals/cases/*.yaml`. Two modes:

- `--offline` (default): scores **retrieval only**, deterministic, no
  subscription, no network — so it runs in CI on every commit.
- `--live`: runs the full pipeline against the real subscription, `-k N` times
  per case, scoring review-level outcomes and flake rate. Never in default CI.

Retrieval metrics: recall@1 / recall@3 of the labelled target repo, MRR, and
file-level hit recall. Review metrics: category match, evidence-repo match,
evidence-file match, verdict match, discard rate, and false-positive rate on
negative cases.

**Testing:** unit tests for the scorer against synthetic rankings including
ties, empty rankings, and multi-target cases; a golden test that the report
renders identically for a fixed input; a test that `--offline` invokes no
network and no `claude`.

**Exit:** baseline numbers for V1 retrieval on the existing 4 cases, committed to
`evals/baseline.json`. Every later milestone reports its delta against it.

### V2.2 — Corpus expansion, polyglot · 2.5h

Grow the fixture org from 4 repos / 4 cases to 6 repos / ~18 cases, spanning
TypeScript, Python, and Go.

Two new fixture repos: a Python consumer of the API, and a Go service. Cases
cover every category — `contract_break` (field rename, type narrowing, removed
endpoint, changed status code, removed enum member), `duplicate_logic`
(validator, retry, date formatting), `convention` (error envelope, timestamps,
versioning), `cross_repo_conflict` (two repos defining one constant
differently), `single_repo`.

**About one third must be negative controls**, and they must be *hard* ones: a
docs change, a test-only change, a formatting-only change, a dependency bump,
and — the important one — a rename of a **private** symbol that no sibling
consumes. A reviewer that flags that has learned to flag renames, not to find
contract breaks.

**Testing:** every case's labels are asserted self-consistent (target repo
exists, target file exists on the target's branch at the labelled path); the
existing constraint-#2 test is extended to the new fixture data; `git diff`
produces a non-empty diff for every positive case and the expected shape for
every negative.

**Exit:** 18 cases bootstrap from checked-in data with no network, `panorama
eval --offline` scores all of them, and the baseline is re-measured on the full
corpus. Expect the baseline to *drop* — that is the corpus doing its job.

### V2.3 — Cache, language packs, channel interface · 2.5h

The foundation the retrieval channels plug into. No user-visible change.

- SQLite cache at `~/.panorama/cache/<owner>.db`, mode `0600`, stdlib
  `sqlite3`. Tables keyed by `(repo, head_sha)` so a SHA change invalidates
  naturally. Schema version column with a drop-and-rebuild migration — the
  cache is a derived artifact, so throwing it away is always a legal repair.
- **Language packs**: a declarative table per language — manifest filename and
  how to read declared name / dependency list from it, export patterns, import
  patterns, file extensions. TypeScript/JS, Python, Go at launch.
- `RetrievalChannel` protocol: takes `(PullRequest, Workspace)`, returns a
  ranked list of repos plus per-repo justifications. The existing lexical pass
  is refactored to be the first implementation, with no behaviour change.

**Note on two reversed V1 bans.** V1 explicitly deferred "a language-plugin
framework" and "persistent semantic indexes". V2 reverses both, narrowly and
deliberately: a declarative table of regexes is not a plugin framework, and a
SHA-keyed cache of derived facts that is always safe to delete is not a semantic
index. Both reversals are recorded in `DECISIONS.md` when they land, with the
reasoning, rather than quietly contradicting V1.

**Testing:** cache round-trip, SHA-change invalidation, schema-version rebuild,
concurrent-writer behaviour, `0600` enforcement; language-pack extraction unit
tests per language including files that parse as nothing; and — critically — a
**refactor-equivalence test** asserting the channel-wrapped lexical pass returns
byte-identical results to V1 on all 18 cases.

**Exit:** `panorama eval --offline` scores identically to the V2.2 baseline. A
foundation milestone that changes a number is a foundation milestone with a bug.

### V2.4 — Dependency-graph channel · 2h

Channel B. Manifest parsing across the workspace, declared-name resolution, edge
construction, ranking by edge direction and distance (direct dependent ranks
above transitive).

**Testing:** name-resolution unit tests including a package whose declared name
differs from its directory, a dependency on something outside the workspace, a
malformed manifest, a missing manifest, a dependency cycle, and a monorepo-style
manifest with workspaces. A constraint-#2 test asserting the module contains no
fixture tokens.

**Exit:** recall@3 improves over V2.2 baseline, and the convention case that
tied in S4 now ranks its target first. No regression on any other case.

### V2.5 — Symbol index channel · 2.5h

Channel C. Export/import extraction per language pack, cached by SHA, plus the
removed-symbol-to-sibling-import lookup that makes rename detection exact.

**Testing:** extraction correctness per language; cache hit/miss and
invalidation on SHA change; a large-file cap; a test that a symbol appearing
only in a comment is not indexed; and a performance assertion that a warm cache
run does no file reads for unchanged repos.

**Exit:** recall@1 improves over V2.4; warm-cache retrieval is measurably faster
than cold on the full corpus.

### V2.6 — Co-change channel · 1.5h · **stretch**

Channel D. Skip without hesitation if V2.1–V2.5 ran long — it is the least
certain channel and the fixture corpus is the environment most likely to
flatter it.

**Exit:** measured on the corpus *and* on a real org before being enabled by
default. If it does not clearly help, it ships **off** behind a flag, and that
result is recorded rather than buried.

### V2.7 — Fusion, tuning, and the honest number · 2h

Wire RRF across all live channels. Re-measure everything. Add per-finding
provenance so the report can state why a repository was examined.

At most **two tuning iterations**, same discipline as V1's S6 — and tuning means
changing generic mechanism, never adding a fixture-specific special case.

**Testing:** RRF unit tests (single channel, disagreeing channels, one channel
silent, full disagreement); provenance rendering; end-to-end offline eval;
full existing suite green.

**Exit — and the cut line.** Recall@3 ≥ 0.90 on positive cases, false-positive
rate ≤ 0.15 on negative cases, no case regressed versus V1 baseline. `DECISIONS.md`
and `README.md` updated with the measured before/after table. **If the weekend
ends here, V2 is a complete, coherent release.**

*(Targets are provisional until V2.1 produces the real baseline. If the V1
baseline is already at 0.85, a 0.90 target is not ambitious; the numbers get
calibrated once, in V2.1, and then held.)*

---

### V2.8 — Manifest-first selection and blobless clone · 2h

Two-phase provisioning, the clone budget, `--all-repos` escape hatch, and
selection recall as a first-class reported metric.

**Testing:** the existing `provision` suite (real git against local bare
remotes) extended with a blobless clone, on-demand tree materialisation, the
selection cut, a forced `--all-repos` run, and an org above the old 50 ceiling.
An eval assertion that selection never drops a labelled target on the corpus.

**Exit:** a review completes on an org larger than 50 repos while cloning ≤ 15,
and the report states considered-vs-cloned counts.

### V2.9 — `panorama watch` · 3h

The polling watcher. Cursor state in the cache (per-PR last-reviewed head SHA),
conditional requests with ETags, backoff on rate limits, a per-run and per-hour
review budget, and structured JSON logs.

**Safety defaults, all of them deliberate:**

- **Dry-run by default.** Posting requires `--post`, and `--post` requires an
  explicit repo allowlist. An autonomous process that writes to GitHub by
  default is a mistake waiting for a bad night.
- One review in flight at a time — the workspace lock already enforces this, and
  the watcher must queue rather than fail on contention.
- A PR whose head SHA has not moved is never re-reviewed.
- Draft PRs skipped by default; bot-authored PRs skipped by default.
- Hard hourly cap on reviews, so a force-push loop cannot spend the day.

**Testing:** the poll loop driven entirely by a fake `gh` — new PR, updated PR,
unchanged PR, draft, closed mid-review, rate-limit 403 with backoff, network
failure and recovery, and a restart resuming from cursor state without
re-reviewing. Clean SIGTERM shutdown mid-review. All offline.

**Exit:** watch a seeded private demo org, push a commit to a seeded PR, and see
exactly one review appear — then push again and see exactly one update, not a
second comment.

### V2.10 — Inline comments, fingerprints, suppression · 2.5h

Inline review comments via `gh api /pulls/{n}/reviews`, mapping validated
`pr_path`/`pr_line` to diff position. Fingerprints and the suppression flow.

**The idempotency trade-off, recorded up front.** V1's summary comment is
idempotent because a single marked issue comment can be edited in place. A
review with inline comments cannot be — the API creates a new review each time.
So: **the summary comment stays the canonical, always-updated artifact**, and
inline comments are posted only for findings whose fingerprint was not present
in the previous run, with prior inline comments left in place rather than
churned. Re-reviewing a PR therefore never duplicates the summary, and adds
inline comments only for genuinely new findings.

**Testing:** position mapping against real diff fixtures including added lines,
context lines, a multi-hunk file, a rename, and a line at a hunk boundary;
fingerprint stability across reworded titles and shifted lines; fingerprint
*in*stability when evidence changes repo; suppression round-trip through the
cache; a forged-dismissal test proving it can only subtract; `--inline` with
zero valid `pr_line`s posting nothing rather than erroring.

**Exit:** two consecutive runs on a changing PR produce one summary comment and
inline comments only for new findings, verified live on the demo org.

### V2.11 — Operations and documentation · 1.5h

`panorama status` (cache size, last watch cursor, suppression count, recent run
outcomes), `panorama cache clear`, structured logging throughout, `--fail-on
<severity>` exit codes for CI use, and the docs pass: `README.md`, a V2
architecture section, `DECISIONS.md` entries for every trade-off above, and the
updated limitations list.

**Exit:** a fresh clone can install, bootstrap, eval offline, review a fixture,
and start a watcher using only the README.

---

## Budget

| Milestone | Hours | Day |
|---|---:|---|
| V2.1 Eval harness + baseline | 2.5 | Sat |
| V2.2 Corpus expansion, polyglot | 2.5 | Sat |
| V2.3 Cache + language packs + channels | 2.5 | Sat |
| V2.4 Dependency-graph channel | 2.0 | Sat |
| V2.5 Symbol index channel | 2.5 | Sat |
| V2.6 Co-change channel *(stretch)* | 1.5 | Sat |
| V2.7 Fusion + tuning — **cut line** | 2.0 | Sat/Sun |
| **Subtotal to a coherent release** | **15.5** | |
| V2.8 Selection + blobless clone | 2.0 | Sun |
| V2.9 `panorama watch` | 3.0 | Sun |
| V2.10 Inline + fingerprints + suppression | 2.5 | Sun |
| V2.11 Operations + docs | 1.5 | Sun |
| **Total** | **24.5** | |

24.5 hours is more than a weekend. That is why the cut line exists and why V2.6
is marked stretch. **Honest read: V2.1–V2.5 plus V2.7 is a full, hard Saturday
(~11.5h) and leaves Sunday for V2.9–V2.11 (~7h) with V2.8 as the first thing to
drop.** If Saturday slips, drop V2.6 first, then V2.8, then V2.10's inline
comments (keeping fingerprints and suppression, which are cheap and independent).

**Drop order, explicit:** V2.6 → V2.8 → inline comments within V2.10 → V2.11's
`status` command. Never drop: the eval harness, the negative controls, host
validation, or the dry-run default on `watch`.

---

## Testing strategy

Unchanged in spirit from V1: the default suite is offline, deterministic, and
needs no subscription. V2 adds two tiers.

| Tier | Runs | Needs | Gate |
|---|---|---|---|
| Unit | every commit | nothing | must be green to commit |
| Integration (fake `claude`, fake `gh`, real git against bare remotes) | every commit | nothing | must be green to commit |
| **`eval --offline`** (retrieval scoring) | every commit | fixtures bootstrapped | **must not regress** |
| `eval --live` (review scoring, k=3) | per milestone with a quality claim | real subscription | recorded in `DECISIONS.md`, never in CI |
| Real-org smoke | before V2.7 exit and before V2.9 exit | network, a real org | manual, recorded |

Three rules that carry the most weight:

- **`eval --offline` is a regression gate, not a report.** A commit that lowers
  recall without a recorded reason does not land. This is what makes the
  retrieval track safe to move fast in.
- **Negative cases are scored as loudly as positive ones.** The failure mode
  that destroys trust is a confident finding about nothing.
- **Live evaluation records outcomes, never model wording.** Same discipline as
  S6; wording varies and is not the thing being measured.

### Open question for the real-org corpus

Public OSS orgs are on the list as a robustness corpus, and the choice of *which*
matters: it needs several genuinely interdependent repos in one org, ideally
polyglot, ideally with merged PRs that fixed a cross-repo break (those give
after-the-fact ground truth without hand labelling). I have candidates in mind
but no strong opinion — **worth picking together before V2.7's real-org smoke,
not before.** It is not on the critical path for V2.1–V2.5.

---

## Deferred in V2 — do not build

Carried forward from V1 and still out: hosted GitHub App, webhooks, queues,
background services, automatic patches, a second LLM verification pass, embeddings
and vector stores, follow-up chat, broad configuration systems.

Newly considered and deliberately deferred:

- **Tree-sitter parsing.** Only if V2.7 measurement proves regex recall is the
  binding constraint. A measured upgrade, not a speculative one.
- **Multi-machine / shared cache.** The cache is per-machine. Sharing it means a
  server, which means the subscription problem again.
- **Monorepo path scoping.** Generalising "repository" to "unit" touches the
  whole data model. It is a V3 decision made early, not a V2 bolt-on.
- **Permission-aware evidence.** Real and worth doing — a comment should not cite
  a path in a repo its readers cannot see. Needs a visibility check per reader,
  which is more design than the weekend has room for.
- **Learning from accepted findings.** Suppression handles rejection. Positive
  reinforcement needs a lot more data than one weekend generates.

---

## What "production level" means at the end of this

- Retrieval quality is a **number**, measured on 18 labelled cases including
  hard negatives, gated in CI, with a recorded before/after against V1.
- Panorama reviews **without being asked**, safely: dry-run by default, an
  explicit allowlist to post, rate-capped, resumable, one comment per PR.
- It works on orgs too large for V1, and says what it chose not to look at.
- It stops repeating findings its readers have rejected.
- Every V1 safety property still holds — reference-only output, host-validated
  citations, discard-never-downgrade, no credential in a child process, and now
  a cache that is provably unable to make a review unsupported.
