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
    ChannelResult,
    Hit,
    RankedRepo,
    RetrievalChannel,
    competition_ranked,
)
from panorama.evaluation.cases import load_cases
from panorama.evaluation.runner import _load_case_pr
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.retrieval import ACTIVE_CHANNELS, LexicalChannel


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
    assert ACTIVE_CHANNELS
    for channel in ACTIVE_CHANNELS:
        assert isinstance(channel, RetrievalChannel)


def test_channel_names_are_unique() -> None:
    names = [c.name for c in ACTIVE_CHANNELS]
    assert len(names) == len(set(names))


def test_the_channel_view_agrees_with_the_pipeline_view(org: Path) -> None:
    """Both views come from one computation, and this proves they stay agreed.

    The pipeline consumes `RetrievalResult`; provenance and fusion consume
    `ChannelResult`. If those ever drift, the report would explain a ranking
    the review did not actually use.
    """
    from panorama.retrieval import retrieve

    case = case_by_id("contract-break-field-rename")
    pull_request, workspace = _load_case_pr(case, org)

    pipeline = retrieve(pull_request, workspace)
    channel = LexicalChannel().rank(pull_request, workspace)

    assert [r.repo for r in pipeline.ranked_repos] == list(channel.repos)
    assert [r.score for r in pipeline.ranked_repos] == [r.score for r in channel.ranked]
    assert list(pipeline.hits) == list(channel.hits)
    assert pipeline.truncated == channel.truncated


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
