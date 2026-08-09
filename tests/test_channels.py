"""Tests for the retrieval channel interface.

The interface carries three promises the later milestones lean on: ties are
visible rather than broken by sort order, silence is a legal answer, and every
ranked repository can say why it is there. Each is cheap to break by accident
and expensive to notice later, so each is pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panorama.channels import (
    RRF_K,
    ChannelResult,
    Hit,
    RankedRepo,
    RetrievalChannel,
    competition_ranked,
    fuse,
)
from panorama.evaluation.cases import load_cases
from panorama.evaluation.runner import _load_case_pr
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.retrieval import LexicalChannel, active_channels


def justify_nothing(repo: str, score: float) -> str:
    return f"{repo}={score}"


# ---------------------------------------------------------------------------
# competition ranking
# ---------------------------------------------------------------------------


def test_ranks_descend_by_score() -> None:
    ranked = competition_ranked([("a", 1.0), ("b", 3.0)], justify=justify_nothing)
    assert [(r.repo, r.rank) for r in ranked] == [("b", 1), ("a", 2)]


def test_a_tie_shares_a_rank() -> None:
    """The property the evaluation scorer depends on.

    If ties were broken by list position, a repository could take rank 1 purely
    because of its name, and every metric built on rank would be measuring the
    alphabet.
    """
    ranked = competition_ranked(
        [("zebra", 5.0), ("alpha", 5.0), ("other", 1.0)], justify=justify_nothing
    )
    by_repo = {r.repo: r.rank for r in ranked}
    assert by_repo == {"alpha": 1, "zebra": 1, "other": 3}


def test_a_tie_still_has_a_deterministic_order() -> None:
    """Sharing a rank must not mean an unstable list."""
    pairs = [("zebra", 5.0), ("alpha", 5.0)]
    first = [r.repo for r in competition_ranked(pairs, justify=justify_nothing)]
    second = [r.repo for r in competition_ranked(list(reversed(pairs)), justify=justify_nothing)]
    assert first == second == ["alpha", "zebra"]


def test_the_rank_after_a_tie_skips(  ) -> None:
    """Two repositories tied at 1 means the next is rank 3, not rank 2 —
    otherwise the tie is invisible in the numbers."""
    ranked = competition_ranked(
        [("a", 9.0), ("b", 9.0), ("c", 4.0)], justify=justify_nothing
    )
    assert {r.repo: r.rank for r in ranked}["c"] == 3


def test_an_empty_ranking_is_legal() -> None:
    assert competition_ranked([], justify=justify_nothing) == ()


def test_every_ranked_repo_carries_a_justification() -> None:
    ranked = competition_ranked([("a", 1.0)], justify=lambda r, s: "because reasons")
    assert ranked[0].justification == "because reasons"


# ---------------------------------------------------------------------------
# the result shape
# ---------------------------------------------------------------------------


def test_a_silent_channel_is_a_valid_result() -> None:
    """Silence is part of the contract, not a failure.

    The dependency-graph channel is silent for a Python consumer of a
    TypeScript API, because no manifest records that edge. Fusion has to
    survive that, so an empty result must be ordinary.
    """
    result = ChannelResult(channel="quiet")
    assert result.ranked == () and result.hits == () and result.repos == ()
    assert result.truncated is False


def test_repos_reads_out_in_rank_order() -> None:
    result = ChannelResult(
        channel="c",
        ranked=(
            RankedRepo("first", 1, 9.0, "why"),
            RankedRepo("second", 2, 1.0, "why"),
        ),
    )
    assert result.repos == ("first", "second")


def test_a_hit_needs_no_window() -> None:
    """A channel that ranks without reading files still produces usable leads."""
    hit = Hit(repo="r", path="src/a.ts", line=4, token="alpha")
    assert hit.window == ()


def test_results_are_immutable() -> None:
    """A channel must not be able to edit another channel's output — the
    independence that lets the harness attribute a change to its cause."""
    result = ChannelResult(channel="c", ranked=(RankedRepo("r", 1, 1.0, "why"),))
    with pytest.raises(AttributeError):
        result.ranked[0].rank = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.channel = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------


def result_of(channel: str, *repos: str) -> ChannelResult:
    """A channel result ranking ``repos`` in the order given."""
    return ChannelResult(
        channel=channel,
        ranked=tuple(
            RankedRepo(repo=repo, rank=i, score=1.0 / i, justification=f"{channel} says so")
            for i, repo in enumerate(repos, start=1)
        ),
    )


def test_a_single_channel_fuses_to_its_own_order() -> None:
    fused = fuse([result_of("one", "a", "b", "c")])
    assert [f.repo for f in fused] == ["a", "b", "c"]


def test_channels_that_agree_reinforce() -> None:
    fused = fuse([result_of("one", "a", "b"), result_of("two", "a", "b")])
    assert [f.repo for f in fused] == ["a", "b"]
    # Scores are rounded for legibility, so compare with an absolute tolerance
    # rather than a relative one.
    assert fused[0].score == pytest.approx(2 / (RRF_K + 1), abs=1e-6)


def test_a_silent_channel_changes_nothing() -> None:
    """The property that makes abstention safe.

    A channel with no opinion must not be able to alter another channel's
    ordering, or every silent channel would be a thumb on the scale.
    """
    alone = fuse([result_of("one", "a", "b")])
    with_silence = fuse([result_of("one", "a", "b"), ChannelResult(channel="quiet")])
    assert [f.repo for f in alone] == [f.repo for f in with_silence]
    assert [f.score for f in alone] == [f.score for f in with_silence]


def test_total_disagreement_ties() -> None:
    """Two channels each certain about a different repository cannot be
    resolved by rank alone — and the tie says exactly that."""
    fused = fuse([result_of("one", "a"), result_of("two", "b")])
    assert fused[0].score == fused[1].score


def test_two_second_places_outweigh_one_first() -> None:
    """The cost of rank fusion, pinned as a test rather than left as a surprise.

    RRF discards magnitude: a channel that is overwhelmingly certain about its
    top result cannot say so. Agreement between two channels therefore beats one
    channel's strong conviction, however wide its margin was. This is a real
    effect on the corpus, not a hypothetical, and it is the concrete input to
    any future tuning.
    """
    fused = fuse([result_of("one", "winner", "shared"), result_of("two", "other", "shared")])
    assert fused[0].repo == "shared"


def test_a_repo_ranked_by_nobody_does_not_appear() -> None:
    fused = fuse([result_of("one", "a")])
    assert [f.repo for f in fused] == ["a"]


def test_fusion_of_nothing_is_nothing() -> None:
    assert fuse([]) == []
    assert fuse([ChannelResult(channel="quiet")]) == []


def test_provenance_names_every_channel_that_voted() -> None:
    fused = fuse([result_of("one", "a"), result_of("two", "a")])
    provenance = fused[0].provenance
    assert len(provenance) == 2
    assert any(line.startswith("one #1") for line in provenance)
    assert any(line.startswith("two #1") for line in provenance)


def test_fused_scores_keep_enough_precision_to_separate_adjacent_ranks() -> None:
    """Rounding is a real hazard here, not a cosmetic choice.

    Adjacent RRF scores differ in the fourth significant figure, so rounding
    too aggressively would collapse rank 1 and rank 2 into a tie and silently
    destroy the ordering the whole channel exists to produce.
    """
    fused = fuse([result_of("one", "a", "b")])
    assert fused[0].score != fused[1].score


def test_fusion_order_is_deterministic_under_ties() -> None:
    fused = fuse([result_of("one", "zebra"), result_of("two", "alpha")])
    assert [f.repo for f in fused] == ["alpha", "zebra"]


def test_the_fusion_constant_barely_matters_at_this_scale() -> None:
    """Measured in V2.7's tuning pass, pinned here so it stays a known fact.

    The standard constant is calibrated for many systems ranking thousands of
    documents. Here three channels rank at most a handful of repositories, so
    the gap between rank 1 and rank 3 is a couple of percent while an extra
    channel's vote doubles the score. Fusion is therefore close to pure vote
    counting, and the constant is nearly inert.

    That is worth knowing in both directions: it means no tuning of the constant
    is available to overfit with, and it means agreement between channels
    dominates any single channel's confidence.
    """
    results = [result_of("one", "solo"), result_of("two", "pair"), result_of("three", "pair")]
    orders = {
        tuple(f.repo for f in fuse(results, k=k)) for k in (1, 2, 5, 10, 20, 60, 120)
    }
    assert len(orders) == 1, "changing the constant reordered results"
    # Two channels ranking a repository first beat one channel ranking another
    # first, at every constant above zero.
    assert orders.pop()[0] == "pair"


def test_without_the_constant_two_seconds_exactly_tie_one_first() -> None:
    """Why zero was considered and rejected in tuning.

    At k=0 reciprocal rank is 1/rank, so two second places sum to exactly one
    first place. On the corpus that converts two losses into ties and lifts
    recall@1 to a perfect score — while making the ranking *less* able to
    discriminate. Plain recall@1 cannot see the difference; the outright
    variant can, which is what it was added for.
    """
    results = [
        result_of("one", "solo"),
        result_of("two", "x", "pair"),
        result_of("three", "y", "pair"),
    ]
    fused = {f.repo: f.score for f in fuse(results, k=0)}
    assert fused["solo"] == pytest.approx(fused["pair"])


# ---------------------------------------------------------------------------
# the lexical channel as an implementation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def org(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("channels") / "demo-org"
    bootstrap_fixtures(dest_root=root)
    return root


def case_by_id(case_id: str):
    return next(c for c in load_cases(Path("evals/cases")) if c.id == case_id)


def test_the_lexical_channel_satisfies_the_protocol() -> None:
    assert isinstance(LexicalChannel(), RetrievalChannel)
    assert LexicalChannel().name == "lexical"


def test_active_channels_all_satisfy_the_protocol() -> None:
    assert active_channels()
    for channel in active_channels():
        assert isinstance(channel, RetrievalChannel)


def test_channel_names_are_unique() -> None:
    names = [c.name for c in active_channels()]
    assert len(names) == len(set(names))


def test_the_pipeline_ranking_is_exactly_the_union_of_its_channels(org: Path) -> None:
    """Fusion must neither invent a repository nor lose one.

    Inventing would mean the report explaining a ranking no channel produced;
    losing would mean a channel doing work that never reaches the review. Both
    are silent failures, so both are pinned.
    """
    from panorama.retrieval import active_channels, retrieve

    case = case_by_id("contract-break-field-rename")
    pull_request, workspace = _load_case_pr(case, org)

    pipeline = retrieve(pull_request, workspace)
    voted = {
        entry.repo
        for channel in active_channels()
        for entry in channel.rank(pull_request, workspace).ranked
    }

    assert {r.repo for r in pipeline.ranked_repos} == voted


def test_file_level_leads_come_only_from_the_lexical_channel(org: Path) -> None:
    """The structural channels say *which* repository, never *where* in it.

    If a manifest edge produced a lead, every negative control in the corpus
    would surface one for a change that touched nothing relevant.
    """
    from panorama.retrieval import retrieve

    case = case_by_id("contract-break-field-rename")
    pull_request, workspace = _load_case_pr(case, org)

    pipeline = retrieve(pull_request, workspace)
    lexical = LexicalChannel().rank(pull_request, workspace)

    assert list(pipeline.hits) == list(lexical.hits)
    assert pipeline.truncated == lexical.truncated


def test_every_ranked_repository_can_say_why_it_is_there(org: Path) -> None:
    from panorama.retrieval import retrieve

    case = case_by_id("convention-endpoint-drift")
    pull_request, workspace = _load_case_pr(case, org)

    for entry in retrieve(pull_request, workspace).ranked_repos:
        assert entry.provenance, f"{entry.repo} appeared with no explanation"
        assert all(":" in line for line in entry.provenance)


def test_a_repository_found_only_structurally_still_ranks(org: Path) -> None:
    """The whole point of the channel, end to end.

    This case's diff shares no vocabulary at all with the document it violates,
    because the violation is an *absence*. Lexical retrieval finds nothing; the
    declared dependency edge finds it anyway.
    """
    from panorama.retrieval import retrieve

    case = case_by_id("convention-unversioned-endpoint")
    pull_request, workspace = _load_case_pr(case, org)

    lexical = LexicalChannel().rank(pull_request, workspace)
    pipeline = retrieve(pull_request, workspace)

    assert lexical.ranked == (), "the lexical channel is supposed to be blind here"
    assert [r.repo for r in pipeline.ranked_repos] == list(case.target_repos)
    assert "dependency" in pipeline.ranked_repos[0].provenance[0]


def test_the_justification_names_the_matched_tokens(org: Path) -> None:
    """Provenance has to be arguable, not decorative."""
    case = case_by_id("contract-break-field-rename")
    pull_request, workspace = _load_case_pr(case, org)

    result = LexicalChannel().rank(pull_request, workspace)

    assert result.ranked, "expected the consumer to surface"
    top = result.ranked[0]
    assert "identifier" in top.justification
    # The tokens named must be the ones actually matched in that repository.
    matched = {hit.token for hit in result.hits if hit.repo == top.repo}
    assert any(token in top.justification for token in matched)


def test_a_diff_that_matches_nothing_produces_an_empty_ranking(org: Path) -> None:
    """The negative controls exercise the silent path end to end."""
    case = case_by_id("negative-private-symbol-rename")
    pull_request, workspace = _load_case_pr(case, org)

    result = LexicalChannel().rank(pull_request, workspace)

    assert result.ranked == ()
    assert result.hits == ()
    assert result.truncated is False


def test_the_justification_summarises_rather_than_listing_everything(org: Path) -> None:
    """A justification is for a human to read, so it stays bounded."""
    case = case_by_id("contract-break-status-code")
    pull_request, workspace = _load_case_pr(case, org)

    result = LexicalChannel().rank(pull_request, workspace)

    for entry in result.ranked:
        assert len(entry.justification) < 200
