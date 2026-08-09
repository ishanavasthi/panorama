"""Refactor-equivalence: the channel-wrapped lexical pass must not change.

V2.3 is a foundation milestone. It adds a cache, language packs and a channel
interface, and it is supposed to change **nothing a user or the evaluation can
see**. The plan states the rule bluntly: *a foundation milestone that changes a
number is a foundation milestone with a bug.*

The evaluation baseline catches a change in *ranking*. It cannot catch a change
in everything else retrieval produces — which signals were extracted, in what
order they were searched, which exact lines were matched, what context window
each hit carries, whether the run truncated. All of that reaches the model, so a
silent change there is a silent change to review quality that no aggregate would
report.

So this test compares against a snapshot recorded from the pre-refactor code and
checked in: `tests/data/retrieval_golden.json`. Regenerate it only when a change
to retrieval behaviour is *intended*, and say so in the commit message —
regenerating it to make a test pass is the one move that makes it worthless.

    uv run python -m tests.regen_retrieval_golden
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from panorama.evaluation.cases import load_cases
from panorama.evaluation.runner import retrieve_for_case
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.retrieval import RetrievalResult

GOLDEN_PATH = Path(__file__).parent / "data" / "retrieval_golden.json"
CASES_ROOT = Path("evals/cases")


def snapshot(result: RetrievalResult) -> dict:
    """Every observable field of a retrieval result, in a stable JSON shape.

    Deliberately exhaustive rather than convenient: the point is to notice a
    change nobody meant to make, so anything the model can see is recorded.
    """
    return {
        # Signal *order* matters as much as membership: it decides which
        # signals survive the search cap, and therefore what gets searched.
        "signals": [
            {
                "token": s.token,
                "kind": s.kind,
                "weight": s.weight,
                "polarity": sorted(s.polarity),
            }
            for s in result.signals
        ],
        "searched": [s.token for s in result.searched],
        "hits": [
            {
                "repo": h.repo,
                "path": h.path,
                "line": h.line,
                "token": h.token,
                "window": list(h.window),
            }
            for h in result.hits
        ],
        "ranked_repos": [asdict(r) for r in result.ranked_repos],
        "convention_docs": result.convention_docs,
        "truncated": result.truncated,
    }


def build_snapshot(org_root: Path) -> dict:
    """Snapshot retrieval for every checked-in case, keyed by case id."""
    return {
        case.id: snapshot(retrieve_for_case(case, org_root)[2])
        for case in load_cases(CASES_ROOT)
    }


@pytest.fixture(scope="module")
def org(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("equivalence") / "demo-org"
    bootstrap_fixtures(dest_root=root)
    return root


def test_retrieval_matches_the_recorded_snapshot(org: Path) -> None:
    assert GOLDEN_PATH.is_file(), (
        f"{GOLDEN_PATH} is missing; regenerate it with "
        "`uv run python -m tests.regen_retrieval_golden`"
    )
    expected = json.loads(GOLDEN_PATH.read_text())
    actual = build_snapshot(org)

    # Compare case by case so a failure names the case rather than dumping the
    # whole corpus at whoever broke it.
    assert sorted(actual) == sorted(expected), "the corpus changed; regenerate the golden"
    for case_id in sorted(expected):
        assert actual[case_id] == expected[case_id], (
            f"retrieval output changed for {case_id!r}. If that was intended, "
            "regenerate the golden and justify it in the commit message."
        )


def test_the_snapshot_actually_covers_something(org: Path) -> None:
    """A golden of empty results would pass forever and prove nothing."""
    recorded = json.loads(GOLDEN_PATH.read_text())
    assert len(recorded) >= 18
    assert sum(len(c["hits"]) for c in recorded.values()) > 100
    assert sum(len(c["ranked_repos"]) for c in recorded.values()) > 20
