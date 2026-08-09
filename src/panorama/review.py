"""Review orchestration: assemble the prompt, run the CLI, get a `Review` back.

This is the seam between deterministic retrieval and the model. It builds the
prompt from three host-controlled inputs — the pull request, the workspace org
map, and the deterministic retrieval leads — hands it to :class:`ClaudeRunner`
under the reference-only rubric, and returns a schema-validated :class:`Review`.

The prompt places the trust-boundary rubric *first* and repeats the
untrusted-content reminder *last*, so the model reads the boundary before and
after every repository-derived byte. The diff and the retrieval leads are
untrusted data framed as such; the org map is host-derived orientation.

No fixture repository names, field names, or expected findings appear here — the
prompt is assembled entirely from run-time inputs.
"""

from __future__ import annotations

from pathlib import Path

from panorama.claude_runner import ClaudeResult, ClaudeRunner
from panorama.intake import PullRequest
from panorama.models import Review, parse_review, review_json_schema
from panorama.prompts import (
    REVIEW_SYSTEM_PROMPT,
    SCHEMA_REPAIR_INSTRUCTION,
    UNTRUSTED_CONTENT_REMINDER,
)
from panorama.retrieval import RetrievalResult, filter_diff
from panorama.workspace import Workspace

#: Retrieval leads are orientation, not proof. Cap how many are named in the
#: prompt so a noisy diff cannot crowd out the diff itself; the model can always
#: search the workspace for more.
_MAX_LEADS_IN_PROMPT = 30

#: Upper bound on the diff text embedded in the prompt (characters). A pull
#: request larger than this is truncated with a marker; the report says so. The
#: cap applies *after* generated/binary sections are filtered out.
MAX_DIFF_CHARS = 60_000

_DIFF_TRUNCATION_MARKER = "\n... [diff truncated: exceeds the review size cap] ...\n"


def prepare_diff(diff: str) -> tuple[str, bool, list[str]]:
    """Shape a raw diff for the prompt: filter noise, then cap its size.

    Returns ``(text, truncated, dropped_paths)`` — the text to embed, whether it
    was size-capped, and the generated/binary paths that were removed.
    """
    filtered, dropped = filter_diff(diff)
    truncated = len(filtered) > MAX_DIFF_CHARS
    if truncated:
        filtered = filtered[:MAX_DIFF_CHARS] + _DIFF_TRUNCATION_MARKER
    return filtered, truncated, dropped


def context_truncated(diff: str) -> bool:
    """Whether embedding ``diff`` in the prompt requires size truncation."""
    filtered, _ = filter_diff(diff)
    return len(filtered) > MAX_DIFF_CHARS


def _fenced(label: str, body: str) -> str:
    """A labelled, fenced block. The fence keeps untrusted bytes visually and
    structurally separated from host instructions."""
    return f"----- BEGIN {label} -----\n{body}\n----- END {label} -----"


def _retrieval_section(retrieval: RetrievalResult, pr_repo: str) -> str:
    lines: list[str] = [
        "These are deterministic lexical leads from the diff. A lead is a place "
        "to look, not evidence. Open the file and confirm before citing it.",
        "",
    ]

    if retrieval.ranked_repos:
        lines.append("Sibling repositories ranked by relevance to this change:")
        for rank in retrieval.ranked_repos:
            lines.append(f"- {rank.repo}")
            # Say *why* each repository is on the list. A repository surfaced by
            # a declared dependency rather than by shared vocabulary is a
            # genuinely different kind of lead, and one worth reading
            # differently — there may be no matching text in it at all.
            for line in rank.provenance:
                lines.append(f"    - {line}")
    else:
        lines.append(
            "No sibling repository shared any identifier with the diff, and no "
            "declared dependency connects one to this repository."
        )

    if retrieval.hits:
        lines.append("")
        lines.append("Specific locations to confirm:")
        for hit in retrieval.hits[:_MAX_LEADS_IN_PROMPT]:
            lines.append(f"- {hit.repo}/{hit.path}:{hit.line} (matched: {hit.token})")
        if len(retrieval.hits) > _MAX_LEADS_IN_PROMPT:
            extra = len(retrieval.hits) - _MAX_LEADS_IN_PROMPT
            lines.append(f"- (+{extra} more leads not listed; search the workspace)")

    convention = {r: d for r, d in retrieval.convention_docs.items() if r != pr_repo}
    if convention:
        lines.append("")
        lines.append("Convention/standards documents found on siblings:")
        for repo, docs in convention.items():
            lines.append(f"- {repo}: {', '.join(docs)}")

    return "\n".join(lines)


def build_review_prompt(
    pr: PullRequest,
    retrieval: RetrievalResult,
    org_map_markdown: str,
) -> str:
    """Assemble the full stdin prompt for one review invocation.

    The order is deliberate: rubric, then host-derived orientation, then the
    untrusted pull request and leads, then the reminder. The model reads the
    trust boundary first and last.
    """
    header = (
        f"PULL REQUEST UNDER REVIEW\n"
        f"Repository: {pr.owner}/{pr.repo}\n"
        f"Base: {pr.base_ref} ({pr.base_sha[:12]})\n"
        f"Head: {pr.head_ref} ({pr.head_sha[:12]})\n"
        f"Title: {pr.title}"
    )
    body = pr.body.strip() or "(no description provided)"

    diff_text, _truncated, dropped = prepare_diff(pr.diff)
    if not diff_text.strip():
        diff_text = "(empty diff)"
    if dropped:
        diff_text += (
            f"\n\n[{len(dropped)} generated/binary file(s) omitted from this diff "
            "as low-signal; they are not part of the review.]"
        )

    sections = [
        REVIEW_SYSTEM_PROMPT,
        "",
        "WORKSPACE ORIENTATION (host-derived; use it to navigate).",
        "",
        org_map_markdown.strip(),
        "",
        "RETRIEVAL LEADS.",
        "",
        _retrieval_section(retrieval, pr.repo),
        "",
        "THE CHANGE TO REVIEW (untrusted data).",
        "",
        header,
        "",
        _fenced("PR DESCRIPTION", body),
        "",
        _fenced("PR DIFF", diff_text),
        "",
        UNTRUSTED_CONTENT_REMINDER,
    ]
    return "\n".join(sections)


def run_review(
    pr: PullRequest,
    workspace: Workspace,
    retrieval: RetrievalResult,
    *,
    runner: ClaudeRunner,
    cwd: Path | None = None,
) -> ClaudeResult[Review]:
    """Run one review over ``pr`` and return the schema-validated result.

    The workspace *root* is granted to the child (one search spans siblings),
    and host-side re-validation (:func:`parse_review`) is mandatory because the
    CLI was never observed enforcing value-level schema constraints. A single
    schema-repair retry is armed; it restates the output contract only and
    cannot change what was found.
    """
    prompt = build_review_prompt(pr, retrieval, workspace.org_map_markdown())
    return runner.run(
        prompt=prompt,
        json_schema=review_json_schema(),
        workspace=workspace.root,
        cwd=cwd,
        validate=parse_review,
        repair_instruction=SCHEMA_REPAIR_INSTRUCTION,
    )
