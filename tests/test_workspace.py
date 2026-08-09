"""Offline tests for the read-only workspace view and org-map generation.

Builds the fixtures with real git into a temp dir, then asserts the workspace
enumerates every repo at known SHAs, contains references safely, and derives an
org map from repository content with no fixture names in the code.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from panorama.fixtures import bootstrap
from panorama.workspace import Workspace, WorkspaceError

EXPECTED_REPOS = {
    "acme-api",
    "acme-web",
    "acme-shared",
    "acme-contracts",
    "acme-analytics",
    "acme-gateway",
}
_FULL_SHA_LEN = 40


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = bootstrap(dest_root=tmp_path / "demo-org").root
    return Workspace(root)


def git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_exposes_all_repos_at_known_shas(workspace: Workspace) -> None:
    repos = workspace.repos()
    assert {r.name for r in repos} == EXPECTED_REPOS
    for view in repos:
        assert len(view.head_sha) == _FULL_SHA_LEN
        # The exposed SHA is the repo's real HEAD.
        assert view.head_sha == git_head(view.path)


def test_ignores_non_git_children(tmp_path: Path) -> None:
    root = bootstrap(dest_root=tmp_path / "demo-org").root
    (root / "not-a-repo").mkdir()
    (root / "loose-file.txt").write_text("x\n")
    assert {r.name for r in Workspace(root).repos()} == EXPECTED_REPOS


def test_repo_lookup_and_unknown(workspace: Workspace) -> None:
    assert workspace.repo("acme-api").name == "acme-api"
    with pytest.raises(WorkspaceError):
        workspace.repo("no-such-repo")


def test_repo_name_with_separator_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        workspace.repo("../acme-web")


def test_resolve_within_accepts_inside_path(workspace: Workspace) -> None:
    resolved = workspace.resolve_within("acme-api", "src/types.ts")
    assert resolved.name == "types.ts"
    assert resolved.is_relative_to(workspace.repo("acme-api").path.resolve())


def test_resolve_within_rejects_traversal(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        workspace.resolve_within("acme-api", "../acme-web/src/render.ts")
    with pytest.raises(WorkspaceError):
        workspace.resolve_within("acme-api", "../../../../etc/passwd")


def test_org_map_lists_every_repo(workspace: Workspace) -> None:
    org_map = workspace.org_map_markdown()
    for name in EXPECTED_REPOS:
        assert f"## {name}" in org_map
    assert org_map.startswith("# Organisation map")
    assert f"{len(EXPECTED_REPOS)} repositories" in org_map


def test_org_map_discovers_convention_documents(workspace: Workspace) -> None:
    entries = {e.name: e for e in workspace.org_entries()}

    contracts = entries["acme-contracts"]
    assert "CONVENTIONS.md" in contracts.convention_docs
    assert "docs/error-envelope.md" in contracts.convention_docs
    assert "docs/timestamps.md" in contracts.convention_docs

    # A code repo with no org docs surfaces none, rather than guessing.
    assert entries["acme-shared"].convention_docs == []


def test_org_map_summaries_from_content(workspace: Workspace) -> None:
    entries = {e.name: e for e in workspace.org_entries()}
    # Description comes from package.json; summary from the README's first para.
    assert "link" in entries["acme-api"].description.lower()
    assert entries["acme-api"].readme_summary
    assert "package.json" in entries["acme-api"].top_level


def test_missing_workspace_raises(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError):
        Workspace(tmp_path / "absent").repos()


def test_no_fixture_names_in_workspace_code() -> None:
    module = importlib.import_module("panorama.workspace")
    source = Path(module.__file__).read_text().lower()
    for token in ("acme", "target_url", "validateurl", "p1-rename"):
        assert token not in source
