# Backlog — known open items

Everything currently known to be wrong, weak, or deliberately unfinished, with a
stable id so it can be picked up by name. Each entry says what the evidence is,
what has already been tried, and what fixing it would actually involve.

`CHECKLIST.md` tracks whether a *milestone* is done. This tracks what is *open*
regardless of milestone. Nothing here is a surprise — every item is already
recorded in `DECISIONS.md` or the README's limitations; this file just puts them
in one place and gives them names.

Status: `open` · `accepted` (understood, not worth fixing) · `done`

**Start at section F.** It is the newest evidence and the only section written
from Panorama running against real pull requests rather than the fixture corpus.
It changes what the most important open problem is.

---

## A. Retrieval quality

> **The offline corpus is saturated again.** With A3 closed, every retrieval
> metric reads 1.000 — recall@1, outright recall@1, recall@3, MRR and file
> recall — across all twelve positive cases, with zero leads on every negative
> control. That is the same position V2.1 found itself in on the four inherited
> cases, and it means the same thing: **the offline harness can now only detect
> regressions, not improvements.** It remains a gate worth keeping and is no
> longer evidence for any further retrieval change. Anything after this needs a
> larger or harder corpus, or the live tier, before it can claim to have helped.
>
> **`F6` is the stronger version of this note.** Saturation says the offline
> tier can no longer detect an *improvement*. F6 says neither tier can detect an
> *omission*, because every case is labelled with exactly one target.

### A1 — Two contract-break cases rank #2 instead of #1 · `done`

Fixed by **A3**, exactly as predicted, and without touching fusion at all.

`contract-break-removed-endpoint` and `contract-break-status-code` both now rank
their labelled target **outright first**. Corpus-wide: recall@1 0.833 →
**1.000**, outright recall@1 0.833 → **1.000**, MRR 0.917 → **1.000**.

**Why it was open.** In both cases the conventions repository was the lexical
runner-up *and* a declared dependency of the pull request's repository, so it
collected a vote from each of two channels while the true target collected one.
Reciprocal rank fusion discards magnitude, so it could not see that in one case
the true target led lexically by 18.0 to 4.0.

**Why the fix is not a tuning hack.** Nothing about fusion changed — the
constant is still 60 and still untuned. The two targets each gained a *third*
channel's vote because a fourth channel now sees the coupling they always had.
The conventions repository gained nothing, because it has no source code and
therefore speaks no HTTP. The tie broke because a real signal was added, not
because a number was leaned on.

**What was tried and rejected before.** Sweeping the fusion constant: every
value ≥ 1 gives byte-identical results, and 0 turns the two losses into *ties*
(a perfect recall@1 describing a ranking that discriminates less — the reason
the ties-excluded metric exists). Max-fusion instead of sum fixes these two and
immediately un-fixes the convention case V2.4 existed for. Both are recorded in
`DECISIONS.md` under V2.7.

---

### A2 — Cross-language duplicate detection barely works · `open`

`duplicate-logic-date-formatting` is the corpus's naming-boundary case: the same
idea spelled `format_timestamp` in Python and `formatTimestamp` in TypeScript.
Those are different strings, and neither the lexical channel nor the symbol
index will ever match them. The case is winnable today only because a shared
constant (`TIMESTAMP_FORMAT`) is spelled identically in both languages.

Live, this case scores **0.00** on category — though it cites the right
repository and file on all three runs (see **B1**).

**What a fix needs.** Identifier normalisation across naming conventions —
splitting on case and underscore boundaries into token sets, then matching
those. Cheap to try, and the harness would say immediately whether it helps.
The risk is false positives: normalised identifiers collide far more often, and
this project's whole posture is that a confident wrong finding is worse than a
missed one. Needs a measured decision, not a hunch.

---

### A3 — HTTP-contract couplings are invisible to both structural channels · `done`

Closed by a fourth live channel, `httpcontract`. It extracts the tokens that
travel over the wire — route segments, payload field names, payload values,
status codes — from the diff, and matches them against the same tokens in
siblings that demonstrably **speak HTTP**: make a call, serve a route, or decode
a response body.

Two filters are what stop it collapsing back into the lexical channel:

- **Only string-literal and wire positions count.** An identifier spelled the
  same is not a match.
- **Only repositories that speak HTTP are eligible.** Defining an error-code
  constant is not consuming a contract; the coupling belongs to whoever sends or
  reads it. A documentation repository has no source and is never eligible.

That second filter is the whole mechanism: on the corpus it is exactly what
separates the gateway that *reads* an error code from the shared library that
*defines* it and the conventions document that *describes* it.

**Measured:** recall@1 0.833 → **1.000**, outright recall@1 0.833 → **1.000**,
MRR 0.917 → **1.000**, recall@3 and file recall held at 1.000. No case
regressed; all four negative controls still surface zero leads. Fixes **A1**.

**One honest limit, now recorded as `A4`:** on this corpus the channel changes
*ranking only* and contributes no new file-level leads, because every line it
points at the lexical pass had already found.

---

### A4 — The HTTP channel adds no leads the lexical pass had not found · `open`

Every hit the new channel produces on the corpus is deduplicated away as
something the lexical pass already surfaced. Its measured value is entirely in
the *ranking*, which is real and is what A1 needed — but the claim "it shows the
model places it would not otherwise have seen" is **not** supported by any
measurement.

**Why that is expected here.** The corpus is small and its wire tokens are
distinctive, so anything matching as a route or a field also matches as a bare
string. The channel should separate from lexical exactly where lexical is
weakest: a field name that is a common English word, a route segment that
appears as an ordinary identifier in a dozen places.

**Blocked on the same thing as C2:** a real organisation. Worth checking there
before claiming lead-level value anywhere user-facing.

---

## B. Review quality (model-side)

### B1 — Category accuracy 0.718, and most misses are disagreements · `open`

The weakest live number. Two cases score 0.00 on category —
`contract-break-removed-enum-member` and `duplicate-logic-date-formatting` — but
both cite the **right repository and the right file** on 5 of 6 runs. They
simply choose a different category, and the choice is defensible: adding a UTC
formatter genuinely touches the timestamps *convention* as well as duplicating a
helper.

**What a fix needs.** Deciding whether this is a *model* problem or a *ground
truth* problem. Options: sharpen the category definitions in the prompt with
tie-break rules ("if it both duplicates and violates a convention, report the
duplication"); allow a case to accept more than one category; or score
"cited the right place" separately from "chose the right label" as the headline.
**Do not** relabel the two cases to make the number go up.

**B3's `k = 10` re-measurement is direct evidence for the third option.** On
that case the model cites the right repository on 6 of 10 runs and matches the
labelled category on 4 — so "found the right place" and "chose the right label"
are measurably different numbers on a case where nobody disputes the location.
Reporting them as one figure hides which of the two is actually failing.

---

### B2 — Flake rate 22.2% · `accepted, worth reducing`

Four of eighteen cases answered differently across three runs. Host validation
bounds *correctness*, not *consistency*. This is why the live tier runs each
case three times and why no quality claim rests on a single run.

Partly irreducible — the model is not deterministic. Could plausibly be reduced
by tightening the prompt's rubric on severity and category. Would need k>3 to
measure a change with any confidence.

---

### B3 — `contract-break-status-code` passes 1 run in 3 · `open, retrieval half fixed`

The weakest single case, live and offline.

**Offline: fixed by A3.** It was the joint-weakest retrieval case, at rank 2.
The HTTP channel now ranks the Go gateway — the repository that actually reads
the removed error code — while leaving the shared library that merely defines it
and the conventions document that describes it unranked. It is **outright rank
1**, and the right repository is now what the model is handed first.

**Live: re-measured at `k = 5` and confirmed at `k = 10`. It did *not* move.**

| | before A3 | after, `k=5` | after, `k=10` |
|---|---:|---:|---:|
| pass rate | 1 of 3 (0.33) | 2 of 5 (0.40) | **4 of 10 (0.40)** |
| category accuracy | — | 0.400 | **0.400** |
| evidence cites the right repo | — | 0.400 | **0.600** |
| evidence cites the right file | — | 0.400 | **0.600** |

The two independent post-A3 measurements agree at 0.40, so that figure is solid.
Whether it is *better* than the 0.33 it replaced is unanswerable: the earlier
number came from three runs, and 1-of-3 cannot be distinguished from 4-of-10.
**Treat it as unchanged.**

**What the `k = 10` numbers decompose into**, and this is the useful part. The
model cites the right repository on **6 of 10** runs but matches the labelled
category on only **4**. So roughly:

- **4 runs** miss the gateway entirely — even though retrieval now hands it over
  ranked outright first;
- **2 runs** find the right place and call it something other than
  `contract_break` — the same disagreement **B1** documents;
- **4 runs** pass.

One finding across the ten was discarded by host validation (0.10 per run).

**Conclusion: A3 fixed the retrieval half and left the review half untouched.**
The two tiers now disagree about this case, which is the most useful thing the
measurement produced — it localises what remains to the prompt's category and
severity rubric, and it means the offline gate can no longer stand in for
whether this case works.

**Still open, and now well specified.** The lever is the prompt, not retrieval,
and it overlaps **B1** almost exactly. Anything claiming to move it needs
`k ≥ 10`: at 0.40 with this flake rate, `k = 3` would report anything between
0 and 3 passes without the underlying behaviour changing at all.

---

## C. Features deliberately not built

### C1 — Inline review comments · `deferred, by plan`

Dropped in V2.10 per `v2plan.md`'s own drop order. A summary comment is
idempotent because one marked comment can be edited in place; a review with
inline comments is not — the API creates a new review each time. Doing it safely
means tracking which fingerprints were posted inline on which run: persistent
state in the code path that writes to someone else's pull request, in exchange
for better *placement* of findings already being delivered.

**If picked up:** post inline only for fingerprints new since the last run,
leave prior inline comments alone, keep the summary comment canonical. Needs the
`pr_line` → diff-position mapping and a live two-run check.

---

### C2 — Co-change channel is unmeasured · `open, blocked on data`

Built, tested, and shipped **disabled**. On the fixture corpus it abstains
entirely — every repository has one commit, so the thin-history guard excludes
all of them. Verify with `panorama eval --experimental cochange`.

**Blocked on:** a real organisation with genuine history to measure against.
Turning it on without that would be a quality claim nobody has checked.

---

### C3 — Suppression's live two-run check · `open`

V2.10's live exit criterion: two runs on a changing pull request, confirming a
dismissed finding stays dismissed. The seeded organisation now exists, so this
is straightforward — reply `panorama: dismiss <ref>` on a live PR, re-review,
confirm the finding is withheld and *counted*.

---

## D. Robustness

### D1 — Selection can silently drop a relevant repository · `open`

On organisations larger than the clone budget, a sibling whose coupling nothing
declares is never cloned, so it is never searched. Demonstrated on the corpus:
squeeze the budget and the field-rename consumer is dropped, because its
coupling is a *mirrored response type* that no manifest records.

**Bounded, not fixed:** the counts are printed on every run, `--all-repos`
undoes it, and the cached symbol index makes the second review better targeted
than the first.

**A3 does not fix this, contrary to what this entry used to predict.** The HTTP
channel finds undeclared couplings at *retrieval* time, but selection chooses
what to clone **before** any sibling source exists to read. The channel needs a
checkout; selection runs without one. On a cold cache nothing has changed, and
the guarantee is still the honest one: the loss is reported, and `--all-repos`
recovers it.

**The available next step is `D4`.**

---

### D4 — Selection ignores the cached HTTP surface · `open`

Selection already consults a **previous run's symbol index** to keep a
repository whose coupling no manifest declares. The HTTP surface is cached the
same way, keyed the same way, and is precisely a record of undeclared coupling —
but selection does not read it.

Wiring it in would make the second review of an organisation resistant to the
exact failure D1 describes, for repositories whose only link is the wire. It was
deliberately not done alongside A3: selection has its own documented guarantee
and its own tests, and changing what gets cloned is a larger blast radius than
adding a channel that only re-ranks what is already there.

**Not blocked on anything.** Needs the selection tests extended with an
HTTP-coupled repository that a tight budget would otherwise drop.

---

### D2 — Symbol extraction is regex, not a parser · `accepted`

A comment delimiter inside a string literal blinds the rest of the file. Costs
some recall, and the loss is safe in the direction that matters: a symbol not
indexed is a lead not offered, never a citation invented.

Tree-sitter becomes justified only if measurement shows extraction recall is the
binding constraint — a measured upgrade, not a speculative one. It is still not
the binding constraint. With A3 closed and offline retrieval saturated, there is
no longer a *measurable* binding constraint at all on this corpus, which is
itself the finding: see the note on the corpus at the top of section A.

---

### D3 — A reworded finding title is a new fingerprint · `accepted`

Normalisation covers case, punctuation and whitespace but not word changes, so a
model that drops "the" between runs produces a new identity and an existing
dismissal stops applying — the finding must be dismissed again.

Deliberate. Stripping common words would collapse genuinely different findings
into one identity, and **silencing a real finding is far worse than repeating a
dismissed one**. Revisit only with evidence that re-dismissal is a real
annoyance in practice.

---

## E. Accepted constraints — not defects

Recorded so they are not re-litigated.

- **Reviews lag by the poll interval and only run while your machine is up.**
  A consequence of the subscription-only constraint: a hosted service has no
  Claude Code subscription. Not fixable without breaking a hard constraint.
- **Absence of a finding is not proof of safety.** Panorama is a reviewer's
  assistant, not a merge gate.
- **The model reads private source.** Mitigated by read-only sandboxing,
  owner-only storage, prompt-injection treatment and reference-only output —
  not eliminated.
- **Two of eighteen cases are not scorable offline** (the docs-only control and
  the single-repository defect). Both make claims about the *review*, not about
  retrieval. The report says so on every run.

---

## F. Breadth — what a real-organisation evaluation found

> **Where this evidence came from.** An external reviewer ran Panorama, four
> other submissions and their own in-house reviewer against the **same six real
> pull requests** from a production organisation — a Go backend, a Next.js
> frontend, a Python orchestrator — over twenty-plus runs, then independently
> verified every citation by hand: does the file exist, does the line exist, is
> the claim true. This is the first evidence about Panorama that does not come
> from the fixture corpus, and it disagrees with the corpus.
>
> **What held.** Zero fabricated claims across six pull requests. Every citation
> resolved. The stated security controls were true under adversarial test. On
> the hardest pull request Panorama contradicted the PR description's own
> justification and supported it with twelve references that all resolved, and
> it found a HIGH-severity gap — a build-path gate still calling the old
> unconditional trigger — that the evaluator had missed twice while building
> their own ground truth.
>
> **What did not.** **Three findings across six pull requests.** The broadest
> submission produced thirty-three, with every citation resolving, and caught
> two real defects Panorama did not. The verdict was "precise and narrow"; the
> best reviewer in the set was broad.
>
> Every item below pushes toward breadth, and breadth is exactly the direction
> that produced the other submissions' failure modes. `F7` is the gate on all of
> them.

### F1 — Three findings across six real pull requests · `open`

The headline defect, and the only axis where Panorama measurably lost.

**Not caused by** the things worth ruling out first: there is no cap on the
number of findings (`models.py:97` — "zero or more"), and host validation is not
eating them (B3's `k = 10` run discarded 0.10 findings per run). The ceiling is
behavioural, and four things build it, in descending order of how much of the
gap I think each explains:

1. **The job is defined as cross-repository only.** `prompts.py` opens with
   "find impact that is only visible when you look beyond the repository the
   pull request lives in". Anything true and serious *inside* the diff is out of
   scope by the first sentence.
2. **Restraint is explicitly ranked above coverage.** The rubric's own heading
   reads `RESTRAINT -- this matters more than coverage`. It is doing precisely
   what it was told, and that instruction bought the zero-fabrication result.
3. **One pass per pull request.** `run_review` makes a single `claude -p` call
   with one finite attention budget over the whole diff (see `F2`).
4. **The corpus taught it.** Every labelled case has exactly one target, so a
   reviewer emitting one correct finding per pull request scores perfectly
   (see `F6`).

**What a fix needs.** Yield raised by *structure* — more passes, more aimed
signals, a wider category surface — never by weakening the restraint clause.
The sentence "a plausible-looking guess is worse than no finding" is load-
bearing and stays. Unmeasurable until `F6` moves.

**Also contributing on a large organisation, and worth checking separately:**
`D1` (a relevant repository never cloned), and the prompt's own size caps —
`MAX_DIFF_CHARS = 60_000` and `_MAX_LEADS_IN_PROMPT = 30`. Both were sized
against six fixture repositories and small diffs. Neither has been measured
against a real pull request.

---

### F2 — The review stops at the first strong theme · `open`

On the orchestrator pull request Panorama found the HIGH-severity functional gap
the evaluator themselves had missed — and in the *same diff* missed a cross-repo
contract drift another submission caught and a critical security bypass the
in-house reviewer caught.

**That is not a retrieval failure.** The evidence for all three was in the same
workspace, in the same pass, for the same diff. Finding the hardest one and
missing two easier ones is the signature of a single pass spending its budget
proving one mechanism to the depth step 3 of the rubric demands.

**What a fix needs.** A second pass over the signals *not* covered by any
finding the first pass emitted: same diff, same workspace, plus the findings
already reported, asked only what else is there. Costs one additional
subscription call per review. The coverage gain has to be measured rather than
assumed — and measuring it needs `F6`.

---

### F3 — Consumers outside the organisation are invisible · `open`

A route move left every unsubscribe link in **already-delivered mail**
permanently dead. Real, and missed.

No repository contains those URLs. The consumer is an artifact already emitted
into the world, and all five channels — lexical, dependency, symbol, co-change,
HTTP — search sibling *repositories*. There is nothing for them to match.

**This is a category gap, not a retrieval gap.** Removing or moving a
publicly-issued route is a break whether or not any sibling references it. The
same shape covers emitted links, webhook URLs handed to third parties, QR codes,
deep links in shipped mobile builds, and anything already cached downstream.

**What a fix needs.** A diff-side rule, not a search: a removed or moved public
route is worth a finding *on its own terms*, evidenced by the diff itself, with
severity set by whether the old address still resolves. The discriminator that
keeps this from becoming "flag every route change" is checkable in the diff —
whether the same change adds a redirect, alias or compatibility route for the
old path. Note the consequence for host validation: such a finding has no
outside evidence by its nature, so it is `single_repo`, which the cross-repo
evidence rule permits and the current prompt discourages.

---

### F4 — A test that does not test what it names is not looked for · `open`

A test named `…ResolvesTheInstructorPhoto` never calls the function it claims to
test. Real, missed, and the evaluator's to fix.

**The mechanism is explicit, not emergent.** The restraint clause instructs that
a change touching only "documentation, comments, formatting or tests ... should
produce no findings at all". Panorama is told, in those words, to ignore this.

**The class is larger than tests:** claims inside the diff that the diff itself
contradicts — a test name against its body, a docstring against the signature,
a comment against the branch it sits above, an error message naming a field the
code no longer reads. None of these need a sibling repository, retrieval, or a
second checkout. They are decidable from the diff alone, which makes them the
cheapest breadth available and the least likely to fabricate.

**What a fix needs.** Narrow the carve-out from "tests produce no findings" to
"*mechanical* test changes produce no findings", and add self-consistency to the
step-1 signal list. Prompt-only, no schema change, no new channel.

---

### F5 — No security lens · `open`

The in-house reviewer found a critical security bypass on the orchestrator pull
request; Panorama did not. Security is not one of the five categories, not a
retrieval channel, and appears in the rubric only insofar as severity is set by
consequence.

**Do not conflate this with M0.** Panorama's own boundary — what the model may
touch — is the most thoroughly tested claim in the submission and it held. None
of that is about the security of the *code under review*.

**The capability is present but unaimed:** the HIGH finding Panorama did produce
here — a gate still calling an old unconditional trigger — is adjacent to this
class. It was found as a functional gap, not as a security one.

**What a fix needs.** Aim it: add security-shaped signals to the step-1 list —
an authorization branch made unconditional, a validation or authentication call
dropped from a path that still executes, a check moved to a caller that does not
always run. Prompt-first, since it needs no schema change; a distinct category
only if measurement shows the label is what is missing. **Not** a second LLM
verification pass — that is explicitly deferred and remains so.

---

### F6 — The corpus cannot see a miss · `open, blocks F1–F5`

Every case in `evals/cases/` is labelled with exactly **one** target. The
harness asks "was the labelled thing found, and how highly ranked" — never "what
else was in this diff that a competent reviewer should have found". A reviewer
that emits precisely one correct finding per pull request scores perfectly on
every metric this project has.

That is the number the whole of V2 was tuned against, and it is the number the
external evaluation contradicted. Precision has a gate; **yield has never been
measured at all**.

**What a fix needs.** Cases annotated with every finding that should be
produced, so omissions are scorable. That means **real pull requests with
independently verified ground truth**, not more fixtures — the same blocker as
`A4` and `C2`. The cheapest honest version: score a small set of real pull
requests by hand once, freeze it, and treat it as the reference set for `F1`–`F5`.

**Do not** invent multi-label fixture cases to fill the gap. A fixture author
writing both the diff and the list of things it should surface is measuring the
fixture author.

---

### F7 — Precision is the asset F1–F5 put at risk · `accepted, a gate on F1–F5`

Recorded so it is not traded away by accident.

Under six real pull requests, twenty-plus runs and hand-verification of every
citation: **zero fabricated claims, every reference resolved**, and every stated
control true under test. That last point separated Panorama from the field —
another submission shipped `--tools Read,Grep,Glob,Bash` narrowed by an
`--allowedTools` allowlist believing it enforced, and arbitrary commands ran
through it with no permission denial. M0 exists because that assumption was
tested rather than trusted.

**The gate.** Any change made for breadth is measured for fabrication and dead
references on the same set before it lands. A breadth gain paid for with one
unresolvable citation is a loss, not a trade. Discard-never-downgrade,
reference-only output, and "a plausible-looking guess is worse than no finding"
are not on the table.
