"""Secret-shape screening for model-authored text.

The review contract is *reference-only*: the schema has no source-excerpt field
and the prompt forbids quotation, so the primary defence against leaking
repository contents is structural. This module is the host-side backstop for the
one thing structure cannot catch — a credential-shaped string smuggled into a
free-text field (a title, a rationale, a summary).

It screens for *shapes*, not specific values: known token prefixes and generic
high-entropy blobs. It deliberately does not attempt to detect source-code
quotation in general — that is fuzzy and false-positive-prone, and is instead
prevented by the schema shape and the prompt. What lands here is the residual
risk: a secret a reviewer might echo verbatim while describing a finding.

No fixture names, field names, or finding text appear here.
"""

from __future__ import annotations

import re

#: Each pattern matches a *shape* that is almost never legitimate prose. Kept
#: conservative so ordinary review text (identifiers, paths, line numbers) is
#: never mistaken for a secret.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Anthropic / OpenAI-style keys and OAuth tokens: `sk-...`, `sk-ant-...`.
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"),
    # GitHub personal-access / OAuth / app tokens: ghp_, gho_, ghu_, ghs_, ghr_.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Google API key.
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # PEM private-key header.
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    # JSON Web Token: three dot-separated base64url segments.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)


def contains_secret(text: str) -> bool:
    """True if ``text`` contains anything credential-shaped."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def is_clean(*texts: str | None) -> bool:
    """True only if none of the supplied strings contains a secret shape.

    ``None`` entries (absent optional fields) are ignored, so callers can pass a
    finding's whole text surface without pre-filtering.
    """
    return not any(text and contains_secret(text) for text in texts)
