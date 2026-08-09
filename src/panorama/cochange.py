"""Repositories that historically change together.

The other three channels read the *present*: what the diff says, what the
manifests declare, what the source exports. This one reads the *past*, on the
theory that repositories which have repeatedly been changed together are
probably coupled in ways nobody wrote down — which is exactly the coupling the
structural channels are blind to.

It is the least certain of the four, and it ships **disabled by default**. The
reason is stated here rather than buried, because it is the whole story of this
milestone: the only corpus available to measure it on has synthetic history, and
synthetic history is precisely the condition under which a coupling prior looks
far better than it is.

## Two signals, both generic

**Shared references.** A ticket key or issue reference that appears in commits of
two different repositories is a deliberate statement by a human that one piece of
work spanned both. It is the stronger signal, because someone typed it on
purpose.

**Temporal co-commit.** Commits by the same author in two repositories inside a
short window suggest one change being carried across a boundary. Weaker, and
much easier to fool.

## The degeneracy guard, which is the interesting part

A coupling prior is only meaningful if it *discriminates* — if it says some pairs
are coupled and others are not. Two situations make it say nothing useful while
appearing to say a great deal:

- **Too little history.** A prior derived from a handful of commits is noise
  wearing a number. Repositories below a minimum commit count are excluded.
- **A signal that fires for nearly every pair.** If temporal co-commit couples
  almost the whole organisation — which is what happens when an entire fixture
  is created in one scripted burst by one author, and also what happens in a
  small team that ships everything on Fridays — then it carries no information.
  It is dropped, and the report says it was dropped.

Those guards are not defensive padding. On the only corpus available they are
what fires, and the channel correctly declines to guess.

## What would justify turning it on

A measurement on a real organisation with genuine history showing it improves
ranking on cases the other channels miss. Until that exists, an unmeasured
channel that is on by default is a quality claim nobody has checked, and this
project does not make those.

Contains no fixture names (constraint #2).
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

from panorama.channels import ChannelResult, RankedRepo
from panorama.intake import PullRequest
from panorama.workspace import RepoView, Workspace

#: How much history is read per repository. Enough to see a pattern, bounded so
#: an old repository does not make a review slow.
MAX_COMMITS = 300

#: Below this, a repository's history cannot support a prior about anything.
#: A handful of commits produces a number, not evidence.
MIN_COMMITS = 5

#: Commits this far apart are not one piece of work being carried across a
#: boundary, whatever else they are.
CO_COMMIT_WINDOW_HOURS = 24

#: If the temporal signal couples more than this share of all possible pairs it
#: is not discriminating between them, so it is discarded rather than reported.
MAX_TEMPORAL_DENSITY = 0.5

#: Claim kinds, strongest first. The index into this tuple is the rank.
SHARED_REFERENCE = "shared-reference"
CO_COMMIT = "co-commit"
_CLAIMS = (SHARED_REFERENCE, CO_COMMIT)

#: A ticket key like `PROJ-1234`. Deliberately not `#123`, which is repo-local
#: on every forge and would couple two repositories that merely both have an
#: issue numbered 12.
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


@dataclass(frozen=True)
class Commit:
    """The parts of a commit this channel reads."""

    sha: str
    author: str
    when: datetime
    text: str

    @property
    def tickets(self) -> set[str]:
        return set(_TICKET.findall(self.text))


@dataclass
class Coupling:
    """Per-pair coupling evidence for one workspace."""

    #: ``frozenset({a, b}) -> claim kind``, strongest claim per pair.
    pairs: dict[frozenset, str] = field(default_factory=dict)
    #: Repositories excluded for want of history.
    thin: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def claim_for(self, one: str, other: str) -> str | None:
        return self.pairs.get(frozenset({one, other}))


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
    except FileNotFoundError:  # pragma: no cover - git is a hard prerequisite
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def read_history(view: RepoView, *, max_commits: int = MAX_COMMITS) -> list[Commit]:
    """Recent commits on the repository's current branch, newest first.

    Only the checked-out branch, deliberately: in the fixture organisation the
    other branches *are* the pull requests under review, and folding them into
    "history" would let a case's own change count as evidence for itself.
    """
    # git expands `%x1f` and `%x1e` itself, so the separators never appear as
    # bytes in the argument list — an argv entry cannot contain a NUL, and a
    # commit message can contain very nearly anything else.
    raw = _git(
        view.path,
        "log",
        f"-n{max_commits}",
        "--format=%H%x1f%an%x1f%aI%x1f%s%n%b%x1e",
    )
    separator = "\x1f"
    commits: list[Commit] = []
    for block in raw.split("\x1e"):
        block = block.strip("\n")
        if not block.strip():
            continue
        parts = block.split(separator, 3)
        if len(parts) < 4:
            continue
        sha, author, timestamp, text = parts
        try:
            when = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        commits.append(Commit(sha=sha, author=author, when=when, text=text))
    return commits


def build_coupling(workspace: Workspace, *, exclude: str | None = None) -> Coupling:
    """Derive per-pair coupling from history, or decline to."""
    histories: dict[str, list[Commit]] = {}
    thin: list[str] = []

    for view in workspace.repos():
        commits = read_history(view)
        if len(commits) < MIN_COMMITS:
            thin.append(view.name)
            continue
        histories[view.name] = commits

    coupling = Coupling(thin=tuple(sorted(thin)))
    notes: list[str] = []

    if thin:
        notes.append(
            f"{len(thin)} repository/repositories have fewer than {MIN_COMMITS} "
            "commits of history, which cannot support a coupling prior: "
            + ", ".join(sorted(thin))
        )
    if len(histories) < 2:
        notes.append("not enough repositories with usable history to compare")
        coupling.notes = tuple(notes)
        return coupling

    # -- shared references: someone said these belong together ---------------
    tickets_by_repo = {
        repo: {ticket for commit in commits for ticket in commit.tickets}
        for repo, commits in histories.items()
    }
    for one, other in combinations(sorted(histories), 2):
        if tickets_by_repo[one] & tickets_by_repo[other]:
            coupling.pairs[frozenset({one, other})] = SHARED_REFERENCE

    # -- temporal co-commit, kept only if it discriminates -------------------
    temporal: set[frozenset] = set()
    window = CO_COMMIT_WINDOW_HOURS * 3600
    for one, other in combinations(sorted(histories), 2):
        if frozenset({one, other}) in coupling.pairs:
            continue
        if _co_committed(histories[one], histories[other], window):
            temporal.add(frozenset({one, other}))

    possible = len(histories) * (len(histories) - 1) / 2
    density = len(temporal) / possible if possible else 0.0
    if temporal and density > MAX_TEMPORAL_DENSITY:
        # A signal that fires for nearly every pair is not telling the pairs
        # apart, which is the only thing it was for.
        notes.append(
            f"temporal co-commit coupled {density:.0%} of pairs and was discarded "
            "as non-discriminating"
        )
    else:
        for pair in temporal:
            coupling.pairs[pair] = CO_COMMIT

    if not coupling.pairs:
        notes.append("history showed no usable coupling between repositories")

    coupling.notes = tuple(notes)
    return coupling


def _co_committed(one: list[Commit], other: list[Commit], window_seconds: float) -> bool:
    """Whether the same author committed to both inside the window."""
    by_author: dict[str, list[datetime]] = defaultdict(list)
    for commit in other:
        by_author[commit.author].append(commit.when)
    for commit in one:
        for when in by_author.get(commit.author, ()):
            if abs((commit.when - when).total_seconds()) <= window_seconds:
                return True
    return False


def claim_rank(claim: str) -> int:
    return _CLAIMS.index(claim) + 1


class CoChangeChannel:
    """Historical coupling between repositories.

    **Disabled by default.** It has never been measured on an organisation with
    real history, and a channel whose value is unmeasured has no business
    influencing a review by default.
    """

    name = "cochange"

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        coupling = build_coupling(workspace, exclude=pr.repo)

        scored = [
            (view.name, coupling.claim_for(pr.repo, view.name))
            for view in workspace.repos()
            if view.name != pr.repo
        ]
        ranked = tuple(
            RankedRepo(
                repo=repo,
                rank=claim_rank(claim),
                score=round(1.0 / claim_rank(claim), 6),
                justification=_justify(claim),
            )
            for repo, claim in sorted(
                ((r, c) for r, c in scored if c is not None),
                key=lambda item: (claim_rank(item[1]), item[0]),
            )
        )

        return ChannelResult(channel=self.name, ranked=ranked, notes=coupling.notes)


def _justify(claim: str) -> str:
    if claim == SHARED_REFERENCE:
        return "shares a ticket reference with this repository's history"
    return "has been committed to alongside this repository by the same author"
