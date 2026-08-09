"""A local watcher that reviews pull requests without being asked.

## Why this is a poller and not a webhook

The obvious design for automatic review is a hosted GitHub App: a webhook
endpoint, an event handler, a queue. Panorama cannot build that, and the reason
is structural rather than a matter of effort.

A hosted service has no Claude Code subscription. That subscription is an
interactive credential belonging to a person and a machine; it cannot go in a
serverless function, and putting one there would break both "no API key" and
"credentials stay with the tool that owns them" at once.

So automation is a **pull** model: a long-lived local process that polls for
pull requests which are new or whose head commit moved. No inbound network, no
public URL, no tunnel, no runner registration, nothing to operate. The honest
cost is that reviews lag by the poll interval and only happen while someone's
machine is awake.

## Safety defaults, all deliberate

An autonomous process that writes to a shared repository is a different kind of
risk from a command someone runs and reads. Everything here is arranged so the
dangerous version has to be asked for explicitly and repeatedly:

- **Dry-run by default.** Posting needs `--post`, and `--post` on its own is
  refused: it additionally requires an explicit list of repositories it may
  write to. A process that posts to anything it finds is a mistake waiting for
  a bad night.
- **An unmoved head commit is never re-reviewed.** The cursor is the head SHA
  the pull request was last reviewed at, kept on disk, so a restart resumes
  rather than re-reviewing the world.
- **A hard hourly cap**, counted from disk rather than memory, so a
  crash-looping watcher cannot reset its own budget by restarting. A force-push
  loop cannot spend the day.
- **Drafts and bot authors are skipped** unless asked for. Both are usually
  noise, and a bot-authored pull request reviewed by a bot is a machine talking
  to itself.
- **One review at a time.** The workspace lock already enforces this; the
  watcher treats contention as "try again next poll" rather than as an error,
  because the other holder is usually a human running a review by hand.
- **Clean shutdown.** A termination signal stops the loop at the next safe
  point rather than killing a review halfway through and leaving a workspace
  half-updated.

Everything the watcher talks to is injectable, so the whole loop — including
rate limiting, network failure and recovery — is tested offline with no `gh`,
no network and no subscription.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from panorama.cache import Cache
from panorama.errors import PanoramaError

#: How long between polls when nothing is happening.
DEFAULT_INTERVAL_SECONDS = 60

#: Reviews per hour, counted across restarts. Deliberately low: this is a
#: backstop against a loop, not a throughput setting.
DEFAULT_HOURLY_CAP = 10

#: Backoff for rate limits and network failures: doubling, bounded.
_BACKOFF_START_SECONDS = 30
_BACKOFF_MAX_SECONDS = 900

_HOUR_SECONDS = 3600


class RateLimited(PanoramaError):
    """The forge asked us to slow down. Not a failure — an instruction."""


class TransientPollError(PanoramaError):
    """The listing failed in a way that is worth retrying (network, 5xx)."""


@dataclass(frozen=True)
class PullSummary:
    """The little Panorama needs to decide whether to review a pull request."""

    repo: str
    number: int
    head_sha: str
    title: str = ""
    url: str = ""
    draft: bool = False
    author: str = ""
    author_is_bot: bool = False

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class PollResult:
    """What one listing returned, plus the ETag to send next time."""

    pulls: tuple[PullSummary, ...] = ()
    etag: str | None = None
    #: True when the forge said nothing changed, so `pulls` is empty because
    #: there was no body — not because there are no open pull requests.
    not_modified: bool = False


#: Lists open pull requests for an owner. Takes the previous ETag so the forge
#: can answer "nothing changed" cheaply.
PullLister = Callable[[str | None], PollResult]

#: Reviews one pull request. Returns True if a review was actually delivered.
Reviewer = Callable[[PullSummary, bool], bool]


@dataclass
class WatchConfig:
    """Everything that changes what the watcher does."""

    owner: str
    interval: int = DEFAULT_INTERVAL_SECONDS
    post: bool = False
    #: Repositories `--post` may write to. Empty with `post=True` is refused.
    allowlist: frozenset[str] = frozenset()
    hourly_cap: int = DEFAULT_HOURLY_CAP
    #: Stop after this many polls. Only tests pass a value.
    max_polls: int | None = None
    include_drafts: bool = False
    include_bots: bool = False

    def validate(self) -> None:
        if self.post and not self.allowlist:
            raise PanoramaError(
                "--post requires --repo to name at least one repository it may "
                "comment on. A watcher that posts to anything it finds is not "
                "something to enable by accident."
            )
        if self.interval < 1:
            raise PanoramaError("the poll interval must be at least one second")


@dataclass
class WatchOutcome:
    """What happened to one pull request in one poll."""

    ref: str
    action: str
    detail: str = ""


@dataclass
class WatchStats:
    """Running totals, for the final line and for tests."""

    polls: int = 0
    reviewed: int = 0
    skipped: int = 0
    posted: int = 0
    errors: int = 0
    outcomes: list[WatchOutcome] = field(default_factory=list)


class Watcher:
    """The poll loop.

    ``list_pulls`` and ``review`` are injected so the whole thing runs offline.
    ``sleep`` and ``now`` are injected so tests do not actually wait.
    """

    def __init__(
        self,
        config: WatchConfig,
        cache: Cache,
        *,
        list_pulls: PullLister,
        review: Reviewer,
        sleep: Callable[[float], None] = time.sleep,
        stream: Any = None,
    ) -> None:
        config.validate()
        self.config = config
        self.cache = cache
        self._list_pulls = list_pulls
        self._review = review
        self._sleep = sleep
        self._stream = stream if stream is not None else sys.stdout
        self.stats = WatchStats()
        self._etag: str | None = None
        self._backoff = _BACKOFF_START_SECONDS
        self._stopping = False

    # -- logging ------------------------------------------------------------

    def log(self, event: str, **fields: Any) -> None:
        """One JSON object per line.

        Structured because a watcher runs unattended: the interesting question
        later is "what did it do at 3am", and grepping prose does not answer it.
        Never logs raw forge output or command environments.
        """
        record = {"event": event, "owner": self.config.owner, **fields}
        print(json.dumps(record, sort_keys=True), file=self._stream, flush=True)

    # -- shutdown -----------------------------------------------------------

    def stop(self, *_signal_args: Any) -> None:
        """Ask the loop to finish the work in hand and exit."""
        if not self._stopping:
            self._stopping = True
            self.log("shutdown_requested")

    @property
    def stopping(self) -> bool:
        return self._stopping

    # -- the loop -----------------------------------------------------------

    def run(self) -> WatchStats:
        """Poll until stopped, or until ``max_polls`` is exhausted."""
        previous = self._install_signal_handlers()
        self.log(
            "watch_started",
            interval=self.config.interval,
            post=self.config.post,
            allowlist=sorted(self.config.allowlist),
            hourly_cap=self.config.hourly_cap,
        )
        try:
            while not self._stopping:
                if self.config.max_polls is not None:
                    if self.stats.polls >= self.config.max_polls:
                        break
                self.poll_once()
                if self._stopping:
                    break
                if self.config.max_polls is not None:
                    if self.stats.polls >= self.config.max_polls:
                        break
                self._sleep(self.config.interval)
        finally:
            self._restore_signal_handlers(previous)
            self.log(
                "watch_stopped",
                polls=self.stats.polls,
                reviewed=self.stats.reviewed,
                posted=self.stats.posted,
                skipped=self.stats.skipped,
                errors=self.stats.errors,
            )
        return self.stats

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, self.stop)
            except (ValueError, OSError):  # pragma: no cover - not on main thread
                pass
        return previous

    def _restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass

    def poll_once(self) -> list[WatchOutcome]:
        """One listing, and a review of anything that needs one."""
        self.stats.polls += 1
        outcomes: list[WatchOutcome] = []

        try:
            result = self._list_pulls(self._etag)
        except RateLimited as exc:
            self.stats.errors += 1
            self._back_off("rate_limited", exc.message)
            return outcomes
        except (TransientPollError, PanoramaError) as exc:
            self.stats.errors += 1
            self._back_off("poll_failed", exc.message)
            return outcomes

        # A successful listing means whatever we were backing off from is over.
        self._backoff = _BACKOFF_START_SECONDS

        if result.not_modified:
            self.log("not_modified")
            return outcomes

        if result.etag:
            self._etag = result.etag

        self.log("polled", open_pulls=len(result.pulls))

        for pull in result.pulls:
            if self._stopping:
                # Stop taking new work; anything already reviewed is recorded.
                outcomes.append(WatchOutcome(pull.ref, "skipped", "shutting down"))
                break
            outcome = self._consider(pull)
            outcomes.append(outcome)

        self.stats.outcomes.extend(outcomes)
        return outcomes

    def _back_off(self, event: str, detail: str) -> None:
        delay = self._backoff
        self.log(event, detail=detail, backoff_seconds=delay)
        self._sleep(delay)
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX_SECONDS)

    # -- deciding what to do with one pull request --------------------------

    def _consider(self, pull: PullSummary) -> WatchOutcome:
        skip = self._skip_reason(pull)
        if skip is not None:
            self.stats.skipped += 1
            self.log("skipped", ref=pull.ref, reason=skip)
            return WatchOutcome(pull.ref, "skipped", skip)

        if self.cache.reviews_since(_HOUR_SECONDS) >= self.config.hourly_cap:
            # Counted from disk, so restarting does not refill the budget.
            self.stats.skipped += 1
            self.log("hourly_cap_reached", ref=pull.ref, cap=self.config.hourly_cap)
            return WatchOutcome(pull.ref, "skipped", "hourly cap reached")

        should_post = self.config.post and pull.repo in self.config.allowlist
        self.log("reviewing", ref=pull.ref, head=pull.head_sha[:12], post=should_post)

        try:
            delivered = self._review(pull, should_post)
        except PanoramaError as exc:
            # A workspace held by another run is contention, not failure: the
            # next poll will pick it up. Anything else is logged and skipped so
            # one bad pull request cannot stop the watcher.
            self.stats.errors += 1
            self.log("review_failed", ref=pull.ref, detail=exc.message)
            return WatchOutcome(pull.ref, "error", exc.message)

        # The cursor advances only after a review actually completed. Advancing
        # it first would mean a crash mid-review silently marked the pull
        # request done.
        self.cache.set_cursor(pull.repo, pull.number, pull.head_sha)
        self.stats.reviewed += 1
        if delivered and should_post:
            self.stats.posted += 1
        self.log(
            "reviewed",
            ref=pull.ref,
            head=pull.head_sha[:12],
            posted=bool(delivered and should_post),
        )
        return WatchOutcome(pull.ref, "reviewed")

    def _skip_reason(self, pull: PullSummary) -> str | None:
        if pull.draft and not self.config.include_drafts:
            return "draft"
        if pull.author_is_bot and not self.config.include_bots:
            # A bot reviewing a bot's pull request is a machine talking to
            # itself, and it is the easiest way to build a review loop.
            return "bot author"
        if self.cache.get_cursor(pull.repo, pull.number) == pull.head_sha:
            return "already reviewed at this commit"
        return None


def summarise(stats: WatchStats) -> str:
    """A human-readable closing line for the terminal."""
    return (
        f"{stats.polls} poll(s): {stats.reviewed} reviewed, "
        f"{stats.posted} posted, {stats.skipped} skipped, {stats.errors} error(s)."
    )


def parse_pull_list(payload: Sequence[dict]) -> tuple[PullSummary, ...]:
    """Turn `gh` JSON into summaries, tolerating missing fields.

    Forge payloads are external data: a field that is absent, null, or a
    different type must produce a conservative summary rather than an
    exception, or one unusual pull request would stop the watcher.
    """
    pulls: list[PullSummary] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        head = item.get("headRefOid") or item.get("head_sha") or ""
        repo = _repo_name(item)
        if not isinstance(number, int) or not head or not repo:
            continue
        author = item.get("author") or {}
        if not isinstance(author, dict):
            author = {}
        pulls.append(
            PullSummary(
                repo=repo,
                number=number,
                head_sha=str(head),
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                draft=bool(item.get("isDraft") or item.get("draft")),
                author=str(author.get("login") or ""),
                author_is_bot=_is_bot(author),
            )
        )
    return tuple(pulls)


def _repo_name(item: dict) -> str:
    repository = item.get("repository")
    if isinstance(repository, dict):
        name = repository.get("name") or repository.get("nameWithOwner") or ""
        return str(name).rsplit("/", 1)[-1]
    return str(item.get("repo") or "")


def _is_bot(author: dict) -> bool:
    if bool(author.get("is_bot")) or author.get("__typename") == "Bot":
        return True
    login = str(author.get("login") or "")
    return login.endswith("[bot]")


# ---------------------------------------------------------------------------
# the real listing
# ---------------------------------------------------------------------------

#: One GraphQL query for the whole owner. `repositoryOwner` rather than
#: `organization` so a personal account works too — a demo org is usually a
#: user, and failing there would make the feature untestable by its author.
_PULLS_QUERY = """
query($owner: String!) {
  repositoryOwner(login: $owner) {
    repositories(first: 100, isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name
        pullRequests(states: OPEN, first: 20, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            number
            title
            url
            isDraft
            headRefOid
            author { login __typename }
          }
        }
      }
    }
  }
}
"""

#: Message fragments that mean "slow down" rather than "something is broken".
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse detection", "403")
#: ...and ones that mean "try again shortly".
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporary failure",
    "could not resolve",
    "502",
    "503",
    "504",
)


class GhPullLister:
    """Lists an owner's open pull requests through the `gh` CLI.

    One GraphQL request per poll for the whole organisation, which is cheap
    enough that conditional requests would not pay for themselves: the
    alternative — a REST call per repository with an ETag each — trades one
    request for dozens in order to make most of them free. The `PollResult`
    type still carries an ETag because the interface should not foreclose a
    lister that benefits from one.

    Never surfaces raw `gh` output: stderr is the most likely place for a token
    hint or an operator path to appear, so failures become host-authored,
    classified messages.
    """

    def __init__(self, owner: str, *, gh_path: str = "gh", run=None) -> None:
        self.owner = owner
        self.gh_path = gh_path
        self._run = run

    def __call__(self, etag: str | None = None) -> PollResult:
        import subprocess

        argv = [
            self.gh_path,
            "api",
            "graphql",
            "-f",
            f"query={_PULLS_QUERY}",
            "-F",
            f"owner={self.owner}",
        ]
        if self._run is not None:
            code, out, err = self._run(argv)
        else:
            try:
                proc = subprocess.run(argv, input="", capture_output=True, text=True)
            except FileNotFoundError as exc:
                raise PanoramaError(
                    f"{self.gh_path!r} was not found on PATH; it is required to "
                    "list pull requests."
                ) from exc
            code, out, err = proc.returncode, proc.stdout, proc.stderr

        if code != 0:
            raise _classify(err)

        try:
            payload = json.loads(out)
        except ValueError as exc:
            raise TransientPollError("the pull request listing was not valid JSON") from exc

        return PollResult(pulls=parse_pull_list(_flatten(payload)))


def _flatten(payload: Any) -> list[dict]:
    """Pull the flat pull-request list out of the nested GraphQL response."""
    owner = ((payload or {}).get("data") or {}).get("repositoryOwner") or {}
    repositories = (owner.get("repositories") or {}).get("nodes") or []
    flat: list[dict] = []
    for repository in repositories:
        if not isinstance(repository, dict):
            continue
        name = repository.get("name")
        for node in (repository.get("pullRequests") or {}).get("nodes") or []:
            if isinstance(node, dict):
                flat.append({**node, "repository": {"name": name}})
    return flat


def _classify(stderr: str) -> PanoramaError:
    """Turn a failed listing into the right kind of error, without echoing it.

    The distinction matters: a rate limit is an instruction to wait, a network
    blip is worth retrying, and anything else should be reported rather than
    retried forever.
    """
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return RateLimited("the forge is rate limiting this account")
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return TransientPollError("the pull request listing failed; will retry")
    return PanoramaError(
        "could not list pull requests. Check `panorama doctor` and that `gh` is "
        "authenticated for this owner."
    )
