# V2 checklist

Tracks what is actually done. `v2plan.md` is authoritative on ordering and exit
criteria; this file is the ticking surface.

**Definition of done for any milestone:** code + tests written, `uv run pytest`
green, `uv run ruff check` clean, `panorama eval --offline` not regressed, work
committed and pushed, and — where the milestone produced a trade-off — a
`DECISIONS.md` entry written *after* the implementation.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (with reason)

---

## V2.1 — Evaluation harness and baseline · **DONE**

- [x] `evals/cases/*.toml` schema: case id, repo, base, head, category, target
      repos, target files, `expect_finding`, `forbid_repos`, `expect_no_hits`
      — *TOML, not YAML: `tomllib` is stdlib at the 3.11 floor, so ground truth
      costs no dependency*
- [x] Pydantic model + loader with validation errors that name the case file
- [x] Scorer: recall@1, recall@3, MRR, file-level hit recall
- [x] Scorer: **outright recall@1** (rank 1 with no tie) — added after the
      baseline came back saturated; see the finding below
- [x] Scorer: review-level — category match, evidence-repo, evidence-file,
      verdict, discard rate, false-positive rate on negatives, flake
- [x] `panorama eval` — retrieval only, no `claude`, no network
- [x] `panorama eval --live -k N` — full pipeline, flake rate across runs
      *(implemented and unit-tested; not yet exercised against a real
      subscription — that happens at V2.7)*
- [x] Report renderer (table + JSON), `--json` output
- [x] Baseline compare: per-case **and** aggregate, `--check/--no-check`,
      exit 4 on regression
- [x] Unit tests: ties, empty ranking, multi-target case, missing target repo
- [x] Golden test: report renders identically for fixed input
- [x] Test: `--offline` invokes neither `claude` nor the network
- [x] Test: labels point at repos and files that actually exist
- [x] **Baseline recorded** in `evals/baseline.json` for V1 retrieval, 4 cases
- [x] 53 new tests; full suite 424 green; ruff clean

**Exit met:** baseline committed. Every later milestone reports its delta.

### Findings from V2.1

- **The V1 corpus is saturated and cannot measure improvement.** recall@1,
  recall@3, MRR and file recall all came back at **1.000** on the four
  inherited cases. Building any retrieval channel against this corpus would
  have produced no evidence either way. This is the clearest possible
  vindication of doing measurement before mechanism — and it makes **V2.2 a
  hard prerequisite**, not a nice-to-have.
- **Outright recall@1 = 0.667 is the one number with room to move.** The
  convention case ranks its target first only in a **three-way tie**, exactly
  as V1's S4 predicted. Competition ranking surfaces that instead of letting
  alphabetical order resolve it; a naive index-based scorer would have reported
  a clean rank 1 and hidden the weakness. This is V2.4's target.
- **One of four cases asserts nothing checkable offline.** The docs-cleanup
  negative is reported as *not retrieval-scorable* rather than counted as a
  pass, because retrieval always ranks something. The offline gate therefore
  covers 3 of 4 inherited cases, and the report says so on every run.
- **Fixed a latent bootstrap bug:** fixture-repo enumeration did not skip
  dot-directories, so a stray `.panorama/` in the data tree presented as a
  broken fixture repo with a confusing error.

---

## V2.2 — Corpus expansion, polyglot · **DONE**

- [x] Fixture repo: Python consumer of the API (`acme-analytics`)
- [x] Fixture repo: Go service (`acme-gateway`)
- [x] Language coverage confirmed across TS/JS, Python, Go in the corpus
      — one manifest per language, asserted present for every repo
- [x] `contract_break` cases: field rename · type narrowing · removed endpoint ·
      changed status code · removed enum member
- [x] `duplicate_logic` cases: validator · retry · date formatting
- [x] `convention` cases: error envelope · timestamps · versioning
- [x] `cross_repo_conflict` case: one constant, two definitions
- [x] `single_repo` case
- [x] Negative controls (5 of 18, 28%): docs-only · test-only · formatting-only
      · dependency bump · **private-symbol rename with no consumer**
- [x] Bootstrap materialises all 6 repos and all 17 case branches offline
- [x] Test: every case's labels self-consistent (target repo + file exist at the
      labelled path on the target's branch)
- [x] Test: constraint #2 extended to the new fixture data — and to *every*
      production module, not the two that were listed before
- [x] Test: every positive case produces a non-empty diff
- [x] Test: the corpus covers every category the model can emit
- [x] Test: the negative controls are still controls (no public surface changed,
      no identifier added or dropped by the formatting-only case)
- [x] **Baseline re-measured** on the full 18-case corpus — it dropped
- [x] `docs/corpus.md`: the corpus design spec, including what it does *not*
      cover and the rules for changing it
- [x] Full suite 466 green; ruff clean

**Exit met:** 18 cases bootstrap and score with no network; baseline re-recorded.

### Findings from V2.2

- **The baseline dropped, which is the milestone working.** recall@1 1.000 →
  **0.750**, recall@3 1.000 → **0.917**, MRR 1.000 → **0.833**. Outright
  recall@1 went the other way, 0.667 → **0.750**, purely because the corpus
  gained ten easy-to-rank cases; it is an average, and averages move for
  uninteresting reasons. The per-case table is the thing to read.
- **The inherited convention case got measurably worse without retrieval
  changing at all.** It ranked first in a three-way tie on four cases; on 18 it
  ranks *second*, beaten by a reporting client that shares incidental
  vocabulary with the diff. Nothing regressed — the corpus simply grew enough
  bystanders for the existing weakness to show its true shape. This is the
  single clearest argument for having done the expansion before the channels.
- **One case is a deliberate, load-bearing zero.** The unversioned-endpoint
  violation shares no vocabulary with the document it breaks, because the
  violation *is* an absence. It is unfindable lexically by construction and is
  V2.4's target. It is pinned by id in the tests, so a different case breaking
  cannot hide inside the same failure count.
- **Incidental token collision is the dominant noise source at this size.**
  Two negative controls needed a local variable renamed before they produced
  zero hits — one collided with `formatted` inside the phrase
  "locale-formatted" in a conventions document. That is what the fusion step
  will have to be robust to, and it is worth remembering when a channel looks
  like it is working.
- **A case designed to be hard turned out easy, and the note says so.** The
  status-code break ranks its target outright first, because the consumer
  happens to mention the removed error code twice while the definer and the
  documenter mention it once each. That is luck of proportion, not
  understanding, and the rank is fragile.
- **Two process bugs found by doing this.** Ruff was linting the fixture data
  tree — an autofix there would have changed a labelled case without touching a
  label. And bootstrapping from the wrong working directory silently built a
  second organisation inside the data tree, producing eval results that
  disagreed with a hand probe until the stray directory was found.

---

## V2.3 — Cache, language packs, channel interface · **DONE**

- [x] SQLite cache at `~/.panorama/cache/<owner>.db`, stdlib `sqlite3`
- [x] Mode `0600` enforced on file and parent — restated on every open, not
      only at creation
- [x] Tables keyed `(repo, head_sha)`; schema-version column
- [x] Drop-and-rebuild migration on version mismatch
- [x] Corrupt or unreadable database recovered by rebuilding, never raised
- [x] Owner names sanitised before becoming filenames
- [x] Language pack table: manifest file, declared-name reader, dependency
      reader, export patterns, import patterns, extensions
- [x] Pack: TypeScript / JavaScript
- [x] Pack: Python
- [x] Pack: Go
- [x] `RetrievalChannel` protocol — ranked repos + per-repo justification
- [x] Competition ranking in the channel interface (ties share a rank)
- [x] Existing lexical pass refactored into the first channel, no behaviour change
- [x] Test: cache round-trip, SHA invalidation, schema rebuild, concurrent writer
- [x] Test: `0600` enforcement, on the file and the directory
- [x] Test: no nearest-SHA or latest-entry fallback exists
- [x] Test: per-language extraction, including a file that yields nothing
- [x] Test: a symbol appearing only in a comment or docstring is not indexed
- [x] Test: manifest declared name differing from the directory name
- [x] Test: both forms of Go's `require`
- [x] **Refactor-equivalence test**: channel-wrapped lexical == V1, all 18 cases,
      compared against a snapshot recorded *before* the refactor
- [x] `docs/how-it-works.md`: the mechanism walkthrough
- [x] 102 new tests; full suite 568 green; ruff clean

**Exit met:** `eval --offline` byte-identical to V2.2 — same aggregates, same
per-case ranks — and the golden snapshot matches field for field.

### Findings from V2.3

- **The equivalence check needed to be wider than the baseline.** The eval
  baseline proves *ranking* did not move. It says nothing about which signals
  were extracted, in what order they were searched, which exact lines matched,
  or what context window each lead carries — all of which reach the model. A
  snapshot of every observable field was recorded from the pre-refactor code
  *before* anything was touched, so the test compares against real prior
  behaviour rather than against itself.
- **A real bug in the cache, caught by a test written for a lesser reason.**
  Creating the tables stamps the current schema version, and the version check
  originally ran afterwards — so a database whose version row had gone missing
  would silently present as current and keep its incompatible rows. The version
  is now read before any table is created.
- **Go imports are paths, not name lists.** The first implementation tokenised
  every import group, turning `"encoding/json"` into `encoding` and `json` —
  inventing two names that are symbols in no repository, and discarding exactly
  the path the dependency graph resolves edges on. Packs now declare whether a
  captured import group is a list to split or a single literal name.
- **The fixture organisation already exercises the hard manifest case.** The
  conventions repository declares `@acme/contracts` while living in a directory
  called `acme-contracts`, and the API repository depends on the declared name.
  V2.4's edge resolves correctly through declared names and would find nothing
  through directory names.

---

---

## V2.4 — Dependency-graph channel · **DONE, with one exit criterion missed**

- [x] Manifest parsing per language pack across the workspace
- [x] `declared_package_name -> repo` map (never repo-name string matching)
- [x] Edge construction + direction
- [x] Ranking: direct dependent above transitive, and a **provider never takes
      rank 1** — rank encodes the kind of edge, not a position among whatever
      this channel happened to find
- [x] Reciprocal rank fusion across channels (`k=60`) — **landed here rather
      than in V2.7**, because a channel that is not fused changes no number, and
      V2.4's exit criteria are all measurements
- [x] Per-repository provenance, rendered into the model's prompt
- [x] Test: declared name differs from directory name
- [x] Test: dependency outside the workspace, recorded rather than dropped
- [x] Test: malformed manifest · missing manifest · dependency cycle (2- and
      3-repository) · duplicate declared name · self-dependency
- [x] Test: monorepo-style manifest with workspaces creates no phantom edges
- [x] Test: constraint #2 — no fixture tokens in the module
- [x] 56 new tests; full suite 608 green; ruff clean
- [x] Test: fusion — single channel · agreement · silence · total disagreement ·
      score precision · determinism
- [x] Measured: **recall@3 improves, 0.917 → 1.000**
- [x] Measured: **the S4 convention case now ranks its target first** (2 → 1)
- [ ] Measured: no case regressed — **not met.** Two contract-break cases moved
      from rank 1 to rank 2. Cause identified, recorded below, and accepted.

**Exit:** two of three met; the third missed for a understood structural reason
rather than a defect. Recorded in `DECISIONS.md` rather than worked around.

### Findings from V2.4

- **The load-bearing zero is gone, by exactly the intended mechanism.**
  `convention-unversioned-endpoint` scored nothing at the V2.2 baseline because
  the diff and the document it violates share no vocabulary at all — the
  violation is an *absence*. It now ranks its target **first**, found purely by
  a declared dependency edge. This is the single result V2.4 existed to produce.
- **recall@3 is 1.000.** Every positive case now has its target in the top
  three. MRR rose 0.833 → 0.875.
- **Two cases regressed, and the cause is the trade-off RRF was chosen with.**
  `contract-break-removed-endpoint` and `contract-break-status-code` each fell
  from rank 1 to rank 2. In both, the conventions repository is lexical #2 *and*
  a declared dependency of the pull request's repository, so it collects two
  votes and overtakes a target that had one. Fusion by rank discards magnitude,
  so it cannot see that the demoted target led lexically by 18.0 to 4.0 in one
  case and by a hair in the other. Both look identical to it.
- **No unfitted knob fixes this**, which is why it was accepted rather than
  tuned away. Lowering `k` does not help: two second places beat one first place
  at every value. Using `max` instead of `sum` fixes these two cases and
  immediately un-fixes the convention case that motivated the milestone. The
  only remaining lever is per-channel weights, and the sole data available to
  fit them is the corpus they would then be scored against.
- **The dependency channel deliberately produces no file-level leads.** A
  manifest edge says *which* repository, never *where* in it. Emitting the
  manifest line as a lead would be true and useless — and it would make every
  negative control in the corpus surface a lead for a change that touched
  nothing. All four scorable negatives still produce zero leads.
- **The channel is diff-blind, which is its real cost.** It votes identically
  for every pull request in a repository, including ones that change nothing
  consequential. That is what produced the two demotions, and it is pinned by a
  test so it stays a known property rather than a surprise.

---

---

## V2.5 — Symbol index channel · **DONE**

- [x] Export extraction per language pack, with `path:line`
- [x] Import extraction per language pack
- [x] Cached by `(repo, head_sha)`, pruned so history does not accumulate
- [x] Removed-symbol → sibling-import lookup (exact rename detection)
- [x] Added-symbol → sibling-export lookup (duplication)
- [x] Rank encodes the *kind* of claim: a break always outranks a duplicate
- [x] Large-file cap, plus a per-repository file cap and a lead cap
- [x] Vendored directories excluded — their symbols belong to a third party
- [x] Cache wired into `panorama review`; a cache that will not open is skipped
      rather than failing the review
- [x] Test: extraction correctness per language
- [x] Test: cache hit/miss and SHA invalidation, and stale-generation pruning
- [x] Test: a symbol appearing only in a comment is not indexed
- [x] Test: warm cache does no file reads for unchanged repos
- [x] Test: a symbol on both sides of a diff is neither added nor removed
- [x] Test: using a cache does not change what is concluded
- [x] Test: a poisoned cache can mislead but cannot fabricate evidence
- [x] Measured: **recall@1 improves over V2.4, 0.750 → 0.833**
- [x] Measured: **warm-cache indexing ~33% faster** on the full corpus and
      rebuilds nothing
- [x] 33 new tests; full suite 641 green; ruff clean

**Exit met:** both measurements recorded.

### Findings from V2.5

- **recall@1 0.750 → 0.833, MRR 0.875 → 0.917, recall@3 held at 1.000, nothing
  regressed.** The case that moved is the cross-language duplicate, which had
  been beaten by the conventions repository on incidental shared vocabulary.
  The symbol index sees that the shared library actually *exports* a name the
  change adds, which the conventions document does not, and that is enough to
  separate them.
- **It did not rescue the two cases V2.4 demoted, and the reason is worth
  keeping.** Both consumers reach the changed service over HTTP. One is a
  Python client, the other a Go gateway, and neither *imports* anything from
  it — there is no declaration to match. This channel is silent for exactly the
  couplings the dependency graph is silent for, which means the corpus has a
  third of its cases where both structural channels abstain and only lexical
  matching speaks. That is a real limitation of syntax-based retrieval, not a
  gap in this implementation.
- **A bug in the diff reader, in the most consequential place.** A deleted file
  is written `+++ /dev/null`, and that was overwriting the path recovered from
  the `---` line — so the removed declarations of an entire deleted file were
  invisible. Deleting a file is the largest contract break there is. Caught by
  a test written specifically because that path looked awkward.
- **The added-and-removed subtraction matters more than it looks.** A reformat
  re-emits declarations on both sides of a diff; without cancelling those out,
  every formatting change would read as deleting and re-declaring the whole
  file. The formatting-only control would have failed loudly, which is the
  corpus doing its job.
- **The warm-cache claim is asserted as "read no files", not as a stopwatch.**
  On a six-repository fixture a timing assertion is mostly noise; "it did not
  open a single file" is the property that actually makes it faster, and it
  holds regardless of the machine.

---

---

## V2.6 — Co-change channel · **DONE — shipped OFF, verdict recorded**

- [x] Same-author / same-window commit coupling
- [x] Cross-repo references mined from commit messages (shared ticket keys)
- [x] Per-pair prior
- [x] **Degeneracy guards**: repositories with too little history are excluded,
      and a temporal signal that couples most pairs is discarded as
      non-discriminating
- [x] Measured on the fixture corpus — **it abstains entirely**
- [ ] Measured on a real org — **not done.** This is what would justify turning
      it on, and it is why it is off.
- [x] Decision recorded: **shipped off, behind `--experimental cochange`**
- [x] `panorama eval --experimental <name>` so the verdict is reproducible
      rather than asserted
- [x] Test: the mechanism fires on real history (shared ticket, discriminating
      co-commit), so "abstained" is distinguishable from "broken"
- [x] Test: a typo'd channel name is refused rather than silently enabling
      nothing
- [x] 18 new tests; ruff clean

**Exit met:** a recorded verdict. It is "not proven", and it is published rather
than buried.

### Findings from V2.6

- **On the only corpus available, the channel abstains completely.** Every
  fixture repository has a single commit on its default branch, so the
  thin-history guard excludes all six and no pair is coupled. Enabling it changes
  no number, which is verifiable with one command.
- **That is the corpus being honest, not the channel failing.** `v2plan.md`
  predicted this precisely: "the corpus of fixture repos has shallow synthetic
  history, which is exactly the condition under which it will look better than
  it is." The temptation was to enrich the fixture history so the channel had
  something to find — which would have been manufacturing the evidence used to
  justify it.
- **The density guard is the part worth keeping.** A coupling prior is only
  useful if it *discriminates*. One author creating an entire organisation in a
  scripted burst couples every pair — and so does a small team that ships
  everything on Fridays. A signal firing for most pairs carries no information,
  so it is dropped and the report says it was dropped. This is a real-world
  guard, not a fixture workaround.
- **A silent channel must explain itself.** "It abstained" and "it is broken"
  look identical from outside, so the channel always reports why it said
  nothing, and the tests build organisations with genuine history to prove the
  mechanism fires when there is something to find.
- **Shipping off by default is the point, not a compromise.** An unmeasured
  channel enabled by default is a quality claim nobody checked, and this project
  does not make those. Turning it on requires a measurement on a real
  organisation showing it helps cases the other channels miss.

---

---

## V2.7 — Fusion, tuning, and the honest number · **CUT LINE** · **DONE**

- [x] RRF (`k=60`) across all live channels *(landed early, in V2.4 — a channel
      that is not fused changes no number, and V2.4's exits are measurements)*
- [x] Per-repository provenance: why each repository was examined
- [x] Provenance rendered in the Markdown report **and** the JSON output
- [x] Test: RRF with one channel · disagreeing channels · one channel silent ·
      total disagreement · score precision · determinism
- [x] Test: provenance rendering, including the "nothing surfaced" case and a
      check that provenance never becomes a source-quoting channel
- [x] **Tuning iteration 1**: swept the fusion constant across the whole corpus.
      **No change made**, reason recorded below.
- [-] Tuning iteration 2 — **not spent.** The sweep showed there is nothing to
      spend it on; every constant above zero is identical.
- [~] `eval --live -k 3` across the full corpus — running; results land in a
      follow-up commit
- [x] Recall@3 ≥ 0.90 on positives — **met at 1.000**
- [~] False-positive rate ≤ 0.15 on negatives — pending the live run
- [x] No case regressed versus V1 baseline — **met**; the V1 case that only
      *tied* for first is now outright first
- [x] `DECISIONS.md`: V2 retrieval entry with the before/after tables
- [x] `README.md`: measured quality section

**Exit met: this is a shippable V2 on its own.**

### Findings from V2.7

- **The V1 weakness is closed.** The convention case that S4 recorded as ranking
  first only in a three-way tie is now outright first. All four V1 cases pass.
- **Retrieval on the 18-case corpus:** recall@3 **1.000**, recall@1 **0.833**
  (identical outright — no ties anywhere), MRR **0.917**, file recall **1.000**,
  zero leads on every negative control.
- **The fusion constant is inert.** Swept across the corpus, every value from 1
  upward produces byte-identical results. The standard value is calibrated for
  many systems ranking thousands of documents; here three channels rank a
  handful of repositories, so rank differences are a couple of percent while an
  extra channel's vote doubles a score. **Fusion at this scale is close to pure
  vote counting** — a structural property, not a tuning failure, and the
  complete explanation of the two cases that sit at rank 2.
- **Zero was measured and rejected.** At zero, two second places sum to exactly
  one first place, so the two demoted cases become ties and recall@1 and MRR
  both reach 1.000. That buys a perfect headline by making the ranking *less*
  discriminating, and reports it using the one metric blind to the difference.
  Refusing that is the whole reason the outright metric exists.
- **There is no knob left, and that is the real finding.** Beating two weak
  agreements with one strong conviction requires expressing magnitude, and every
  way of doing that needs a parameter fitted against the only corpus available.
  The remaining headroom is not in fusion — it is in the HTTP-contract couplings
  that nothing declares, which is a different design rather than a tuning pass.

---

---

## V2.8 — Manifest-first selection and blobless clone

- [ ] Phase 1: manifest + default-branch SHA via `gh api` contents, no clone
- [ ] Dependency graph built from metadata alone
- [ ] Phase 2: rank candidates, clone top N (default 15) blobless
- [ ] On-demand working-tree materialisation
- [ ] `--all-repos` escape hatch (V1 behaviour)
- [ ] Old 50-repo ceiling replaced by the clone budget
- [ ] Report states considered-vs-cloned counts
- [ ] Selection recall reported as its own eval metric
- [ ] Test: blobless clone + on-demand materialisation against local bare remotes
- [ ] Test: the selection cut · `--all-repos` · an org above 50 repos
- [ ] Test: selection never drops a labelled target on the corpus

**Exit:** a review completes on a >50-repo org cloning ≤ 15.

---

## V2.9 — `panorama watch`

- [ ] Poll loop over open PRs for an owner
- [ ] Per-PR cursor (last-reviewed head SHA) in the cache
- [ ] Conditional requests with ETags
- [ ] Rate-limit backoff
- [ ] Per-run and per-hour review budget
- [ ] Structured JSON logging
- [ ] **Dry-run by default; `--post` requires an explicit repo allowlist**
- [ ] One review in flight; queue on workspace-lock contention rather than fail
- [ ] Unmoved head SHA is never re-reviewed
- [ ] Draft PRs skipped by default
- [ ] Bot-authored PRs skipped by default
- [ ] Clean SIGTERM shutdown mid-review
- [ ] Test (fake `gh`): new PR · updated PR · unchanged PR · draft · closed
      mid-review · 403 rate limit with backoff · network failure and recovery
- [ ] Test: restart resumes from cursor without re-reviewing
- [ ] Live: push to a seeded demo PR → exactly one review appears
- [ ] Live: push again → exactly one update, not a second comment

**Exit:** both live checks pass on the private demo org.

---

## V2.10 — Inline comments, fingerprints, suppression

- [ ] Finding fingerprint: category + normalized title + evidence paths (not lines)
- [ ] Inline comments via `gh api /pulls/{n}/reviews`
- [ ] `pr_line` → diff position mapping
- [ ] Summary comment remains the canonical idempotent artifact
- [ ] Inline comments posted only for fingerprints new since the last run
- [ ] Dismissal read from 👎 reaction or explicit reply directive
- [ ] Per-owner suppression store in the cache
- [ ] Suppressed findings **counted in the report** (silence always explained)
- [ ] `panorama suppressions list` / `clear`
- [ ] Test: position mapping — added line · context line · multi-hunk · rename ·
      hunk boundary
- [ ] Test: fingerprint stable across reworded title and shifted lines
- [ ] Test: fingerprint changes when evidence changes repo
- [ ] Test: suppression round-trip
- [ ] Test: **a forged dismissal can only subtract** — never add, escalate, or
      alter a citation
- [ ] Test: `--inline` with zero valid `pr_line`s posts nothing, does not error
- [ ] Live: two runs on a changing PR → one summary, inline only for new findings

**Exit:** the live two-run check passes.

---

## V2.11 — Operations and documentation

- [ ] `panorama status` — cache size, watch cursor, suppression count, recent runs
- [ ] `panorama cache clear`
- [ ] Structured logging throughout
- [ ] `--fail-on <severity>` exit codes
- [ ] `README.md` rewritten for V2
- [ ] V2 architecture section
- [ ] `DECISIONS.md` entries for every V2 trade-off
- [ ] Limitations list updated
- [ ] Fresh-clone walkthrough verified: install → bootstrap → eval → review → watch

**Exit:** the fresh-clone walkthrough works from the README alone.

---

## Standing rules for V2 (unchanged from V1 unless noted)

- [ ] No fixture names, field names, or expected findings in production code
- [ ] Findings cite `repo/path:line`; never quote another repository's source
- [ ] The only AI integration is the local `claude` CLI on subscription auth
- [ ] `gh` owns the GitHub credential; no token is read, stored, logged, or passed
- [ ] Model child process gets `Read`/`Grep`/`Glob` and a stripped environment
- [ ] **New:** the cache may only change *what is looked at*, never whether a
      citation is validated — host validation always runs against the live
      checkout at the reviewed SHA
- [ ] **New:** anything read from repository content or a PR thread can only
      *reduce* output, never add or escalate a finding
- [ ] Commit and push at each milestone boundary and each coherent unit within one
- [ ] `DECISIONS.md` written after implementation, only where there was a real
      trade-off
