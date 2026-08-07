"""Offline unit tests for local pull-request intake.

Uses real git (local, no network) to build tiny repositories and assert that
`LocalPullRequestSource` normalizes them into the shared `PullRequest` shape,
and that empty diffs and unknown refs are handled the way S2 requires.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panorama.cli import app
from panorama.errors import IntakeError
from panorama.fixtures import bootstrap
from panorama.intake import LocalPullRequestSource, resolve_local_repo

_FULL_SHA_LEN = 40


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """A repo at <org>/<svc> with main, a feature branch, and a no-op branch."""
    repo = tmp_path / "org" / "svc"
    repo.mkdir(parents=True)
    (repo / "a.txt").write_text("one\n")
    run_git(repo, "init", "-b", "main")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "base")

    run_git(repo, "checkout", "-b", "feature", "main")
    (repo / "a.txt").write_text("one\ntwo\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", "Add second line", "-m", "some body detail")

    # A branch that points at the same commit as main -> empty diff.
    run_git(repo, "checkout", "-b", "noop", "main")
    run_git(repo, "checkout", "main")
    return repo


def test_normalizes_local_branch(mini_repo: Path) -> None:
    pr = LocalPullRequestSource(mini_repo, "main", "feature").load()

    assert pr.owner == "org"
    assert pr.repo == "svc"
    assert pr.number is None
    assert pr.url is None
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature"
    assert len(pr.base_sha) == _FULL_SHA_LEN
    assert len(pr.head_sha) == _FULL_SHA_LEN
    assert pr.base_sha != pr.head_sha
    assert pr.title == "Add second line"
    assert pr.body == "some body detail"
    assert "+two" in pr.diff


def test_empty_diff_is_valid(mini_repo: Path) -> None:
    pr = LocalPullRequestSource(mini_repo, "main", "noop").load()
    assert pr.diff == ""
    assert pr.base_sha == pr.head_sha


def test_unknown_head_ref_raises(mini_repo: Path) -> None:
    with pytest.raises(IntakeError):
        LocalPullRequestSource(mini_repo, "main", "does-not-exist").load()


def test_unknown_base_ref_raises(mini_repo: Path) -> None:
    with pytest.raises(IntakeError):
        LocalPullRequestSource(mini_repo, "nope", "feature").load()


def test_missing_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(IntakeError):
        LocalPullRequestSource(tmp_path / "absent", "main", "feature").load()


def test_non_git_directory_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("hi\n")
    with pytest.raises(IntakeError):
        LocalPullRequestSource(plain, "main", "feature").load()


def test_resolve_local_repo_by_path(mini_repo: Path) -> None:
    assert resolve_local_repo(str(mini_repo)) == mini_repo


def test_resolve_local_repo_under_demo_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / ".panorama" / "demo-org"
    bootstrap(dest_root=dest)
    monkeypatch.chdir(tmp_path)
    assert resolve_local_repo("acme-api") == Path(".panorama") / "demo-org" / "acme-api"


def test_resolve_local_repo_unknown_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(IntakeError):
        resolve_local_repo("no-such-repo")


# -- the review command over a bootstrapped fixture ------------------------


@pytest.fixture
def demo_org(tmp_path: Path) -> Path:
    return bootstrap(dest_root=tmp_path / "demo-org").root


def test_review_local_json(demo_org: Path) -> None:
    repo = demo_org / "acme-api"
    result = CliRunner().invoke(
        app,
        ["review", "--local", str(repo), "--base", "main", "--head", "p1-rename", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo"] == "acme-api"
    assert payload["head_ref"] == "p1-rename"
    assert payload["base_ref"] == "main"
    assert len(payload["head_sha"]) == _FULL_SHA_LEN
    # The seeded rename must be present in the normalized diff.
    assert "target_url" in payload["diff"]


def test_review_local_summary_hides_diff_body(demo_org: Path) -> None:
    repo = demo_org / "acme-api"
    result = CliRunner().invoke(
        app, ["review", "--local", str(repo), "--head", "p1-rename"]
    )
    assert result.exit_code == 0, result.output
    assert "Local pull request" in result.output
    assert "p1-rename" in result.output
    # The default summary must not dump the diff body (hunk headers / +/- lines).
    assert "diff --git" not in result.output
    assert "+  target_url: string;" not in result.output


def test_review_requires_head(demo_org: Path) -> None:
    repo = demo_org / "acme-api"
    result = CliRunner().invoke(app, ["review", "--local", str(repo)])
    assert result.exit_code != 0


def test_review_github_intake_not_implemented() -> None:
    result = CliRunner().invoke(app, ["review", "acme/acme-api#1"])
    assert result.exit_code != 0
    assert "not implemented" in result.output.lower()
