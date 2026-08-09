"""Tests for the operational surface: `status`, `cache clear`, and `--fail-on`.

These are small commands, and two of them carry more weight than their size
suggests. `status` is how persistent state becomes inspectable — and two kinds
of that state are read from untrusted input or drive an unattended process, so
being able to look at it is part of the safety story. `cache clear` has to keep
the "always safe to delete" promise honest while not quietly discarding the one
thing in there that is *not* derived.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from panorama import cli
from panorama.cache import Cache
from panorama.errors import EXIT_FINDINGS_AT_THRESHOLD
from panorama.fingerprint import fingerprint
from panorama.models import Evidence, Finding
from panorama.validation import ValidatedReview

SHA = "a" * 40


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "owner.db"


@pytest.fixture
def wired(db: Path, monkeypatch) -> Path:
    monkeypatch.setattr(cli.Cache, "for_owner", lambda *a, **k: Cache(db))
    return db


def finding(severity: str = "high", title: str = "t") -> Finding:
    return Finding(
        severity=severity,
        category="contract_break",
        title=title,
        rationale="because",
        evidence=[Evidence(repo="consumer", path="src/a.ts", line=1)],
        confidence="high",
    )


def review(*findings: Finding) -> ValidatedReview:
    return ValidatedReview(
        verdict="comment", summary="s", findings=list(findings), discarded=[]
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_an_empty_cache_without_error(wired: Path) -> None:
    result = CliRunner().invoke(cli.app, ["status", "acme"])
    assert result.exit_code == 0, result.output
    assert "indexed facts     0" in result.output
    assert "always safe to delete" in result.output


def test_status_reports_what_is_held(wired: Path) -> None:
    with Cache(wired) as cache:
        cache.put_facts("svc", SHA, "symbols", {"exports": {}, "imports": {}})
        cache.set_cursor("svc", 7, SHA)
        cache.suppress(fingerprint(finding()), title="dismissed thing", source="reply")

    result = CliRunner().invoke(cli.app, ["status", "acme"])

    assert result.exit_code == 0, result.output
    assert "watch cursors     1" in result.output
    assert "suppressions      1" in result.output
    assert "svc#7 at " in result.output


def test_status_truncates_a_long_cursor_list(wired: Path) -> None:
    with Cache(wired) as cache:
        for number in range(15):
            cache.set_cursor("svc", number, SHA)
    result = CliRunner().invoke(cli.app, ["status", "acme"])
    assert "+5 more" in result.output


# ---------------------------------------------------------------------------
# cache clear
# ---------------------------------------------------------------------------


def test_cache_clear_drops_derived_facts(wired: Path) -> None:
    with Cache(wired) as cache:
        cache.put_facts("svc", SHA, "symbols", {"exports": {}, "imports": {}})

    result = CliRunner().invoke(cli.app, ["cache", "clear", "acme"])

    assert result.exit_code == 0, result.output
    assert "Cleared 1 cached fact" in result.output
    with Cache(wired) as cache:
        assert cache.stats().n_facts == 0


def test_cache_clear_keeps_dismissals_and_cursors_by_default(wired: Path) -> None:
    """Those are the one thing in here that is *not* derived from repository
    content: dropping them means re-reviewing, and seeing dismissed findings
    again. It should take asking."""
    with Cache(wired) as cache:
        cache.put_facts("svc", SHA, "symbols", {"exports": {}, "imports": {}})
        cache.set_cursor("svc", 7, SHA)
        cache.suppress("deadbeefcafe", title="t", source="reply")

    CliRunner().invoke(cli.app, ["cache", "clear", "acme"])

    with Cache(wired) as cache:
        assert len(cache.suppressions()) == 1
        assert cache.get_cursor("svc", 7) == SHA


def test_cache_clear_everything_forgets_the_lot(wired: Path) -> None:
    with Cache(wired) as cache:
        cache.set_cursor("svc", 7, SHA)
        cache.suppress("deadbeefcafe", title="t", source="reply")

    result = CliRunner().invoke(cli.app, ["cache", "clear", "acme", "--everything"])

    assert "1 dismissal" in result.output
    with Cache(wired) as cache:
        assert cache.suppressions() == ()
        assert cache.get_cursor("svc", 7) is None


def test_clearing_watch_state_also_clears_the_review_budget(wired: Path) -> None:
    """Otherwise a cleared cursor would be re-reviewed against a budget already
    spent on the review being forgotten."""
    with Cache(wired) as cache:
        cache.set_cursor("svc", 7, SHA)
        assert cache.reviews_since(3600) == 1
        cache.clear_watch_state()
        assert cache.reviews_since(3600) == 0


# ---------------------------------------------------------------------------
# --fail-on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("severity", "threshold", "expected"),
    [
        ("high", "high", EXIT_FINDINGS_AT_THRESHOLD),
        ("medium", "high", 0),
        ("medium", "medium", EXIT_FINDINGS_AT_THRESHOLD),
        ("low", "medium", 0),
        ("low", "low", EXIT_FINDINGS_AT_THRESHOLD),
        ("high", "low", EXIT_FINDINGS_AT_THRESHOLD),
    ],
)
def test_fail_on_uses_severity_order(severity: str, threshold: str, expected: int) -> None:
    """`--fail-on medium` means medium *or worse*, which is the only reading
    that makes it useful as a gate."""
    import typer

    if expected == 0:
        cli._fail_on_exit(review(finding(severity)), threshold)
        return
    with pytest.raises(typer.Exit) as caught:
        cli._fail_on_exit(review(finding(severity)), threshold)
    assert caught.value.exit_code == expected


def test_fail_on_is_inert_without_a_threshold() -> None:
    cli._fail_on_exit(review(finding("high")), None)


def test_fail_on_ignores_an_empty_review() -> None:
    cli._fail_on_exit(review(), "low")


def test_fail_on_rejects_an_unknown_severity() -> None:
    import typer

    with pytest.raises(typer.BadParameter):
        cli._fail_on_exit(review(finding()), "catastrophic")


def test_a_suppressed_finding_cannot_trip_the_gate(wired: Path) -> None:
    """Suppression removes findings before this runs, so a dismissed finding
    does not keep failing somebody's build."""
    validated = ValidatedReview(
        verdict="comment", summary="s", findings=[], discarded=[], suppressed=[finding()]
    )
    cli._fail_on_exit(validated, "high")


def test_the_threshold_exit_code_is_not_an_error_code() -> None:
    """"The tool broke" and "the tool worked and you should look" are different
    outcomes; a CI job that cannot tell them apart gets configured to ignore
    both."""
    from panorama.errors import (
        EXIT_CLAUDE_INVOCATION_ERROR,
        EXIT_GENERAL_ERROR,
        EXIT_PREFLIGHT_ERROR,
        EXIT_VALIDATION_ERROR,
    )

    assert EXIT_FINDINGS_AT_THRESHOLD not in {
        EXIT_GENERAL_ERROR,
        EXIT_PREFLIGHT_ERROR,
        EXIT_CLAUDE_INVOCATION_ERROR,
        EXIT_VALIDATION_ERROR,
    }
