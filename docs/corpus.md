# The evaluation corpus

Specification for the labelled fixture organisation that `panorama eval` scores
against. Written at V2.2, when the corpus grew from 4 repositories / 4 cases to
6 repositories / 18 cases across three languages.

`v2plan.md` says *why* this milestone exists; this document says *what the
corpus is and why each case is labelled the way it is*. It is the reference to
read before adding, removing, or relabelling a case.

---

## Why the corpus had to grow

V2.1 measured V1 retrieval on the four inherited cases and every metric came
back at **1.000** except outright recall@1. A corpus that everything passes
cannot tell an improvement from a regression, so it cannot gate anything. The
retrieval track (V2.3–V2.7) is a sequence of quality claims, and each one needs
somewhere to fail.

So the corpus is designed for **headroom and honesty**, not for a good score.
Several cases are expected to fail at the V2.2 baseline. That is the point: a
baseline you can already beat is not a baseline.

---

## The organisation

Six repositories, three languages. The languages are not decoration — V2.3
ships a language pack per language and V2.4 builds a dependency graph from
manifests, and both need something to be wrong about.

| Repo | Language | Manifest | Role |
|---|---|---|---|
| `acme-api` | TypeScript | `package.json` (`acme-api`) | Link API. Owns the response shape, the error envelope in practice, and the endpoints. |
| `acme-web` | TypeScript | `package.json` (`acme-web`) | Browser client. Types and destructures the API response; imports shared helpers. |
| `acme-shared` | TypeScript | `package.json` (`acme-shared`) | Shared library: URL validation, timestamp formatting, error constants, retry policy, pagination limits. |
| `acme-contracts` | docs | `package.json` (`@acme/contracts`) | Organisation-wide conventions: error envelope, timestamps, versioning. |
| `acme-analytics` | Python | `pyproject.toml` (`acme-analytics`) | Reporting client. Consumes the API **over HTTP**. |
| `acme-gateway` | Go | `go.mod` (`github.com/acme/gateway`) | Edge gateway. Proxies to the API **over HTTP**. |

Three properties of that table are deliberate.

**`acme-contracts` declares a package name that is not its directory name.**
It publishes as `@acme/contracts` and `acme-api` depends on that string. A
dependency-graph channel that resolves edges by matching a dependency string
against a directory name would get this wrong, and V2.4's plan explicitly
forbids that shortcut. The fixture now proves it rather than trusting it.

**The Python and Go consumers have no manifest edge to anything.** A Go module
cannot `require` an npm package. Their coupling to the API is the HTTP contract
and nothing else. This keeps the dependency-graph channel honest: it will be
silent on a third of the corpus, and the fusion step (V2.7) has to cope with a
channel that says nothing rather than a channel that is always right.

**Each consumer consumes a *different slice* of the contract.** `acme-web` uses
`url` and the shared formatter; `acme-analytics` uses `created_at`, the status
enum, and `GET /stats`; `acme-gateway` uses `expires_at` and the 404 mapping.
Without this, every contract-break case would have three correct answers and
recall would be trivially high. With it, each case has exactly the targets it
really breaks.

---

## The cases

18 cases: 13 positive, 5 negative controls (28% of the corpus).

### `contract_break` — 5

| Case | PR | Target | What retrieval has to do |
|---|---|---|---|
| `contract-break-field-rename` | `acme-api` renames a response field | `acme-web` | Easy. The removed field name appears verbatim in the consumer. Inherited from V1. |
| `contract-break-type-narrowing` | `acme-shared` narrows a parameter from `Date \| string` to `Date` | `acme-web` | Easy. One distinctive function name, one consumer. |
| `contract-break-removed-endpoint` | `acme-api` deletes `GET /stats` | `acme-analytics` | Medium, cross-language. The route literal and the response field names appear in Python. |
| `contract-break-status-code` | `acme-api` returns 410 instead of 404 | `acme-gateway` | **Hard.** The status number is not a searchable token, and the one distinctive token it does remove (`not_found`) also appears in the shared library and the conventions doc. The correct repo has to beat two plausible bystanders. |
| `contract-break-removed-enum-member` | `acme-api` drops an enum member | `acme-analytics` | Medium, cross-language. The removed value string appears as a Python constant. |

### `duplicate_logic` — 3

| Case | PR | Target | Notes |
|---|---|---|---|
| `duplicate-logic-local-validator` | `acme-web` adds its own URL validator | `acme-shared` | Inherited from V1. Exercises the *added*-identifier path. |
| `duplicate-logic-retry` | `acme-gateway` adds a backoff loop | `acme-shared` | Cross-language, but the helper name and its parameters match, so it is findable. |
| `duplicate-logic-date-formatting` | `acme-analytics` adds UTC formatting | `acme-shared` | Cross-language and **naming-hostile**: `format_timestamp` and `formatTimestamp` are not the same token. The only bridge is a shared constant name, `TIMESTAMP_FORMAT`, which both sides spell identically. |

That last case is the corpus's honest statement of a real limitation: lexical
retrieval does not cross a naming-convention boundary. `snake_case` and
`camelCase` versions of the same idea are different strings, and no amount of
ranking fixes that. The case is winnable only because organisations really do
share constant names across languages — and if V2.5's symbol index cannot beat
it either, that is a finding worth recording rather than a corpus bug.

### `convention` — 3

| Case | PR | Target | Notes |
|---|---|---|---|
| `convention-endpoint-drift` | `acme-api` adds an endpoint with bare-string errors and a local timestamp | `acme-contracts` | Inherited from V1; the case that ties for first instead of leading. |
| `convention-local-timestamps` | `acme-gateway` formats a timestamp with a locale-dependent layout | `acme-contracts` | Findable, because the conventions doc names the anti-pattern **in each language**. |
| `convention-unversioned-endpoint` | `acme-api` mounts a new public endpoint without a version prefix | `acme-contracts` | **Hard, by construction.** The diff shares no vocabulary at all with the versioning document. Lexical retrieval cannot see this; the manifest edge `acme-api → @acme/contracts` is the only thing that can. |

`convention-unversioned-endpoint` is the case V2.4 exists to fix, and it should
score zero at the V2.2 baseline. If it ever passes for a lexical reason, the
case has decayed and needs rewriting.

The polyglot anti-pattern list in the timestamps document deserves a note: it
was added so the Go case has *some* lexical footprint. That is a corpus choice,
not a retrieval trick — a real polyglot organisation's conventions document
does name the wrong idiom in each language it uses. But it does make the case
easier than it would otherwise be, and the spec says so out loud.

### `cross_repo_conflict` — 1

`cross-repo-conflict-page-limit`: `acme-analytics` introduces its own
`MAX_LINKS_PER_PAGE` with a different value from the one `acme-shared` already
publishes. Cross-language but the constant name is spelled identically in both,
which is exactly how this failure happens in practice.

### `single_repo` — 1

`single-repo-unguarded-cache`: `acme-web` reads a cache entry without a
presence check. A real finding with **no cross-repository claim**, so it names
no target repo and is reported as *not retrieval-scorable*. It exists to keep
the review tier honest: a reviewer that can only produce cross-repo findings
will either miss this or manufacture a sibling for it.

### Negative controls — 5

Negatives are scored as loudly as positives, and they are the cases most easily
faked. Each one states an expectation retrieval can actually be wrong about.

| Case | PR | Offline expectation |
|---|---|---|
| `negative-docs-cleanup` | `acme-api` README and comment edits | **None.** Not retrieval-scorable; live tier only. |
| `negative-test-only` | `acme-gateway` adds a unit test for a package-private helper | `expect_no_hits` |
| `negative-formatting-only` | `acme-analytics` re-wraps local presentation code | `expect_no_hits` |
| `negative-dependency-bump` | `acme-api` bumps an external dependency version | `expect_no_hits` |
| `negative-private-symbol-rename` | `acme-api` renames an unexported helper nothing else calls | `expect_no_hits` |

`negative-private-symbol-rename` is the one that matters most. A reviewer that
flags it has learned to flag *renames*, not to find *contract breaks*, and that
reviewer will be muted by its readers within a week. It is deliberately
constructed so the diff contains no token any sibling could match: the helper's
parameter is named so that it falls in the stopword list, and the call site's
field name is too short to be a signal, leaving only the two helper names —
neither of which exists anywhere else in the organisation.

#### Why `expect_no_hits` and not `forbid_repos`

`forbid_repos` asserts that a named repository does not appear in the top *K*.
On a six-repo corpus that assertion is weaker than it looks: under competition
ranking, every repository with any hit at all tends to share a rank inside the
top 3, so a single incidental match makes the label fail for a reason that has
nothing to do with quality. Four of the five negatives therefore assert the
stronger and cleaner property — that the change produced **no sibling hits at
all** — and the fixture content is designed so that expectation is achievable
without contrivance. `forbid_repos` stays in the schema for a larger corpus,
where a top-*K* assertion means something again.

---

## What the corpus does *not* cover

Stated so it is a known gap rather than a discovered one:

- **Monorepos.** Every unit here is a whole repository.
- **Deep transitive dependency chains.** The graph is one hop wide, so V2.4's
  "direct dependent ranks above transitive" rule is exercised by unit tests
  rather than by the corpus.
- **Repository history.** The fixture history is one commit per branch, which
  is exactly the condition under which V2.6's co-change channel would look far
  better than it is. That channel must be measured on a real organisation.
- **Scale.** Six repositories say nothing about the clone budget in V2.8; that
  needs a synthesised large org, which is that milestone's own test fixture.
- **Adversarial content.** Prompt injection in repository content is covered by
  the boundary tests, not by the corpus.

---

## Rules for changing the corpus

1. **A case is ground truth, not a target.** If retrieval fails a case, fix
   retrieval or prove the label wrong. Never relax a label to make a number go
   up — that is the single fastest way to make the whole harness worthless.
2. **Changing the corpus invalidates the baseline.** The comparison refuses to
   compare aggregates across a changed corpus on purpose. Re-record the
   baseline in the same commit that changes the cases, and say in the commit
   message what moved.
3. **Fixture data stays data.** Repository names, field names and branch names
   live in `src/panorama/fixtures/data/` and `evals/cases/` only. Production
   code never learns them (constraint #2).
4. **New negatives must be hard.** A negative that no plausible reviewer would
   flag measures nothing.
