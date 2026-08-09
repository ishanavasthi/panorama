"""Seed the mock organisation as private GitHub repositories for a live demo.

`panorama demo --github <owner>` turns the checked-in fixtures into real,
**private** repositories under an owner, pushes the seeded branches, and opens a
pull request for each — so the whole cross-repository review can be shown end to
end against live GitHub rather than local fixtures.

This is the one command that *writes* to GitHub. It is therefore:

- **confirmed** — the CLI refuses to create anything without an explicit yes;
- **never run by automated tests** — the orchestration is exercised offline with
  an injected command runner, but nothing here is invoked against real GitHub in
  CI;
- **credential-clean** — every write goes through `gh`, which owns the token;
  Panorama places no token in a URL and never echoes raw `gh`/`git` stderr.

The plan is assembled from the fixtures, not hard-coded: repository names,
branch names, and PR titles all come from the data tree.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from panorama.errors import PanoramaError
from panorama.fixtures import bootstrap
from panorama.fixtures.bootstrap import DATA_ROOT

_MAIN_BRANCH = "main"

#: A command runner returns ``(returncode, stdout)`` and never raises, so the
#: seeder decides what a failure means. Injected in tests; subprocess by default.
CommandRunner = Callable[..., "tuple[int, str]"]


class DemoError(PanoramaError):
    """A live-seeding step failed or was refused."""


@dataclass
class SeededPR:
    repo: str
    branch: str
    url: str


@dataclass
class DemoResult:
    owner: str
    repos: list[str] = field(default_factory=list)
    prs: list[SeededPR] = field(default_factory=list)
    #: Repositories that already existed and were refreshed in place, as
    #: opposed to created. Reported so a run says which it did.
    updated: list[str] = field(default_factory=list)


def demo_repo_names(source_root: Path = DATA_ROOT) -> list[str]:
    """The repositories the demo will create, read from the fixture data."""
    if not source_root.is_dir():
        return []
    return sorted(p.name for p in source_root.iterdir() if (p / "base").is_dir())


class DemoSeeder:
    """Create the private demo repositories and open their pull requests."""

    def __init__(
        self,
        owner: str,
        *,
        source_root: Path = DATA_ROOT,
        gh_path: str = "gh",
        git_path: str = "git",
        run: CommandRunner | None = None,
        recreate: bool = False,
        update: bool = False,
    ) -> None:
        self.owner = owner
        self.source_root = source_root
        self.gh_path = gh_path
        self.git_path = git_path
        self.recreate = recreate
        self.update = update
        self._run = run if run is not None else self._subprocess_run

    # -- command plumbing ---------------------------------------------------

    def _subprocess_run(self, argv, *, cwd=None, input_text: str = "") -> tuple[int, str]:
        import subprocess

        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            input=input_text,
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout

    def _gh_ok(self, *argv: str) -> bool:
        rc, _ = self._run([self.gh_path, *argv])
        return rc == 0

    def _gh(self, *argv: str, cwd=None, what: str) -> str:
        rc, out = self._run([self.gh_path, *argv], cwd=cwd)
        if rc != 0:
            raise DemoError(
                f"failed to {what} (gh exit {rc}). Check `gh auth status` and your "
                "repository-creation permissions."
            )
        return out

    def _git(self, *argv: str, cwd=None, what: str) -> str:
        rc, out = self._run([self.git_path, *argv], cwd=cwd)
        if rc != 0:
            raise DemoError(f"failed to {what} (git exit {rc}).")
        return out

    # -- steps --------------------------------------------------------------

    def preflight(self) -> None:
        if not self._gh_ok("auth", "status"):
            raise DemoError(
                "`gh` is not authenticated. Run `gh auth login` before seeding the demo."
            )

    def _pr_meta(self, repo: str, branch: str) -> tuple[str, str]:
        pr_file = self.source_root / repo / "branches" / branch / "pr.json"
        if pr_file.is_file():
            try:
                data = json.loads(pr_file.read_text())
            except ValueError:
                data = {}
            if isinstance(data, dict):
                return (data.get("title") or branch, data.get("body") or "")
        return branch, ""

    def _resolve_existing(self, slug: str) -> bool:
        """Decide what to do about a repository that is already there.

        Returns True when the caller should *update* it in place rather than
        create it. Three modes, and the default is still to refuse: creating
        repositories is the one thing this command does that cannot be undone by
        running it again.
        """
        if not self._gh_ok("repo", "view", slug):
            return False
        if self.recreate:
            self._gh("repo", "delete", slug, "--yes", what=f"delete existing {slug}")
            return False
        if self.update:
            return True
        raise DemoError(
            f"repository {slug} already exists. Re-run with --update to refresh it "
            "in place, or --recreate to replace it (which needs the delete_repo "
            "scope), or seed under a different owner."
        )

    def _create(self, repo, slug: str) -> None:
        # gh handles auth and configures the local repo so the follow-up push
        # reuses its credential helper. No token is handled here.
        self._gh(
            "repo", "create", slug, "--private", "--source", str(repo.path),
            "--remote", "origin", "--push",
            cwd=repo.path, what=f"create private repository {slug}",
        )
        self._git(
            "-C", str(repo.path), "push", "origin", "--all",
            what=f"push branches to {slug}",
        )

    def _refresh(self, repo, slug: str) -> None:
        """Force the existing repository to match the current fixture data.

        The fixtures are rebuilt from scratch on every bootstrap, so their
        commits are new objects with no ancestry in common with whatever is on
        the remote. A fast-forward is therefore impossible by construction and
        the push has to be forced — which is safe here in a way it would never
        be elsewhere, because these repositories are *generated*: every byte is
        reproducible from the checked-in data tree.

        Updating in place rather than deleting and recreating keeps the demo's
        existing pull requests and their review comments, which are the most
        interesting thing in the organisation. It also works with an ordinary
        `repo` token, where deletion needs a scope most people do not grant.
        """
        # A plain HTTPS remote: gh's credential helper supplies the token, so
        # none is ever written into a URL or handled here.
        self._run(
            [self.git_path, "-C", str(repo.path), "remote", "add", "origin",
             f"https://github.com/{slug}.git"]
        )
        self._git(
            "-C", str(repo.path), "push", "--force", "origin", "--all",
            what=f"update branches on {slug}",
        )

    def _open_branches(self, slug: str) -> set[str]:
        """Branches that already have an open pull request.

        Read so an update does not try to open a second pull request for a
        branch that has one — `gh pr create` fails in that case, and failing
        the whole run over an existing pull request would make `--update`
        unusable exactly when it is most useful.
        """
        rc, out = self._run(
            [self.gh_path, "pr", "list", "--repo", slug, "--state", "open",
             "--json", "headRefName", "--limit", "100"]
        )
        if rc != 0:
            return set()
        try:
            listed = json.loads(out or "[]")
        except ValueError:
            return set()
        return {
            str(item.get("headRefName"))
            for item in listed
            if isinstance(item, dict) and item.get("headRefName")
        }

    def seed(self) -> DemoResult:
        """Create or refresh every repository and open any missing demo PRs."""
        self.preflight()

        with tempfile.TemporaryDirectory(prefix="panorama-demo-") as tmp:
            built = bootstrap(dest_root=Path(tmp) / "demo-org")
            result = DemoResult(owner=self.owner)

            for repo in built.repos:
                slug = f"{self.owner}/{repo.name}"
                existing = self._resolve_existing(slug)

                if existing:
                    self._refresh(repo, slug)
                    result.updated.append(repo.name)
                    already_open = self._open_branches(slug)
                else:
                    self._create(repo, slug)
                    already_open = set()

                result.repos.append(repo.name)

                for branch in repo.branches:
                    if branch == _MAIN_BRANCH or branch in already_open:
                        continue
                    title, body = self._pr_meta(repo.name, branch)
                    out = self._gh(
                        "pr", "create", "--repo", slug, "--base", _MAIN_BRANCH,
                        "--head", branch, "--title", title, "--body", body,
                        what=f"open pull request for {slug}#{branch}",
                    )
                    result.prs.append(SeededPR(repo=repo.name, branch=branch, url=out.strip()))

            return result
