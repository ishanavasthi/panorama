# V2 checklist

Tracks what is actually done. `v2plan.md` is authoritative on ordering and exit
criteria; this file is the ticking surface.

**Definition of done for any milestone:** code + tests written, `uv run pytest`
green, `uv run ruff check` clean, `panorama eval --offline` not regressed, work
committed and pushed, and — where the milestone produced a trade-off — a
`DECISIONS.md` entry written *after* the implementation.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[-]` dropped (with reason)

---

## V2.1 — Evaluation harness and baseline

- [ ] `evals/cases/*.yaml` schema: case id, repo, base, head, category, target
      repos, target files, `expect_finding: true|false`, notes
- [ ] Pydantic model + loader with validation errors that name the case file
- [ ] Scorer: recall@1, recall@3, MRR, file-level hit recall
- [ ] Scorer: review-level — category match, evidence-repo, evidence-file,
      verdict, discard rate, false-positive rate on negatives
- [ ] `panorama eval --offline` — retrieval only, no `claude`, no network
- [ ] `panorama eval --live -k N` — full pipeline, flake rate across runs
- [ ] Report renderer (table + JSON), `--json` output
- [ ] Unit tests: ties, empty ranking, multi-target case, missing target repo
- [ ] Golden test: report renders identically for fixed input
- [ ] Test: `--offline` invokes neither `claude` nor the network
- [ ] **Baseline recorded** in `evals/baseline.json` for V1 retrieval, 4 cases

**Exit:** baseline committed. Every later milestone reports its delta.

---

## V2.2 — Corpus expansion, polyglot

- [ ] Fixture repo: Python consumer of the API
- [ ] Fixture repo: Go service
- [ ] Language coverage confirmed across TS/JS, Python, Go in the corpus
- [ ] `contract_break` cases: field rename · type narrowing · removed endpoint ·
      changed status code · removed enum member
- [ ] `duplicate_logic` cases: validator · retry · date formatting
- [ ] `convention` cases: error envelope · timestamps · versioning
- [ ] `cross_repo_conflict` case: one constant, two definitions
- [ ] `single_repo` case
- [ ] Negative controls (~1/3 of corpus): docs-only · test-only · formatting-only
      · dependency bump · **private-symbol rename with no consumer**
- [ ] Bootstrap materialises all 6 repos and all case branches offline
- [ ] Test: every case's labels self-consistent (target repo + file exist at the
      labelled path on the target's branch)
- [ ] Test: constraint #2 extended to the new fixture data
- [ ] Test: every positive case produces a non-empty diff
- [ ] **Baseline re-measured** on the full 18-case corpus (expect it to drop)

**Exit:** 18 cases, no network, baseline re-recorded.

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
