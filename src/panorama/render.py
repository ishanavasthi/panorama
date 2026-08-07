"""Reference-only rendering of a validated review.

Turns a :class:`ValidatedReview` into the Markdown a user reads and the JSON a
machine consumes. Two invariants hold here:

- **Reference-only.** Evidence is printed as ``repo/path:line`` and nothing
  more; no source is ever quoted, because the model was never given a field to
  put source in and the validator screened the free text.
- **Honest silence.** Zero surviving findings renders as an explicit
  "no supported cross-repository impact detected", never as an empty page, and
  the discard count and any retrieval truncation are always stated — so the
  reader knows what was checked and what was dropped.

No fixture repository names, field names, or finding text appear here.
"""

from __future__ import annotations

from typing import Any

from panorama.intake import PullRequest
from panorama.models import Finding
from panorama.validation import ValidatedReview

_NO_IMPACT = "**No supported cross-repository impact detected.**"

_VERDICT_LABEL = {
    "approve": "approve",
    "comment": "comment",
    "request_changes": "request changes",
}


def _evidence_refs(finding: Finding) -> list[str]:
    """The finding's evidence as bare ``repo/path:line`` references."""
    return [f"{e.repo}/{e.path}:{e.line}" for e in finding.evidence]


def _render_finding(index: int, finding: Finding) -> list[str]:
    tag = f"{finding.severity.upper()} · {finding.category}"
    out = [
        f"### {index}. [{tag}] {finding.title}",
        "",
        f"_Confidence: {finding.confidence}._",
        "",
        finding.rationale,
        "",
    ]
    if finding.pr_path is not None:
        location = finding.pr_path
        if finding.pr_line is not None:
            location += f":{finding.pr_line}"
        out.append(f"- In this pull request: `{location}`")
    refs = _evidence_refs(finding)
    if refs:
        out.append("- Evidence:")
        out.extend(f"  - `{ref}`" for ref in refs)
    if finding.recommendation:
        out.append(f"- Recommendation: {finding.recommendation}")
    out.append("")
    return out


def render_markdown(
    pr: PullRequest,
    validated: ValidatedReview,
    *,
    retrieval_truncated: bool = False,
) -> str:
    """Render the validated review as reference-only Markdown."""
    out: list[str] = [
        f"# Panorama review — {pr.owner}/{pr.repo}",
        "",
        f"`{pr.base_ref}` ← `{pr.head_ref}`",
        "",
        f"**Verdict:** {_VERDICT_LABEL.get(validated.verdict, validated.verdict)}",
        "",
        validated.summary,
        "",
    ]

    if validated.has_findings:
        out.append(f"## Findings ({len(validated.findings)})")
        out.append("")
        for index, finding in enumerate(validated.findings, start=1):
            out.extend(_render_finding(index, finding))
    else:
        out.append(_NO_IMPACT)
        out.append("")

    out.append("---")
    out.append("")
    out.extend(_render_footer(validated, retrieval_truncated))
    return "\n".join(out).rstrip() + "\n"


def _render_footer(validated: ValidatedReview, retrieval_truncated: bool) -> list[str]:
    """The validation ledger: what was dropped and why, and any truncation."""
    lines: list[str] = []
    count = validated.discard_count
    if count:
        noun = "finding" if count == 1 else "findings"
        lines.append(f"_{count} {noun} discarded during host validation:_")
        for item in validated.discarded:
            lines.append(f"- {item.title} ({item.category}): {item.reason}")
    else:
        lines.append("_No findings were discarded during host validation._")

    if validated.summary_redacted:
        lines.append("")
        lines.append("_The model's summary was withheld: it did not pass secret screening._")

    if retrieval_truncated:
        lines.append("")
        lines.append(
            "_Retrieval was truncated: the diff produced more leads than the cap, "
            "so cross-repository coverage may be incomplete._"
        )
    return lines


def review_json_obj(
    pr: PullRequest,
    validated: ValidatedReview,
    *,
    retrieval_truncated: bool = False,
) -> dict[str, Any]:
    """A stable, machine-readable view of the validated review.

    Carries only reference-only data: findings with ``repo/path:line`` evidence,
    the discard ledger, and the truncation flag. No source, ever.
    """
    return {
        "repo": f"{pr.owner}/{pr.repo}",
        "base": {"ref": pr.base_ref, "sha": pr.base_sha},
        "head": {"ref": pr.head_ref, "sha": pr.head_sha},
        "verdict": validated.verdict,
        "summary": validated.summary,
        "summary_redacted": validated.summary_redacted,
        "findings": [f.model_dump() for f in validated.findings],
        "discarded": [
            {"title": d.title, "category": d.category, "reason": d.reason}
            for d in validated.discarded
        ],
        "retrieval_truncated": retrieval_truncated,
    }
