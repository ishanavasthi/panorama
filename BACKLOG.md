# Backlog — known open items

Everything currently known to be wrong, weak, or deliberately unfinished, with a
stable id so it can be picked up by name. Each entry says what the evidence is,
what has already been tried, and what fixing it would actually involve.

`CHECKLIST.md` tracks whether a *milestone* is done. This tracks what is *open*
regardless of milestone. Nothing here is a surprise — every item is already
recorded in `DECISIONS.md` or the README's limitations; this file just puts them
in one place and gives them names.

Status: `open` · `accepted` (understood, not worth fixing) · `done`

---

## A. Retrieval quality

### A1 — Two contract-break cases rank #2 instead of #1 · `open`

`contract-break-removed-endpoint` and `contract-break-status-code` both put
their labelled target at rank 2. Measured: recall@1 **0.833**, recall@3 1.000.

**Why.** In both, the conventions repository is the lexical runner-up *and* a
declared dependency of the pull request's repository, so it collects a vote from
each of two channels while the true target collects one. Reciprocal rank fusion
discards magnitude, so it cannot see that in one case the true target led
lexically by 18.0 to 4.0 and in the other by almost nothing — **both look
identical to it**.

**Already tried.** The fusion constant was swept across the whole corpus:
every value ≥ 1 gives byte-identical results, and 0 turns the two losses into
*ties* (a perfect recall@1 describing a ranking that discriminates less — the
reason the ties-excluded metric exists). Max-fusion instead of sum fixes these
two and immediately un-fixes the convention case V2.4 existed for.

**What a real fix needs.** A way to express channel *confidence*, not just
order. Every such mechanism needs a parameter, and the only data available to
fit it is the corpus it would then be scored against. Options worth considering:
a channel declaring its own rank *gaps* rather than dense ranks; or accepting a
small number of principled, documented weights and validating them on a held-out
split of a larger corpus. Related to **A3**.

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

### A3 — HTTP-contract couplings are invisible to both structural channels · `open`

A Python client and a Go gateway can each depend entirely on a TypeScript
service and appear in no manifest and no import statement. Roughly **a third of
the corpus** is that shape, and it is the root cause underneath A1: for those
cases only the lexical channel speaks, so there is nothing to break the tie.

**What a fix needs.** Read route strings and response field names as a
first-class signal — an HTTP-contract channel: extract route literals and JSON
field names from the diff, match them against string literals and struct/type
tags in siblings. This is the largest remaining piece of headroom in retrieval
and is a *new channel*, not a tuning pass.

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

---

### B2 — Flake rate 22.2% · `accepted, worth reducing`

Four of eighteen cases answered differently across three runs. Host validation
bounds *correctness*, not *consistency*. This is why the live tier runs each
case three times and why no quality claim rests on a single run.

Partly irreducible — the model is not deterministic. Could plausibly be reduced
by tightening the prompt's rubric on severity and category. Would need k>3 to
measure a change with any confidence.

---

### B3 — `contract-break-status-code` passes 1 run in 3 · `open`

The weakest single case, live and offline. Both structural channels are silent
(it is an A3 case) and the lexical signal is genuinely ambiguous: the removed
error code string appears in the consumer, the shared library *and* the
conventions document. Fixed by A3, most likely — worth re-measuring after,
rather than attacking directly.

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
than the first. A real fix is A3 — a coupling signal that does not need a
declaration.

---

### D2 — Symbol extraction is regex, not a parser · `accepted`

A comment delimiter inside a string literal blinds the rest of the file. Costs
some recall, and the loss is safe in the direction that matters: a symbol not
indexed is a lead not offered, never a citation invented.

Tree-sitter becomes justified only if measurement shows extraction recall is the
binding constraint — a measured upgrade, not a speculative one. It is not
currently the binding constraint; A3 is.

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
