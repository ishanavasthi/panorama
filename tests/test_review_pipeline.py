"""Offline tests for the S5 review pipeline: prompt, validation, rendering.

Two layers, both fully offline:

1. **Host logic** — diff-to-changed-lines parsing, evidence resolution, the
   validator's keep/discard rules, secret screening, and reference-only
   rendering. Pure functions over the real bootstrapped fixtures.

2. **End to end through the fake `claude`** — the S5 exit criteria. The fake CLI
   from ``tests/fake_claude/`` drives ``run_review`` through structured success,
   malformed-then-repaired output, an invalid citation that the host discards, a
   CLI error, and a timeout. No subscription, no network, no real model.

Fixture names (``acme-*``) live only here in test expectations, never in the
production code under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panorama.claude_runner import ClaudeRunner
from panorama.errors import ClaudeInvocationError
from panorama.fixtures import bootstrap
from panorama.intake import LocalPullRequestSource
from panorama.models import Evidence, Finding, Review
from panorama.render import render_markdown, review_json_obj
from panorama.retrieval import RetrievalResult, filter_diff, retrieve
from panorama.review import (
    MAX_DIFF_CHARS,
    build_review_prompt,
    context_truncated,
    prepare_diff,
    run_review,
)
from panorama.screening import contains_secret, is_clean
from panorama.validation import (
    ValidatedReview,
    changed_lines,
    evidence_resolves,
    validate_review,
)
from panorama.workspace import Workspace
from tests import fake_claude as sc
from tests.conftest import FakeClaude, assert_no_raw_output

_PR_REPO = "acme-api"
_PR_HEAD = "p1-rename"
_CONSUMER = "acme-web"


# ---------------------------------------------------------------------------
# shared fixtures over the real bootstrapped organisation
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(bootstrap(dest_root=tmp_path / "demo-org").root)


@pytest.fixture
def pr(workspace: Workspace):
    return LocalPullRequestSource(workspace.repo(_PR_REPO).path, "main", _PR_HEAD).load()


@pytest.fixture
def retrieval(pr, workspace: Workspace) -> RetrievalResult:
    return retrieve(pr, workspace)


def _real_consumer_ref(retrieval: RetrievalResult) -> Evidence:
    """A concrete, in-bounds citation into the consumer repo, from retrieval."""
    hit = next(h for h in retrieval.hits if h.repo == _CONSUMER)
    return Evidence(repo=hit.repo, path=hit.path, line=hit.line)


def _finding(**overrides) -> Finding:
    base = {
        "severity": "high",
        "category": "contract_break",
        "title": "Rename breaks a consumer",
        "rationale": "Another repository reads the field being renamed.",
        "evidence": [],
        "confidence": "high",
    }
    base.update(overrides)
    return Finding(**base)


# ---------------------------------------------------------------------------
# diff → changed lines
# ---------------------------------------------------------------------------


def test_changed_lines_collects_added_new_side_numbers() -> None:
    diff = (
        "diff --git a/src/x.ts b/src/x.ts\n"
        "--- a/src/x.ts\n"
        "+++ b/src/x.ts\n"
        "@@ -1,2 +1,3 @@\n"
        " const a = 1;\n"      # context: new line 1
        "+const b = 2;\n"      # added:   new line 2
        " const c = 3;\n"      # context: new line 3
    )
    assert changed_lines(diff) == {"src/x.ts": {2}}


def test_changed_lines_handles_deletions_and_dev_null() -> None:
    diff = (
        "diff --git a/gone.ts b/gone.ts\n"
        "--- a/gone.ts\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-const gone = 1;\n"
    )
    # A pure deletion to /dev/null names no post-image path.
    assert changed_lines(diff) == {}


def test_changed_lines_from_real_fixture_diff(pr) -> None:
    changed = changed_lines(pr.diff)
    assert changed, "the P1 rename must change at least one file"
    assert all(isinstance(lines, set) for lines in changed.values())


# ---------------------------------------------------------------------------
# evidence resolution
# ---------------------------------------------------------------------------


def test_evidence_resolves_for_a_real_in_bounds_line(retrieval, workspace) -> None:
    ref = _real_consumer_ref(retrieval)
    assert evidence_resolves(ref.repo, ref.path, ref.line, workspace)


def test_evidence_rejects_unknown_repo(workspace) -> None:
    assert not evidence_resolves("not-a-repo", "src/api/links.ts", 1, workspace)


def test_evidence_rejects_path_traversal(workspace) -> None:
    assert not evidence_resolves(_CONSUMER, "../../etc/passwd", 1, workspace)


def test_evidence_rejects_missing_file(workspace) -> None:
    assert not evidence_resolves(_CONSUMER, "src/does-not-exist.ts", 1, workspace)


def test_evidence_rejects_out_of_bounds_line(workspace) -> None:
    assert not evidence_resolves(_CONSUMER, "src/api/links.ts", 99_999, workspace)
    assert not evidence_resolves(_CONSUMER, "src/api/links.ts", 0, workspace)


# ---------------------------------------------------------------------------
# validator keep / discard rules
# ---------------------------------------------------------------------------


def test_valid_cross_repo_finding_is_kept(pr, workspace, retrieval) -> None:
    review = Review(
        summary="A contract break.",
        verdict="request_changes",
        findings=[_finding(evidence=[_real_consumer_ref(retrieval)])],
    )
    out = validate_review(review, pr, workspace)
    assert len(out.findings) == 1
    assert out.discard_count == 0
    assert out.verdict == "request_changes"


def test_finding_with_no_resolvable_evidence_is_discarded(pr, workspace) -> None:
    review = Review(
        summary="s",
        verdict="request_changes",
        findings=[_finding(evidence=[Evidence(repo=_CONSUMER, path="src/nope.ts", line=1)])],
    )
    out = validate_review(review, pr, workspace)
    assert out.findings == []
    assert out.discard_count == 1
    assert "resolve" in out.discarded[0].reason


def test_cross_repo_finding_citing_only_pr_repo_is_discarded(pr, workspace) -> None:
    # A real, in-bounds citation, but into the pull request's OWN repository.
    review = Review(
        summary="s",
        verdict="request_changes",
        findings=[
            _finding(
                category="contract_break",
                evidence=[Evidence(repo=_PR_REPO, path="src/types.ts", line=1)],
            )
        ],
    )
    out = validate_review(review, pr, workspace)
    assert out.findings == []
    assert "outside the pull request" in out.discarded[0].reason


def test_single_repo_finding_may_cite_the_pr_repo(pr, workspace) -> None:
    review = Review(
        summary="s",
        verdict="comment",
        findings=[
            _finding(
                category="single_repo",
                evidence=[Evidence(repo=_PR_REPO, path="src/types.ts", line=1)],
            )
        ],
    )
    out = validate_review(review, pr, workspace)
    assert len(out.findings) == 1


def test_supplied_pr_location_must_match_a_changed_file(pr, workspace, retrieval) -> None:
    review = Review(
        summary="s",
        verdict="request_changes",
        findings=[
            _finding(
                evidence=[_real_consumer_ref(retrieval)],
                pr_path="src/definitely-not-changed.ts",
                pr_line=1,
            )
        ],
    )
    out = validate_review(review, pr, workspace)
    assert out.findings == []
    assert "diff does not change" in out.discarded[0].reason


def test_supplied_pr_location_matching_a_changed_line_is_kept(pr, workspace, retrieval) -> None:
    changed = changed_lines(pr.diff)
    changed_path, changed_line = next(
        (p, next(iter(lines))) for p, lines in changed.items() if lines
    )
    review = Review(
        summary="s",
        verdict="request_changes",
        findings=[
            _finding(
                evidence=[_real_consumer_ref(retrieval)],
                pr_path=changed_path,
                pr_line=changed_line,
            )
        ],
    )
    out = validate_review(review, pr, workspace)
    assert len(out.findings) == 1


def test_secret_in_finding_text_discards_the_finding(pr, workspace, retrieval) -> None:
    review = Review(
        summary="clean summary",
        verdict="request_changes",
        findings=[
            _finding(
                rationale="leaked token sk-ant-api03-ABCDEFGHIJKLMNOPQRST here",
                evidence=[_real_consumer_ref(retrieval)],
            )
        ],
    )
    out = validate_review(review, pr, workspace)
    assert out.findings == []
    assert "secret" in out.discarded[0].reason


def test_secret_in_summary_is_redacted_not_leaked(pr, workspace) -> None:
    review = Review(
        summary="here is a key sk-ant-api03-ABCDEFGHIJKLMNOPQRST",
        verdict="comment",
        findings=[],
    )
    out = validate_review(review, pr, workspace)
    assert out.summary_redacted
    assert not contains_secret(out.summary)


def test_verdict_falls_back_to_neutral_when_all_findings_discarded(pr, workspace) -> None:
    review = Review(
        summary="s",
        verdict="request_changes",
        findings=[_finding(evidence=[Evidence(repo=_CONSUMER, path="src/nope.ts", line=1)])],
    )
    out = validate_review(review, pr, workspace)
    assert not out.has_findings
    # The host will not assert request_changes with nothing to back it.
    assert out.verdict == "comment"


# ---------------------------------------------------------------------------
# screening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_contains_secret_flags_credential_shapes(text: str) -> None:
    assert contains_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "The field target_url is renamed in src/types.ts:5",
        "acme-web/src/api/links.ts:2 destructures the response",
        "Coordinate the change with the consumer repository.",
    ],
)
def test_contains_secret_ignores_ordinary_review_prose(text: str) -> None:
    assert not contains_secret(text)
    assert is_clean(text, None)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _validated(findings=(), discarded=(), **overrides) -> ValidatedReview:
    base = {
        "verdict": "request_changes" if findings else "comment",
        "summary": "A summary.",
        "findings": list(findings),
        "discarded": list(discarded),
    }
    base.update(overrides)
    return ValidatedReview(**base)


def test_render_lists_kept_findings_with_reference_only_evidence(pr, retrieval) -> None:
    ref = _real_consumer_ref(retrieval)
    finding = _finding(evidence=[ref], recommendation="Coordinate the rename.")
    out = render_markdown(pr, _validated(findings=[finding]))
    assert f"{ref.repo}/{ref.path}:{ref.line}" in out
    assert "## Findings (1)" in out
    assert "Coordinate the rename." in out


def test_render_empty_result_is_an_explicit_no_impact_statement(pr) -> None:
    out = render_markdown(pr, _validated())
    assert "No supported cross-repository impact detected." in out


def test_render_reports_discard_count_and_reasons(pr) -> None:
    from panorama.validation import DiscardedFinding

    discarded = [DiscardedFinding(title="Bogus", category="contract_break", reason="no evidence")]
    out = render_markdown(pr, _validated(discarded=discarded))
    assert "1 finding discarded" in out
    assert "Bogus" in out and "no evidence" in out


def test_render_notes_retrieval_truncation(pr) -> None:
    out = render_markdown(pr, _validated(), retrieval_truncated=True)
    assert "truncated" in out.lower()


def test_render_never_emits_source_from_a_finding(pr, retrieval) -> None:
    # The evidence points at a real line; rendering must show the reference,
    # never the source text on that line.
    ref = _real_consumer_ref(retrieval)
    finding = _finding(evidence=[ref])
    out = render_markdown(pr, _validated(findings=[finding]))
    # The actual source around the cited line must never appear in the output.
    consumer_hit = next(
        h for h in retrieval.hits if h.repo == ref.repo and h.path == ref.path
    )
    for window_line in consumer_hit.window:
        # window entries look like "N: <source>"; the source part must not appear.
        payload = window_line.split(": ", 1)[-1].strip()
        if len(payload) > 8:  # ignore trivially short fragments
            assert payload not in out


def test_review_json_obj_is_reference_only_and_structured(pr, retrieval) -> None:
    ref = _real_consumer_ref(retrieval)
    finding = _finding(evidence=[ref])
    obj = review_json_obj(pr, _validated(findings=[finding]), retrieval_truncated=False)
    assert obj["repo"] == f"{pr.owner}/{pr.repo}"
    assert obj["verdict"] == "request_changes"
    assert obj["findings"][0]["evidence"][0] == {
        "repo": ref.repo,
        "path": ref.path,
        "line": ref.line,
    }
    assert obj["retrieval_truncated"] is False


# ---------------------------------------------------------------------------
# provenance: why each repository was examined
# ---------------------------------------------------------------------------


def test_render_explains_why_each_repository_was_examined(pr) -> None:
    """A reader's first question is "why that repository". A ranking nobody can
    interrogate is a ranking nobody should trust."""
    from panorama.retrieval import RepoRelevance

    ranked = [
        RepoRelevance(
            repo="consumer",
            score=0.03,
            signals=["thing"],
            provenance=["lexical #1: shares 1 identifier(s) with the diff: thing"],
        ),
        RepoRelevance(
            repo="standards",
            score=0.02,
            signals=[],
            provenance=["dependency #2: this repository depends on it (@x/y), directly"],
        ),
    ]
    out = render_markdown(pr, _validated(), ranked_repos=ranked)

    assert "## Repositories examined" in out
    assert "**consumer**" in out and "**standards**" in out
    assert "lexical #1" in out and "dependency #2" in out


def test_render_states_plainly_when_nothing_was_surfaced(pr) -> None:
    """Silence has to be explained rather than merely absent."""
    out = render_markdown(pr, _validated(), ranked_repos=[])
    assert "No sibling repository was surfaced" in out


def test_render_omits_the_section_entirely_when_not_given_a_ranking(pr) -> None:
    """The parameter is optional, so older callers render exactly as before."""
    assert "Repositories examined" not in render_markdown(pr, _validated())


def test_provenance_reaches_the_json_output(pr) -> None:
    from panorama.retrieval import RepoRelevance

    ranked = [RepoRelevance(repo="consumer", score=0.03, provenance=["lexical #1: why"])]
    obj = review_json_obj(pr, _validated(), ranked_repos=ranked)
    assert obj["examined"] == [
        {"repo": "consumer", "score": 0.03, "provenance": ["lexical #1: why"]}
    ]


def test_provenance_never_carries_source(pr) -> None:
    """It names repositories and reasons. It must not become a quoting channel."""
    from panorama.retrieval import RepoRelevance

    ranked = [RepoRelevance(repo="consumer", score=0.03, provenance=["lexical #1: why"])]
    out = render_markdown(pr, _validated(), ranked_repos=ranked)
    section = out.split("## Repositories examined", 1)[1]
    assert "```" not in section


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------


def _multi_file_diff(*files: tuple[str, str]) -> str:
    parts = []
    for path, added in files:
        parts.append(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{added}\n"
        )
    return "".join(parts)


def test_filter_diff_drops_generated_and_binary_sections() -> None:
    diff = (
        _multi_file_diff(("src/app.ts", "realChange"), ("package-lock.json", "lockNoise"))
        + "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    )
    filtered, dropped = filter_diff(diff)
    assert "realChange" in filtered
    assert "lockNoise" not in filtered and "logo.png" not in filtered
    assert set(dropped) == {"package-lock.json", "logo.png"}


def test_prepare_diff_caps_oversized_diffs_and_flags_truncation() -> None:
    big = "diff --git a/big.ts b/big.ts\n--- a/big.ts\n+++ b/big.ts\n@@ -1 +1 @@\n" + (
        "+x\n" * (MAX_DIFF_CHARS // 2)
    )
    text, truncated, _dropped = prepare_diff(big)
    assert truncated
    assert context_truncated(big)
    assert "truncated" in text.lower()
    assert len(text) <= MAX_DIFF_CHARS + 200


def test_prompt_omits_generated_files_and_notes_them(pr, retrieval, workspace) -> None:
    noisy = pr.model_copy(
        update={
            "diff": pr.diff
            + _multi_file_diff(("yarn.lock", "generatedTokenXYZ"))
        }
    )
    prompt = build_review_prompt(noisy, retrieval, workspace.org_map_markdown())
    assert "generatedTokenXYZ" not in prompt
    assert "omitted" in prompt.lower()


def test_prompt_places_rubric_first_and_reminder_last(pr, retrieval, workspace) -> None:
    prompt = build_review_prompt(pr, retrieval, workspace.org_map_markdown())
    assert prompt.startswith("You are a code reviewer")
    assert prompt.rstrip().endswith("report nothing you cannot support.")
    # The diff and a retrieval lead are both present as untrusted material.
    assert "PR DIFF" in prompt
    assert _CONSUMER in prompt


# ---------------------------------------------------------------------------
# end to end through the fake `claude` — the S5 exit criteria
# ---------------------------------------------------------------------------


@pytest.fixture
def make_review_runner(fake_bin: FakeClaude, workspace: Workspace, tmp_path: Path):
    """A runner bound to the fake CLI, scoped to the fixture workspace."""

    def factory(*, timeout_seconds: float = 30.0) -> ClaudeRunner:
        return ClaudeRunner(
            executable=str(fake_bin.path),
            allowed_workspace_roots=[workspace.root],
            artifact_root=tmp_path / "runs",
            timeout_seconds=timeout_seconds,
        )

    return factory


def test_e2e_structured_success(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    ref = _real_consumer_ref(retrieval)
    payload = sc.review(
        verdict="request_changes",
        findings=[sc.finding(evidence_refs=[sc.evidence(ref.repo, ref.path, ref.line)])],
    )
    fake_bin.set_scenario(sc.scenario(sc.structured(payload)))

    result = run_review(pr, workspace, retrieval, runner=make_review_runner())
    assert result.telemetry.attempts == 1

    validated = validate_review(result.data, pr, workspace)
    assert len(validated.findings) == 1
    out = render_markdown(pr, validated)
    assert f"{ref.repo}/{ref.path}:{ref.line}" in out
    assert_no_raw_output(out)


def test_e2e_malformed_then_repaired(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    # First invocation returns prose (no structured_output); the single repair
    # retry then returns a valid structured review.
    payload = sc.review()
    fake_bin.set_scenario(sc.scenario(sc.prose(), sc.structured(payload)))

    result = run_review(pr, workspace, retrieval, runner=make_review_runner())
    assert result.telemetry.attempts == 2
    assert fake_bin.call_count == 2


def test_e2e_invalid_citation_is_discarded(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    # The model returns a well-formed review whose evidence does not exist.
    payload = sc.review(
        verdict="request_changes",
        findings=[sc.finding(evidence_refs=[sc.evidence(_CONSUMER, "src/ghost.ts", 1)])],
    )
    fake_bin.set_scenario(sc.scenario(sc.structured(payload)))

    result = run_review(pr, workspace, retrieval, runner=make_review_runner())
    validated = validate_review(result.data, pr, workspace)
    assert validated.findings == []
    assert validated.discard_count == 1
    out = render_markdown(pr, validated)
    assert "No supported cross-repository impact detected." in out


def test_e2e_cli_error_raises_invocation_error(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    fake_bin.set_scenario(sc.scenario(sc.cli_error()))
    with pytest.raises(ClaudeInvocationError) as excinfo:
        run_review(pr, workspace, retrieval, runner=make_review_runner())
    assert_no_raw_output(str(excinfo.value))


def test_e2e_timeout_raises_invocation_error(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    fake_bin.set_scenario(sc.scenario(sc.hang(sleep=30)))
    with pytest.raises(ClaudeInvocationError) as excinfo:
        run_review(pr, workspace, retrieval, runner=make_review_runner(timeout_seconds=1.0))
    assert_no_raw_output(str(excinfo.value))


def test_e2e_empty_review_renders_no_impact(
    fake_bin, make_review_runner, pr, workspace, retrieval
) -> None:
    fake_bin.set_scenario(sc.scenario(sc.structured(sc.review(verdict="comment", findings=[]))))
    result = run_review(pr, workspace, retrieval, runner=make_review_runner())
    validated = validate_review(result.data, pr, workspace)
    out = render_markdown(pr, validated)
    assert "No supported cross-repository impact detected." in out
