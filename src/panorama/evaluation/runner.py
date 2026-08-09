"""Run the corpus: drive each labelled case through the pipeline and score it.

Two entry points matching the two tiers. Both reuse the *production* pipeline —
the same intake, workspace, retrieval, review and validation code a real run
uses. An evaluation harness that reimplements any of that measures itself.

``run_offline`` stops after retrieval, so it never constructs a `ClaudeRunner`,
never spawns a process, and never touches the network. That is a property worth
protecting: it is what lets retrieval scoring be a per-commit gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from panorama.claude_runner import ClaudeRunner
from panorama.config import DEMO_ORG_ROOT
from panorama.errors import PanoramaError
from panorama.evaluation.cases import EvalCase
from panorama.evaluation.scoring import (
    RetrievalCaseScore,
    ReviewCaseScore,
    score_retrieval,
    score_review_run,
)
from panorama.intake import LocalPullRequestSource, PullRequest
from panorama.retrieval import RetrievalResult, retrieve
from panorama.review import run_review
from panorama.validation import validate_review
from panorama.workspace import Workspace


@dataclass
class CaseFailure:
    """A case that could not be run at all, as opposed to one that scored badly.

    Kept distinct from a low score on purpose: a bootstrap that never ran and a
    retrieval that found nothing produce very different numbers, and conflating
    them would let a broken environment read as a quality regression.
    """

    case_id: str
    message: str


def _load_case_pr(case: EvalCase, org_root: Path) -> tuple[PullRequest, Workspace]:
    repo_path = org_root / case.repo
    pull_request = LocalPullRequestSource(repo_path, case.base, case.head).load()
    return pull_request, Workspace(repo_path.resolve().parent)


def retrieve_for_case(
    case: EvalCase,
    org_root: Path = DEMO_ORG_ROOT,
    *,
    experimental: tuple[str, ...] = (),
) -> tuple[PullRequest, Workspace, RetrievalResult]:
    """Intake + retrieval for one case. No model, no network.

    No cache is passed, deliberately. A scored run that depends on state left
    behind by an earlier run is not a measurement — and the cache is required to
    change only *effort*, so measuring without it measures the same thing.
    """
    pull_request, workspace = _load_case_pr(case, Path(org_root))
    return (
        pull_request,
        workspace,
        retrieve(pull_request, workspace, experimental=experimental),
    )


def run_offline(
    cases: list[EvalCase],
    *,
    org_root: Path = DEMO_ORG_ROOT,
    experimental: tuple[str, ...] = (),
) -> tuple[list[RetrievalCaseScore], list[CaseFailure]]:
    """Score retrieval for every case. Deterministic; safe in CI.

    ``experimental`` turns on channels that are not enabled by default, so the
    claim "it did not help" is something anyone can reproduce rather than
    something they have to take on trust.
    """
    scores: list[RetrievalCaseScore] = []
    failures: list[CaseFailure] = []
    for case in cases:
        try:
            _, _, result = retrieve_for_case(case, org_root, experimental=experimental)
        except PanoramaError as exc:
            failures.append(CaseFailure(case.id, exc.message))
            continue
        scores.append(score_retrieval(case, result))
    return scores, failures


def run_live(
    cases: list[EvalCase],
    *,
    runs: int = 3,
    org_root: Path = DEMO_ORG_ROOT,
    runner_factory=None,
) -> tuple[list[ReviewCaseScore], list[CaseFailure]]:
    """Run the full pipeline ``runs`` times per case and score the reviews.

    ``runs`` defaults to 3 because a single run cannot distinguish a real
    quality change from model variance, and V1's S6 already observed a case
    whose findings differed between two passes. ``runner_factory`` exists so
    tests can inject a fake CLI; production passes nothing.
    """
    org_root = Path(org_root)
    scores: list[ReviewCaseScore] = []
    failures: list[CaseFailure] = []

    for case in cases:
        outcomes = []
        try:
            pull_request, workspace = _load_case_pr(case, org_root)
            retrieval = retrieve(pull_request, workspace)
            for _ in range(runs):
                runner = (
                    runner_factory()
                    if runner_factory is not None
                    else ClaudeRunner(allowed_workspace_roots=[workspace.root])
                )
                result = run_review(pull_request, workspace, retrieval, runner=runner)
                validated = validate_review(result.data, pull_request, workspace)
                outcomes.append(score_review_run(case, validated))
        except PanoramaError as exc:
            failures.append(CaseFailure(case.id, exc.message))
            continue
        scores.append(
            ReviewCaseScore(
                case_id=case.id, expect_finding=case.expect_finding, runs=outcomes
            )
        )

    return scores, failures
