"""Tests for the polling watcher.

The watcher is the only part of Panorama that acts without a human in the loop,
so its tests are weighted towards **what it refuses to do**: post without being
told exactly where, review the same commit twice, review past its budget, or
keep going once asked to stop.

Everything is injected — the listing, the review, and the clock — so the entire
loop including rate limiting, network failure and recovery runs offline with no
`gh`, no network and no subscription.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from panorama.cache import Cache
from panorama.errors import PanoramaError
from panorama.watch import (
    PollResult,
    PullSummary,
    RateLimited,
    TransientPollError,
    WatchConfig,
    Watcher,
    parse_pull_list,
    summarise,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    with Cache(tmp_path / "cache" / "owner.db") as opened:
        yield opened


def pull(number: int = 1, *, repo: str = "svc", head: str = SHA_A, **kw) -> PullSummary:
    return PullSummary(repo=repo, number=number, head_sha=head, **kw)


class Harness:
    """Drives one watcher with scripted listings and a recording reviewer."""

    def __init__(self, cache: Cache, *responses, **config_kw) -> None:
        self.responses = list(responses)
        self.reviewed: list[tuple[str, bool]] = []
        self.slept: list[float] = []
        self.etags: list[str | None] = []
        self.raise_on: dict[str, Exception] = {}
        self.stream = io.StringIO()

        config_kw.setdefault("max_polls", len(self.responses))
        self.config = WatchConfig(owner="acme", interval=1, **config_kw)
        self.watcher = Watcher(
            self.config,
            cache,
            list_pulls=self._list,
            review=self._review,
            sleep=self.slept.append,
            stream=self.stream,
        )

    def _list(self, etag):
        self.etags.append(etag)
        if not self.responses:
            return PollResult()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _review(self, summary: PullSummary, should_post: bool) -> bool:
        if summary.ref in self.raise_on:
            raise self.raise_on[summary.ref]
        self.reviewed.append((summary.ref, should_post))
        return True

    def run(self):
        return self.watcher.run()

    @property
    def events(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.stream.getvalue().splitlines()
            if line.strip()
        ]

    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events]


def listing(*pulls: PullSummary, etag: str | None = None) -> PollResult:
    return PollResult(pulls=tuple(pulls), etag=etag)


# ---------------------------------------------------------------------------
# posting: the thing that must be hard to do by accident
# ---------------------------------------------------------------------------


def test_posting_requires_an_explicit_allowlist(cache: Cache) -> None:
    """`--post` alone is refused. A watcher that writes to anything it finds is
    not something to enable by accident."""
    with pytest.raises(PanoramaError, match="requires --repo"):
        Watcher(
            WatchConfig(owner="acme", post=True),
            cache,
            list_pulls=lambda _e: PollResult(),
            review=lambda _p, _s: True,
        )


def test_dry_run_is_the_default(cache: Cache) -> None:
    harness = Harness(cache, listing(pull()))
    harness.run()
    assert harness.reviewed == [("svc#1", False)]


def test_a_repository_outside_the_allowlist_is_reviewed_but_not_posted(
    cache: Cache,
) -> None:
    """The allowlist gates *writing*, not reviewing — a dry-run review of an
    unlisted repository is still useful and still safe."""
    harness = Harness(
        cache,
        listing(pull(1, repo="allowed"), pull(2, repo="other")),
        post=True,
        allowlist=frozenset({"allowed"}),
    )
    harness.run()
    assert harness.reviewed == [("allowed#1", True), ("other#2", False)]


def test_posting_happens_for_an_allowlisted_repository(cache: Cache) -> None:
    harness = Harness(
        cache, listing(pull(repo="allowed")), post=True, allowlist=frozenset({"allowed"})
    )
    stats = harness.run()
    assert stats.posted == 1


# ---------------------------------------------------------------------------
# cursors: never review the same commit twice
# ---------------------------------------------------------------------------


def test_a_new_pull_request_is_reviewed(cache: Cache) -> None:
    harness = Harness(cache, listing(pull()))
    stats = harness.run()
    assert stats.reviewed == 1
    assert cache.get_cursor("svc", 1) == SHA_A


def test_an_unchanged_pull_request_is_not_reviewed_again(cache: Cache) -> None:
    harness = Harness(cache, listing(pull()), listing(pull()))
    stats = harness.run()
    assert harness.reviewed == [("svc#1", False)]
    assert stats.skipped == 1


def test_a_moved_head_is_reviewed_again(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(head=SHA_A)), listing(pull(head=SHA_B)))
    harness.run()
    assert harness.reviewed == [("svc#1", False), ("svc#1", False)]
    assert cache.get_cursor("svc", 1) == SHA_B


def test_a_restart_resumes_from_the_cursor(cache: Cache) -> None:
    """The property that makes an unattended process tolerable: restarting does
    not re-review the world."""
    Harness(cache, listing(pull())).run()

    resumed = Harness(cache, listing(pull()))
    stats = resumed.run()

    assert resumed.reviewed == []
    assert stats.skipped == 1


def test_the_cursor_only_advances_after_a_review_completes(cache: Cache) -> None:
    """Advancing first would let a crash mid-review mark the pull request done."""
    harness = Harness(cache, listing(pull()))
    harness.raise_on["svc#1"] = PanoramaError("workspace is locked")
    harness.run()
    assert cache.get_cursor("svc", 1) is None


# ---------------------------------------------------------------------------
# what gets skipped
# ---------------------------------------------------------------------------


def test_drafts_are_skipped_by_default(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(draft=True)))
    harness.run()
    assert harness.reviewed == []
    assert any(e.get("reason") == "draft" for e in harness.events)


def test_drafts_can_be_included(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(draft=True)), include_drafts=True)
    harness.run()
    assert harness.reviewed == [("svc#1", False)]


def test_bot_authors_are_skipped_by_default(cache: Cache) -> None:
    """A bot reviewing a bot's pull request is a machine talking to itself, and
    the easiest way to build a review loop."""
    harness = Harness(cache, listing(pull(author="dependabot[bot]", author_is_bot=True)))
    harness.run()
    assert harness.reviewed == []


def test_bot_authors_can_be_included(cache: Cache) -> None:
    harness = Harness(
        cache,
        listing(pull(author="dependabot[bot]", author_is_bot=True)),
        include_bots=True,
    )
    harness.run()
    assert harness.reviewed == [("svc#1", False)]


def test_a_pull_request_that_vanishes_is_simply_absent(cache: Cache) -> None:
    """Closed mid-run: the next listing does not contain it, and nothing breaks."""
    harness = Harness(cache, listing(pull(1), pull(2)), listing(pull(1)))
    harness.run()
    assert harness.reviewed == [("svc#1", False), ("svc#2", False)]


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------


def test_the_hourly_cap_stops_further_reviews(cache: Cache) -> None:
    harness = Harness(
        cache,
        listing(*[pull(n) for n in range(1, 6)]),
        hourly_cap=2,
    )
    stats = harness.run()
    assert stats.reviewed == 2
    assert any(e["event"] == "hourly_cap_reached" for e in harness.events)


def test_the_cap_survives_a_restart(cache: Cache) -> None:
    """Counted from disk on purpose: a crash-looping watcher must not be able
    to refill its own budget by restarting."""
    Harness(cache, listing(pull(1), pull(2)), hourly_cap=2).run()
    resumed = Harness(cache, listing(pull(3), pull(4)), hourly_cap=2)
    stats = resumed.run()
    assert stats.reviewed == 0


# ---------------------------------------------------------------------------
# failure and recovery
# ---------------------------------------------------------------------------


def test_a_rate_limit_backs_off_without_reviewing(cache: Cache) -> None:
    harness = Harness(cache, RateLimited("secondary rate limit"), listing(pull()))
    stats = harness.run()
    assert harness.slept, "expected a backoff sleep"
    assert stats.reviewed == 1  # it recovers on the next poll
    assert any(e["event"] == "rate_limited" for e in harness.events)


def test_backoff_doubles_while_the_failure_persists(cache: Cache) -> None:
    harness = Harness(
        cache,
        RateLimited("slow down"),
        RateLimited("slow down"),
        RateLimited("slow down"),
    )
    harness.run()
    backoffs = [e["backoff_seconds"] for e in harness.events if "backoff_seconds" in e]
    assert backoffs == sorted(backoffs) and backoffs[0] < backoffs[-1]


def test_backoff_resets_after_a_successful_poll(cache: Cache) -> None:
    harness = Harness(
        cache,
        RateLimited("slow down"),
        listing(),
        RateLimited("slow down"),
    )
    harness.run()
    backoffs = [e["backoff_seconds"] for e in harness.events if "backoff_seconds" in e]
    assert backoffs[0] == backoffs[1], "a good poll should clear the backoff"


def test_a_network_failure_is_survived(cache: Cache) -> None:
    harness = Harness(cache, TransientPollError("connection reset"), listing(pull()))
    stats = harness.run()
    assert stats.errors == 1
    assert stats.reviewed == 1


def test_one_failing_review_does_not_stop_the_watcher(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(1), pull(2)))
    harness.raise_on["svc#1"] = PanoramaError("workspace is locked")
    stats = harness.run()
    assert harness.reviewed == [("svc#2", False)]
    assert stats.errors == 1


# ---------------------------------------------------------------------------
# conditional requests
# ---------------------------------------------------------------------------


def test_the_etag_is_sent_back_on_the_next_poll(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(), etag='W/"abc"'), listing(pull()))
    harness.run()
    assert harness.etags == [None, 'W/"abc"']


def test_not_modified_is_not_mistaken_for_an_empty_organisation(cache: Cache) -> None:
    """A 304 means "nothing changed", not "there are no open pull requests" —
    conflating them would drop the cursor logic on the floor."""
    harness = Harness(cache, PollResult(not_modified=True))
    stats = harness.run()
    assert stats.reviewed == 0 and stats.skipped == 0
    assert "not_modified" in harness.event_names()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------


def test_stopping_ends_the_loop(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(1)), listing(pull(2)), max_polls=None)

    original = harness._review

    def review_then_stop(summary, should_post):
        result = original(summary, should_post)
        harness.watcher.stop()
        return result

    harness.watcher._review = review_then_stop
    stats = harness.run()

    assert stats.reviewed == 1
    assert "shutdown_requested" in harness.event_names()


def test_shutdown_stops_taking_new_work_mid_poll(cache: Cache) -> None:
    """A termination signal must not kill a review halfway through, but it must
    stop the watcher picking up the next one."""
    harness = Harness(cache, listing(pull(1), pull(2), pull(3)))

    original = harness._review

    def review_then_stop(summary, should_post):
        result = original(summary, should_post)
        harness.watcher.stop()
        return result

    harness.watcher._review = review_then_stop
    harness.run()

    assert len(harness.reviewed) == 1


def test_a_completed_review_is_still_recorded_when_stopping(cache: Cache) -> None:
    harness = Harness(cache, listing(pull(1), pull(2)))
    original = harness._review

    def review_then_stop(summary, should_post):
        result = original(summary, should_post)
        harness.watcher.stop()
        return result

    harness.watcher._review = review_then_stop
    harness.run()
    assert cache.get_cursor("svc", 1) == SHA_A


# ---------------------------------------------------------------------------
# logging and parsing
# ---------------------------------------------------------------------------


def test_every_log_line_is_json(cache: Cache) -> None:
    """A watcher runs unattended; the useful question later is "what did it do
    at 3am", and grepping prose does not answer it."""
    harness = Harness(cache, listing(pull()))
    harness.run()
    assert harness.events
    assert all("event" in e and e["owner"] == "acme" for e in harness.events)


def test_the_start_line_records_the_dangerous_settings(cache: Cache) -> None:
    harness = Harness(
        cache, listing(), post=True, allowlist=frozenset({"allowed"}), hourly_cap=3
    )
    harness.run()
    started = next(e for e in harness.events if e["event"] == "watch_started")
    assert started["post"] is True
    assert started["allowlist"] == ["allowed"]
    assert started["hourly_cap"] == 3


def test_parse_pull_list_reads_a_gh_payload() -> None:
    pulls = parse_pull_list(
        [
            {
                "number": 7,
                "headRefOid": SHA_A,
                "title": "t",
                "url": "u",
                "isDraft": True,
                "repository": {"name": "svc"},
                "author": {"login": "someone"},
            }
        ]
    )
    assert pulls[0].repo == "svc" and pulls[0].number == 7
    assert pulls[0].draft is True and pulls[0].author == "someone"


def test_parse_pull_list_detects_bots() -> None:
    pulls = parse_pull_list(
        [
            {
                "number": 1,
                "headRefOid": SHA_A,
                "repository": {"name": "svc"},
                "author": {"login": "dependabot[bot]"},
            }
        ]
    )
    assert pulls[0].author_is_bot is True


def test_parse_pull_list_tolerates_junk() -> None:
    """Forge payloads are external data. One unusual pull request must not be
    able to stop the watcher."""
    pulls = parse_pull_list(
        [
            "not a dict",
            {},
            {"number": "seven", "headRefOid": SHA_A, "repository": {"name": "svc"}},
            {"number": 1, "headRefOid": "", "repository": {"name": "svc"}},
            {"number": 2, "headRefOid": SHA_A, "repository": {"name": "svc"}, "author": None},
        ]
    )
    assert [p.number for p in pulls] == [2]


def test_summarise_reports_the_totals(cache: Cache) -> None:
    harness = Harness(cache, listing(pull()))
    line = summarise(harness.run())
    assert "1 reviewed" in line and "0 posted" in line


# ---------------------------------------------------------------------------
# the gh-backed lister
# ---------------------------------------------------------------------------


def gh_payload(*repos) -> str:
    return json.dumps(
        {
            "data": {
                "repositoryOwner": {
                    "repositories": {
                        "nodes": [
                            {"name": name, "pullRequests": {"nodes": list(nodes)}}
                            for name, nodes in repos
                        ]
                    }
                }
            }
        }
    )


def lister(code: int = 0, out: str = "", err: str = ""):
    from panorama.watch import GhPullLister

    return GhPullLister("acme", run=lambda _argv: (code, out, err))


def test_the_lister_flattens_the_graphql_response() -> None:
    out = gh_payload(
        (
            "svc",
            [
                {
                    "number": 4,
                    "headRefOid": SHA_A,
                    "title": "t",
                    "url": "u",
                    "isDraft": False,
                    "author": {"login": "someone", "__typename": "User"},
                }
            ],
        ),
        ("other", []),
    )
    pulls = lister(out=out)().pulls
    assert [(p.repo, p.number) for p in pulls] == [("svc", 4)]


def test_the_lister_classifies_a_rate_limit() -> None:
    from panorama.watch import RateLimited as RL

    with pytest.raises(RL):
        lister(code=1, err="You have exceeded a secondary rate limit")()


def test_the_lister_classifies_a_network_failure() -> None:
    with pytest.raises(TransientPollError):
        lister(code=1, err="dial tcp: connection refused")()


def test_the_lister_reports_anything_else_without_retrying_forever() -> None:
    with pytest.raises(PanoramaError) as caught:
        lister(code=1, err="gh: Not Found")()
    assert not isinstance(caught.value, (RateLimited, TransientPollError))


def test_the_lister_never_echoes_raw_gh_stderr() -> None:
    """stderr is the likeliest place for a token hint or an operator path."""
    secret = "ghp_averyrealisticlookingtokenvalue"
    with pytest.raises(PanoramaError) as caught:
        lister(code=1, err=f"error: bad credentials {secret}")()
    assert secret not in str(caught.value)


def test_the_lister_survives_a_malformed_body() -> None:
    with pytest.raises(TransientPollError):
        lister(out="{not json")()


def test_the_lister_survives_an_unknown_owner() -> None:
    assert lister(out=json.dumps({"data": {"repositoryOwner": None}}))().pulls == ()


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------


def test_cli_refuses_post_without_an_allowlist(tmp_path: Path, monkeypatch) -> None:
    """The safety default, asserted at the surface a user actually touches."""
    from typer.testing import CliRunner

    from panorama import cli

    monkeypatch.setattr(cli.Cache, "for_owner", lambda *a, **k: Cache(tmp_path / "c.db"))
    result = CliRunner().invoke(cli.app, ["watch", "acme", "--post", "--max-polls", "1"])
    assert result.exit_code != 0
    assert "--repo" in result.output


def test_cli_dry_run_polls_without_posting(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from panorama import cli

    monkeypatch.setattr(cli.Cache, "for_owner", lambda *a, **k: Cache(tmp_path / "c.db"))
    monkeypatch.setattr(
        cli, "GhPullLister", lambda *a, **k: (lambda _etag: PollResult())
    )
    result = CliRunner().invoke(
        cli.app, ["watch", "acme", "--max-polls", "1", "--interval", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "1 poll(s)" in result.output
