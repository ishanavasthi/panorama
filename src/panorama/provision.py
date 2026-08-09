"""Provision a real multi-repository workspace for a GitHub pull request.

S3 built a read-only :class:`~panorama.workspace.Workspace` over a directory of
git repositories; the local pipeline points it at the bootstrapped fixtures.
This module builds that directory for a *GitHub* pull request: it lists the
organisation's repositories, clones or updates them under
``~/.panorama/workspaces/<owner>/`` at ``0700``, and checks the pull request's
own repository out — detached — at the immutable head SHA the review is pinned
to. The result is the same :class:`Workspace` the rest of the pipeline already
consumes, so retrieval, review, validation and rendering are unchanged.

Hard boundaries this module respects:

- **`gh` owns the GitHub credential.** Cloning private repositories goes through
  ``gh repo clone``, which handles auth; Panorama never reads, stores, prints, or
  places a token in a URL. Raw ``gh``/``git`` stderr is never surfaced.
- **Small organisations only.** Above a fixed ceiling the provisioner fails
  *before* cloning anything, rather than degrading.
- **No side effects beyond a plain checkout.** No submodules, no fetching LFS
  objects, no running a project's own commands — a repository is untrusted data.
- **One writer at a time.** An advisory lock on the owner directory means two
  runs can never mutate the same checkout concurrently.

The ``gh``-backed listing and cloning are injectable so the machinery can be
tested offline against local git remotes with no network and no ``gh``.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from panorama.cache import Cache
from panorama.config import WORKSPACE_ROOT, ensure_dir
from panorama.errors import PreflightError
from panorama.intake import PullRequest
from panorama.languages import LANGUAGE_PACKS, ManifestFacts
from panorama.selection import (
    DEFAULT_CLONE_BUDGET,
    MAX_LISTED_REPOS,
    Selection,
    select_repositories,
)
from panorama.symbols import diff_symbols
from panorama.workspace import Workspace, WorkspaceError

#: V1 refused organisations above fifty repositories. V2 replaces that ceiling
#: with a clone budget, so this is no longer a limit on what is reviewable —
#: only a bound on how much one listing command will enumerate.
MAX_ORG_REPOS = MAX_LISTED_REPOS

#: Name of the advisory lock file placed inside each owner directory.
_LOCK_NAME = ".panorama.lock"

#: Clone history without file contents; the working tree is materialised on
#: checkout and blobs are fetched on demand. A review reads the checked-out
#: tree, not history, so this removes work nothing was going to use.
_BLOBLESS = "--filter=blob:none"

RepoLister = Callable[[], list[str]]
RepoCloner = Callable[[str, Path], None]
#: Reads one repository's manifest without cloning it. Returns the facts and the
#: language pack that read them, or empty facts when the repository has none.
ManifestReader = Callable[[str], tuple[ManifestFacts, str | None]]


class WorkspaceLock:
    """An advisory, non-blocking exclusive lock on a single file.

    Backed by ``flock`` on a dedicated lock file. A second acquisition — in this
    process or another — fails immediately with a clear error rather than
    blocking, so a stuck or concurrent run is reported, not waited on.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WorkspaceError(
                f"another Panorama run holds the workspace lock at {self.path}. "
                "Wait for it to finish, or remove the lock file if it is stale."
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class WorkspaceProvisioner:
    """Build (and hold a lock on) a workspace for one GitHub pull request.

    Use as a context manager so the lock is released when the review is done::

        with WorkspaceProvisioner(owner) as prov:
            workspace = prov.provision(pr)
            # ... retrieve / review over `workspace` ...
    """

    def __init__(
        self,
        owner: str,
        *,
        root: Path | None = None,
        list_repos: RepoLister | None = None,
        clone_repo: RepoCloner | None = None,
        read_manifest: ManifestReader | None = None,
        budget: int = DEFAULT_CLONE_BUDGET,
        all_repos: bool = False,
        cache: Cache | None = None,
        gh_path: str = "gh",
        git_path: str = "git",
    ) -> None:
        self.owner = owner
        self.root = Path(root) if root is not None else WORKSPACE_ROOT
        self.owner_dir = self.root / owner
        self._list_repos = list_repos
        self._clone_repo = clone_repo
        self._read_manifest = read_manifest
        self.budget = budget
        self.all_repos = all_repos
        self.cache = cache
        self.gh_path = gh_path
        self.git_path = git_path
        self._lock = WorkspaceLock(self.owner_dir / _LOCK_NAME)
        self.selection: Selection | None = None
        """What the last `provision` chose, for the report. A review that looked
        at 12 of 140 repositories has to be able to say so."""

    # -- context management -------------------------------------------------

    def __enter__(self) -> WorkspaceProvisioner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._lock.release()

    # -- the one public operation ------------------------------------------

    def provision(self, pr: PullRequest) -> Workspace:
        """Materialise the workspace and return a read-only view over it.

        Two phases. First the organisation is listed and every repository's
        manifest is read **without cloning anything**, which is cheap enough for
        hundreds of repositories. Then the candidates are ranked and only the
        top few are cloned, blobless.

        Idempotent: re-running updates existing clones in place. Repositories
        cloned by a previous run but not selected this time are left alone —
        deleting them would throw away work that the next review may want, and
        they are not searched unless selected.
        """
        ensure_dir(self.owner_dir)  # 0700 all the way down
        self._lock.acquire()

        names = self._org_repo_names()
        if pr.repo not in names:
            raise WorkspaceError(
                f"pull request repository {pr.repo!r} is not among the "
                f"{self.owner!r} organisation repositories that were listed."
            )

        # -- phase 1: metadata only ----------------------------------------
        manifests = {name: self._manifest_for(name) for name in names}

        # -- phase 2: choose, then clone what was chosen -------------------
        changed = diff_symbols(pr.diff)
        selection = select_repositories(
            pr.repo,
            manifests,
            budget=self.budget,
            all_repos=self.all_repos,
            cache=self.cache,
            removed_symbols=changed.removed_exports,
            added_symbols=changed.added_exports,
        )
        self.selection = selection

        for name in selection.selected:
            dest = self.owner_dir / name
            if (dest / ".git").is_dir():
                self._update(dest)
            else:
                self._clone(name, dest)

        self._checkout_head(pr)
        return Workspace(self.owner_dir, only=set(selection.selected))

    # -- organisation listing ----------------------------------------------

    def _org_repo_names(self) -> list[str]:
        names = self._list_repos() if self._list_repos is not None else self._gh_list_repos()
        if len(names) > MAX_ORG_REPOS:
            raise WorkspaceError(
                f"the {self.owner!r} organisation has more than {MAX_ORG_REPOS} "
                "repositories, which is out of scope for this tool. Nothing was "
                "cloned."
            )
        return names

    def _gh_list_repos(self) -> list[str]:
        # Ask for one more than the bound: if that many come back, enumeration
        # is refused without anything having been cloned.
        raw = self._run(
            self.gh_path,
            "repo",
            "list",
            self.owner,
            "--no-archived",
            "--limit",
            str(MAX_ORG_REPOS + 1),
            "--json",
            "name",
            "--jq",
            ".[].name",
            what=f"list repositories for {self.owner!r}",
        )
        return [line.strip() for line in raw.splitlines() if line.strip()]

    # -- phase 1: manifests without cloning ---------------------------------

    def _manifest_for(self, name: str) -> tuple[ManifestFacts, str | None]:
        """Read one repository's manifest through the API, cloning nothing.

        A repository with no manifest is an ordinary outcome, not an error: it
        simply contributes no dependency edge, and the lexical channel can still
        reach it if it is cloned.
        """
        if self._read_manifest is not None:
            return self._read_manifest(name)

        for pack in LANGUAGE_PACKS:
            for manifest_name in pack.manifest_names:
                text = self._fetch_file(name, manifest_name)
                if text is None:
                    continue
                return pack.read_manifest(text), pack.name
        return ManifestFacts(), None

    def _fetch_file(self, repo: str, path: str) -> str | None:
        """One file's contents from the default branch, or ``None`` if absent.

        A missing file is by far the commonest outcome — most repositories have
        one manifest and not the other two — so a non-zero exit is not an error
        here and must not be reported as one.
        """
        proc = subprocess.run(
            [
                self.gh_path,
                "api",
                f"repos/{self.owner}/{repo}/contents/{path}",
                "--jq",
                ".content",
            ],
            input="",
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return base64.b64decode(proc.stdout.strip()).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            return None

    # -- cloning and updating ----------------------------------------------

    def _clone(self, name: str, dest: Path) -> None:
        if self._clone_repo is not None:
            self._clone_repo(name, dest)
            return
        # `gh repo clone` handles credentials and configures the local repo so a
        # later `git fetch` reuses gh's credential helper. No token is handled
        # here, and no submodules or LFS objects are pulled.
        #
        # Blobless: history arrives without file contents, and the working tree
        # is materialised at checkout. A review reads the checked-out tree rather
        # than history, so this skips work nothing was going to use — on a
        # repository with a long history that is most of the download.
        self._run(
            self.gh_path,
            "repo",
            "clone",
            f"{self.owner}/{name}",
            str(dest),
            "--",
            _BLOBLESS,
            what=f"clone {self.owner}/{name}",
        )

    def _update(self, dest: Path) -> None:
        self._run(
            self.git_path,
            "-C",
            str(dest),
            "fetch",
            "--prune",
            "--no-tags",
            "origin",
            what=f"update {dest.name}",
        )

    # -- head-SHA checkout --------------------------------------------------

    def _checkout_head(self, pr: PullRequest) -> None:
        dest = self.owner_dir / pr.repo
        if not (dest / ".git").is_dir():
            raise WorkspaceError(f"the pull request repository {pr.repo!r} was not cloned.")

        # For a fork PR the head commit is not on origin; the pull ref carries
        # it. Only fetch it if the commit is missing, and treat that fetch as
        # best-effort (a plain-branch PR already has the commit).
        if not self._has_commit(dest, pr.head_sha) and pr.number is not None:
            self._try_fetch_pull_ref(dest, pr.number)
        if not self._has_commit(dest, pr.head_sha):
            raise WorkspaceError(
                f"the pull request head commit is not available in {pr.repo!r}; "
                "the branch may have been deleted or force-pushed."
            )

        self._run(
            self.git_path,
            "-C",
            str(dest),
            "checkout",
            "--detach",
            pr.head_sha,
            what=f"check out {pr.repo} at the reviewed commit",
        )

    def _has_commit(self, dest: Path, sha: str) -> bool:
        proc = subprocess.run(
            [self.git_path, "-C", str(dest), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def _try_fetch_pull_ref(self, dest: Path, number: int) -> None:
        # Best-effort: a local remote (tests) has no refs/pull/*, and that is fine.
        subprocess.run(
            [
                self.git_path,
                "-C",
                str(dest),
                "fetch",
                "origin",
                f"+refs/pull/{number}/head:refs/panorama/pr-{number}",
            ],
            capture_output=True,
            text=True,
        )

    # -- subprocess plumbing ------------------------------------------------

    def _run(self, *argv: str, what: str) -> str:
        """Run a git/gh command, returning stdout; never echo child stderr.

        stderr is the most likely place a token hint or an operator path would
        appear, so a failure becomes a host-authored, redacted message.
        """
        try:
            proc = subprocess.run(list(argv), input="", capture_output=True, text=True)
        except FileNotFoundError as exc:
            tool = argv[0]
            raise PreflightError(
                f"{tool!r} was not found on PATH; it is required to {what}."
            ) from exc
        if proc.returncode != 0:
            raise WorkspaceError(
                f"failed to {what} (exit {proc.returncode}). "
                "Check `panorama doctor` and that you have access to the repository."
            )
        return proc.stdout
