"""A read-only view over a multi-repository workspace.

For local review the workspace is simply the bootstrapped fixture directory
(`.panorama/demo-org/`); later milestones point the same abstraction at cloned
sibling repositories under `~/.panorama/workspaces/<owner>/`. Either way it
answers the same questions the pipeline needs before any model runs:

- which repositories exist, and at which immutable HEAD SHA;
- a containment primitive that proves a `repo/path` reference stays inside its
  repository (the foundation the S5 evidence validator builds on);
- an *org map*: a concise, generically-derived orientation document — repo
  names, README/manifest summaries, top-level paths, and any organisation
  convention documents — with no fixture-specific knowledge baked into the code.

This module is strictly read-only: it runs `git rev-parse` and reads files. It
never writes to a repository, checks anything out, or touches the network.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from panorama.errors import IntakeError, PanoramaError, PreflightError

#: Top-level document *stems* (case-insensitive) that describe org conventions.
#: Generic engineering-doc names, not fixture data.
_CONVENTION_STEMS = frozenset(
    {"conventions", "convention", "architecture", "contributing", "standards", "codeowners"}
)

#: Directory globs that conventionally hold contracts/standards.
_CONVENTION_GLOBS = ("openapi/**/*", "docs/standards/**/*", "docs/**/*.md")

#: Never let the org map balloon: cap discovered docs and characters per summary.
_MAX_CONVENTION_DOCS = 20
_MAX_SUMMARY_CHARS = 300

_FULL_SHA_LEN = 40


class WorkspaceError(PanoramaError):
    """Raised when the workspace or a reference into it is invalid (unknown
    repository, a path that escapes its repository)."""

    exit_code = IntakeError.exit_code


@dataclass(frozen=True)
class RepoView:
    """One repository in the workspace, pinned to its current HEAD."""

    name: str
    path: Path
    head_sha: str


@dataclass
class RepoOrgEntry:
    """The org-map facts derived from a single repository's content."""

    name: str
    head_sha: str
    description: str
    readme_summary: str
    top_level: list[str] = field(default_factory=list)
    convention_docs: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            "git was not found on PATH; install git to read the workspace."
        ) from exc


def _head_sha(repo: Path) -> str | None:
    """The repository's current HEAD SHA, or ``None`` if it is not a git repo."""
    proc = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and len(sha) == _FULL_SHA_LEN else None


def _summarize_readme(repo_path: Path) -> str:
    """First paragraph of the repo's README, heading and blank lines skipped."""
    readme = next(
        (
            p
            for p in sorted(repo_path.iterdir())
            if p.is_file() and p.name.lower().startswith("readme")
        ),
        None,
    )
    if readme is None:
        return ""
    lines: list[str] = []
    for raw in readme.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line:
            if lines:  # end of the first real paragraph
                break
            continue
        if line.startswith("#"):  # skip the title heading(s)
            continue
        lines.append(line)
    summary = " ".join(lines)
    return summary[:_MAX_SUMMARY_CHARS].strip()


def _manifest_description(repo_path: Path) -> str:
    """The ``description`` from a top-level ``package.json``, if present."""
    manifest = repo_path / "package.json"
    if not manifest.is_file():
        return ""
    try:
        data = json.loads(manifest.read_text(errors="replace"))
    except ValueError:
        return ""
    description = data.get("description") if isinstance(data, dict) else None
    return description.strip() if isinstance(description, str) else ""


def _top_level_paths(repo_path: Path) -> list[str]:
    """Top-level entries, directories marked with a trailing slash. Excludes .git."""
    entries: list[str] = []
    for child in repo_path.iterdir():
        if child.name == ".git":
            continue
        entries.append(f"{child.name}/" if child.is_dir() else child.name)
    return sorted(entries)


def _convention_docs(repo_path: Path) -> list[str]:
    """Repo-relative paths of generic organisation/convention documents."""
    found: set[str] = set()

    for child in repo_path.iterdir():
        if child.is_file() and child.stem.lower() in _CONVENTION_STEMS:
            found.add(child.name)

    for pattern in _CONVENTION_GLOBS:
        for match in repo_path.glob(pattern):
            if match.is_file():
                found.add(match.relative_to(repo_path).as_posix())

    return sorted(found)[:_MAX_CONVENTION_DOCS]


class Workspace:
    """A directory whose immediate children are git repositories."""

    def __init__(self, root: Path, *, only: set[str] | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.only = only
        """Restrict the view to these repository names, or ``None`` for all.

        Provisioning leaves clones from previous runs in place rather than
        deleting work the next review may want. Without this restriction those
        leftovers would be searched, so a review would quietly examine a
        repository it did not select — and, worse, one that may be checked out
        at some other run's commit.
        """

    def exists(self) -> bool:
        return self.root.is_dir()

    def repos(self) -> list[RepoView]:
        """Every child that is a git repository, sorted by name, pinned to HEAD."""
        if not self.root.is_dir():
            raise WorkspaceError(
                f"workspace directory not found: {self.root}. "
                "Run `panorama fixtures bootstrap` first?"
            )
        views: list[RepoView] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            if self.only is not None and child.name not in self.only:
                continue
            sha = _head_sha(child)
            if sha is None:
                continue
            views.append(RepoView(name=child.name, path=child, head_sha=sha))
        return views

    def repo(self, name: str) -> RepoView:
        """Look up one repository by name, or raise if it is not in the workspace."""
        # A name with a path separator is not a workspace child by definition.
        if name != Path(name).name:
            raise WorkspaceError(f"{name!r} is not a workspace repository name")
        for view in self.repos():
            if view.name == name:
                return view
        raise WorkspaceError(f"repository {name!r} is not in the workspace")

    def resolve_within(self, repo_name: str, relpath: str) -> Path:
        """Resolve ``relpath`` inside ``repo_name`` and prove it stays inside it.

        Resolution follows symlinks first, so a path that escapes via ``..`` or a
        symlink is rejected before the caller ever touches it. Existence is not
        asserted here — that is the evidence validator's separate job.
        """
        base = self.repo(repo_name).path.resolve()
        target = (base / relpath).resolve()
        if target != base and base not in target.parents:
            raise WorkspaceError(
                f"path {relpath!r} escapes repository {repo_name!r}"
            )
        return target

    # -- org map ------------------------------------------------------------

    def org_entries(self) -> list[RepoOrgEntry]:
        """Derive the org-map facts for every repository, from content alone."""
        entries: list[RepoOrgEntry] = []
        for view in self.repos():
            entries.append(
                RepoOrgEntry(
                    name=view.name,
                    head_sha=view.head_sha,
                    description=_manifest_description(view.path),
                    readme_summary=_summarize_readme(view.path),
                    top_level=_top_level_paths(view.path),
                    convention_docs=_convention_docs(view.path),
                )
            )
        return entries

    def org_map_markdown(self) -> str:
        """Render the org map as a compact Markdown orientation document."""
        entries = self.org_entries()
        out: list[str] = [
            "# Organisation map",
            "",
            f"Generated from workspace content. {len(entries)} repositories.",
        ]
        for entry in entries:
            out.append("")
            out.append(f"## {entry.name}")
            out.append(f"- HEAD: {entry.head_sha[:7]}")
            if entry.description:
                out.append(f"- Description: {entry.description}")
            out.append(f"- Top-level: {', '.join(entry.top_level) or '(empty)'}")
            docs = ", ".join(entry.convention_docs) if entry.convention_docs else "(none)"
            out.append(f"- Convention documents: {docs}")
            if entry.readme_summary:
                out.append(f"- Summary: {entry.readme_summary}")
        out.append("")
        return "\n".join(out)
