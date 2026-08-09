"""Tests for the evaluation harness (V2.1).

The harness is the thing every later retrieval milestone is judged by, so its
own correctness matters more than most modules': a scorer that flatters a
change is worse than no scorer, because it launders a regression into evidence
of progress.

Three properties get the most attention here:

- **ties are not resolved by luck** — competition ranking, and the outright
  variant that refuses to credit a tie;
- **negatives are never a free pass** — an unscorable case is reported as
  unscorable, not counted as a win;
- **offline really is offline** — no `claude`, no network, so it can gate CI.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from panorama.errors import ValidationError
from panorama.evaluation import runner as runner_mod
from panorama.evaluation.cases import EvalCase, load_cases, split_file_ref
from panorama.evaluation.report import (
    baseline_object,
    compare_to_baseline,
    load_baseline,
    render_offline_report,
)
from panorama.evaluation.runner import run_offline
from panorama.evaluation.scoring import (
    aggregate_retrieval,
    competition_rank,
    score_retrieval,
    tie_width,
)
from panorama.fixtures import bootstrap as bootstrap_fixtures
from panorama.retrieval import Hit, RepoRelevance, RetrievalResult

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def result(ranked: list[tuple[str, float]], hits: list[tuple[str, str]] = ()) -> RetrievalResult:
    """A RetrievalResult carrying only what the scorer reads."""
    return RetrievalResult(
        signals=[],
        searched=[],
        hits=[
            Hit(repo=repo, path=path, line=1, token="t", window=()) for repo, path in hits
        ],
        ranked_repos=[RepoRelevance(repo=r, score=s, signals=[]) for r, s in ranked],
        convention_docs={},
    )


def positive(**over) -> EvalCase:
    base = dict(
        id="c",
        repo="pr-repo",
        head="branch",
        expect_finding=True,
        category="contract_break",
        target_repos=["consumer"],
    )
    return EvalCase(**{**base, **over})


def negative(**over) -> EvalCase:
    base = dict(id="n", repo="pr-repo", head="branch", expect_finding=False)
    return EvalCase(**{**base, **over})


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------


def test_competition_rank_gives_tied_repos_the_same_rank():
    ranked = [("a", 5.0), ("b", 5.0), ("c", 5.0), ("d", 1.0)]
    res = result(ranked)
    for name in ("a", "b", "c"):
        assert competition_rank(res.ranked_repos, name) == 1
    # The tie occupies three places, so the next repo is rank 4, not rank 2.
    assert competition_rank(res.ranked_repos, "d") == 4


def test_competition_rank_is_none_for_an_unranked_repo():
    assert competition_rank(result([("a", 1.0)]).ranked_repos, "absent") is None


def test_tie_width_reports_how_many_share_the_score():
    ranked = result([("a", 5.0), ("b", 5.0), ("c", 1.0)]).ranked_repos
    assert tie_width(ranked, "a") == 2
    assert tie_width(ranked, "c") == 1
    assert tie_width(ranked, "absent") == 0


def test_a_tie_for_first_counts_as_recall_at_1_but_not_outright():
    """The distinction that keeps a saturated metric informative."""
    score = score_retrieval(positive(), result([("consumer", 5.0), ("other", 5.0)]))
    assert score.rank == 1
    assert score.recall_at_1 is True
    assert score.recall_at_1_outright is False
    assert score.tied_with == 2


def test_an_outright_first_place_counts_both_ways():
    score = score_retrieval(positive(), result([("consumer", 9.0), ("other", 1.0)]))
    assert score.recall_at_1 and score.recall_at_1_outright


# ---------------------------------------------------------------------------
# retrieval scoring
# ---------------------------------------------------------------------------


def test_target_outside_top_k_fails_but_still_records_its_rank():
    ranked = [("x", 9.0), ("y", 8.0), ("z", 7.0), ("consumer", 1.0)]
    score = score_retrieval(positive(), result(ranked))
    assert score.rank == 4
    assert score.recall_at_k is False
    assert score.passed is False
    assert score.reciprocal_rank == pytest.approx(0.25)


def test_an_unranked_target_scores_zero_rather_than_crashing():
    score = score_retrieval(positive(), result([("unrelated", 3.0)]))
    assert score.rank is None
    assert score.reciprocal_rank == 0.0
    assert score.recall_at_1 is False and score.recall_at_k is False
    assert score.passed is False


def test_empty_ranking_scores_as_a_miss():
    score = score_retrieval(positive(), result([]))
    assert score.rank is None and score.passed is False


def test_multi_target_case_takes_the_best_ranked_target():
    case = positive(target_repos=["far", "near"])
    score = score_retrieval(case, result([("near", 9.0), ("mid", 5.0), ("far", 1.0)]))
    assert score.rank == 1


def test_file_recall_counts_target_files_present_in_hits():
    case = positive(target_files=["consumer/a.ts", "consumer/b.ts"])
    score = score_retrieval(
        case, result([("consumer", 9.0)], hits=[("consumer", "a.ts")])
    )
    assert score.file_recall == pytest.approx(0.5)
    assert score.matched_files == ["consumer/a.ts"]
    assert score.missed_files == ["consumer/b.ts"]


def test_file_recall_is_none_when_a_case_gives_no_file_ground_truth():
    assert score_retrieval(positive(), result([("consumer", 1.0)])).file_recall is None


def test_a_hit_in_the_right_repo_but_wrong_file_does_not_count():
    case = positive(target_files=["consumer/a.ts"])
    score = score_retrieval(case, result([("consumer", 9.0)], hits=[("consumer", "z.ts")]))
    assert score.file_recall == 0.0


# ---------------------------------------------------------------------------
# negatives
# ---------------------------------------------------------------------------


def test_a_bare_negative_is_unscorable_not_a_pass():
    """The property that stops offline numbers overstating their coverage."""
    score = score_retrieval(negative(), result([("anything", 5.0)]))
    assert score.scorable is False
    assert "live" in score.unscorable_reason
    # It does not fail, but it must not be counted among the scored either.
    agg = aggregate_retrieval([score])
    assert agg.n_scored == 0 and agg.n_unscorable == 1


def test_forbid_repos_fails_when_a_forbidden_repo_reaches_the_top_k():
    case = negative(forbid_repos=["bystander"])
    score = score_retrieval(case, result([("bystander", 5.0), ("other", 1.0)]))
    assert score.scorable is True
    assert score.forbidden_surfaced == ["bystander"]
    assert score.passed is False


def test_forbid_repos_passes_when_the_forbidden_repo_stays_below_top_k():
    case = negative(forbid_repos=["bystander"])
    ranked = [("a", 9.0), ("b", 8.0), ("c", 7.0), ("bystander", 1.0)]
    score = score_retrieval(case, result(ranked))
    assert score.forbidden_surfaced == [] and score.passed is True


def test_expect_no_hits_fails_when_retrieval_surfaced_something():
    case = negative(expect_no_hits=True)
    score = score_retrieval(case, result([("a", 1.0)], hits=[("a", "f.ts")]))
    assert score.no_hits_ok is False and score.passed is False


def test_expect_no_hits_passes_on_an_empty_hit_list():
    score = score_retrieval(negative(expect_no_hits=True), result([]))
    assert score.no_hits_ok is True and score.passed is True


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def test_aggregate_uses_labels_not_outcomes_to_split_positives():
    """A positive whose target went unranked must still count as a positive.

    Inferring positivity from the outcome would reclassify exactly the misses
    that need counting, and the mean would silently improve.
    """
    scores = [
        score_retrieval(positive(id="hit"), result([("consumer", 9.0)])),
        score_retrieval(positive(id="miss"), result([("unrelated", 9.0)])),
    ]
    agg = aggregate_retrieval(scores)
    assert agg.n_positive == 2
    assert agg.recall_at_1 == pytest.approx(0.5)
    assert agg.n_failed == 1


def test_negatives_do_not_inflate_recall():
    scores = [
        score_retrieval(positive(id="p"), result([("unrelated", 9.0)])),
        score_retrieval(negative(id="n", forbid_repos=["x"]), result([("y", 1.0)])),
    ]
    agg = aggregate_retrieval(scores)
    # One positive, and it missed. Adding a passing negative must not help.
    assert agg.n_positive == 1
    assert agg.recall_at_1 == 0.0 and agg.recall_at_k == 0.0


def test_aggregate_of_nothing_is_zero_not_a_crash():
    agg = aggregate_retrieval([])
    assert agg.n_cases == 0 and agg.recall_at_1 == 0.0 and agg.mrr == 0.0


# ---------------------------------------------------------------------------
# case loading and label validation
# ---------------------------------------------------------------------------


def write_case(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
id = "{id}"
repo = "pr-repo"
head = "branch"
expect_finding = true
category = "contract_break"
target_repos = ["consumer"]
"""


def test_load_cases_reads_and_sorts(tmp_path):
    write_case(tmp_path, "b", MINIMAL.format(id="b-case"))
    write_case(tmp_path, "a", MINIMAL.format(id="a-case"))
    cases = load_cases(tmp_path)
    assert [c.id for c in cases] == ["a-case", "b-case"]
    assert cases[0].source is not None


def test_load_cases_rejects_duplicate_ids(tmp_path):
    write_case(tmp_path, "one", MINIMAL.format(id="same"))
    write_case(tmp_path, "two", MINIMAL.format(id="same"))
    with pytest.raises(ValidationError, match="duplicate"):
        load_cases(tmp_path)


def test_load_cases_rejects_an_unknown_case_filter(tmp_path):
    """A typo'd --case must fail, not silently score an empty corpus."""
    write_case(tmp_path, "a", MINIMAL.format(id="a-case"))
    with pytest.raises(ValidationError, match="unknown evaluation case"):
        load_cases(tmp_path, only=["nope"])


def test_load_cases_errors_on_a_missing_directory(tmp_path):
    with pytest.raises(ValidationError, match="not found"):
        load_cases(tmp_path / "absent")


def test_load_cases_errors_on_an_empty_directory(tmp_path):
    tmp_path.joinpath("readme.txt").write_text("not a case")
    with pytest.raises(ValidationError, match="no evaluation cases"):
        load_cases(tmp_path)


def test_invalid_toml_names_the_file(tmp_path):
    write_case(tmp_path, "broken", "id = = =")
    with pytest.raises(ValidationError, match="broken.toml"):
        load_cases(tmp_path)


def test_an_unknown_key_is_rejected_as_a_typo(tmp_path):
    write_case(tmp_path, "x", MINIMAL.format(id="x") + '\ntarget_repo = "typo"\n')
    with pytest.raises(ValidationError):
        load_cases(tmp_path)


@pytest.mark.parametrize(
    "body, match",
    [
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=true\n',
            "needs a category",
        ),
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=true\ncategory="contract_break"\n',
            "target_repos",
        ),
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=false\ncategory="convention"\n',
            "must not set a category",
        ),
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=false\ntarget_repos=["x"]\n',
            "must not set target_repos",
        ),
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=true\ncategory="contract_break"\n'
            'target_repos=["r"]\n',
            "own repository",
        ),
        (
            'id="a"\nrepo="r"\nhead="h"\nexpect_finding=true\ncategory="contract_break"\n'
            'target_repos=["c"]\ntarget_files=["nopath"]\n',
            "must be 'repo/path'",
        ),
    ],
)
def test_label_consistency_is_enforced(tmp_path, body, match):
    write_case(tmp_path, "case", body)
    with pytest.raises(ValidationError, match=match):
        load_cases(tmp_path)


def test_single_repo_category_needs_no_target(tmp_path):
    """A real finding with no cross-repository claim is a legal positive."""
    write_case(
        tmp_path,
        "sr",
        'id="sr"\nrepo="r"\nhead="h"\nexpect_finding=true\ncategory="single_repo"\n',
    )
    assert load_cases(tmp_path)[0].is_cross_repo is False


def test_split_file_ref():
    assert split_file_ref("repo/dir/file.ts") == ("repo", "dir/file.ts")


# ---------------------------------------------------------------------------
# baseline comparison
# ---------------------------------------------------------------------------


def baseline_from(scores):
    return baseline_object(aggregate_retrieval(scores), scores)


def test_an_identical_run_reports_no_regression():
    scores = [score_retrieval(positive(id="a"), result([("consumer", 9.0)]))]
    regressions, _ = compare_to_baseline(baseline_from(scores), aggregate_retrieval(scores), scores)
    assert regressions == []


def test_a_case_that_stops_passing_is_a_regression():
    before = [score_retrieval(positive(id="a"), result([("consumer", 9.0)]))]
    after = [score_retrieval(positive(id="a"), result([("unrelated", 9.0)]))]
    regressions, _ = compare_to_baseline(baseline_from(before), aggregate_retrieval(after), after)
    assert any("passed at baseline" in r for r in regressions)


def test_losing_outright_first_place_to_a_tie_is_caught():
    before = [score_retrieval(positive(id="a"), result([("consumer", 9.0), ("x", 1.0)]))]
    after = [score_retrieval(positive(id="a"), result([("consumer", 9.0), ("x", 9.0)]))]
    regressions, _ = compare_to_baseline(baseline_from(before), aggregate_retrieval(after), after)
    # Still recall@1 under competition ranking, so the aggregate is unchanged,
    # but the outright metric fell — the gate must notice.
    assert any("outright" in r for r in regressions)


def test_one_case_improving_does_not_hide_another_breaking():
    """The reason per-case outcomes are stored alongside the aggregate."""
    before = [
        score_retrieval(positive(id="a"), result([("consumer", 9.0)])),
        score_retrieval(positive(id="b"), result([("unrelated", 9.0)])),
    ]
    after = [
        score_retrieval(positive(id="a"), result([("unrelated", 9.0)])),
        score_retrieval(positive(id="b"), result([("consumer", 9.0)])),
    ]
    agg_after = aggregate_retrieval(after)
    regressions, _ = compare_to_baseline(baseline_from(before), agg_after, after)
    # The mean is identical; only the per-case check can see the swap.
    assert agg_after.recall_at_1 == pytest.approx(0.5)
    assert any("a:" in r for r in regressions)


def test_a_shrunken_corpus_skips_aggregate_comparison():
    before = [
        score_retrieval(positive(id="a"), result([("consumer", 9.0)])),
        score_retrieval(positive(id="b"), result([("unrelated", 9.0)])),
    ]
    after = [before[0]]
    regressions, notes = compare_to_baseline(
        baseline_from(before), aggregate_retrieval(after), after
    )
    assert any("corpus changed" in n for n in notes)
    assert any("gone" in n for n in notes)
    # Dropping the failing case must not be reported as an improvement.
    assert regressions == []


def test_a_forbidden_repo_appearing_is_a_regression():
    case = negative(id="n", forbid_repos=["bystander"])
    before = [score_retrieval(case, result([("ok", 1.0)]))]
    after = [score_retrieval(case, result([("bystander", 9.0)]))]
    regressions, _ = compare_to_baseline(baseline_from(before), aggregate_retrieval(after), after)
    assert any("must not" in r for r in regressions)


def test_improvements_are_notes_not_regressions():
    before = [score_retrieval(positive(id="a"), result([("unrelated", 9.0)]))]
    after = [score_retrieval(positive(id="a"), result([("consumer", 9.0)]))]
    regressions, notes = compare_to_baseline(
        baseline_from(before), aggregate_retrieval(after), after
    )
    assert regressions == []
    assert any("improved" in n for n in notes)


def test_load_baseline_returns_none_for_missing_or_corrupt(tmp_path):
    assert load_baseline(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_baseline(bad) is None


def test_baseline_object_round_trips_through_json():
    scores = [score_retrieval(positive(id="a"), result([("consumer", 9.0)]))]
    obj = baseline_from(scores)
    assert json.loads(json.dumps(obj))["cases"]["a"]["recall_at_1"] is True


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_report_renders_identically_for_a_fixed_input():
    """Golden test: the report is stable, so a diff means a real change."""
    scores = [
        score_retrieval(positive(id="alpha"), result([("consumer", 9.0), ("x", 1.0)])),
        score_retrieval(positive(id="beta"), result([("consumer", 5.0), ("x", 5.0)])),
        score_retrieval(negative(id="gamma"), result([("x", 1.0)])),
    ]
    rendered = render_offline_report(scores, aggregate_retrieval(scores))
    assert rendered == (
        "# Retrieval evaluation\n"
        "\n"
        "case           rank   files   hits  result\n"
        "-----  ------------  ------  -----  ------\n"
        "alpha             1       —      0  pass\n"
        "beta      1 (tie×2)       —      0  pass\n"
        "gamma             —       —      0  not scored\n"
        "\n"
        "Cases: 3 (2 positive, 1 not retrieval-scorable)\n"
        "recall@1: 1.000  (outright, ties excluded: 0.500)\n"
        "recall@3: 1.000\n"
        "MRR: 1.000\n"
        "\n"
        "1 case(s) assert nothing about retrieval and are scored only by `--live`. "
        "Offline numbers do not cover them.\n"
    )


def test_report_names_a_failing_case_and_its_regressions():
    scores = [score_retrieval(positive(id="alpha"), result([("unrelated", 9.0)]))]
    rendered = render_offline_report(
        scores, aggregate_retrieval(scores), regressions=["alpha: broke"]
    )
    assert "FAIL" in rendered and "unranked" in rendered
    assert "## Regressions" in rendered and "alpha: broke" in rendered


# ---------------------------------------------------------------------------
# the runner, over real bootstrapped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(tmp_path: Path) -> Path:
    """A real bootstrapped fixture organisation to retrieve against."""
    root = tmp_path / "demo-org"
    bootstrap_fixtures(dest_root=root)
    return root


def real_cases() -> list[EvalCase]:
    return load_cases(Path("evals/cases"))


def test_run_offline_scores_every_checked_in_case(org):
    scores, failures = run_offline(real_cases(), org_root=org)
    assert failures == []
    assert len(scores) == len(real_cases())


def test_checked_in_cases_all_pass_retrieval(org):
    """The corpus is ground truth: if it fails, either retrieval or a label is wrong."""
    scores, _ = run_offline(real_cases(), org_root=org)
    failing = [s.case_id for s in scores if not s.passed]
    assert failing == []


def test_offline_never_constructs_a_claude_runner(org, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("offline evaluation must not invoke the model")

    monkeypatch.setattr(runner_mod, "ClaudeRunner", boom)
    scores, failures = run_offline(real_cases(), org_root=org)
    assert failures == [] and scores


def test_offline_opens_no_network_socket(org, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("offline evaluation must not touch the network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    scores, _ = run_offline(real_cases(), org_root=org)
    assert scores


def test_a_case_pointing_at_a_missing_branch_is_a_failure_not_a_zero(org):
    """A broken environment must be distinguishable from bad retrieval."""
    case = positive(id="bogus", repo="acme-api", head="no-such-branch")
    scores, failures = run_offline([case], org_root=org)
    assert scores == []
    assert len(failures) == 1 and failures[0].case_id == "bogus"


def test_labels_point_at_files_that_exist_in_the_workspace(org):
    """Ground truth that names a path typo'd out of existence scores nothing."""
    for case in real_cases():
        for ref in case.target_files:
            repo, path = split_file_ref(ref)
            assert (org / repo / path).is_file(), f"{case.id}: missing {ref}"


def test_labels_name_repositories_that_exist(org):
    present = {p.name for p in org.iterdir() if p.is_dir()}
    for case in real_cases():
        assert case.repo in present, f"{case.id}: unknown repo {case.repo}"
        for target in [*case.target_repos, *case.forbid_repos]:
            assert target in present, f"{case.id}: unknown target {target}"
