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

## V2.3 — Cache, language packs, channel interface

- [ ] SQLite cache at `~/.panorama/cache/<owner>.db`, stdlib `sqlite3`
- [ ] Mode `0600` enforced on file and parent
- [ ] Tables keyed `(repo, head_sha)`; schema-version column
- [ ] Drop-and-rebuild migration on version mismatch
- [ ] Language pack table: manifest file, declared-name reader, dependency
      reader, export patterns, import patterns, extensions
- [ ] Pack: TypeScript / JavaScript
- [ ] Pack: Python
- [ ] Pack: Go
- [ ] `RetrievalChannel` protocol — ranked repos + per-repo justification
- [ ] Existing lexical pass refactored into the first channel, no behaviour change
- [ ] Test: cache round-trip, SHA invalidation, schema rebuild, concurrent writer
- [ ] Test: `0600` enforcement
- [ ] Test: per-language extraction, including a file that yields nothing
- [ ] **Refactor-equivalence test**: channel-wrapped lexical == V1, all 18 cases

**Exit:** `eval --offline` byte-identical to V2.2. A changed number here is a bug.

---

## V2.4 — Dependency-graph channel

- [ ] Manifest parsing per language pack across the workspace
- [ ] `declared_package_name -> repo` map (never repo-name string matching)
- [ ] Edge construction + direction
- [ ] Ranking: direct dependent above transitive
- [ ] Test: declared name differs from directory name
- [ ] Test: dependency outside the workspace
- [ ] Test: malformed manifest · missing manifest · dependency cycle
- [ ] Test: monorepo-style manifest with workspaces
- [ ] Test: constraint #2 — no fixture tokens in the module
- [ ] Measured: recall@3 improves over V2.2 baseline
- [ ] Measured: the S4 convention case now ranks its target first
- [ ] Measured: no case regressed

**Exit:** all three measurements recorded.

---

## V2.5 — Symbol index channel

- [ ] Export extraction per language pack, with `path:line`
- [ ] Import extraction per language pack
- [ ] Cached by `(repo, head_sha)`
- [ ] Removed-symbol → sibling-import lookup (exact rename detection)
- [ ] Large-file cap
- [ ] Test: extraction correctness per language
- [ ] Test: cache hit/miss and SHA invalidation
- [ ] Test: a symbol appearing only in a comment is not indexed
- [ ] Test: warm cache does no file reads for unchanged repos
- [ ] Measured: recall@1 improves over V2.4
- [ ] Measured: warm-cache retrieval faster than cold on the full corpus

**Exit:** both measurements recorded.

---

## V2.6 — Co-change channel · **stretch**

- [ ] Same-author / same-window commit coupling
- [ ] Cross-repo references mined from commit messages and branch names
- [ ] Per-pair prior, cached
- [ ] Measured on the fixture corpus
- [ ] Measured on a real org (fixture history is shallow and will flatter it)
- [ ] Decision recorded: on by default, or shipped off behind a flag

**Exit:** a recorded verdict either way. "Didn't help" is a valid, published result.

---

## V2.7 — Fusion, tuning, and the honest number · **CUT LINE**

- [ ] RRF (`k=60`) across all live channels
- [ ] Per-finding provenance: why each repository was examined
- [ ] Provenance rendered in the report
- [ ] Test: RRF with one channel · disagreeing channels · one channel silent ·
      total disagreement
- [ ] Test: provenance rendering
- [ ] Tuning iteration 1 (generic mechanism only, no fixture special-casing)
- [ ] Tuning iteration 2 (optional; hard stop at two)
- [ ] `eval --live -k 3` across the full corpus
- [ ] Recall@3 ≥ 0.90 on positives *(target confirmed against V2.1 baseline)*
- [ ] False-positive rate ≤ 0.15 on negatives
- [ ] No case regressed versus V1 baseline
- [ ] `DECISIONS.md`: V2 retrieval entry with the before/after table
- [ ] `README.md`: measured quality section

**Exit: this is a shippable V2 on its own. Stop here without regret if the
weekend runs out.**

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
