"""Scenario builders for the fake `gh` executable.

The executable (``tests/fake_gh/gh``) is standalone and communicates only
through a ``gh_scenario.json`` sidecar. These helpers build that sidecar so
tests read declaratively, mirroring ``tests/fake_claude``.
"""

from __future__ import annotations

from typing import Any


def sha(seed: str) -> str:
    """A deterministic 40-hex string derived from ``seed`` (for readable tests)."""
    body = (seed * 40)[:40]
    return "".join(c if c in "0123456789abcdef" else "0" for c in body)


def pr_meta(
    *,
    number: int = 1,
    title: str = "Rename response field",
    body: str = "Renames the url field.",
    owner: str = "acme",
    repo: str = "acme-api",
    base_ref: str = "main",
    head_ref: str = "p1-rename",
    base_sha: str | None = None,
    head_sha: str | None = None,
    head_full_name: str | None = None,
) -> dict[str, Any]:
    """A minimal but realistic GitHub REST pull-request object."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "base": {"ref": base_ref, "sha": base_sha or sha("a")},
        "head": {
            "ref": head_ref,
            "sha": head_sha or sha("b"),
            "repo": {"full_name": head_full_name or f"{owner}/{repo}"},
        },
    }


def scenario(
    *,
    meta: dict[str, Any] | None = None,
    diff: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Compose a sidecar body from a metadata object, a diff, and any overrides."""
    body: dict[str, Any] = {"meta": meta if meta is not None else pr_meta()}
    if diff is not None:
        body["diff"] = diff
    body.update(overrides)
    return body


#: A small, well-formed unified diff usable as a default head-vs-base diff.
SAMPLE_DIFF = (
    "diff --git a/src/types.ts b/src/types.ts\n"
    "--- a/src/types.ts\n"
    "+++ b/src/types.ts\n"
    "@@ -1,3 +1,3 @@\n"
    " export interface LinkResponse {\n"
    "-  url: string;\n"
    "+  target_url: string;\n"
    " }\n"
)
