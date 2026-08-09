"""Scoring: turn a retrieval result or a review into comparable numbers.

Two scorers, matching the two evaluation tiers.

**Retrieval scoring** asks whether the deterministic pass put the right sibling
repository in front of the model. Deterministic, so it is a CI gate.

**Review scoring** asks whether the finished review said the right thing. It
needs a real subscription, so it is opt-in.

One decision worth reading before trusting any number this produces:

*Ties are ranked by competition ranking, not list position.* The ranked-repo
list is sorted by score and then alphabetically, so a repo that merely *ties*
for the top score could land at index 0 purely because of its name. Counting
that as recall@1 would measure the alphabet. Competition ranking gives every
member of a tie the same rank — ``1 + the number of repos scoring strictly
higher`` — so a three-way tie for first is rank 1 for all three and honestly
reports that retrieval did not discriminate. This matters concretely: V1's S4
already recorded a case where the correct neighbour ties rather than leads.

This module contains no fixture names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from panorama.evaluation.cases import EvalCase, split_file_ref
from panorama.retrieval import RepoRelevance, RetrievalResult
from panorama.validation import ValidatedReview

#: How deep "did retrieval surface it" is scored at.
RECALL_K = 3


def competition_rank(ranked: list[RepoRelevance], repo: str) -> int | None:
    """1-based rank of ``repo``, ties sharing a rank. ``None`` if unranked."""
    entry = next((r for r in ranked if r.repo == repo), None)
    if entry is None:
        return None
    return 1 + sum(1 for other in ranked if other.score > entry.score)


def tie_width(ranked: list[RepoRelevance], repo: str) -> int:
    """How many repositories share ``repo``'s score. 1 means an outright rank."""
    entry = next((r for r in ranked if r.repo == repo), None)
    if entry is None:
        return 0
    return sum(1 for other in ranked if other.score == entry.score)


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalCaseScore:
    """What offline scoring can say about one case."""

    case_id: str
    scorable: bool
    expect_finding: bool = True
    unscorable_reason: str = ""

    rank: int | None = None
    tied_with: int = 0
    recall_at_1: bool = False
    recall_at_k: bool = False
    reciprocal_rank: float = 0.0

    file_recall: float | None = None
    matched_files: list[str] = field(default_factory=list)
    missed_files: list[str] = field(default_factory=list)

    total_hits: int = 0
    forbidden_surfaced: list[str] = field(default_factory=list)
    no_hits_ok: bool | None = None

    @property
    def recall_at_1_outright(self) -> bool:
        """Rank 1 *and* not tied with anything else.

        Plain recall@1 credits a three-way tie for first, because under
        competition ranking nothing scored higher — which is correct but
        flattering. Retrieval that cannot separate the true consumer from two
        bystanders has not really put it in front of the model. Tracking the
        outright version alongside gives improvements somewhere to show up once
        plain recall@1 saturates.
        """
        return self.rank == 1 and self.tied_with == 1

    @property
    def passed(self) -> bool:
        """A single pass/fail per case, for the at-a-glance table.

        Positive: the target repo made the top K. Negative: nothing forbidden
        surfaced and the no-hits expectation, if any, held.
        """
        if not self.scorable:
            return True  # nothing asserted, so nothing failed
        if self.expect_finding:
            return self.recall_at_k
        return not self.forbidden_surfaced and self.no_hits_ok is not False


def score_retrieval(case: EvalCase, result: RetrievalResult) -> RetrievalCaseScore:
    """Score one case's retrieval result against its labels."""
    if not case.retrieval_scorable:
        return RetrievalCaseScore(
            case_id=case.id,
            scorable=False,
            expect_finding=case.expect_finding,
            unscorable_reason=(
                "negative case with no retrieval expectation; scored live only"
            ),
            total_hits=len(result.hits),
        )

    ranked = result.ranked_repos
    top_k = {r.repo for r in ranked if (competition_rank(ranked, r.repo) or 99) <= RECALL_K}

    # -- negative case: assert what must NOT be there ------------------------
    if not case.expect_finding:
        forbidden = sorted(set(case.forbid_repos) & top_k)
        no_hits_ok = (len(result.hits) == 0) if case.expect_no_hits else None
        return RetrievalCaseScore(
            case_id=case.id,
            scorable=True,
            expect_finding=False,
            total_hits=len(result.hits),
            forbidden_surfaced=forbidden,
            no_hits_ok=no_hits_ok,
        )

    # -- positive case: find the best-ranked target --------------------------
    ranks = [
        (competition_rank(ranked, repo), repo)
        for repo in case.target_repos
        if competition_rank(ranked, repo) is not None
    ]
    best_rank, best_repo = min(ranks, default=(None, ""))

    hit_locations = {(hit.repo, hit.path) for hit in result.hits}
    matched, missed = [], []
    for ref in case.target_files:
        (matched if split_file_ref(ref) in hit_locations else missed).append(ref)
    file_recall = (
        len(matched) / len(case.target_files) if case.target_files else None
    )

    return RetrievalCaseScore(
        case_id=case.id,
        scorable=True,
        expect_finding=True,
        rank=best_rank,
        tied_with=tie_width(ranked, best_repo) if best_repo else 0,
        recall_at_1=best_rank == 1,
        recall_at_k=best_rank is not None and best_rank <= RECALL_K,
        reciprocal_rank=(1.0 / best_rank) if best_rank else 0.0,
        file_recall=file_recall,
        matched_files=matched,
        missed_files=missed,
        total_hits=len(result.hits),
    )


@dataclass(frozen=True)
class RetrievalAggregate:
    """Corpus-level retrieval numbers. This is what the baseline records."""

    n_cases: int
    n_scored: int
    n_unscorable: int
    n_positive: int
    recall_at_1: float
    recall_at_1_outright: float
    recall_at_k: float
    mrr: float
    file_recall: float | None
    n_forbidden_violations: int
    n_failed: int


def aggregate_retrieval(scores: list[RetrievalCaseScore]) -> RetrievalAggregate:
    """Aggregate per-case scores. Positive-only metrics ignore negatives.

    Averaging recall over negative cases would let a corpus inflate its score by
    adding easy negatives, so recall/MRR are computed over positive cases only
    and negatives are reported through their own violation count.
    """
    scored = [s for s in scores if s.scorable]
    # Split by the case's *label*, never by its outcome: a positive case whose
    # target went unranked has rank None and zero RR, and inferring from that
    # would quietly reclassify exactly the failures we most need counted.
    positive = [s for s in scored if s.expect_finding]

    file_scores = [s.file_recall for s in positive if s.file_recall is not None]

    return RetrievalAggregate(
        n_cases=len(scores),
        n_scored=len(scored),
        n_unscorable=len(scores) - len(scored),
        n_positive=len(positive),
        recall_at_1=mean([s.recall_at_1 for s in positive]) if positive else 0.0,
        recall_at_1_outright=(
            mean([s.recall_at_1_outright for s in positive]) if positive else 0.0
        ),
        recall_at_k=mean([s.recall_at_k for s in positive]) if positive else 0.0,
        mrr=mean([s.reciprocal_rank for s in positive]) if positive else 0.0,
        file_recall=mean(file_scores) if file_scores else None,
        n_forbidden_violations=sum(len(s.forbidden_surfaced) for s in scored),
        n_failed=sum(1 for s in scored if not s.passed),
    )


# ---------------------------------------------------------------------------
# review (live)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRunOutcome:
    """One live run of one case, reduced to comparable booleans.

    Deliberately no model text: wording varies run to run and is not the thing
    being measured (the same discipline V1's S6 evaluation used).
    """

    found_finding: bool
    cross_repo_finding: bool
    category_match: bool
    evidence_repo_match: bool
    evidence_file_match: bool
    verdict: str
    discarded: int


def score_review_run(case: EvalCase, validated: ValidatedReview) -> ReviewRunOutcome:
    """Score one live pipeline run against a case's labels."""
    findings = validated.findings
    targets = set(case.target_repos)
    target_files = {split_file_ref(ref) for ref in case.target_files}

    evidence = [(ev.repo, ev.path) for f in findings for ev in f.evidence]

    return ReviewRunOutcome(
        found_finding=bool(findings),
        cross_repo_finding=any(
            any(ev.repo != case.repo for ev in f.evidence) for f in findings
        ),
        category_match=any(f.category == case.category for f in findings),
        evidence_repo_match=bool(targets) and any(repo in targets for repo, _ in evidence),
        evidence_file_match=bool(target_files) and any(loc in target_files for loc in evidence),
        verdict=validated.verdict,
        discarded=validated.discard_count,
    )


@dataclass(frozen=True)
class ReviewCaseScore:
    """All runs of one case, plus whether they agreed with each other."""

    case_id: str
    expect_finding: bool
    runs: list[ReviewRunOutcome]

    @property
    def primary_signals(self) -> list[bool]:
        """The one signal per run that flake is measured on.

        For a positive case that is "did it report the right category"; for a
        negative case, "did it invent a cross-repository finding". Those are the
        outcomes a reader would notice changing between two runs.
        """
        if self.expect_finding:
            return [r.category_match for r in self.runs]
        return [r.cross_repo_finding for r in self.runs]

    @property
    def flaky(self) -> bool:
        return len(set(self.primary_signals)) > 1

    @property
    def pass_rate(self) -> float:
        if not self.runs:
            return 0.0
        signals = self.primary_signals
        # A negative case passes when the signal is *absent*.
        return mean(signals) if self.expect_finding else 1.0 - mean(signals)


@dataclass(frozen=True)
class ReviewAggregate:
    n_cases: int
    n_runs: int
    category_accuracy: float
    evidence_repo_accuracy: float
    evidence_file_accuracy: float | None
    false_positive_rate: float
    mean_discards: float
    flake_rate: float


def aggregate_review(scores: list[ReviewCaseScore]) -> ReviewAggregate:
    """Aggregate live outcomes; positives and negatives measured separately."""
    positives = [s for s in scores if s.expect_finding]
    negatives = [s for s in scores if not s.expect_finding]

    pos_runs = [r for s in positives for r in s.runs]
    neg_runs = [r for s in negatives for r in s.runs]
    all_runs = pos_runs + neg_runs

    file_runs = [r.evidence_file_match for s in positives if s.runs for r in s.runs]

    return ReviewAggregate(
        n_cases=len(scores),
        n_runs=len(all_runs),
        category_accuracy=mean([r.category_match for r in pos_runs]) if pos_runs else 0.0,
        evidence_repo_accuracy=(
            mean([r.evidence_repo_match for r in pos_runs]) if pos_runs else 0.0
        ),
        evidence_file_accuracy=mean(file_runs) if file_runs else None,
        # The number that matters most: how often a docs-only or private-rename
        # change provoked a confident cross-repository claim about nothing.
        false_positive_rate=(
            mean([r.cross_repo_finding for r in neg_runs]) if neg_runs else 0.0
        ),
        mean_discards=mean([r.discarded for r in all_runs]) if all_runs else 0.0,
        flake_rate=mean([s.flaky for s in scores]) if scores else 0.0,
    )
