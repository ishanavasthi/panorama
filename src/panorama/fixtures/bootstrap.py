"""Materialise the checked-in fixture data into real git repositories.

``panorama fixtures bootstrap`` turns the plain source files under this
package's ``data/demo-org`` tree into working git repositories under
``.panorama/demo-org/``. Each repository gets a ``main`` branch plus one branch
per seeded pull request, so a reviewer can inspect a seeded defect with nothing
more than ``git diff`` — no network, no ``gh``, no cloning.

This module is deliberately GENERIC. It contains no fixture repository names,
field names, PR numbers, branch names, or expected findings. Everything
specific lives in the data tree, which is plain data rather than review logic
(hard constraint #2). The layout it consumes::

    data/demo-org/
      <repo>/
        base/                 files committed to `main`
        branches/
          <branch>/
            files/            full-content files added or replaced on the branch
            DELETE            optional: newline-separated repo-relative paths to remove
            pr.json           optional: {"title", "body"}; title is the commit subject

A branch is materialised as a real git branch cut from ``main`` with the
overlay applied, so ``git diff main <branch>`` shows exactly the seeded change.
No nested ``.git`` is ever committed to the outer repository: the output lives
under ``.panorama/``, which is gitignored.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from panorama.config import DEMO_ORG_ROOT, ensure_dir
from panorama.errors import PanoramaError

#: Root of the checked-in fixture data, resolved relative to this module so it
#: works regardless of the current working directory.
DATA_ROOT = Path(__file__).resolve().parent / "data" / "demo-org"

#: Generic committer identity for fixture history. Not fixture-specific data —
#: just a stable author so commits are reproducible across machines.
_GIT_AUTHOR_NAME = "Panorama Fixtures"
_GIT_AUTHOR_EMAIL = "fixtures@panorama.local"

#: The branch every repository starts on and every seeded branch is cut from.
_MAIN_BRANCH = "main"


class BootstrapError(PanoramaError):
    """Raised when the fixture organisation cannot be built (e.g. git missing,
    a target already exists without ``--force``, or the data tree is malformed).
    """


@dataclass
class RepoResult:
    """What was built for a single fixture repository."""

    name: str
    path: Path
    branches: list[str] = field(default_factory=list)


@dataclass
class BootstrapResult:
    """The outcome of one bootstrap run."""

    root: Path
    repos: list[RepoResult] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside ``repo`` and return its stdout.

    Commits carry a fixed author/committer identity and disable GPG signing so
    the fixture history is reproducible and never blocks on a signing prompt.
    """
    cmd = [
        "git",
        "-c",
        f"user.name={_GIT_AUTHOR_NAME}",
        "-c",
        f"user.email={_GIT_AUTHOR_EMAIL}",
        "-c",
        "commit.gpgsign=false",
        "-C",
        str(repo),
        *args,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # git not installed
        raise BootstrapError(
            "git was not found on PATH; install git to build the fixtures."
        ) from exc
    except subprocess.CalledProcessError as exc:
        # git's own stderr is safe to surface: it is our fixture data, not
        # private repo content or credentials.
        detail = (exc.stderr or exc.stdout or "").strip()
        raise BootstrapError(f"git {' '.join(args)} failed: {detail}") from exc
    return completed.stdout


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy every file under ``src`` into ``dest``, creating parents as needed."""
    for source in sorted(src.rglob("*")):
        if source.is_dir():
            continue
        relative = source.relative_to(src)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _branch_commit_subject(branch_dir: Path, branch_name: str) -> str:
    """Commit subject for a seeded branch: its ``pr.json`` title if present."""
    pr_file = branch_dir / "pr.json"
    if pr_file.is_file():
        try:
            data = json.loads(pr_file.read_text())
        except ValueError as exc:
            raise BootstrapError(f"{pr_file} is not valid JSON: {exc}") from exc
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return f"Seed branch {branch_name}"


def _apply_overlay(branch_dir: Path, repo_dest: Path) -> None:
    """Apply one branch overlay onto an already-checked-out ``main`` tree."""
    files_dir = branch_dir / "files"
    if files_dir.is_dir():
        _copy_tree(files_dir, repo_dest)

    delete_file = branch_dir / "DELETE"
    if delete_file.is_file():
        for raw in delete_file.read_text().splitlines():
            rel = raw.strip()
            if not rel or rel.startswith("#"):
                continue
            target = (repo_dest / rel).resolve()
            # Never let a DELETE line escape the repository it belongs to.
            if repo_dest.resolve() not in target.parents:
                raise BootstrapError(
                    f"DELETE entry {rel!r} escapes repository {repo_dest.name}"
                )
            if target.exists():
                target.unlink()


def _build_repo(repo_src: Path, repo_dest: Path, *, force: bool) -> RepoResult:
    """Build one fixture repository with its ``main`` and seeded branches."""
    base_dir = repo_src / "base"
    if not base_dir.is_dir():
        raise BootstrapError(
            f"fixture repo {repo_src.name!r} has no base/ directory"
        )

    if repo_dest.exists():
        # Only reclaim something that is clearly a prior bootstrap (or when the
        # caller explicitly forces it). Refuse to delete anything else.
        if force or (repo_dest / ".git").is_dir():
            shutil.rmtree(repo_dest)
        else:
            raise BootstrapError(
                f"{repo_dest} already exists and is not a fixture checkout; "
                "pass force=True (--force) to overwrite it."
            )

    ensure_dir(repo_dest)
    _copy_tree(base_dir, repo_dest)

    _git(repo_dest, "init", "-b", _MAIN_BRANCH)
    _git(repo_dest, "add", "-A")
    _git(repo_dest, "commit", "-m", "Initial commit")

    result = RepoResult(name=repo_src.name, path=repo_dest, branches=[_MAIN_BRANCH])

    branches_dir = repo_src / "branches"
    if branches_dir.is_dir():
        for branch_dir in sorted(p for p in branches_dir.iterdir() if p.is_dir()):
            branch_name = branch_dir.name
            # Cut a fresh branch from a clean main, then apply the overlay.
            _git(repo_dest, "checkout", "-B", branch_name, _MAIN_BRANCH)
            _apply_overlay(branch_dir, repo_dest)
            _git(repo_dest, "add", "-A")
            _git(
                repo_dest,
                "commit",
                "-m",
                _branch_commit_subject(branch_dir, branch_name),
            )
            result.branches.append(branch_name)

    # Leave every repository resting on main.
    _git(repo_dest, "checkout", _MAIN_BRANCH)
    return result


def bootstrap(
    dest_root: Path | None = None,
    *,
    data_root: Path | None = None,
    force: bool = False,
) -> BootstrapResult:
    """Build the fixture organisation as git repositories.

    ``dest_root`` defaults to ``.panorama/demo-org`` under the current working
    directory. ``data_root`` defaults to the checked-in fixture tree and is a
    seam for tests. With ``force``, an existing target is rebuilt even if it is
    not recognisably a prior bootstrap.
    """
    source = data_root or DATA_ROOT
    if not source.is_dir():
        raise BootstrapError(f"fixture data directory not found: {source}")

    dest = dest_root or DEMO_ORG_ROOT
    ensure_dir(dest)

    # Dot-directories are never fixture repositories. Without this, anything
    # that lands in the data tree — an editor's cache, or a `.panorama/` created
    # by running a command from the wrong directory — is read as a fixture repo
    # and fails with a confusing "has no base/ directory" instead of being
    # ignored.
    repo_sources = sorted(
        p for p in source.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not repo_sources:
        raise BootstrapError(f"no fixture repositories found under {source}")

    result = BootstrapResult(root=dest)
    for repo_src in repo_sources:
        repo_result = _build_repo(repo_src, dest / repo_src.name, force=force)
        result.repos.append(repo_result)
    return result
