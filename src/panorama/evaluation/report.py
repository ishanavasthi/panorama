"""Rendering, baselines, and the regression comparison that gates a commit.

The baseline is a small JSON file (`evals/baseline.json`) holding the corpus
aggregates *and* the per-case outcome. Both halves matter: aggregates catch a
broad decline, and per-case outcomes catch the change that fixes two cases while
silently breaking a third — the kind a mean happily hides.

A baseline is only meaningful next to the corpus that produced it, so it records
the case ids it was measured over and the comparison refuses to declare
"no regression" when the corpus has changed underneath it.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from panorama.evaluation.scoring import (
    RECALL_K,
    RetrievalAggregate,
    RetrievalCaseScore,
    ReviewAggregate,
    ReviewCaseScore,
)

#: Floating-point slack when comparing aggregates, so a rounding wobble is not
#: reported as a regression.
_EPSILON = 1e-9

BASELINE_VERSION = 1


def baseline_object(
    aggregate: RetrievalAggregate, scores: list[RetrievalCaseScore]
) -> dict:
    """The serialisable baseline: aggregates plus per-case outcomes."""
    return {
        "version": BASELINE_VERSION,
        "recall_k": RECALL_K,
        "aggregate": asdict(aggregate),
        "cases": {
            s.case_id: {
                "scorable": s.scorable,
                "expect_finding": s.expect_finding,
                "rank": s.rank,
                "tied_with": s.tied_with,
                "recall_at_1": s.recall_at_1,
                "recall_at_1_outright": s.recall_at_1_outright,
                "recall_at_k": s.recall_at_k,
                "file_recall": s.file_recall,
                "passed": s.passed,
            }
            for s in scores
        },
    }


def load_baseline(path: Path) -> dict | None:
    """Read a baseline file, or ``None`` if there is not one yet."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def compare_to_baseline(
    baseline: dict, aggregate: RetrievalAggregate, scores: list[RetrievalCaseScore]
) -> tuple[list[str], list[str]]:
    """Compare a run to a baseline. Returns ``(regressions, notes)``.

    Regressions block; notes are informational (improvements, corpus growth).
    """
    regressions: list[str] = []
    notes: list[str] = []

    base_cases = baseline.get("cases") or {}
    base_agg = baseline.get("aggregate") or {}

    current_ids = {s.case_id for s in scores}
    baseline_ids = set(base_cases)

    added = sorted(current_ids - baseline_ids)
    removed = sorted(baseline_ids - current_ids)
    if added:
        notes.append(f"{len(added)} case(s) new since the baseline: {', '.join(added)}")
    if removed:
        # Not a regression in itself, but it invalidates aggregate comparison —
        # a corpus that lost its hard cases would otherwise look like progress.
        notes.append(
            f"{len(removed)} case(s) in the baseline are gone: {', '.join(removed)}"
        )

    # -- per-case: the check that catches a fix that breaks something else ---
    for score in scores:
        prior = base_cases.get(score.case_id)
        if not prior:
            continue
        if prior.get("passed") and not score.passed:
            regressions.append(
                f"{score.case_id}: passed at baseline, fails now "
                f"(rank {prior.get('rank')} -> {score.rank})"
            )
            continue
        if prior.get("recall_at_1") and not score.recall_at_1:
            regressions.append(
                f"{score.case_id}: target lost rank 1 (now rank {score.rank})"
            )
        prior_rank, rank = prior.get("rank"), score.rank
        if isinstance(prior_rank, int) and isinstance(rank, int) and rank > prior_rank:
            notes.append(f"{score.case_id}: rank worsened {prior_rank} -> {rank}")
        prior_files, files = prior.get("file_recall"), score.file_recall
        if (
            isinstance(prior_files, (int, float))
            and isinstance(files, (int, float))
            and files + _EPSILON < prior_files
        ):
            regressions.append(
                f"{score.case_id}: file recall fell {prior_files:.2f} -> {files:.2f}"
            )

    # -- aggregate, only meaningful over a comparable corpus -----------------
    if removed:
        notes.append("aggregate comparison skipped: the corpus changed")
    else:
        for label, key, value in (
            ("recall@1", "recall_at_1", aggregate.recall_at_1),
            (
                "outright recall@1",
                "recall_at_1_outright",
                aggregate.recall_at_1_outright,
            ),
            (f"recall@{RECALL_K}", "recall_at_k", aggregate.recall_at_k),
            ("MRR", "mrr", aggregate.mrr),
        ):
            prior = base_agg.get(key)
            if not isinstance(prior, (int, float)):
                continue
            if value + _EPSILON < prior:
                regressions.append(f"{label} fell {prior:.3f} -> {value:.3f}")
            elif value > prior + _EPSILON:
                notes.append(f"{label} improved {prior:.3f} -> {value:.3f}")

    if aggregate.n_forbidden_violations > int(
        base_agg.get("n_forbidden_violations", 0) or 0
    ):
        regressions.append(
            "a negative case surfaced a repository it must not "
            f"({aggregate.n_forbidden_violations} violation(s))"
        )

    return regressions, notes


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _fmt_rank(score: RetrievalCaseScore) -> str:
    if not score.scorable:
        return "—"
    if not score.expect_finding:
        return "n/a"
    if score.rank is None:
        return "unranked"
    return f"{score.rank}" + (f" (tie×{score.tied_with})" if score.tied_with > 1 else "")


def render_offline_report(
    scores: list[RetrievalCaseScore],
    aggregate: RetrievalAggregate,
    *,
    regressions: list[str] | None = None,
    notes: list[str] | None = None,
    failures: list | None = None,
) -> str:
    """Human-readable retrieval report."""
    out: list[str] = ["# Retrieval evaluation", ""]

    width = max((len(s.case_id) for s in scores), default=4)
    out.append(f"{'case'.ljust(width)}  {'rank':>12}  {'files':>6}  {'hits':>5}  result")
    out.append(f"{'-' * width}  {'-' * 12}  {'-' * 6}  {'-' * 5}  ------")
    for score in scores:
        files = "—" if score.file_recall is None else f"{score.file_recall:.0%}"
        if not score.scorable:
            result = "not scored"
        elif score.passed:
            result = "pass"
        else:
            result = "FAIL"
        out.append(
            f"{score.case_id.ljust(width)}  {_fmt_rank(score):>12}  "
            f"{files:>6}  {score.total_hits:>5}  {result}"
        )

    out += [
        "",
        f"Cases: {aggregate.n_cases} "
        f"({aggregate.n_positive} positive, {aggregate.n_unscorable} not retrieval-scorable)",
        f"recall@1: {aggregate.recall_at_1:.3f}"
        f"  (outright, ties excluded: {aggregate.recall_at_1_outright:.3f})",
        f"recall@{RECALL_K}: {aggregate.recall_at_k:.3f}",
        f"MRR: {aggregate.mrr:.3f}",
    ]
    if aggregate.file_recall is not None:
        out.append(f"file recall: {aggregate.file_recall:.3f}")
    if aggregate.n_forbidden_violations:
        out.append(f"forbidden-repo violations: {aggregate.n_forbidden_violations}")

    if aggregate.n_unscorable:
        out += [
            "",
            f"{aggregate.n_unscorable} case(s) assert nothing about retrieval and are "
            "scored only by `--live`. Offline numbers do not cover them.",
        ]

    if failures:
        out += ["", "## Could not run"]
        out += [f"- {f.case_id}: {f.message}" for f in failures]
    if notes:
        out += ["", "## Notes"] + [f"- {n}" for n in notes]
    if regressions:
        out += ["", "## Regressions"] + [f"- {r}" for r in regressions]

    out.append("")
    return "\n".join(out)


def render_live_report(
    scores: list[ReviewCaseScore],
    aggregate: ReviewAggregate,
    *,
    failures: list | None = None,
) -> str:
    """Human-readable review report. Records outcomes, never model wording."""
    out: list[str] = ["# Review evaluation (live)", ""]

    width = max((len(s.case_id) for s in scores), default=4)
    out.append(f"{'case'.ljust(width)}  {'kind':>8}  {'pass':>6}  {'flaky':>5}  runs")
    out.append(f"{'-' * width}  {'-' * 8}  {'-' * 6}  {'-' * 5}  ----")
    for score in scores:
        kind = "positive" if score.expect_finding else "negative"
        out.append(
            f"{score.case_id.ljust(width)}  {kind:>8}  {score.pass_rate:>6.0%}  "
            f"{'yes' if score.flaky else 'no':>5}  {len(score.runs)}"
        )

    out += [
        "",
        f"Cases: {aggregate.n_cases} over {aggregate.n_runs} runs",
        f"category accuracy: {aggregate.category_accuracy:.3f}",
        f"evidence-repo accuracy: {aggregate.evidence_repo_accuracy:.3f}",
    ]
    if aggregate.evidence_file_accuracy is not None:
        out.append(f"evidence-file accuracy: {aggregate.evidence_file_accuracy:.3f}")
    out += [
        f"false-positive rate (negatives): {aggregate.false_positive_rate:.3f}",
        f"mean discarded findings per run: {aggregate.mean_discards:.2f}",
        f"flake rate: {aggregate.flake_rate:.3f}",
    ]

    if failures:
        out += ["", "## Could not run"]
        out += [f"- {f.case_id}: {f.message}" for f in failures]

    out.append("")
    return "\n".join(out)
