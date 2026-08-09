"""Host-side validation of a model-produced review.

The model's job is to *find* cross-repository impact; the host's job is to
*trust nothing it says without checking*. Everything here runs before a single
character reaches the user or a pull-request comment.

Every finding is checked against ground truth the host controls — the
workspace on disk and the pull request's own diff — and one that fails any check
is **discarded, never downgraded** (a hard project rule: an unverifiable review
costs the reader trust every time they check one and it is wrong). The report
carries the discard count and reasons so silence is always explained.

Checks applied to each finding:

1. Its text passes secret-shape screening (:mod:`panorama.screening`).
2. It has at least one evidence reference that actually resolves — the repo is a
   workspace child, the path stays inside it, the file exists, and the line is
   in bounds.
3. A cross-repository category cites at least one valid reference *outside* the
   pull request's own repository.
4. Any supplied pull-request location corresponds to a file and line the diff
   actually changes.

This module contains no fixture repository names, field names, or finding text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from panorama.intake import PullRequest
from panorama.models import Finding, Review
from panorama.screening import contains_secret, is_clean
from panorama.workspace import Workspace, WorkspaceError

#: Categories whose whole point is impact across a repository boundary. Each
#: must cite evidence outside the pull request's own repository to survive.
#: Derived from the model vocabulary by exclusion: everything that is not an
#: explicitly single-repository finding is treated as cross-repository.
_SINGLE_REPO_CATEGORY = "single_repo"

#: A pull-request verdict the host is always willing to assert on its own: it
#: claims no unsupported impact. Used when every finding has been discarded.
_NEUTRAL_VERDICT = "comment"

#: Shown in the report in place of a summary the model tainted with a secret.
_REDACTED_SUMMARY = "(summary withheld: it did not pass host secret screening)"


@dataclass(frozen=True)
class DiscardedFinding:
    """A finding removed during validation, with a host-authored reason.

    ``title`` is the model's own title, retained only so the report can point at
    *which* finding went — it has already passed secret screening (a finding
    discarded *for* a tainted title carries a safe placeholder instead).
    """

    title: str
    category: str
    reason: str


@dataclass(frozen=True)
class ValidatedReview:
    """The outcome of host validation: what survived, and what did not."""

    verdict: str
    summary: str
    findings: list[Finding]
    discarded: list[DiscardedFinding]
    summary_redacted: bool = False
    #: Findings a human already dismissed, removed after validation. Counted
    #: rather than merely absent: a reviewer that quietly says less than it
    #: found is worse than one that says too much, because nobody can tell
    #: "nothing to report" from "not shown".
    suppressed: list[Finding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def discard_count(self) -> int:
        return len(self.discarded)

    @property
    def suppressed_count(self) -> int:
        return len(self.suppressed)


# ---------------------------------------------------------------------------
# diff → changed locations
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(diff: str) -> dict[str, set[int]]:
    """Map each changed file to the set of new-side line numbers it adds.

    Keyed by the post-image (``+++ b/<path>``) path, because a supplied
    pull-request location refers to the file as it looks *after* the change.
    A pure deletion contributes an entry with an empty set — the file was
    touched, but no new line exists to cite.
    """
    result: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    in_hunk = False

    for line in diff.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw.startswith("b/"):
                raw = raw[2:]
            path = None if raw == "/dev/null" else raw
            if path is not None:
                result.setdefault(path, set())
            in_hunk = False
            continue
        if path is None:
            continue
        hunk = _HUNK.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            result[path].add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue  # removed line: no new-side number to advance
        else:
            new_line += 1  # context line advances the new-side counter

    return result


# ---------------------------------------------------------------------------
# evidence resolution
# ---------------------------------------------------------------------------


def _file_line_count(path) -> int | None:
    try:
        return len(path.read_text(errors="replace").splitlines())
    except OSError:
        return None


def evidence_resolves(repo: str, path: str, line: int, workspace: Workspace) -> bool:
    """True if ``repo/path:line`` names a real, in-bounds location in the workspace.

    Containment is proved first (``resolve_within`` rejects an unknown repo, a
    ``..`` escape, or a symlink leaving the repo) before the file is ever
    touched, so a malicious citation cannot walk the host to an arbitrary file.
    """
    try:
        target = workspace.resolve_within(repo, path)
    except WorkspaceError:
        return False
    if not target.is_file():
        return False
    count = _file_line_count(target)
    return count is not None and 1 <= line <= count


def _finding_reject_reason(
    finding: Finding,
    pr: PullRequest,
    workspace: Workspace,
    changed: dict[str, set[int]],
) -> str | None:
    """Return why ``finding`` must be discarded, or ``None`` to keep it."""
    # 1. Secret-shape screening across every free-text field.
    if not is_clean(finding.title, finding.rationale, finding.recommendation):
        return "model text did not pass secret screening"

    # 2. Evidence must actually resolve on disk.
    valid_repos = {
        e.repo
        for e in finding.evidence
        if evidence_resolves(e.repo, e.path, e.line, workspace)
    }
    if not valid_repos:
        return "no supporting evidence resolved to a real file and line"

    # 3. A cross-repository category needs a valid reference outside the PR repo.
    if finding.category != _SINGLE_REPO_CATEGORY:
        if not (valid_repos - {pr.repo}):
            return "cross-repository finding cites no repository outside the pull request"

    # 4. A supplied pull-request location must match an actually-changed location.
    if finding.pr_path is not None:
        if finding.pr_path not in changed:
            return "cites a pull-request file the diff does not change"
        if finding.pr_line is not None and finding.pr_line not in changed[finding.pr_path]:
            return "cites a pull-request line the diff does not change"

    return None


def _discarded(finding: Finding, reason: str) -> DiscardedFinding:
    # A finding discarded *for* a tainted title must not carry that title into
    # the report; anything else has already passed secret screening.
    safe_title = finding.title if not contains_secret(finding.title) else "(withheld)"
    return DiscardedFinding(title=safe_title, category=finding.category, reason=reason)


def validate_review(review: Review, pr: PullRequest, workspace: Workspace) -> ValidatedReview:
    """Validate every finding against the workspace and the diff.

    Findings that fail any check are discarded with a reason. The verdict is the
    model's own *unless* nothing survived, in which case the host asserts the
    neutral verdict rather than a disposition it cannot support. The summary is
    withheld if it is tainted, so a secret can never ride out in prose.
    """
    changed = changed_lines(pr.diff)

    kept: list[Finding] = []
    discarded: list[DiscardedFinding] = []
    for finding in review.findings:
        reason = _finding_reject_reason(finding, pr, workspace, changed)
        if reason is None:
            kept.append(finding)
        else:
            discarded.append(_discarded(finding, reason))

    summary_redacted = contains_secret(review.summary)
    summary = _REDACTED_SUMMARY if summary_redacted else review.summary

    # A verdict the host cannot back with a surviving finding is not asserted.
    verdict = review.verdict if kept else _NEUTRAL_VERDICT

    return ValidatedReview(
        verdict=verdict,
        summary=summary,
        findings=kept,
        discarded=discarded,
        summary_redacted=summary_redacted,
    )
