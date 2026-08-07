"""Offline unit tests for local pull-request intake.

Uses real git (local, no network) to build tiny repositories and assert that
`LocalPullRequestSource` normalizes them into the shared `PullRequest` shape,
and that empty diffs and unknown refs are handled the way S2 requires.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panorama.cli import app
from panorama.errors import IntakeError
from panorama.fixtures import bootstrap
from panorama.intake import LocalPullRequestSource, resolve_local_repo
from tests import fake_claude as sc
from tests.conftest import FAKE_SOURCE_DIR

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


@pytest.fixture
def stub_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Put ONLY a fake `claude` on PATH and drive it from a scenario.

    Deliberately does not use the whole fake bin: its fake `git` would shadow
    the real git the review pipeline needs for intake and retrieval. A dir
    holding just `claude` is prepended, so `ClaudeRunner(executable="claude")`
    finds the fake while every real tool stays resolvable behind it. The cwd is
    moved to a throwaway dir so run artifacts never land in the repository.
    """
    bin_dir = tmp_path / "claude-only-bin"
    bin_dir.mkdir()
    target = bin_dir / "claude"
    shutil.copy2(FAKE_SOURCE_DIR / "claude", target)
    target.chmod(0o755)
    scenario_file = bin_dir / "scenario.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    def set_scenario(body: dict) -> None:
        scenario_file.write_text(json.dumps(body))

    return set_scenario


def test_review_local_json(demo_org: Path, stub_claude) -> None:
    repo = demo_org / "acme-api"
    # A well-formed review citing a real line in a sibling repository.
    payload = sc.review(
        verdict="request_changes",
        findings=[
            sc.finding(evidence_refs=[sc.evidence("acme-web", "src/api/links.ts", 1)])
        ],
    )
    stub_claude(sc.scenario(sc.structured(payload)))

    result = CliRunner().invoke(
        app,
        ["review", "--local", str(repo), "--base", "main", "--head", "p1-rename", "--json"],
    )
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert obj["repo"] == "demo-org/acme-api"
    assert obj["head"]["ref"] == "p1-rename"
    assert obj["base"]["ref"] == "main"
    assert len(obj["head"]["sha"]) == _FULL_SHA_LEN
    assert obj["verdict"] == "request_changes"
    assert obj["findings"][0]["evidence"][0] == {
        "repo": "acme-web",
        "path": "src/api/links.ts",
        "line": 1,
    }


def test_review_local_markdown_is_reference_only(demo_org: Path, stub_claude) -> None:
    repo = demo_org / "acme-api"
    payload = sc.review(
        verdict="request_changes",
        findings=[
            sc.finding(evidence_refs=[sc.evidence("acme-web", "src/api/links.ts", 1)])
        ],
    )
    stub_claude(sc.scenario(sc.structured(payload)))

    result = CliRunner().invoke(
        app, ["review", "--local", str(repo), "--head", "p1-rename"]
    )
    assert result.exit_code == 0, result.output
    assert "Panorama review" in result.output
    assert "acme-web/src/api/links.ts:1" in result.output
    # The report must not dump the diff body (hunk headers / +/- lines).
    assert "diff --git" not in result.output
    assert "+  target_url: string;" not in result.output


def test_review_local_empty_review_is_explicit(demo_org: Path, stub_claude) -> None:
    repo = demo_org / "acme-api"
    stub_claude(sc.scenario(sc.structured(sc.review(verdict="comment", findings=[]))))

    result = CliRunner().invoke(
        app, ["review", "--local", str(repo), "--head", "p4-docs-cleanup"]
    )
    assert result.exit_code == 0, result.output
    assert "No supported cross-repository impact detected." in result.output


def test_review_requires_head(demo_org: Path) -> None:
    repo = demo_org / "acme-api"
    result = CliRunner().invoke(app, ["review", "--local", str(repo)])
    assert result.exit_code != 0


def test_review_malformed_github_ref_is_rejected_offline() -> None:
    # A ref that cannot be parsed fails before `gh` is ever invoked, so this
    # needs no network and no fake gh. (Well-formed GitHub intake is covered in
    # test_github_intake.py.)
    result = CliRunner().invoke(app, ["review", "definitely-not-a-pr-ref"])
    assert result.exit_code != 0
    assert "parse" in result.output.lower()
