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
    ) -> None:
        self.owner = owner
        self.source_root = source_root
        self.gh_path = gh_path
        self.git_path = git_path
        self.recreate = recreate
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

    def _ensure_absent(self, slug: str) -> None:
        if not self._gh_ok("repo", "view", slug):
            return
        if not self.recreate:
            raise DemoError(
                f"repository {slug} already exists. Re-run with --recreate to replace "
                "it (needs the delete_repo scope), or seed under a different owner."
            )
        self._gh("repo", "delete", slug, "--yes", what=f"delete existing {slug}")

    def seed(self) -> DemoResult:
        """Create every repository, push its branches, and open the demo PRs."""
        self.preflight()

        with tempfile.TemporaryDirectory(prefix="panorama-demo-") as tmp:
            built = bootstrap(dest_root=Path(tmp) / "demo-org")
            result = DemoResult(owner=self.owner)

            for repo in built.repos:
                slug = f"{self.owner}/{repo.name}"
                self._ensure_absent(slug)

                # Create the private repo from the local checkout and push main,
                # then push the remaining seeded branches. gh handles auth and
                # configures the local repo so the follow-up push reuses it.
                self._gh(
                    "repo", "create", slug, "--private", "--source", str(repo.path),
                    "--remote", "origin", "--push",
                    cwd=repo.path, what=f"create private repository {slug}",
                )
                self._git(
                    "-C", str(repo.path), "push", "origin", "--all",
                    what=f"push branches to {slug}",
                )
                result.repos.append(repo.name)

                for branch in repo.branches:
                    if branch == _MAIN_BRANCH:
                        continue
                    title, body = self._pr_meta(repo.name, branch)
                    out = self._gh(
                        "pr", "create", "--repo", slug, "--base", _MAIN_BRANCH,
                        "--head", branch, "--title", title, "--body", body,
                        what=f"open pull request for {slug}#{branch}",
                    )
                    result.prs.append(SeededPR(repo=repo.name, branch=branch, url=out.strip()))

            return result
