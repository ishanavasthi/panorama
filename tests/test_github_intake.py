"""Offline tests for GitHub pull-request intake (S7).

Two layers, both fully offline:

1. **Reference parsing** — pure-function tests of ``parse_pr_ref`` over the
   shorthand and URL forms, and its rejection of malformed input.

2. **Normalization through a fake `gh`** — a standalone fake ``gh`` from
   ``tests/fake_gh/`` answers the two REST reads the source makes, driven by a
   scenario sidecar. Asserts that a GitHub PR normalizes into the *same*
   ``PullRequest`` shape the local source produces, that forks and failures are
   handled, and that raw ``gh`` output never leaks into an error.

No network, no real ``gh``, no token.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from panorama.cli import app
from panorama.errors import IntakeError, PreflightError
from panorama.intake import GitHubPullRequestSource, PullRequest, parse_pr_ref
from tests import fake_gh as gh

FAKE_GH_SOURCE = Path(__file__).parent / "fake_gh" / "gh"


# ---------------------------------------------------------------------------
# reference parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("acme/acme-api#12", ("acme", "acme-api", 12)),
        ("  acme/acme-api#12  ", ("acme", "acme-api", 12)),
        ("https://github.com/acme/acme-api/pull/7", ("acme", "acme-api", 7)),
        ("https://github.com/acme/acme-api/pull/7/files", ("acme", "acme-api", 7)),
        ("http://github.com/acme/acme-api/pull/9?diff=split", ("acme", "acme-api", 9)),
        ("github.com/acme/acme-api/pull/5", ("acme", "acme-api", 5)),
        ("acme/acme-api.git#3", ("acme", "acme-api", 3)),
    ],
)
def test_parse_pr_ref_accepts_known_forms(ref: str, expected: tuple[str, str, int]) -> None:
    assert parse_pr_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "not-a-ref",
        "acme/acme-api",          # no number
        "acme/acme-api#",         # empty number
        "acme#12",                # no repo
        "https://github.com/acme/acme-api",       # no /pull/n
        "https://gitlab.com/acme/acme-api/pull/1",  # wrong host
    ],
)
def test_parse_pr_ref_rejects_malformed(ref: str) -> None:
    with pytest.raises(IntakeError):
        parse_pr_ref(ref)


# ---------------------------------------------------------------------------
# fake gh harness
# ---------------------------------------------------------------------------


class FakeGh:
    """A copied-out fake ``gh`` plus its scenario sidecar."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir
        self.path = bin_dir / "gh"
        self.sidecar = bin_dir / "gh_scenario.json"

    def set_scenario(self, body: dict) -> None:
        import json

        self.sidecar.write_text(json.dumps(body))


@pytest.fixture
def fake_gh(tmp_path: Path) -> FakeGh:
    bin_dir = tmp_path / "gh-bin"
    bin_dir.mkdir()
    target = bin_dir / "gh"
    shutil.copy2(FAKE_GH_SOURCE, target)
    target.chmod(0o755)
    return FakeGh(bin_dir)


def _source(fake_gh: FakeGh, owner="acme", repo="acme-api", number=1) -> GitHubPullRequestSource:
    return GitHubPullRequestSource(owner, repo, number, gh_path=str(fake_gh.path))


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------


def test_normalizes_into_the_shared_pull_request_shape(fake_gh: FakeGh) -> None:
    fake_gh.set_scenario(
        gh.scenario(
            meta=gh.pr_meta(
                number=12,
                title="Rename url to target_url",
                body="Contract change.",
                base_ref="main",
                head_ref="p1-rename",
                base_sha=gh.sha("a"),
                head_sha=gh.sha("b"),
            ),
            diff=gh.SAMPLE_DIFF,
        )
    )
    pr = _source(fake_gh, number=12).load()

    assert isinstance(pr, PullRequest)
    assert (pr.owner, pr.repo, pr.number) == ("acme", "acme-api", 12)
    assert pr.url == "https://github.com/acme/acme-api/pull/12"
    assert pr.base_ref == "main" and pr.head_ref == "p1-rename"
    assert pr.base_sha == gh.sha("a") and pr.head_sha == gh.sha("b")
    assert pr.title == "Rename url to target_url"
    assert "target_url" in pr.diff


def test_shape_parity_with_local_source(fake_gh: FakeGh) -> None:
    # Every field the local source fills, the GitHub source must fill too, plus
    # the two GitHub-only fields (number, url).
    fake_gh.set_scenario(gh.scenario(diff=gh.SAMPLE_DIFF))
    pr = _source(fake_gh).load()
    dumped = pr.model_dump()
    assert set(dumped) == set(PullRequest.model_fields)
    assert dumped["number"] is not None and dumped["url"] is not None


def test_fork_pull_request_still_normalizes(fake_gh: FakeGh) -> None:
    # A cross-repository (fork) PR: head lives in a different repo. The diff is
    # fetched from the REST object, so it resolves without cloning the fork.
    fake_gh.set_scenario(
        gh.scenario(
            meta=gh.pr_meta(head_full_name="contributor/acme-api-fork"),
            diff=gh.SAMPLE_DIFF,
        )
    )
    pr = _source(fake_gh).load()
    assert pr.repo == "acme-api"  # the base repo, where siblings live
    assert "target_url" in pr.diff


def test_empty_body_is_normalized_to_empty_string(fake_gh: FakeGh) -> None:
    meta = gh.pr_meta()
    meta["body"] = None  # GitHub returns null for an empty body
    fake_gh.set_scenario(gh.scenario(meta=meta, diff=gh.SAMPLE_DIFF))
    pr = _source(fake_gh).load()
    assert pr.body == ""


# ---------------------------------------------------------------------------
# failure handling
# ---------------------------------------------------------------------------


def test_gh_failure_raises_intake_error_without_leaking_stderr(fake_gh: FakeGh) -> None:
    secret = "gho_LEAKED_TOKEN_SHOULD_NOT_APPEAR"
    fake_gh.set_scenario(gh.scenario(meta_rc=1, stderr=f"error {secret}\n"))
    with pytest.raises(IntakeError) as excinfo:
        _source(fake_gh).load()
    assert secret not in str(excinfo.value)


def test_missing_gh_binary_raises_preflight_error() -> None:
    source = GitHubPullRequestSource("acme", "acme-api", 1, gh_path="/no/such/gh-binary")
    with pytest.raises(PreflightError):
        source.load()


def test_non_json_metadata_raises_intake_error(fake_gh: FakeGh) -> None:
    fake_gh.set_scenario(gh.scenario(meta_stdout="this is not json"))
    with pytest.raises(IntakeError):
        _source(fake_gh).load()


def test_missing_shas_raise_intake_error(fake_gh: FakeGh) -> None:
    meta = gh.pr_meta()
    meta["head"]["sha"] = "short"
    fake_gh.set_scenario(gh.scenario(meta=meta, diff=gh.SAMPLE_DIFF))
    with pytest.raises(IntakeError):
        _source(fake_gh).load()


# ---------------------------------------------------------------------------
# CLI wiring
#
# The heavy dependencies (real `gh`, cloning, the model) are tested elsewhere:
# GitHub intake above, provisioning + the pipeline-over-a-provisioned-workspace
# in test_provision.py, and the full local pipeline in test_review_pipeline.py.
# Here we test only the CLI's GitHub *branch*: parse -> intake -> provision ->
# pipeline -> emit, and that a provisioning failure surfaces with its exit code.
# ---------------------------------------------------------------------------


def test_cli_github_branch_renders_a_validated_review(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from panorama import cli
    from panorama.intake import PullRequest
    from panorama.models import Evidence, Finding
    from panorama.validation import ValidatedReview

    pr = PullRequest(
        owner="acme",
        repo="acme-api",
        number=7,
        url="https://github.com/acme/acme-api/pull/7",
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_ref="main",
        head_ref="feature",
        title="t",
        body="",
        diff="diff --git a/x b/x\n",
    )
    validated = ValidatedReview(
        verdict="request_changes",
        summary="A cross-repo break.",
        findings=[
            Finding(
                severity="high",
                category="contract_break",
                title="break",
                rationale="why",
                evidence=[Evidence(repo="acme-web", path="src/x.ts", line=1)],
                confidence="high",
            )
        ],
        discarded=[],
    )

    class _FakeSource:
        def __init__(self, owner, repo, number, **kw):
            pass

        def load(self):
            return pr

    class _FakeProvisioner:
        def __init__(self, owner, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        # V2.8: the provisioner records what it chose to clone, so the
        # report can state considered-versus-examined counts.
        selection = None

        def provision(self, _pr):
            return object()  # the workspace is unused: the pipeline is stubbed

    monkeypatch.setattr(cli, "GitHubPullRequestSource", _FakeSource)
    monkeypatch.setattr(cli, "WorkspaceProvisioner", _FakeProvisioner)
    monkeypatch.setattr(cli, "ClaudeRunner", lambda *a, **k: object())
    # Returns (validated, truncated, ranked_repos); the ranking feeds the
    # "Repositories examined" provenance section added in V2.7.
    monkeypatch.setattr(cli, "_run_pipeline", lambda *a, **k: (validated, False, []))

    result = CliRunner().invoke(app, ["review", "acme/acme-api#7", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo"] == "acme/acme-api"
    assert payload["findings"][0]["evidence"][0]["repo"] == "acme-web"


def test_cli_github_branch_surfaces_provisioning_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli
    from panorama.errors import EXIT_VALIDATION_ERROR
    from panorama.intake import PullRequest
    from panorama.workspace import WorkspaceError

    pr = PullRequest(
        owner="acme", repo="acme-api", number=7, url=None,
        base_sha="a" * 40, head_sha="b" * 40, base_ref="main", head_ref="f",
        title="t", body="", diff="",
    )

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def load(self):
            return pr

    class _FailingProvisioner:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def provision(self, _pr):
            raise WorkspaceError("the 'acme' organisation has more than 50 repositories")

    monkeypatch.setattr(cli, "GitHubPullRequestSource", _FakeSource)
    monkeypatch.setattr(cli, "WorkspaceProvisioner", _FailingProvisioner)

    result = CliRunner().invoke(app, ["review", "acme/acme-api#7"])
    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "more than 50" in result.output


def test_cli_rejects_both_local_and_github_ref() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["review", "acme/acme-api#1", "--local", "acme-api"])
    assert result.exit_code != 0


def test_cli_rejects_no_target() -> None:
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["review"])
    assert result.exit_code != 0
