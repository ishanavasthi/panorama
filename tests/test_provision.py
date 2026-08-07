"""Offline tests for multi-repository workspace provisioning (S8).

The network and `gh` are never touched. Instead, the organisation is a set of
**local bare git repositories** built from the bootstrapped fixtures, and the
provisioner's clone step is injected to clone from those local remotes with real
git. That exercises the whole machinery — clone, update, detached head-SHA
checkout, the size ceiling, and the workspace lock — end to end, and then runs a
full review over the provisioned workspace through the fake `claude`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from panorama.claude_runner import ClaudeRunner
from panorama.fixtures import bootstrap
from panorama.intake import LocalPullRequestSource, PullRequest
from panorama.provision import (
    MAX_ORG_REPOS,
    WorkspaceLock,
    WorkspaceProvisioner,
)
from panorama.render import render_markdown
from panorama.retrieval import retrieve
from panorama.review import run_review
from panorama.validation import validate_review
from panorama.workspace import WorkspaceError
from tests import fake_claude as sc
from tests.conftest import FakeClaude


def _git(*args: str) -> str:
    return subprocess.run(list(args), capture_output=True, text=True, check=True).stdout


# ---------------------------------------------------------------------------
# a local "organisation" of bare remotes, built from the fixtures
# ---------------------------------------------------------------------------


class LocalOrg:
    """Bare git remotes standing in for an organisation's GitHub repositories."""

    def __init__(self, remotes: dict[str, Path], source_root: Path) -> None:
        self.remotes = remotes
        self.source_root = source_root

    @property
    def names(self) -> list[str]:
        return sorted(self.remotes)

    def clone_repo(self, name: str, dest: Path) -> None:
        _git("git", "clone", str(self.remotes[name]), str(dest))

    def pull_request(
        self, repo: str, head: str, base: str = "main", number: int = 1
    ) -> PullRequest:
        local = LocalPullRequestSource(self.source_root / repo, base, head).load()
        return local.model_copy(
            update={
                "owner": "acme",
                "number": number,
                "url": f"https://github.com/acme/{repo}/pull/{number}",
            }
        )


@pytest.fixture
def org(tmp_path: Path) -> LocalOrg:
    source_root = bootstrap(dest_root=tmp_path / "demo-org").root
    remotes_dir = tmp_path / "remotes"
    remotes_dir.mkdir()
    remotes: dict[str, Path] = {}
    for child in sorted(source_root.iterdir()):
        if not (child / ".git").is_dir():
            continue
        bare = remotes_dir / f"{child.name}.git"
        _git("git", "clone", "--bare", str(child), str(bare))
        remotes[child.name] = bare
    return LocalOrg(remotes, source_root)


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


def _provisioner(org: LocalOrg, ws_root: Path, *, names=None) -> WorkspaceProvisioner:
    return WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=(lambda: names) if names is not None else (lambda: org.names),
        clone_repo=org.clone_repo,
    )


# ---------------------------------------------------------------------------
# provisioning
# ---------------------------------------------------------------------------


def test_provision_clones_all_repos_and_checks_out_head_sha(org: LocalOrg, ws_root: Path) -> None:
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as prov:
        workspace = prov.provision(pr)

        assert {r.name for r in workspace.repos()} == set(org.names)
        pr_repo = ws_root / "acme" / "acme-api"
        # The PR repo is detached at the exact reviewed commit...
        assert _git("git", "-C", str(pr_repo), "rev-parse", "HEAD").strip() == pr.head_sha
        # ...so its working tree shows the renamed field (the S5 gap, now closed).
        assert "target_url" in (pr_repo / "src" / "types.ts").read_text()
        # A sibling stays on its own default branch.
        assert (ws_root / "acme" / "acme-web").is_dir()


def test_provision_is_idempotent(org: LocalOrg, ws_root: Path) -> None:
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as prov:
        prov.provision(pr)
    # A second run updates existing clones in place rather than re-cloning.
    with _provisioner(org, ws_root) as prov:
        workspace = prov.provision(pr)
    pr_repo = ws_root / "acme" / "acme-api"
    assert _git("git", "-C", str(pr_repo), "rev-parse", "HEAD").strip() == pr.head_sha
    assert {r.name for r in workspace.repos()} == set(org.names)


def test_provision_rejects_oversized_orgs_before_cloning(org: LocalOrg, ws_root: Path) -> None:
    calls: list[str] = []

    def counting_clone(name: str, dest: Path) -> None:  # pragma: no cover - must not run
        calls.append(name)

    over = [f"repo-{i}" for i in range(MAX_ORG_REPOS + 1)]
    prov = WorkspaceProvisioner(
        "acme", root=ws_root, list_repos=lambda: over, clone_repo=counting_clone
    )
    pr = org.pull_request("acme-api", "p1-rename")
    with prov, pytest.raises(WorkspaceError, match="out of scope"):
        prov.provision(pr)
    assert calls == []  # nothing was cloned


def test_provision_fails_when_pr_repo_not_in_org(org: LocalOrg, ws_root: Path) -> None:
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root, names=["acme-web", "acme-shared"]) as prov:
        with pytest.raises(WorkspaceError, match="not among"):
            prov.provision(pr)


def test_provision_fails_when_head_commit_is_absent(org: LocalOrg, ws_root: Path) -> None:
    pr = org.pull_request("acme-api", "p1-rename").model_copy(update={"head_sha": "de" * 20})
    with _provisioner(org, ws_root) as prov:
        with pytest.raises(WorkspaceError, match="head commit is not available"):
            prov.provision(pr)


# ---------------------------------------------------------------------------
# gh listing path (parsing + ceiling), without a real gh
# ---------------------------------------------------------------------------


def test_gh_list_parses_names(monkeypatch: pytest.MonkeyPatch, ws_root: Path) -> None:
    prov = WorkspaceProvisioner("acme", root=ws_root)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: "acme-api\nacme-web\n\n")
    assert prov._org_repo_names() == ["acme-api", "acme-web"]


def test_gh_list_ceiling_is_enforced(monkeypatch: pytest.MonkeyPatch, ws_root: Path) -> None:
    prov = WorkspaceProvisioner("acme", root=ws_root)
    names = "\n".join(f"repo-{i}" for i in range(MAX_ORG_REPOS + 1))
    monkeypatch.setattr(prov, "_run", lambda *a, **k: names)
    with pytest.raises(WorkspaceError, match="out of scope"):
        prov._org_repo_names()


# ---------------------------------------------------------------------------
# the workspace lock
# ---------------------------------------------------------------------------


def test_lock_is_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "wp.lock"
    first = WorkspaceLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(WorkspaceError, match="holds the workspace lock"):
            WorkspaceLock(lock_path).acquire()
    finally:
        first.release()
    # Once released, it can be taken again.
    again = WorkspaceLock(lock_path)
    again.acquire()
    again.release()


def test_second_provisioner_cannot_mutate_a_locked_workspace(org: LocalOrg, ws_root: Path) -> None:
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as held:
        held.provision(pr)  # holds the lock for the duration of the `with`
        contender = _provisioner(org, ws_root)
        with pytest.raises(WorkspaceError, match="holds the workspace lock"):
            contender.provision(pr)


# ---------------------------------------------------------------------------
# a full review over a provisioned workspace — the S8 "reviews end to end" proof
# ---------------------------------------------------------------------------


def test_review_end_to_end_over_a_provisioned_workspace(
    org: LocalOrg, ws_root: Path, fake_bin: FakeClaude, tmp_path: Path
) -> None:
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as prov:
        workspace = prov.provision(pr)

        retrieval = retrieve(pr, workspace)
        # Cite a real line in a sibling clone, discovered by retrieval.
        hit = next(h for h in retrieval.hits if h.repo == "acme-web")
        payload = sc.review(
            verdict="request_changes",
            findings=[
                sc.finding(evidence_refs=[sc.evidence(hit.repo, hit.path, hit.line)])
            ],
        )
        fake_bin.set_scenario(sc.scenario(sc.structured(payload)))

        runner = ClaudeRunner(
            executable=str(fake_bin.path),
            allowed_workspace_roots=[workspace.root],
            artifact_root=tmp_path / "runs",
        )
        result = run_review(pr, workspace, retrieval, runner=runner)
        validated = validate_review(result.data, pr, workspace)

    assert len(validated.findings) == 1
    out = render_markdown(pr, validated)
    assert f"{hit.repo}/{hit.path}:{hit.line}" in out
