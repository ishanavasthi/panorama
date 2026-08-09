"""Measured evaluation of Panorama's retrieval and review quality.

V1 was tuned against four fixture pull requests, run twice, scored by hand.
That is not enough evidence to make a quality decision on, and every V2
retrieval change is a quality decision. This package makes the question
numeric.

Two tiers, deliberately separated by what they cost to run:

- **Offline** scores *retrieval only*. It is fully deterministic, needs no
  Claude subscription and no network, and therefore runs on every commit as a
  regression gate rather than as an occasional report.
- **Live** runs the whole pipeline against the real subscription, several times
  per case, and scores review-level outcomes plus run-to-run flake. It is
  opt-in and never part of default CI.

The engine here contains **no fixture repository names, field names, or
expected findings** (hard constraint #2). All ground truth lives in checked-in
case files under ``evals/cases/``, outside the installed package.
"""

from panorama.evaluation.cases import EvalCase, load_cases
from panorama.evaluation.report import (
    baseline_object,
    compare_to_baseline,
    render_live_report,
    render_offline_report,
)
from panorama.evaluation.runner import run_live, run_offline
from panorama.evaluation.scoring import (
    RetrievalAggregate,
    RetrievalCaseScore,
    ReviewAggregate,
    ReviewCaseScore,
    aggregate_retrieval,
    aggregate_review,
    score_retrieval,
    score_review_run,
)

__all__ = [
    "EvalCase",
    "RetrievalAggregate",
    "RetrievalCaseScore",
    "ReviewAggregate",
    "ReviewCaseScore",
    "aggregate_retrieval",
    "aggregate_review",
    "baseline_object",
    "compare_to_baseline",
    "load_cases",
    "render_live_report",
    "render_offline_report",
    "run_live",
    "run_offline",
    "score_retrieval",
    "score_review_run",
]
