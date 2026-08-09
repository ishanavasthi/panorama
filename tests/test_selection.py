"""Tests for manifest-first selection and the clone budget.

Selection introduces the worst failure mode in the system: a relevant sibling
that is never cloned produces no lead, no finding, and no warning. The review
just comes back thinner and nobody knows why. A wrong finding is visible; a
missing one is not.

So these tests are weighted towards the things that keep that bounded — the
pull request's own repository is never dropped, the escape hatch really does
restore the old behaviour, the counts are always reported, and a labelled target
in the evaluation corpus survives selection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panorama.cache import Cache
from panorama.languages import ManifestFacts
from panorama.selection import (
    DEFAULT_CLONE_BUDGET,
    REASON_BUDGET_FILL,
    REASON_DEPENDENCY_EDGE,
    REASON_PR_REPO,
    REASON_SYMBOL_MATCH,
    select_repositories,
)


def facts(name: str | None, deps: tuple[str, ...] = ()) -> tuple[ManifestFacts, str | None]:
    return ManifestFacts(declared_name=name, dependencies=deps), "typescript"


def org(**repos: tuple[ManifestFacts, str | None]) -> dict:
    return dict(repos)


def filler(count: int, start: int = 0) -> dict:
    """Repositories with no relationship to anything, for budget pressure."""
    return {f"pad{i:03d}": facts(f"pad{i:03d}") for i in range(start, start + count)}


# ---------------------------------------------------------------------------
# the things that must never happen
# ---------------------------------------------------------------------------


def test_the_pull_requests_own_repository_is_always_selected() -> None:
    """Dropping it would make the review impossible, not merely worse."""
    repos = {"target": facts("target"), **filler(50)}
    selection = select_repositories("target", repos, budget=3)
    assert "target" in selection.selected
    assert selection.reasons["target"] == REASON_PR_REPO


def test_the_budget_is_never_exceeded() -> None:
    repos = {"target": facts("target"), **filler(100)}
    selection = select_repositories("target", repos, budget=7)
    assert selection.n_selected == 7


def test_a_declared_dependency_beats_an_arbitrary_repository() -> None:
    repos = {
        "app": facts("app", ("lib",)),
        "library": facts("lib"),
        **filler(50),
    }
    selection = select_repositories("app", repos, budget=2)
    assert set(selection.selected) == {"app", "library"}
    assert selection.reasons["library"] == REASON_DEPENDENCY_EDGE


def test_a_consumer_is_selected_as_readily_as_a_provider() -> None:
    """The direction that matters most for a contract break."""
    repos = {"library": facts("lib"), "app": facts("app", ("lib",)), **filler(50)}
    selection = select_repositories("library", repos, budget=2)
    assert set(selection.selected) == {"library", "app"}


def test_selection_resolves_declared_names_not_directory_names() -> None:
    """The same resolution retrieval uses. If selection resolved edges
    differently it would drop repositories retrieval was about to ask for."""
    repos = {
        "api-server": facts("@scope/api", ("@scope/toolkit",)),
        "helpers-repo": facts("@scope/toolkit"),
        **filler(50),
    }
    selection = select_repositories("api-server", repos, budget=2)
    assert set(selection.selected) == {"api-server", "helpers-repo"}


# ---------------------------------------------------------------------------
# the escape hatch and the small-organisation case
# ---------------------------------------------------------------------------


def test_all_repos_takes_everything() -> None:
    repos = {"target": facts("target"), **filler(100)}
    selection = select_repositories("target", repos, budget=5, all_repos=True)
    assert selection.n_selected == 101
    assert selection.complete is True


def test_an_organisation_inside_the_budget_is_taken_whole() -> None:
    """No selection happens, and the report must not imply one did."""
    repos = {"target": facts("target"), **filler(3)}
    selection = select_repositories("target", repos, budget=DEFAULT_CLONE_BUDGET)
    assert selection.complete is True
    assert "all 4 repositories" in selection.summary()


def test_an_organisation_far_above_the_old_ceiling_is_reviewable() -> None:
    """V1 refused above fifty. The ceiling is now a clone budget."""
    repos = {"target": facts("target"), **filler(200)}
    selection = select_repositories("target", repos, budget=DEFAULT_CLONE_BUDGET)
    assert selection.n_considered == 201
    assert selection.n_selected == DEFAULT_CLONE_BUDGET
    assert selection.complete is False


# ---------------------------------------------------------------------------
# saying what was left out
# ---------------------------------------------------------------------------


def test_an_incomplete_selection_says_so_and_says_how_to_undo_it() -> None:
    """A silent recall loss is the worst outcome, so it is never silent."""
    repos = {"target": facts("target"), **filler(100)}
    summary = select_repositories("target", repos, budget=5).summary()
    assert "5 of 101" in summary
    assert "not searched" in summary
    assert "--all-repos" in summary


def test_every_selected_repository_records_why() -> None:
    repos = {"app": facts("app", ("lib",)), "library": facts("lib"), **filler(50)}
    selection = select_repositories("app", repos, budget=4)
    assert set(selection.reasons) == set(selection.selected)
    assert all(selection.reasons.values())


# ---------------------------------------------------------------------------
# spending the remaining budget
# ---------------------------------------------------------------------------


def test_spare_budget_is_spent_rather_than_left_idle() -> None:
    """An unspent budget is strictly worse: the lexical channel finds links
    through shared vocabulary and needs no declared edge to do it."""
    repos = {"target": facts("target"), **filler(50)}
    selection = select_repositories("target", repos, budget=6)
    assert selection.n_selected == 6
    assert selection.reasons[selection.selected[-1]] == REASON_BUDGET_FILL


def test_selection_is_deterministic() -> None:
    """Two runs of the same review must examine the same repositories."""
    repos = {"target": facts("target"), **filler(50)}
    first = select_repositories("target", repos, budget=9)
    second = select_repositories("target", repos, budget=9)
    assert first.selected == second.selected


# ---------------------------------------------------------------------------
# what a previous run learned
# ---------------------------------------------------------------------------


def test_a_cached_symbol_match_beats_an_arbitrary_repository(tmp_path: Path) -> None:
    """The cache earning its keep: the second review of an organisation is
    better targeted than the first."""
    repos = {"target": facts("target"), **filler(50)}

    with Cache(tmp_path / "c" / "o.db") as cache:
        cache.put_facts(
            "pad042",
            "a" * 40,
            "symbols",
            {
                "version": 1,
                "files_indexed": 1,
                "truncated": False,
                "exports": {},
                "imports": {"vanishingHelper": [["src/use.ts", 3]]},
            },
        )
        selection = select_repositories(
            "target",
            repos,
            budget=2,
            cache=cache,
            removed_symbols=frozenset({"vanishingHelper"}),
        )

    assert set(selection.selected) == {"target", "pad042"}
    assert selection.reasons["pad042"] == REASON_SYMBOL_MATCH


def test_a_cached_index_for_an_unrelated_name_does_not_promote(tmp_path: Path) -> None:
    repos = {"target": facts("target"), **filler(50)}
    with Cache(tmp_path / "c" / "o.db") as cache:
        cache.put_facts(
            "pad042",
            "a" * 40,
            "symbols",
            {
                "version": 1,
                "files_indexed": 1,
                "truncated": False,
                "exports": {},
                "imports": {"somethingElse": [["src/use.ts", 3]]},
            },
        )
        selection = select_repositories(
            "target",
            repos,
            budget=2,
            cache=cache,
            removed_symbols=frozenset({"vanishingHelper"}),
        )
    assert selection.reasons["pad042" if "pad042" in selection.selected else "target"] != (
        REASON_SYMBOL_MATCH
    )


def test_selection_without_a_cache_still_works() -> None:
    repos = {"target": facts("target"), **filler(20)}
    assert select_repositories("target", repos, budget=3).n_selected == 3


# ---------------------------------------------------------------------------
# awkward organisations
# ---------------------------------------------------------------------------


def test_repositories_without_manifests_are_still_candidates() -> None:
    """A documents repository has no manifest and can still be the answer."""
    repos = {"target": facts("target"), "docs": (ManifestFacts(), None)}
    selection = select_repositories("target", repos, budget=5)
    assert "docs" in selection.selected


def test_a_pull_request_repository_outside_the_listing_does_not_crash() -> None:
    """Provisioning rejects this case separately; selection must not explode."""
    repos = {**filler(30)}
    selection = select_repositories("stranger", repos, budget=4)
    assert selection.n_selected == 4
    assert "stranger" not in selection.selected


@pytest.mark.parametrize("budget", [0, -1])
def test_a_meaningless_budget_takes_everything(budget: int) -> None:
    """Better to do too much work than to silently review nothing."""
    repos = {"target": facts("target"), **filler(10)}
    assert select_repositories("target", repos, budget=budget).complete is True


# ---------------------------------------------------------------------------
# selection over the real corpus
# ---------------------------------------------------------------------------


def corpus_manifests(root: Path) -> dict:
    from panorama.languages import read_repo_manifest
    from panorama.workspace import Workspace

    manifests = {}
    for view in Workspace(root).repos():
        pack, facts_read = read_repo_manifest(view.path)
        manifests[view.name] = (facts_read, pack.name if pack else None)
    return manifests


def test_selection_never_drops_a_labelled_target_at_the_default_budget(
    tmp_path: Path,
) -> None:
    """The corpus assertion the plan asks for.

    Every labelled case names the sibling a correct finding must cite. If
    selection removed one, the case would fail for a reason retrieval could do
    nothing about — and it would look exactly like a retrieval regression.
    """
    from panorama.evaluation.cases import load_cases
    from panorama.fixtures import bootstrap as bootstrap_fixtures

    root = tmp_path / "demo-org"
    bootstrap_fixtures(dest_root=root)
    manifests = corpus_manifests(root)

    for case in load_cases(Path("evals/cases")):
        selection = select_repositories(case.repo, manifests, budget=DEFAULT_CLONE_BUDGET)
        assert selection.complete
        for target in case.target_repos:
            assert target in selection.selected, (
                f"{case.id}: selection dropped the labelled target {target!r}"
            )


def test_a_budget_below_the_organisation_can_drop_a_target_but_never_silently(
    tmp_path: Path,
) -> None:
    """The limitation, pinned as a test rather than left to be discovered.

    Squeeze the budget below what the corpus needs and selection *does* drop a
    labelled target — the consumer in the field-rename case, whose coupling to
    the changed repository is a mirrored response type that no manifest records.
    No signal available before cloning can see that: there is no dependency
    edge, and on a cold cache there is no symbol index either.

    That is a real recall loss and the reason it is tolerable is that it is
    never *silent*. Selection reports that it was incomplete, the report repeats
    it on every run, and `--all-repos` undoes it. This test asserts the
    reporting, because the reporting is the actual guarantee.
    """
    from panorama.evaluation.cases import load_cases
    from panorama.fixtures import bootstrap as bootstrap_fixtures

    root = tmp_path / "demo-org"
    bootstrap_fixtures(dest_root=root)
    manifests = corpus_manifests(root)

    dropped = []
    for case in load_cases(Path("evals/cases")):
        selection = select_repositories(case.repo, manifests, budget=4)
        for target in case.target_repos:
            if target not in selection.selected:
                dropped.append((case.id, target))
                assert not selection.complete
                assert "not searched" in selection.summary()

    assert dropped, (
        "the budget was not tight enough to demonstrate the limitation; "
        "if the corpus grew, lower the budget here"
    )

    # And the escape hatch genuinely recovers every one of them.
    for case_id, target in dropped:
        case = next(c for c in load_cases(Path("evals/cases")) if c.id == case_id)
        recovered = select_repositories(case.repo, manifests, budget=4, all_repos=True)
        assert target in recovered.selected
