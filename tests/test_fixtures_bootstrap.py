"""Offline integration tests for the fixture organisation bootstrap.

These use REAL git (installed locally, no network) to build the checked-in
fixture data into git repositories under a temp directory, and assert the
seeded defects are visible through ``git diff`` — the S1 exit criterion.

Fixture repo/branch names appear here on purpose: test expectations are not
review logic (hard constraint #2), so they are allowed to be specific. The
production code under test must NOT contain them, which ``test_no_fixture_
names_in_production_code`` checks directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panorama.cli import app
from panorama.fixtures import bootstrap
from panorama.fixtures.bootstrap import BootstrapError

# The mock organisation and its seeded PR branches. This is the expectation the
# graded core is built against; it lives in the test, never in production code.
EXPECTED_REPOS = {"acme-api", "acme-web", "acme-shared", "acme-contracts"}
SEEDED_BRANCHES = {
    "acme-api": {"p1-rename", "p3-endpoint-conventions", "p4-docs-cleanup"},
    "acme-web": {"p2-local-validator"},
    "acme-shared": set(),
    "acme-contracts": set(),
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def branches(repo: Path) -> set[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


@pytest.fixture
def built(tmp_path: Path):
    """Bootstrap the fixtures into a throwaway directory once per test."""
    dest = tmp_path / "demo-org"
    return bootstrap(dest_root=dest)


def test_builds_all_four_repos(built) -> None:
    assert {r.name for r in built.repos} == EXPECTED_REPOS
    for repo in built.repos:
        assert (repo.path / ".git").is_dir(), f"{repo.name} is not a git repo"


def test_seeded_branches_exist(built) -> None:
    for repo in built.repos:
        expected = SEEDED_BRANCHES[repo.name] | {"main"}
        assert branches(repo.path) == expected


def test_every_repo_rests_on_main(built) -> None:
    for repo in built.repos:
        assert current_branch(repo.path) == "main"


@pytest.mark.parametrize(
    ("repo_name", "branch"),
    [
        ("acme-api", "p1-rename"),
        ("acme-api", "p3-endpoint-conventions"),
        ("acme-api", "p4-docs-cleanup"),
        ("acme-web", "p2-local-validator"),
    ],
)
def test_seeded_defect_visible_via_diff(built, repo_name: str, branch: str) -> None:
    """S1 exit criterion: each seeded defect shows up in ``git diff``."""
    repo = next(r for r in built.repos if r.name == repo_name)
    # `git diff --quiet` exits non-zero when there IS a difference.
    result = git(repo.path, "diff", "--quiet", "main", branch)
    assert result.returncode != 0, f"{repo_name}#{branch} produced an empty diff"


def test_p1_renames_response_field(built) -> None:
    repo = next(r for r in built.repos if r.name == "acme-api")
    diff = git(repo.path, "diff", "main", "p1-rename").stdout
    assert "-  url: string;" in diff
    assert "+  target_url: string;" in diff


def test_idempotent_rerun_reclaims_prior_bootstrap(tmp_path: Path) -> None:
    dest = tmp_path / "demo-org"
    first = bootstrap(dest_root=dest)
    # A second run without --force must succeed by reclaiming the git checkouts.
    second = bootstrap(dest_root=dest)
    assert {r.name for r in first.repos} == {r.name for r in second.repos}
    for repo in second.repos:
        assert branches(repo.path) == SEEDED_BRANCHES[repo.name] | {"main"}


def test_refuses_to_overwrite_foreign_directory(tmp_path: Path) -> None:
    dest = tmp_path / "demo-org"
    # Pre-create a non-git directory where a repo would land.
    (dest / "acme-api").mkdir(parents=True)
    (dest / "acme-api" / "important.txt").write_text("not ours to delete\n")
    with pytest.raises(BootstrapError):
        bootstrap(dest_root=dest)
    # The foreign file must be untouched.
    assert (dest / "acme-api" / "important.txt").read_text() == "not ours to delete\n"


def test_force_overwrites_foreign_directory(tmp_path: Path) -> None:
    dest = tmp_path / "demo-org"
    (dest / "acme-api").mkdir(parents=True)
    (dest / "acme-api" / "important.txt").write_text("clobber me\n")
    result = bootstrap(dest_root=dest, force=True)
    assert {r.name for r in result.repos} == EXPECTED_REPOS
    assert not (dest / "acme-api" / "important.txt").exists()


def test_no_fixture_names_in_production_code() -> None:
    """Constraint #2: no fixture repo/field/branch names in production code."""
    import importlib

    forbidden = ("acme", "target_url", "validateurl", "p1-rename", "p2-local")
    for name in ("panorama.cli", "panorama.fixtures.bootstrap"):
        module = importlib.import_module(name)
        source = Path(module.__file__).read_text().lower()
        for token in forbidden:
            assert token not in source, (
                f"fixture-specific token {token!r} leaked into {name}"
            )


def test_cli_bootstrap_command(tmp_path: Path) -> None:
    dest = tmp_path / "demo-org"
    result = CliRunner().invoke(app, ["fixtures", "bootstrap", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_REPOS:
        assert name in result.output
