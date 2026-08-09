"""Giving a finding a stable identity across runs.

A reviewer that repeats a finding its readers have already rejected gets muted,
and then everything it says is lost — including the finding that mattered. So
findings need an identity that survives the things that change between runs, and
changes when the finding really is a different one.

## What the identity is made of

Three things: the **category**, a **normalised title**, and the set of
**evidence paths**.

Three things it deliberately excludes, each for a reason:

- **Line numbers.** Lines drift constantly — an import added at the top of a
  file moves every finding below it. A fingerprint that changed when unrelated
  code moved would make dismissal useless within a day.
- **Severity and confidence.** The model's own hedging varies between runs on
  identical input. Including it would let the same finding come back wearing a
  different label.
- **Rationale and recommendation.** Free prose, reworded on every generation.

The title is normalised — lowercased, punctuation dropped, whitespace collapsed
— because the model says the same thing slightly differently each time. That
normalisation is doing real work: without it, "Renamed field breaks consumer"
and "Renamed field breaks the consumer" would be two different findings.

## What it is not

Not a security boundary. A fingerprint is a *label*, and the only thing it
controls is whether Panorama repeats itself. Anything read from a pull request
thread can only ever **subtract** — suppression removes a finding and nothing
else. It cannot add one, raise a severity, or alter a citation, so a forged
dismissal costs one suppressed finding, visibly and reversibly.

Contains no fixture names (constraint #2).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

#: How much of the digest is shown to a human. Long enough that a collision
#: inside one pull request is not a practical concern, short enough to type
#: into a dismissal reply without resenting it.
SHORT_LENGTH = 10

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Reduce a title to the part that is stable between runs."""
    lowered = (title or "").strip().lower()
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", lowered)).strip()


def fingerprint(finding: Any) -> str:
    """A stable identity for one finding.

    Built from the category, the normalised title, and the sorted set of
    ``repo/path`` evidence locations — *paths*, never lines.
    """
    locations = sorted(f"{e.repo}/{e.path}" for e in getattr(finding, "evidence", ()))
    material = "\n".join(
        [
            str(getattr(finding, "category", "")),
            normalize_title(str(getattr(finding, "title", ""))),
            *locations,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def short(digest: str) -> str:
    """The form a human sees and types back."""
    return digest[:SHORT_LENGTH]


def matches(digest: str, candidate: str) -> bool:
    """Whether a user-supplied reference identifies this fingerprint.

    Accepts the short form, because that is what the report prints and what
    somebody will paste into a reply. Comparison is case-insensitive and
    prefix-based against the full digest, so an over-long paste still works and
    a truncated one does not silently match something else.
    """
    candidate = (candidate or "").strip().lower()
    if len(candidate) < SHORT_LENGTH:
        return False
    return digest.lower().startswith(candidate[: len(digest)])
