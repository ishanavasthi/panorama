"""Pull-request intake: turn a source into the normalized `PullRequest` model.

Everything downstream of intake (retrieval, review, validation, delivery) is
source-agnostic: it only ever sees a `PullRequest`. V1 has two sources that
produce this identical shape — the local git source implemented here, and the
GitHub source that lands in a later milestone.

This module is strictly read-only. It runs `git rev-parse`, `git diff`, and
`git log` and nothing else — no writes to the repository, no network.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel

from panorama.config import DEMO_ORG_ROOT
from panorama.errors import IntakeError, PreflightError

#: Length of a full git object name in hex.
_FULL_SHA_LEN = 40

#: `owner/repo#123`.
_PR_SHORT_RE = re.compile(r"^(?P<owner>[^/#\s]+)/(?P<repo>[^/#\s]+)#(?P<number>\d+)$")
#: A GitHub PR URL, with or without scheme and with any trailing path/query
#: (`/files`, `?diff=split`, `#discussion_r1`). Only the three parts matter.
_PR_URL_RE = re.compile(
    r"github\.com[/:]+(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
)


class PullRequest(BaseModel):
    """The one shape every intake source produces and every later stage reads.

    ``number``/``url`` are GitHub concepts and are ``None`` for a local PR.
    ``base_sha``/``head_sha`` are the immutable commit ids the review is pinned
    to, so findings stay auditable no matter what the branch does afterwards.
    """

    owner: str
    repo: str
    number: int | None = None
    url: str | None = None
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    title: str
    body: str
    diff: str


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo`` and return the completed process.

    Does not raise on a non-zero exit — callers inspect ``returncode`` so they
    can turn a git failure into a specific, user-facing message. A missing git
    binary is an environment problem, so it surfaces as a preflight error.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            "git was not found on PATH; install git to review a local repository."
        ) from exc


def _assert_git_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise IntakeError(f"local repository not found: {repo}")
    proc = _run_git(repo, "rev-parse", "--is-inside-work-tree")
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        raise IntakeError(f"not a git repository: {repo}")


def _rev_parse(repo: Path, ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA, or raise on an unknown ref.

    ``rev-parse --verify --quiet <ref>^{commit}`` prints the commit id and
    exits non-zero silently when the ref does not resolve to a commit, which is
    exactly the unknown-ref signal we want.
    """
    proc = _run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or len(sha) != _FULL_SHA_LEN:
        raise IntakeError(f"unknown git ref {ref!r} in repository {repo.name!r}")
    return sha


def _diff(repo: Path, base_sha: str, head_sha: str) -> str:
    """The changes introduced by ``head`` relative to its merge-base with ``base``.

    Three-dot ``base...head`` is PR semantics: it diffs from the point the two
    diverged, so unrelated commits landing on base afterwards do not pollute the
    review. When the two resolve to the same commit the diff is empty, which is
    a valid (no-op) pull request, not an error.
    """
    proc = _run_git(repo, "diff", "--no-color", f"{base_sha}...{head_sha}")
    if proc.returncode != 0:
        raise IntakeError(f"git diff failed in {repo.name!r}: {proc.stderr.strip()}")
    return proc.stdout


def _commit_field(repo: Path, sha: str, fmt: str) -> str:
    return _run_git(repo, "log", "-1", f"--format={fmt}", sha).stdout.strip()


class LocalPullRequestSource:
    """Normalize a branch in a local git repository into a `PullRequest`.

    ``owner`` and ``repo`` are read from the checkout's own location — the repo
    directory name and its parent (the organisation directory) — so a local PR
    carries the same identifying shape as a GitHub one without inventing data.
    """

    def __init__(self, repo_path: Path, base_ref: str, head_ref: str) -> None:
        self.repo_path = Path(repo_path)
        self.base_ref = base_ref
        self.head_ref = head_ref

    def load(self) -> PullRequest:
        repo = self.repo_path
        _assert_git_repo(repo)
        base_sha = _rev_parse(repo, self.base_ref)
        head_sha = _rev_parse(repo, self.head_ref)
        return PullRequest(
            owner=repo.parent.name,
            repo=repo.name,
            number=None,
            url=None,
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=self.base_ref,
            head_ref=self.head_ref,
            title=_commit_field(repo, head_sha, "%s"),
            body=_commit_field(repo, head_sha, "%b"),
            diff=_diff(repo, base_sha, head_sha),
        )


def resolve_local_repo(name_or_path: str) -> Path:
    """Resolve a ``--local`` argument to a repository directory.

    Accepts either a direct path to a checkout or a bare repository name, which
    is looked up under the bootstrapped demo organisation (`.panorama/demo-org`).
    """
    candidate = Path(name_or_path)
    if candidate.is_dir():
        return candidate
    under_demo_org = DEMO_ORG_ROOT / name_or_path
    if under_demo_org.is_dir():
        return under_demo_org
    raise IntakeError(
        f"local repository {name_or_path!r} not found "
        f"(looked for a directory, then under {DEMO_ORG_ROOT}/). "
        "Run `panorama fixtures bootstrap` first?"
    )


def parse_pr_ref(ref: str) -> tuple[str, str, int]:
    """Parse a pull-request reference into ``(owner, repo, number)``.

    Accepts the ``owner/repo#123`` shorthand and a GitHub PR URL in any of its
    common shapes (with or without scheme, with a trailing ``/files`` or query
    string). Raises :class:`IntakeError` on anything else — this is user input,
    so a bad reference maps to the validation exit code, not a crash.
    """
    ref = ref.strip()
    match = _PR_SHORT_RE.match(ref) or _PR_URL_RE.search(ref)
    if match is None:
        raise IntakeError(
            f"could not parse pull-request reference {ref!r}. "
            "Expected 'owner/repo#123' or a GitHub PR URL."
        )
    repo = match["repo"]
    if repo.endswith(".git"):  # a clone URL pasted by habit
        repo = repo[: -len(".git")]
    return match["owner"], repo, int(match["number"])


class GitHubPullRequestSource:
    """Normalize a GitHub pull request into the shared `PullRequest` shape.

    Uses ``gh`` — and only ``gh`` — to read the pull request. Panorama never
    reads, stores, or prompts for a token; ``gh`` owns the GitHub credential, so
    (unlike the sandboxed `claude` child) ``gh`` is run with the normal
    environment and left to find its own auth however it is configured.

    Two REST reads via ``gh api`` cover everything the pipeline needs and work
    across a fork boundary without a local clone: the pull-request object (for
    base/head SHAs and refs, title, body, URL) and the same object requested as
    a unified diff. Raw ``gh`` stderr is never surfaced — a failure becomes a
    host-authored message, because stderr is the most likely place for a token
    hint or an operator path to appear.
    """

    def __init__(self, owner: str, repo: str, number: int, *, gh_path: str = "gh") -> None:
        self.owner = owner
        self.repo = repo
        self.number = number
        self.gh_path = gh_path

    @property
    def _slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def _run_gh(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                [self.gh_path, *args],
                input="",
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise PreflightError(
                "the GitHub CLI (`gh`) was not found on PATH; install gh and run "
                "`gh auth login` to review a GitHub pull request."
            ) from exc
        if proc.returncode != 0:
            # Deliberately no stderr echo: host-authored, actionable, redacted.
            raise IntakeError(
                f"could not read pull request {self._slug}#{self.number} via gh "
                f"(gh exit {proc.returncode}). Check the reference exists and that "
                "`gh auth status` reports you are signed in."
            )
        return proc.stdout

    def _pull_path(self) -> str:
        return f"repos/{self._slug}/pulls/{self.number}"

    def load(self) -> PullRequest:
        raw = self._run_gh("api", self._pull_path())
        try:
            meta = json.loads(raw)
        except ValueError as exc:
            raise IntakeError(
                f"gh returned output that was not valid JSON for {self._slug}"
                f"#{self.number}."
            ) from exc
        if not isinstance(meta, dict):
            raise IntakeError(f"unexpected gh response shape for {self._slug}#{self.number}.")

        base = meta.get("base") or {}
        head = meta.get("head") or {}
        base_sha = base.get("sha", "")
        head_sha = head.get("sha", "")
        if len(head_sha) != _FULL_SHA_LEN or len(base_sha) != _FULL_SHA_LEN:
            raise IntakeError(
                f"gh did not return base and head commit SHAs for {self._slug}"
                f"#{self.number}."
            )

        # The diff is the same REST object requested as a unified diff, so it
        # resolves for fork PRs without cloning the fork.
        diff = self._run_gh(
            "api", self._pull_path(), "-H", "Accept: application/vnd.github.diff"
        )

        return PullRequest(
            owner=self.owner,
            repo=self.repo,
            number=self.number,
            url=meta.get("html_url"),
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=base.get("ref", ""),
            head_ref=head.get("ref", ""),
            title=meta.get("title") or "",
            body=meta.get("body") or "",
            diff=diff,
        )
