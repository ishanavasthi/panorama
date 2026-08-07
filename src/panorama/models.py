"""Pydantic v2 models for the Claude review contract.

These models are the *only* description of what a review looks like. They are
used twice:

1. As the JSON Schema handed to the `claude` CLI's ``--json-schema`` flag, so
   the model is constrained to emit exactly this shape.
2. As host-side re-validation of whatever the CLI actually returned. The M0
   probes never observed CLI-side enforcement of value-level constraints
   (``enum``/``minimum``/``maximum``), so host re-validation is mandatory, not
   optional.

The schema deliberately has **no source-excerpt field**. Evidence is a
``repo/path:line`` reference and nothing more (hard constraint: reference-only
output).

This module contains no fixture repository names, field names, PR numbers, or
expected finding text.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["high", "medium", "low"]
Confidence = Literal["high", "medium", "low"]
Verdict = Literal["approve", "comment", "request_changes"]
Category = Literal[
    "contract_break",
    "duplicate_logic",
    "convention",
    "cross_repo_conflict",
    "single_repo",
]


class Evidence(BaseModel):
    """A single reference into a repository in the protected workspace.

    Deliberately carries no source excerpt: the renderer prints
    ``repo/path:line`` and never quotes code.
    """

    model_config = ConfigDict(extra="forbid")

    repo: str = Field(description="Repository name as it appears in the workspace.")
    path: str = Field(description="Repository-relative file path.")
    line: int = Field(description="1-based line number within that file.")


class Finding(BaseModel):
    """One reviewer observation, supported by workspace evidence."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity = Field(description="Impact if this is real.")
    category: Category = Field(description="What kind of problem this is.")
    title: str = Field(description="Short single-line summary of the problem.")
    rationale: str = Field(
        description=(
            "Why this is a problem, written in your own words. Must not quote "
            "or paraphrase source code line-by-line."
        )
    )
    pr_path: str | None = Field(
        default=None,
        description="Path of the changed file in the pull request, if the finding has one.",
    )
    pr_line: int | None = Field(
        default=None,
        description="1-based line in the changed file, if the finding has one.",
    )
    evidence: list[Evidence] = Field(
        description=(
            "Supporting references. A cross-repository finding must include at "
            "least one reference outside the pull request's own repository."
        )
    )
    recommendation: str | None = Field(
        default=None,
        description="Concrete suggested action, if one is warranted.",
    )
    confidence: Confidence = Field(
        description="How sure you are, given only the evidence you cited."
    )


class Review(BaseModel):
    """The complete structured review returned by the Claude CLI."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="Overall assessment in a few sentences.")
    verdict: Verdict = Field(description="Recommended pull-request disposition.")
    findings: list[Finding] = Field(
        description="Zero or more supported findings. An empty list is a valid review."
    )


#: JSON Schema keywords whose value is a mapping of *names* to subschemas.
#: Keys inside these are user data (field names), never schema keywords, so
#: keyword-level rewrites must not touch them. ``Finding`` has a field literally
#: called ``title``, which collides with the ``title`` annotation keyword --
#: that collision is exactly why this distinction is tracked explicitly.
_NAME_KEYED_CONTAINERS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def _strip_titles(node: Any) -> Any:
    """Drop ``title`` *annotations* while preserving a field *named* ``title``.

    Titles are Pydantic-generated documentation noise ("Pr Path") that competes
    with the ``description`` text we actually want the model to read. They are
    removed everywhere except where ``title`` is a property name.
    """
    if isinstance(node, dict):
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title" and isinstance(value, str):
                continue
            if key in _NAME_KEYED_CONTAINERS and isinstance(value, dict):
                result[key] = {name: _strip_titles(sub) for name, sub in value.items()}
            else:
                result[key] = _strip_titles(value)
        return result
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def _assert_self_consistent(schema: Any) -> None:
    """Cheap structural self-check on the emitted schema.

    Guards against a rewrite silently deleting a property that is still listed
    in ``required`` -- the exact failure mode the ``title`` collision produced.
    """
    if isinstance(schema, dict):
        properties = schema.get("properties")
        required = schema.get("required")
        if isinstance(properties, dict) and isinstance(required, list):
            missing = [name for name in required if name not in properties]
            if missing:
                raise AssertionError(f"schema lists required fields with no definition: {missing}")
        for key, value in schema.items():
            if key in _NAME_KEYED_CONTAINERS and isinstance(value, dict):
                for sub in value.values():
                    _assert_self_consistent(sub)
            else:
                _assert_self_consistent(value)
    elif isinstance(schema, list):
        for item in schema:
            _assert_self_consistent(item)


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with every ``$ref``/``$defs`` indirection resolved inline.

    Why this exists: Pydantic's ``model_json_schema()`` hoists nested models
    (``Evidence``, ``Finding``) into a top-level ``$defs`` block and references
    them with ``{"$ref": "#/$defs/Evidence"}``. That is valid JSON Schema, but
    the ``--json-schema`` consumer is only guaranteed (per the M0 spec) to
    round-trip nested objects, arrays of objects, string enums and integers --
    ``$ref`` resolution was never proven. Inlining removes the question
    entirely at the cost of a slightly larger schema, and the emitted document
    is semantically identical.

    The models here form a strict tree (no recursion), so full inlining always
    terminates.
    """
    defs: dict[str, Any] = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs[ref.removeprefix("#/$defs/")]
                merged = resolve(dict(target))
                # Preserve sibling keys (e.g. `description`) alongside the $ref.
                for key, value in node.items():
                    if key != "$ref":
                        merged[key] = resolve(value)
                return merged
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved = resolve(schema)
    assert isinstance(resolved, dict)
    return resolved


def _simplify_optionals(node: Any) -> Any:
    """Collapse Pydantic's ``anyOf: [{...}, {"type": "null"}]`` optional encoding.

    Pydantic renders ``str | None`` as an ``anyOf`` with a ``null`` branch.
    That is correct JSON Schema, but it is a shape the M0 probes never
    exercised, and nullable-via-anyOf is the single most commonly unsupported
    construct in constrained-decoding implementations. We rewrite it to the
    equivalent, far more widely accepted ``{"type": ["string", "null"]}``.
    Optional fields are additionally absent from ``required``, so a model that
    simply omits them is still schema-valid.
    """
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            branches = [b for b in any_of if isinstance(b, dict)]
            nulls = [b for b in branches if b.get("type") == "null"]
            others = [b for b in branches if b.get("type") != "null"]
            if len(nulls) == 1 and len(others) == 1 and set(others[0]) <= {"type"}:
                rewritten = {k: v for k, v in node.items() if k != "anyOf"}
                rewritten["type"] = [others[0]["type"], "null"]
                return {k: _simplify_optionals(v) for k, v in rewritten.items()}
        return {k: _simplify_optionals(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_simplify_optionals(item) for item in node]
    return node


def review_json_schema() -> dict[str, Any]:
    """Emit the JSON Schema for :class:`Review`, shaped for ``--json-schema``.

    Transformations applied to Pydantic's default output, each with a reason:

    - ``$defs``/``$ref`` are inlined (see :func:`_inline_defs`).
    - ``str | None`` ``anyOf`` unions become ``"type": ["string", "null"]``
      (see :func:`_simplify_optionals`).
    - ``title`` *annotations* are stripped (see :func:`_strip_titles`); the
      ``Finding.title`` *field* is preserved.
    - ``additionalProperties: false`` is asserted on every object (it comes
      from ``extra="forbid"`` on the models) so the model cannot invent a
      field -- notably, cannot smuggle a source excerpt into an extra key.

    The result is a plain ``dict``; the caller is responsible for
    ``json.dumps``-ing it, because the CLI flag takes an inline JSON *string*,
    not a file path.
    """
    schema = _inline_defs(Review.model_json_schema())
    schema = _simplify_optionals(schema)
    stripped = _strip_titles(schema)
    assert isinstance(stripped, dict)
    _assert_self_consistent(stripped)
    return stripped


def review_json_schema_str() -> str:
    """The schema as the compact JSON string ``--json-schema`` expects."""
    import json

    return json.dumps(review_json_schema(), separators=(",", ":"), sort_keys=True)


def parse_review(payload: object) -> Review:
    """Host-side re-validation of a structured payload into a :class:`Review`.

    Always call this on whatever the CLI returned. CLI-side value-level
    constraint enforcement was never observed during M0, so this is the only
    guarantee that ``severity``/``category``/``verdict`` are in range and that
    no extra field slipped through.

    Raises :class:`pydantic.ValidationError` on a non-conforming payload; the
    transport layer translates that into a redacted Panorama error.
    """
    return Review.model_validate(payload)
