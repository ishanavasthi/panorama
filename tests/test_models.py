"""Offline tests for the Review / Finding / Evidence contract.

These models do double duty: they are the JSON Schema handed to the CLI via
``--json-schema``, and they are the host-side re-validation gate. The CLI was
never observed enforcing value-level constraints (enums, bounds) itself, so
every enum rejection tested here is load-bearing rather than decorative.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# Property names that would smuggle source text out of a sibling repository.
# Constraint #3 makes their absence a contract, not a style preference.
EXCERPT_LIKE_FIELDS = {
    "excerpt",
    "snippet",
    "code",
    "source",
    "content",
    "body",
    "text",
    "context",
    "lines",
    "quote",
    "quotation",
    "sample",
}

VERDICTS = {"approve", "comment", "request_changes"}
SEVERITIES = {"high", "medium", "low"}
CONFIDENCES = {"high", "medium", "low"}
CATEGORIES = {
    "contract_break",
    "duplicate_logic",
    "convention",
    "cross_repo_conflict",
    "single_repo",
}


@pytest.fixture
def Evidence(models_module: Any):
    return models_module.Evidence


@pytest.fixture
def Finding(models_module: Any):
    return models_module.Finding


@pytest.fixture
def Review(models_module: Any):
    return models_module.Review


@pytest.fixture
def validation_error(models_module: Any):
    """Whatever Pydantic raises on invalid input.

    Imported from pydantic rather than ``panorama.errors`` because these are
    model-construction failures; the runner is expected to translate them.
    """
    import pydantic

    return pydantic.ValidationError


def good_evidence() -> dict:
    return {"repo": "repo-consumer", "path": "src/client.ts", "line": 2}


def good_finding(**overrides: Any) -> dict:
    finding = {
        "severity": "high",
        "category": "contract_break",
        "title": "Renamed response field breaks a sibling consumer",
        "rationale": "The removed field is destructured in another repository.",
        "pr_path": "src/handler.ts",
        "pr_line": 2,
        "evidence": [good_evidence()],
        "recommendation": "Coordinate the rename with the consumer.",
        "confidence": "high",
    }
    finding.update(overrides)
    return finding


def good_review(**overrides: Any) -> dict:
    review = {
        "summary": "One cross-repository contract break detected.",
        "verdict": "request_changes",
        "findings": [good_finding()],
    }
    review.update(overrides)
    return review


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_evidence_validates(Evidence):
    evidence = Evidence.model_validate(good_evidence())
    assert evidence.repo == "repo-consumer"
    assert evidence.path == "src/client.ts"
    assert evidence.line == 2


def test_finding_validates(Finding):
    finding = Finding.model_validate(good_finding())
    assert finding.severity == "high"
    assert finding.category == "contract_break"
    assert len(finding.evidence) == 1
    assert finding.evidence[0].repo == "repo-consumer"


def test_review_validates(Review):
    review = Review.model_validate(good_review())
    assert review.verdict == "request_changes"
    assert len(review.findings) == 1


def test_review_round_trips_through_json(Review):
    """The payload arrives as JSON from the CLI and leaves as JSON via --json."""
    original = Review.model_validate(good_review())
    restored = Review.model_validate_json(original.model_dump_json())
    assert restored == original


def test_review_accepts_an_empty_finding_list(Review):
    """Zero findings is an explicit result, not an error."""
    review = Review.model_validate(good_review(findings=[], verdict="approve"))
    assert review.findings == []


def test_optional_finding_fields_default_to_none(Finding):
    finding = good_finding()
    for optional in ("pr_path", "pr_line", "recommendation"):
        finding.pop(optional, None)
    parsed = Finding.model_validate(finding)
    assert parsed.pr_path is None
    assert parsed.pr_line is None
    assert parsed.recommendation is None


# --------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["critical", "HIGH", "sev-1", "", "none", "info", "blocker"]
)
def test_finding_rejects_bad_severity(Finding, validation_error, bad):
    with pytest.raises(validation_error):
        Finding.model_validate(good_finding(severity=bad))


@pytest.mark.parametrize(
    "bad",
    ["security", "contract-break", "CONTRACT_BREAK", "", "bug", "cross_repo", "style"],
)
def test_finding_rejects_bad_category(Finding, validation_error, bad):
    with pytest.raises(validation_error):
        Finding.model_validate(good_finding(category=bad))


@pytest.mark.parametrize("bad", ["certain", "HIGH", "", "0.9", "unknown"])
def test_finding_rejects_bad_confidence(Finding, validation_error, bad):
    with pytest.raises(validation_error):
        Finding.model_validate(good_finding(confidence=bad))


@pytest.mark.parametrize(
    "bad", ["reject", "APPROVE", "request-changes", "", "block", "lgtm"]
)
def test_review_rejects_bad_verdict(Review, validation_error, bad):
    with pytest.raises(validation_error):
        Review.model_validate(good_review(verdict=bad))


@pytest.mark.parametrize("missing", ["severity", "category", "title", "rationale", "confidence"])
def test_finding_requires_its_mandatory_fields(Finding, validation_error, missing):
    finding = good_finding()
    finding.pop(missing)
    with pytest.raises(validation_error):
        Finding.model_validate(finding)


@pytest.mark.parametrize("missing", ["repo", "path", "line"])
def test_evidence_requires_its_mandatory_fields(Evidence, validation_error, missing):
    evidence = good_evidence()
    evidence.pop(missing)
    with pytest.raises(validation_error):
        Evidence.model_validate(evidence)


@pytest.mark.parametrize("bad_line", ["not-a-number", None, [], {}, "line 4"])
def test_evidence_rejects_non_integer_lines(Evidence, validation_error, bad_line):
    with pytest.raises(validation_error):
        Evidence.model_validate({**good_evidence(), "line": bad_line})


@pytest.mark.parametrize("missing", ["summary", "verdict", "findings"])
def test_review_requires_its_mandatory_fields(Review, validation_error, missing):
    review = good_review()
    review.pop(missing)
    with pytest.raises(validation_error):
        Review.model_validate(review)


def test_review_rejects_a_nested_invalid_finding(Review, validation_error):
    """A bad enum deep in the tree must fail the whole payload, not be dropped."""
    with pytest.raises(validation_error):
        Review.model_validate(good_review(findings=[good_finding(severity="bogus")]))


def test_review_rejects_a_nested_invalid_evidence(Review, validation_error):
    bad = good_finding(evidence=[{"repo": "r", "path": "p"}])  # no line
    with pytest.raises(validation_error):
        Review.model_validate(good_review(findings=[bad]))


# --------------------------------------------------------------------------
# the emitted JSON Schema
# --------------------------------------------------------------------------


def walk(node: Any):
    """Yield every dict in a JSON Schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def all_property_names(schema: dict) -> set[str]:
    names: set[str] = set()
    for node in walk(schema):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
    return names


def all_enum_sets(schema: dict) -> list[set[str]]:
    sets = []
    for node in walk(schema):
        enum = node.get("enum")
        if isinstance(enum, list) and all(isinstance(v, str) for v in enum):
            sets.append(set(enum))
        const = node.get("const")
        if isinstance(const, str):
            sets.append({const})
    return sets


@pytest.fixture
def review_schema(Review) -> dict:
    return Review.model_json_schema()


def test_schema_is_json_serialisable(review_schema):
    """It is passed to ``--json-schema`` as an inline JSON string, not a path.

    A schema the CLI cannot parse fails pre-flight with an empty stdout, which
    is indistinguishable from a bad flag -- so serialisability is a hard gate.
    """
    encoded = json.dumps(review_schema)
    assert json.loads(encoded) == review_schema
    assert len(encoded) > 0


def test_schema_describes_an_object_with_the_review_fields(review_schema):
    assert review_schema.get("type") == "object"
    properties = review_schema.get("properties", {})
    assert {"summary", "verdict", "findings"} <= set(properties)
    assert {"summary", "verdict", "findings"} <= set(review_schema.get("required", []))


def test_schema_inlines_the_nested_models(review_schema):
    """Finding and Evidence must be reachable, or the CLI cannot satisfy them."""
    defs = review_schema.get("$defs") or review_schema.get("definitions") or {}
    assert {"Finding", "Evidence"} <= set(defs), (
        f"nested model definitions missing from the schema: {sorted(defs)}"
    )

    evidence_properties = set(defs["Evidence"].get("properties", {}))
    assert {"repo", "path", "line"} <= evidence_properties


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(VERDICTS, id="verdict"),
        pytest.param(SEVERITIES, id="severity_or_confidence"),
        pytest.param(CATEGORIES, id="category"),
    ],
)
def test_schema_carries_the_closed_vocabularies(review_schema, expected):
    """Literal types must survive into the schema as enums.

    If they are erased to bare strings the CLI has no vocabulary to aim at, and
    every enum violation becomes a host-side rejection after the turn is spent.
    """
    enum_sets = all_enum_sets(review_schema)
    assert any(expected <= candidate for candidate in enum_sets), (
        f"no enum in the schema covers {sorted(expected)}"
    )


def test_schema_has_no_source_excerpt_field(review_schema):
    """Constraint #3: findings cite ``repo/path:line`` and never quote source."""
    offenders = all_property_names(review_schema) & EXCERPT_LIKE_FIELDS
    assert offenders == set(), (
        f"schema exposes excerpt-shaped field(s) {sorted(offenders)}; findings "
        "must be reference-only"
    )


def test_evidence_model_has_no_source_excerpt_field(Evidence):
    offenders = set(Evidence.model_fields) & EXCERPT_LIKE_FIELDS
    assert offenders == set(), (
        f"Evidence exposes excerpt-shaped field(s) {sorted(offenders)}"
    )


def test_confidence_and_severity_share_the_low_medium_high_vocabulary(Finding):
    """Guards against a drifting vocabulary between the two ranking fields."""
    for value in SEVERITIES:
        assert Finding.model_validate(good_finding(severity=value)).severity == value
    for value in CONFIDENCES:
        assert Finding.model_validate(good_finding(confidence=value)).confidence == value
