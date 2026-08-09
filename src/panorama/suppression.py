"""Remembering what a human already rejected.

A reviewer that keeps repeating a finding its readers have dismissed gets muted,
and once it is muted everything it says is lost — including the finding that
mattered. So dismissals are read from the pull request thread and remembered.

## The rule that governs all of this

Everything here reads **untrusted content**. A pull request thread is written by
whoever can comment on the repository, and a diff is written by whoever opened
the change. So constraint #8 applies absolutely:

> Anything read from repository content or a pull request thread may only
> **reduce** what Panorama says. It can never add a finding, raise a severity,
> or alter a citation.

That is not a policy applied carefully — it is the shape of the code. The only
operation available here is *removal from a list*. There is no path from a
comment to a new finding, because nothing in this module constructs one.

The blast radius of a forged dismissal is therefore exactly one suppressed
finding, it is visible in `panorama suppressions list`, and it is undone by
`panorama suppressions clear`.

## How a dismissal is expressed

Two ways, deliberately different in precision:

- **A reply naming a fingerprint** — `panorama: dismiss a1b2c3d4e5` — suppresses
  that one finding. This is the precise form, and the report prints the
  fingerprint next to each finding so there is something to copy.
- **A 👎 reaction on Panorama's own comment** suppresses *every* finding that
  comment reported. Coarse, and chosen anyway because it is the gesture people
  actually make. It is safe precisely because suppression can only subtract, and
  it is reversible.

## Silence is always explained

A suppressed finding is counted in the report, exactly like a validation
discard. A reviewer that quietly says less than it found is worse than one that
says too much, because nobody can tell the difference between "nothing to
report" and "not shown".

Contains no fixture names (constraint #2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from panorama.cache import Cache
from panorama.fingerprint import fingerprint, matches, short

#: `panorama: dismiss <fingerprint>`, tolerant about spacing, punctuation and
#: case, because it is typed by a human into a comment box. Deliberately
#: requires the tool's name: a bare "dismiss abc123" in ordinary discussion
#: must not silently suppress anything.
_DISMISS = re.compile(
    r"panorama\s*[:,-]?\s*(?:please\s+)?dismiss\s+([0-9a-f]{10,64})",
    re.IGNORECASE,
)

#: The reaction that means "stop telling me this".
THUMBS_DOWN = "-1"


@dataclass(frozen=True)
class Dismissal:
    """One instruction to say less, and where it came from."""

    fingerprint_prefix: str
    source: str


@dataclass
class SuppressionResult:
    """What survived, and what did not."""

    kept: list[Any] = field(default_factory=list)
    suppressed: list[Any] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.suppressed)


def parse_dismissals(comments: list[dict]) -> list[Dismissal]:
    """Read explicit dismiss directives out of a pull request thread.

    Comments are untrusted text. Anything that is not a well-formed directive is
    ignored rather than guessed at, and nothing here can produce anything except
    a request to *remove* a finding.
    """
    found: list[Dismissal] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        author = comment.get("author")
        login = ""
        if isinstance(author, dict):
            login = str(author.get("login") or "")
        for match in _DISMISS.finditer(body):
            found.append(
                Dismissal(
                    fingerprint_prefix=match.group(1).lower(),
                    source=f"reply from {login}" if login else "reply",
                )
            )
    return found


def has_thumbs_down(reactions: list[dict]) -> bool:
    """Whether Panorama's own comment carries a 👎."""
    for reaction in reactions or ():
        if not isinstance(reaction, dict):
            continue
        content = str(reaction.get("content") or "").lower()
        if content in {THUMBS_DOWN, "thumbs_down", "-1", "👎"}:
            return True
    return False


def record_dismissals(
    cache: Cache,
    findings: list[Any],
    dismissals: list[Dismissal],
    *,
    blanket: bool = False,
    blanket_source: str = "thumbs-down reaction",
) -> int:
    """Store dismissals that refer to findings we actually reported.

    A directive naming a fingerprint Panorama never produced is ignored. That is
    not defensiveness for its own sake: storing arbitrary strings would let a
    thread fill the suppression table with entries nobody can interpret, and
    `suppressions list` would stop being readable.
    """
    stored = 0
    for finding in findings:
        digest = fingerprint(finding)
        title = str(getattr(finding, "title", ""))

        if blanket:
            cache.suppress(digest, title=title, source=blanket_source)
            stored += 1
            continue

        for dismissal in dismissals:
            if matches(digest, dismissal.fingerprint_prefix):
                cache.suppress(digest, title=title, source=dismissal.source)
                stored += 1
                break
    return stored


def apply_suppressions(findings: list[Any], cache: Cache | None) -> SuppressionResult:
    """Drop findings a human has already dismissed.

    The only operation this module performs on a review. Note what is *not*
    here: nothing adds, reorders, re-scores or re-cites anything.
    """
    result = SuppressionResult()
    if cache is None:
        result.kept = list(findings)
        return result

    for finding in findings:
        if cache.is_suppressed(fingerprint(finding)):
            result.suppressed.append(finding)
        else:
            result.kept.append(finding)
    return result


def describe(finding: Any) -> str:
    """`<short fingerprint> title`, for the suppression listing and the report."""
    return f"{short(fingerprint(finding))} {getattr(finding, 'title', '')}".strip()
