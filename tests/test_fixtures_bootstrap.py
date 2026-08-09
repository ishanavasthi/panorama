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

import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panorama.cli import app
from panorama.fixtures import bootstrap
from panorama.fixtures.bootstrap import BootstrapError

# The mock organisation and its seeded PR branches. This is the expectation the
# graded core is built against; it lives in the test, never in production code.
#
# V2.2 grew this from four TypeScript-and-docs repositories to six across three
# languages. The two new ones consume the API over HTTP only, so nothing in any
# manifest records that they depend on it — which is the condition the
# structural retrieval channels have to cope with.
EXPECTED_REPOS = {
    "acme-api",
    "acme-web",
    "acme-shared",
    "acme-contracts",
    "acme-analytics",
    "acme-gateway",
}
SEEDED_BRANCHES = {
    "acme-api": {
        "p1-rename",
        "p3-endpoint-conventions",
        "p4-docs-cleanup",
        "remove-stats-endpoint",
        "expired-status-code",
        "drop-archived-status",
        "unversioned-exports-endpoint",
        "rename-private-helper",
        "bump-express",
    },
    "acme-web": {"p2-local-validator", "unsafe-cache-access"},
    "acme-shared": {"narrow-format-input"},
    "acme-contracts": set(),
    "acme-analytics": {
        "local-timestamp-format",
        "page-limit-constant",
        "reformat-report",
    },
    "acme-gateway": {
        "add-retry-helper",
        "local-time-cache-endpoint",
        "add-shortcode-tests",
    },
}

# One manifest per language, so the language packs in V2.3 and the dependency
# graph in V2.4 have all three to read.
EXPECTED_MANIFESTS = {
    "acme-api": "package.json",
    "acme-web": "package.json",
    "acme-shared": "package.json",
    "acme-contracts": "package.json",
    "acme-analytics": "pyproject.toml",
    "acme-gateway": "go.mod",
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


def test_builds_every_repo(built) -> None:
    assert {r.name for r in built.repos} == EXPECTED_REPOS
    for repo in built.repos:
        assert (repo.path / ".git").is_dir(), f"{repo.name} is not a git repo"


def test_every_repo_declares_itself_in_a_manifest(built) -> None:
    """V2.4 resolves dependency edges declared-name to declared-name.

    That only works if every repository actually ships the manifest its language
    uses. A repo without one is invisible to the dependency graph, and would be
    a silent hole in the corpus rather than a failing case.
    """
    for repo in built.repos:
        manifest = EXPECTED_MANIFESTS[repo.name]
        assert (repo.path / manifest).is_file(), f"{repo.name} has no {manifest}"


def test_seeded_branches_exist(built) -> None:
    for repo in built.repos:
        expected = SEEDED_BRANCHES[repo.name] | {"main"}
        assert branches(repo.path) == expected


def test_every_repo_rests_on_main(built) -> None:
    for repo in built.repos:
        assert current_branch(repo.path) == "main"


@pytest.mark.parametrize(
    ("repo_name", "branch"),
    sorted((repo, branch) for repo, bs in SEEDED_BRANCHES.items() for branch in bs),
)
def test_seeded_defect_visible_via_diff(built, repo_name: str, branch: str) -> None:
    """S1 exit criterion: each seeded change shows up in ``git diff``.

    A branch that produces an empty diff scores as a case retrieval found
    nothing for, which is indistinguishable from a retrieval failure — so an
    empty diff has to fail here, loudly, at the fixture layer.
    """
    repo = next(r for r in built.repos if r.name == repo_name)
    # `git diff --quiet` exits non-zero when there IS a difference.
    result = git(repo.path, "diff", "--quiet", "main", branch)
    assert result.returncode != 0, f"{repo_name}#{branch} produced an empty diff"


def test_negative_controls_change_no_public_surface(built) -> None:
    """The negative controls have to be *controls*, not just small changes.

    Each of these is a change a reviewer might plausibly comment on, and the
    corpus depends on none of them altering anything another repository could
    reference. Asserting the shape here keeps a later edit to the fixture data
    from quietly turning a control into a positive.
    """
    api = next(r for r in built.repos if r.name == "acme-api")

    # A private rename touches only the helper's own name, nowhere else. Read
    # the changed lines alone: a context line legitimately shows the exported
    # function the helper is called from.
    diff = git(api.path, "diff", "main", "rename-private-helper").stdout
    changed = [
        line
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert any("normalizeShortCode" in line for line in changed)
    assert any("canonicalizeShortCode" in line for line in changed)
    assert not any("export" in line for line in changed), (
        "the renamed helper must not be exported"
    )

    # A dependency bump touches the manifest and nothing else.
    files = git(
        api.path, "diff", "--name-only", "main", "bump-express"
    ).stdout.split()
    assert files == ["package.json"]

    # A formatting-only change adds and removes the same identifiers. Compared
    # on identifiers rather than whitespace-delimited chunks, because re-wrapping
    # a line legitimately moves punctuation around without touching a name.
    analytics = next(r for r in built.repos if r.name == "acme-analytics")
    reformat = git(analytics.path, "diff", "main", "reformat-report").stdout
    added, removed = set(), set()
    for line in reformat.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.update(re.findall(r"[A-Za-z_]\w*", line[1:]))
        elif line.startswith("-"):
            removed.update(re.findall(r"[A-Za-z_]\w*", line[1:]))
    assert added == removed, (
        "a formatting-only change must not introduce or drop an identifier"
    )


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


# Fixture-specific tokens that must never appear in production code: repo names,
# field names, branch names, and the symbols the corpus is labelled against.
# Extended at V2.2 with the vocabulary of the polyglot repositories and the new
# cases — the corpus grew, so the surface constraint #2 has to cover grew too.
#
# Every entry has to be a token no general-purpose reviewer would ever need.
# Generic words are deliberately absent: `archived` would flag `gh repo list
# --no-archived`, and a constraint test that cries wolf is a constraint test
# somebody eventually deletes.
FORBIDDEN_TOKENS = (
    "acme",
    "target_url",
    "validateurl",
    "p1-rename",
    "p2-local",
    "shortcode",
    "linkstatus",
    "max_links_per_page",
    "timestamp_format",
    "retrywithbackoff",
    "formattimestamp",
    "drop-archived-status",
    "unversioned-exports",
    "bump-express",
)

# Every production module, so a fixture token cannot hide in one nobody listed.
PRODUCTION_MODULES = (
    "panorama.cli",
    "panorama.cache",
    "panorama.channels",
    "panorama.config",
    "panorama.dependencies",
    "panorama.claude_runner",
    "panorama.languages",
    "panorama.delivery",
    "panorama.demo",
    "panorama.doctor",
    "panorama.errors",
    "panorama.fixtures.bootstrap",
    "panorama.intake",
    "panorama.models",
    "panorama.prompts",
    "panorama.provision",
    "panorama.render",
    "panorama.retrieval",
    "panorama.review",
    "panorama.screening",
    "panorama.validation",
    "panorama.workspace",
    "panorama.evaluation.cases",
    "panorama.evaluation.report",
    "panorama.evaluation.runner",
    "panorama.evaluation.scoring",
)


@pytest.mark.parametrize("module_name", PRODUCTION_MODULES)
def test_no_fixture_names_in_production_code(module_name: str) -> None:
    """Constraint #2: no fixture repo/field/branch names in production code.

    The risk this guards against gets worse, not better, as the corpus grows: a
    retrieval channel tuned against 18 labelled cases is one careless special
    case away from knowing the answers.
    """
    import importlib

    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text().lower()
    for token in FORBIDDEN_TOKENS:
        assert token not in source, (
            f"fixture-specific token {token!r} leaked into {module_name}"
        )


def test_cli_bootstrap_command(tmp_path: Path) -> None:
    dest = tmp_path / "demo-org"
    result = CliRunner().invoke(app, ["fixtures", "bootstrap", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_REPOS:
        assert name in result.output
