# How Panorama works

A walkthrough of the machinery, for someone who wants to understand what the
system actually does before reading any code. It covers the logic and the
reasoning inside each stage — what is computed, from what, and why that is the
right thing to compute — without descending into implementation.

Where the other documents fit:

| Document | Answers |
|---|---|
| `README.md` | How do I run it? |
| **this file** | How does it work? |
| `DECISIONS.md` | Why is it built this way, and what did we give up? |
| `docs/corpus.md` | What is it measured against? |
| `CHECKLIST.md` / `v2plan.md` | What is done, what is next? |

---

## The problem, stated precisely

A pull request can be completely correct inside its own repository and still
break something elsewhere. A renamed response field that another service
destructures. A helper that reimplements one the shared library already owns.
An endpoint that quietly ignores an organisation-wide convention. Ordinary code
review sees one repository, so it cannot see any of this.

Panorama reviews a pull request *with its sibling repositories in view* and
reports what the change does to its neighbours — with a citation for every
claim.

That last part is the hard half. An AI reviewer that produces plausible
cross-repository claims is worse than no reviewer, because the claims are
expensive to check and wrong often enough to be dangerous. So the system is
built around a single organising rule: **the model proposes, the host
verifies.** Everything below serves that.

---

## The pipeline

```
panorama review <PR>
   │
   ├─ preflight    is the environment capable of this at all?
   ├─ intake       the PR, normalised, pinned to an immutable commit
   ├─ workspace    the sibling repositories, read-only, at known commits
   ├─ retrieve     which siblings matter, and where to look in them
   ├─ review       the model reads and reasons, with search-only tools
   ├─ validate     every citation checked against the real checkout
   ├─ suppress     drop findings a human already rejected
   └─ deliver      Markdown, JSON, or one PR comment
```

Each stage hands the next a narrower, better-established set of facts. The
interesting design pressure is that stages before `review` decide *what the
model gets to see*, and stages after it decide *what survives*.

---

## Intake: pinning the thing under review

Intake turns "a pull request" into a fixed object: owner, repository, number,
base and head commits, refs, title, body, and the unified diff.

The detail that matters is that the **head commit is captured once and used
everywhere afterwards**. A pull request is a moving target — someone can push
while a review is running — and a review that read the diff at one commit and
validated citations against another would be quietly reporting on a state that
never existed. Pinning it makes the whole run describe a single, real snapshot.

The same shape is produced from two sources: a live GitHub pull request via the
`gh` CLI, or a pair of local git branches. Because both produce the identical
object, everything downstream is testable offline with no network and no
credentials — which is what makes the evaluation harness possible at all.

---

## Workspace: the neighbours, read-only

The workspace is a directory whose children are sibling repositories, each
pinned to a known commit. It answers three questions and refuses to do anything
else:

- **Which repositories exist, at which commit.**
- **Does this `repo/path` reference stay inside its repository?** Resolution
  follows symlinks *before* checking containment, so a path escaping via `..`
  or a symlink is rejected before anything reads it. This is the primitive
  citation validation is built on.
- **What does the organisation look like?** An *org map* derived purely from
  content: repository names, README and manifest summaries, top-level layout,
  and any convention documents found by generic naming patterns. No knowledge
  of any particular organisation is baked into the code — it reads what is
  there.

The workspace never writes, never checks anything out, and never touches the
network.

---

## Retrieval: deciding where to look

This is the part that makes the difference between a useful reviewer and an
expensive one. The model cannot read an entire organisation, so something has
to decide which handful of repositories and files are worth its attention.
Retrieval is that decision, and it is **deterministic** — no model involved —
which is precisely why it can be measured and gated in CI.

### Channels

A channel is one way of answering "which siblings matter". Each produces its own
ranked list of repositories, each ranked repository carrying a plain-language
justification.

**The lexical channel** (live) extracts generic *signals* from the diff —
changed field names, added or removed function and type names, route literals,
other identifiers — and searches the siblings for them. Signals are weighted by
what kind of thing they are (a public-looking field outranks an incidental
identifier), and a signal that matches *everywhere* is discounted, because a
token found in every repository discriminates nothing. Generic keywords,
primitive types and ubiquitous builtins are excluded outright, as are
generated, vendored and binary files.

Its strength is renames and duplicated helpers: both leave a distinctive string
in two places. Its weakness is structural and worth being blunt about — it can
only find a link that shares *vocabulary*. A convention violated by an
**absence** leaves no vocabulary at all. A helper duplicated across a naming
boundary (`format_timestamp` versus `formatTimestamp`) is two different
strings. No amount of tuning fixes either.

**The dependency channel** (live) answers what vocabulary cannot. Every
repository already declares the name it publishes under and the names it depends
on. An edge exists when one repository depends on a name another declares —
resolved **declared name to declared name**, never by matching a dependency
string against a directory, because the folder and the published package are
routinely different things in a real organisation.

Its ranking encodes *what kind of edge* a repository has, not where it happened
to land among this channel's own findings:

1. a **direct dependent** — something that can be *broken* by this change;
2. a **direct dependency** — where shared helpers and conventions live, and
   which can only be duplicated or contradicted, never broken;
3. the same two, one hop further out.

So a provider never occupies rank 1, whether or not any consumer exists. That
keeps a rank meaning the same thing across runs, which is what makes fusing it
with another channel's ranks meaningful at all.

Two properties are worth knowing before trusting it. It is **blind to the diff**:
it votes identically for every pull request in a repository, whatever the change
does. And it produces **no file-level leads** — a manifest edge tells you which
repository matters, never where in it.

**The symbol channel** (live) asks the sharpest question available: *does another
repository import a name this change removes?* An import is a declaration of
consumption, written by the consumer itself, so a repository that imports a
deleted symbol is broken by construction rather than by resemblance — and the
lead is a specific line rather than a guess. It makes two claims in a fixed
strength order: importing a removed name is a **break**; exporting a name the
change adds is **duplication**. A duplication signal never outranks a break.

Building this index means reading every source file in every sibling, which is
the most expensive thing retrieval does, so it is cached per repository at a
specific commit.

**The HTTP-contract channel** (live) exists because the two channels above can
only see relationships somebody wrote down, and the coupling that dominates a
service organisation is written down nowhere. A Python client and a Go gateway
can each depend utterly on a TypeScript API and appear in no manifest and no
import statement, because the only thing joining them is a request and the JSON
that comes back.

So this one reads the wire. It pulls four kinds of token out of the diff —
**route segments** (the static parts of a path literal, so `"/links/:id"` and
`base + "/links/" + code` agree), **payload fields** (object keys, response-type
members, `json:"..."` struct tags, dictionary subscripts), **payload values**
(the lowercase wordless literals error codes are made of) and **status codes** —
and looks for the same tokens in the siblings. A token present on both sides of
the diff is discarded, so a reformat is not a contract change.

Two filters are what stop it from being the lexical channel wearing a different
name:

- **Only wire positions count.** A route segment must come from a quoted path, a
  value from a quoted literal, a field from a key or a tag. An identifier that
  happens to be spelled the same is not a match.
- **Only repositories that speak HTTP are eligible** — meaning some source file
  of their own makes a request, serves a route, or decodes a response body.

The second filter is the one that does the work. On the corpus, a change to an
error code has three candidates that all contain the removed string: the gateway
that *reads* it, the shared library that *defines* it, and the conventions
document that *describes* it. Lexical matching sees three repositories
containing one string. But defining an error code is not consuming a contract —
the coupling belongs to whoever sends or reads it. The library is excluded
because it never makes a request; the conventions repository because it has no
source code at all.

It makes two claims in a fixed order: naming a **route** the change moves
outranks naming a **payload** token, because the endpoint is the strongest thing
on the wire.

One guard is worth knowing about. A token named by *every* eligible repository
is discarded as non-discriminating, and the report says so. The organisation's
central resource appears in every client it has, and matching it promotes
everything at once, which is the same as ranking nothing. The cost is that a
change breaking *every* consumer has its broadest token dropped — so this channel
is quietest exactly when a change is most sweeping. That is the case lexical
matching already handles well, since the token is then everywhere.

**The co-change channel** exists but ships **disabled**. It reads history rather
than the present, on the theory that repositories repeatedly changed together are
coupled in ways nobody wrote down. Two signals: a shared ticket key appearing in
both repositories' commits, and the same author committing to both inside a short
window.

It is off because it has never been shown to help. The fixture organisation is
built by a script in one burst, so it has no meaningful history, and the channel
correctly declines to guess. Turning it on requires a measurement on a real
organisation — an unmeasured channel enabled by default is a quality claim
nobody checked.

Its two guards are worth knowing about, because they apply to real
organisations too. A repository with too little history is excluded, since a
prior from a handful of commits is a number rather than evidence. And a signal
that couples *most* pairs is discarded as non-discriminating — a five-person
team that ships everything on Friday would otherwise appear to have every
repository coupled to every other, which says nothing while looking like it says
a great deal.

### What the structural channels cannot see

The dependency graph and the symbol index can only see relationships somebody
wrote down in a manifest or an import. Roughly a third of the evaluation corpus
is built so that they cannot: a Python client and a Go gateway that depend
utterly on a TypeScript service and appear in neither. Building a fixture
organisation where every dependency is declared would have made both channels
look considerably better than they deserve.

For four milestones those cases had only lexical matching to speak for them, and
two of them consequently ranked their target second. The HTTP-contract channel
was added to answer exactly that shape, and it did — but its own blindness is
the same kind of thing and worth stating in the same breath. It recognises HTTP
through a fixed list of common client, server and decoding idioms, so a
repository reaching the network through a library that list does not name is
invisible to it.

Every one of these limits costs recall in the same direction: a coupling not
seen is a lead not offered, never a citation invented.

### Why rankings are fused rather than summed

Channels measure incomparable things. A lexical overlap score and a dependency
edge distance share no unit, so combining them by weighted sum would mean
inventing weights — and the only data available to fit those weights is an
18-case corpus. Weights fitted on 18 cases are overfitting with extra steps.

So channels are combined by **reciprocal rank fusion**: each repository's score
is the sum of `1/(k + rank)` across the channels that ranked it. One constant,
no fitting, robust when a channel is silent, and fully explainable — the report
can say "ranked #1 by dependency edge, #3 by lexical overlap, not seen by the
symbol index".

The trade-off is that fusion discards *magnitude*. A channel that is
overwhelmingly certain about its top result cannot say so, so **two second
places outweigh one first place** regardless of how far ahead that first place
was. This was not hypothetical: for four milestones two cases had their true
target displaced by a repository that two channels each ranked second, even
though in one of them the true target led lexically by 18.0 to 4.0.

Sweeping the constant across the whole corpus showed something worth knowing:
**every value above zero produces identical results.** The standard value is
calibrated for many systems ranking thousands of documents, where damping the
influence of top ranks is the point. Here a handful of channels rank a handful of
repositories, so the gap between first and third is a couple of percent while one
extra channel voting at all doubles a score. Fusion at this scale is therefore
close to pure vote counting — a structural property of the method, not a tuning
opportunity. It also means there is no constant to overfit with.

Zero behaves differently, and was rejected for an instructive reason. At zero,
two second places sum to *exactly* one first place, so those two displaced cases
become ties and recall@1 reaches a perfect score. That is a better number
describing a ranking that discriminates *less*, reported using the one metric
blind to the difference — which is precisely what the ties-excluded metric exists
to catch.

Both cases now rank first, and **the constant was never touched**. What changed
is that a fourth channel sees the coupling they always had, so their targets
gained a third vote while the repository beating them gained none. That is the
distinction worth carrying: the fix for "two weak agreements beat one strong
conviction" was a missing signal, not a better weighting of the signals already
present. Fusion still discards magnitude, and would still produce the same
displacement given the same inputs.

### Silence is a legal answer

A channel with nothing to say returns an empty ranking, and that is ordinary
rather than a failure. The dependency graph is silent for a Python client of a
TypeScript API, because no manifest anywhere records that edge — the coupling is
the HTTP contract and nothing else. Roughly a third of the evaluation corpus is
built to be exactly that case, so fusion is forced to cope with abstention
rather than being flattered by channels that always answer.

### Leads are not evidence

Everything retrieval produces is a *lead* — a place to look. Nothing retrieval
finds is ever cited directly. The prompt says so explicitly, and validation
enforces it independently. This matters because retrieval is heuristic by
nature, and a heuristic feeding citations directly would be a machine for
generating confident nonsense.

### The search is shown, not just its conclusions

Every ranked repository carries a plain-language reason, and the report ends
with a "Repositories examined" section naming each repository and which channel
surfaced it, with what evidence — "ranked #1 by dependency edge, #3 by lexical
overlap, not seen by the symbol index".

That is a sentence a reviewer can disagree with. A bare ordering is not. Since
retrieval is heuristic by construction, showing the search is the honest posture:
it lets a reader judge how much weight the conclusions deserve, and it makes
silence legible — when nothing is surfaced, the report says so and says what was
tried.

---

### What retrieval currently achieves

On the labelled corpus, **every** positive case ranks its target repository
outright first — no ties anywhere — and every negative control surfaces zero
leads. recall@1, outright recall@1, recall@3, MRR and file recall all read
1.000.

The two cases that used to sit second were the ones where every structural
channel was silent, because their consumers reach the changed service over HTTP
and nothing is declared for the graph or the index to find. They were fixed by
adding the signal that was missing rather than by adjusting fusion: the constant
is still 60 and still untuned, and their targets moved because a fourth channel
gave them a vote the repository beating them could not earn.

One thing that follows from a perfect score, and it is not good news. **A
saturated corpus can detect a regression and cannot demonstrate an
improvement.** The harness is still worth running on every commit for the first
reason, and it has stopped being evidence for the second — the same position the
four inherited V1 cases were in before the corpus was expanded. Any further
retrieval work needs a larger or harder corpus, or the live tier, before it can
claim to have helped.

The numbers are re-measured on every commit, and a drop nobody explains does not
land.

---

## Choosing what to clone

An organisation can be far too large to clone whole, and cloning is the
expensive part — not the reasoning. So provisioning happens in two phases.

A **metadata phase** reads each repository's manifest through the API without
cloning anything, cheap enough for hundreds of repositories. A **selection
phase** then ranks candidates and clones only the top few, blobless, so history
arrives without file contents while the working tree is materialised in full.
The old "we refuse organisations above fifty repositories" ceiling becomes a
*clone budget*.

Candidates are ordered by: the pull request's own repository, always; then
repositories connected by a declared dependency, using **exactly the same edge
resolution the dependency channel uses**; then repositories a previous run's
symbol index says touch the changed names; then whatever budget remains, filled
in a stable order.

### The risk this creates, and why it is tolerable

Selection can be wrong, and when it is wrong it is **silent**. A relevant
sibling that is never cloned produces no lead, no finding, and no warning — the
review just comes back thinner. That is worse than a wrong finding, because a
wrong finding is visible.

This is not hypothetical. On the evaluation corpus, squeezing the budget below
the organisation's size drops the repository that genuinely breaks in one case:
its coupling is a mirrored response type, which no manifest records and no
symbol index sees on a first run. Nothing available before cloning can find that
relationship.

What makes it acceptable is that it is never *silent*:

- every review prints how many repositories were considered versus examined;
- `--all-repos` clones everything, restoring the older behaviour;
- the cached symbol index feeds selection, so the second review of an
  organisation is better targeted than the first.

---

## The cache: making it faster without making it wrong

V2 adds persistent local state — a small SQLite database per organisation,
holding derived facts like the symbol index and dependency edges.

One rule governs all of it: **the cache can only change what gets looked at,
never whether a citation is real.** Every citation in a finished review is
validated against the live checkout at the reviewed commit, on every run, with
no cache anywhere in that path. A stale or corrupt cache can produce a *worse*
review. It cannot produce an *unsupported* one.

Three design choices enforce that rather than merely intending it:

- **Everything is keyed by `(repository, commit)`.** Nothing is stored against a
  repository alone. A commit that changes anything changes the key, so a lookup
  at the new commit simply misses and the fact is recomputed. Invalidation is
  not a policy that could be wrong — it is the shape of the key. There is
  deliberately no "nearest commit" or "most recent entry" fallback, because that
  is exactly the shortcut that would let yesterday's index describe today's
  code.
- **A schema mismatch drops everything and rebuilds.** All of it is derived from
  repository content, so discarding it is always a legal repair. Writing
  incremental migrations for data you are allowed to delete is work that buys
  nothing and can itself be buggy.
- **Corruption is recovered from, not raised.** A database that will not open is
  deleted and rebuilt. Failing the review instead would let a scribbled-on file
  stop real work — a worse failure than doing the work again.

The file is owner-only inside an owner-only directory, because it describes
private repository content: symbol names, file paths, dependency edges. And it
is always safe to delete.

---

## Language packs: knowing three languages without a parser

The structural channels need three facts per language: which file declares the
package and how to read its declared name and dependencies, which files are
source, and what an export and an import look like.

That knowledge lives as a **table**, not a framework — a fixed set of entries in
one file, read by name. Nothing is dynamically loaded, discoverable, or
registrable at runtime; there is no lifecycle and no way for one entry to
observe another. Adding a language means adding a literal and its tests. The
distinction matters because V1 explicitly deferred "a language-plugin
framework", and this narrowly reverses that — so the boundary is worth being
precise about rather than drifting across.

Two details carry weight:

**Dependency edges resolve declared name to declared name.** The tempting
implementation matches a dependency string against a directory in the
workspace. It works perfectly on tidy fixtures and fails on real organisations,
where `github.com/acme/api-server` publishes `@acme/api`. Reading every
repository's own manifest and resolving through the names they *declare* costs
nothing extra and is simply correct.

**Comments are removed before symbols are extracted.** Otherwise a file that
*documents* a function looks like a file that *exports* one, and the index ends
up pointing at prose.

Extraction is regex-based rather than AST-based. That is a deliberate,
reversible choice: it costs no dependency, and the evaluation harness can say
empirically whether recall is good enough. Its known cost is that a comment
delimiter inside a string literal confuses the stripper — which loses a symbol
here and there. That loss is *safe*: a symbol not indexed is a lead not offered,
never a citation invented. If measurement shows regex recall is the binding
constraint, that becomes the argument for a real parser — a measured upgrade
rather than a speculative one.

---

## Review: what the model is allowed to do

The model runs through the locally installed Claude Code CLI on its existing
subscription. There is no SDK, no HTTP endpoint, no API key anywhere in the
system, and no credential is ever passed to the child process.

It is given: the pull request, the filtered diff, the org map, and retrieval's
leads. It is granted exactly three tools — **read, grep, glob** — and denied
everything else: no shell, no writes, no network, no plugins, no session
persistence, and no access to the user's personal configuration, so a developer's
own settings cannot influence a review.

Repository content is treated as **untrusted data throughout**. Every diff and
every file could contain text engineered to look like an instruction, so
untrusted content is fenced off from host instructions structurally, and the
system never follows directions found inside the material it is reviewing.

The model returns structured JSON against a fixed schema: a summary, a verdict,
and findings with severity, category, rationale, evidence locations, and
confidence. Evidence is `repo/path:line` — there is deliberately **no field for
a source excerpt**, so quoting another repository's source is not something the
system can do even by accident.

---

## Validation: the part that makes it trustworthy

Nothing the model says is believed until the host checks it, against the real
checkout, at the reviewed commit. For every finding:

- the repository resolves to an allowed workspace member, and the path stays
  inside it;
- the file exists at that commit and the line is in bounds;
- a finding that claims cross-repository impact carries at least one valid
  citation **outside** the pull request's own repository — otherwise the claim
  is not what the category says it is;
- any location it claims inside the pull request corresponds to a file and line
  the pull request actually changed;
- the text passes secret-shape screening and contains no quoted source.

**Findings that fail are discarded, never downgraded.** Downgrading is the
tempting option and the wrong one: it keeps unverifiable claims in front of a
reader with a smaller label attached. Discarding removes them and *counts* them,
and the report states the count — so silence is always explained rather than
merely absent.

Zero surviving findings is an explicit result: "no supported cross-repository
impact detected". Not silence, not an implication of safety.

---

## Feedback: only ever subtracting

A reviewer that repeats a rejected finding gets muted by its readers. So
findings get a stable identity — a fingerprint over category, normalised title,
and evidence locations at *path* granularity, because line numbers drift while
the finding stays the same — and dismissals are remembered.

Dismissals are read from the pull request thread, which is untrusted content.
So the rule is absolute: **anything read from repository content or a PR thread
can only reduce what Panorama says.** It can never add a finding, raise a
severity, or alter a citation. The blast radius of a forged dismissal is one
suppressed finding, and suppressions are listable and clearable.

Suppressed findings are counted in the report, exactly like validation
discards. Silence is always explained.

---

## Automation: a pull model, and why it has to be

The obvious design for automatic review is a hosted GitHub App with a webhook.
Panorama cannot build that, and the reason is structural rather than a matter of
effort.

A hosted service has no Claude Code subscription. The subscription is an
interactive credential belonging to a person and a machine; it cannot go in a
serverless function, and putting one there would break both "no API key" and
"credentials stay with the tool that owns them" at once.

So automation is a **local watcher that polls** for pull requests that are new
or whose head commit moved. No inbound network, no public URL, no runner
registration, and nothing to operate. It is dry-run by default: posting requires
an explicit flag *and* an explicit repository allowlist, because an autonomous
process that writes to GitHub by default is a mistake waiting for a bad night.

The honest cost: reviews lag by the poll interval, and only run while someone's
machine is up. That is a consequence of the subscription constraint, not
something that can be engineered away.

---

## How any of this is known to work

Quality is a **number**, measured on a labelled corpus of 18 pull requests
across six repositories and three languages, of which about a third are
negative controls — changes where the correct answer is to say nothing.

Retrieval scoring is deterministic, needs no subscription and no network, and
runs on every commit as a **regression gate**: a change that lowers recall
without a recorded reason does not land. The full pipeline, including the model,
is scored separately and opt-in, because it needs a real subscription and
because model wording varies between runs — outcomes are recorded, never
phrasing.

Where it stands. Retrieval puts the right repository in the top three for
**every** positive case and outright first for ten of twelve. Running the whole
corpus through the model three times each — 54 reviews — produced a
**false-positive rate of zero** on the negative controls: no docs change, test
addition, reformat, dependency bump or private rename produced a single
cross-repository claim. Evidence cited the right repository 85% of the time and
the right file 62%. Category accuracy is the weakest figure at 72%, and its
misses are largely disagreements about a genuinely fuzzy boundary rather than
wrong locations. Flake — the same case answered differently across runs — is
22%, which is why the live tier never runs a case only once.

`docs/corpus.md` describes what the corpus contains, what each case is for, and
what it deliberately does not cover.
