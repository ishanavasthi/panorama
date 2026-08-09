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
        # Mirrors production's blobless clone. Local remotes need the filter
        # capability turned on explicitly; GitHub has it by default.
        _git(
            "git",
            "clone",
            "--filter=blob:none",
            str(self.remotes[name]),
            str(dest),
        )

    def read_manifest(self, name: str):
        """Stand in for the `gh api` metadata phase, reading from the source tree.

        Injected in every test: without it the provisioner would reach for `gh`
        and hit the network, which these tests exist to avoid.
        """
        from panorama.languages import read_repo_manifest

        pack, facts = read_repo_manifest(self.source_root / name)
        return facts, (pack.name if pack else None)

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
        # Partial clone is a server capability; GitHub enables it, a bare repo
        # on disk does not until asked.
        _git("git", "-C", str(bare), "config", "uploadpack.allowfilter", "true")
        remotes[child.name] = bare
    return LocalOrg(remotes, source_root)


@pytest.fixture
def ws_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


def _provisioner(org: LocalOrg, ws_root: Path, *, names=None, **kw) -> WorkspaceProvisioner:
    return WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=(lambda: names) if names is not None else (lambda: org.names),
        clone_repo=org.clone_repo,
        read_manifest=org.read_manifest,
        **kw,
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


def test_an_unenumerable_listing_is_refused_before_cloning(
    org: LocalOrg, ws_root: Path
) -> None:
    """V1 refused organisations above fifty repositories; V2 replaced that
    ceiling with a clone budget. What remains is a bound on how much one
    listing command will enumerate, and it still fails before cloning."""
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


# ---------------------------------------------------------------------------
# V2.8: manifest-first selection, the clone budget, and blobless clones
# ---------------------------------------------------------------------------


def test_only_selected_repositories_are_cloned(org: LocalOrg, ws_root: Path) -> None:
    """The clone budget is the point: an organisation is no longer cloned whole."""
    cloned: list[str] = []

    def recording_clone(name: str, dest: Path) -> None:
        cloned.append(name)
        org.clone_repo(name, dest)

    prov = WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=lambda: org.names,
        clone_repo=recording_clone,
        read_manifest=org.read_manifest,
        budget=3,
    )
    with prov:
        workspace = prov.provision(org.pull_request("acme-api", "p1-rename"))

    assert len(cloned) == 3
    assert "acme-api" in cloned  # never dropped
    assert prov.selection.n_considered == len(org.names)
    assert prov.selection.n_selected == 3
    # The workspace view is restricted to what was selected, so a clone left
    # over from an earlier run is not silently searched.
    assert {r.name for r in workspace.repos()} == set(cloned)


def test_all_repos_restores_v1_behaviour(org: LocalOrg, ws_root: Path) -> None:
    cloned: list[str] = []

    def recording_clone(name: str, dest: Path) -> None:
        cloned.append(name)
        org.clone_repo(name, dest)

    prov = WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=lambda: org.names,
        clone_repo=recording_clone,
        read_manifest=org.read_manifest,
        budget=3,
        all_repos=True,
    )
    with prov:
        prov.provision(org.pull_request("acme-api", "p1-rename"))

    assert sorted(cloned) == org.names
    assert prov.selection.complete is True


def test_an_organisation_above_the_old_ceiling_is_reviewable(
    org: LocalOrg, ws_root: Path
) -> None:
    """V1 refused above fifty repositories. Now it reviews, cloning a budget.

    The organisation is padded with names that have no remote at all: if
    selection tried to clone them the test would fail loudly, which is exactly
    the assertion — only the selected few are ever touched.
    """
    padded = org.names + [f"unrelated-{i:03d}" for i in range(120)]
    cloned: list[str] = []

    def recording_clone(name: str, dest: Path) -> None:
        cloned.append(name)
        org.clone_repo(name, dest)  # KeyError if a padding name is selected

    prov = WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=lambda: padded,
        clone_repo=recording_clone,
        # The padding repositories have no manifest, which is ordinary.
        read_manifest=lambda name: org.read_manifest(name)
        if name in org.remotes
        else (__import__("panorama.languages", fromlist=["x"]).ManifestFacts(), None),
        budget=4,
    )
    with prov:
        prov.provision(org.pull_request("acme-api", "p1-rename"))

    assert len(padded) > 50
    assert len(cloned) == 4
    assert prov.selection.n_considered == len(padded)
    assert "not searched" in prov.selection.summary()


def test_a_blobless_clone_still_materialises_the_working_tree(
    org: LocalOrg, ws_root: Path
) -> None:
    """The point of blobless: history arrives thin, the checkout is complete.

    Retrieval greps the working tree, so if the tree were not materialised the
    whole approach would silently find nothing.
    """
    with _provisioner(org, ws_root) as prov:
        prov.provision(org.pull_request("acme-api", "p1-rename"))

    sibling = ws_root / "acme" / "acme-web"
    assert (sibling / "src" / "api" / "links.ts").read_text().strip()
    # And it really is a partial clone, not a full one.
    filters = _git("git", "-C", str(sibling), "config", "--get-all", "remote.origin.promisor")
    assert filters.strip() == "true"


def test_the_default_clone_command_asks_for_a_blobless_clone(ws_root: Path) -> None:
    """The injected cloner bypasses production's command, so assert it directly."""
    recorded: list[tuple[str, ...]] = []

    prov = WorkspaceProvisioner("acme", root=ws_root)
    prov._run = lambda *argv, what: recorded.append(argv) or ""  # type: ignore[assignment]
    prov._clone("some-repo", ws_root / "some-repo")

    assert recorded, "no clone command was issued"
    assert "--filter=blob:none" in recorded[0]


def test_a_repository_without_any_manifest_is_not_an_error(
    org: LocalOrg, ws_root: Path
) -> None:
    from panorama.languages import ManifestFacts

    prov = WorkspaceProvisioner(
        "acme",
        root=ws_root,
        list_repos=lambda: org.names,
        clone_repo=org.clone_repo,
        read_manifest=lambda _name: (ManifestFacts(), None),
        budget=3,
    )
    with prov:
        workspace = prov.provision(org.pull_request("acme-api", "p1-rename"))
    assert "acme-api" in {r.name for r in workspace.repos()}


def test_a_second_run_updates_the_sibling_working_tree(
    org: LocalOrg, ws_root: Path
) -> None:
    """Fetching alone leaves the checkout where it was, and retrieval greps the
    *working tree*.

    Without resetting to the remote's default branch, every review after the
    first would silently search whatever a sibling looked like on the day it was
    first cloned — a review that is not wrong so much as answering a question
    about the past. Found by running against a real organisation, where a
    sibling's checkout was two days stale and a manifest added since had simply
    never arrived.
    """
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as prov:
        prov.provision(pr)

    sibling = ws_root / "acme" / "acme-web"
    assert not (sibling / "NEW-FILE.md").exists()

    # Somebody pushes to the sibling's default branch.
    source = org.source_root / "acme-web"
    (source / "NEW-FILE.md").write_text("added upstream\n")
    _git("git", "-C", str(source), "add", "-A")
    _git(
        "git", "-C", str(source),
        "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-m", "upstream change",
    )
    _git("git", "-C", str(source), "push", str(org.remotes["acme-web"]), "main")

    with _provisioner(org, ws_root) as prov:
        prov.provision(pr)

    assert (sibling / "NEW-FILE.md").is_file(), (
        "the sibling checkout was fetched but never updated"
    )


def test_updating_survives_a_missing_remote_head(org: LocalOrg, ws_root: Path) -> None:
    """An older clone may have no `refs/remotes/origin/HEAD`; it is re-derived
    from the remote rather than guessed from a list of popular branch names."""
    pr = org.pull_request("acme-api", "p1-rename")
    with _provisioner(org, ws_root) as prov:
        prov.provision(pr)

    sibling = ws_root / "acme" / "acme-web"
    subprocess.run(
        ["git", "-C", str(sibling), "symbolic-ref", "-d", "refs/remotes/origin/HEAD"],
        capture_output=True,
    )

    with _provisioner(org, ws_root) as prov:
        workspace = prov.provision(pr)

    assert "acme-web" in {r.name for r in workspace.repos()}
