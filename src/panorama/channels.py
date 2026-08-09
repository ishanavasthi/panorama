"""The interface every retrieval channel implements.

Retrieval's job is to answer one question — *which sibling repositories are
worth looking at, and where in them* — and there is more than one honest way to
answer it. V1 had one: shared vocabulary between the diff and the siblings. V2
adds structural ones: who declares a dependency on whom, what a repository
actually exports, what changes alongside what.

This module defines the shape they all share, and nothing else. It contains no
ranking logic, no fusion, and no knowledge of any particular channel.

Three properties are deliberate.

**A channel ranks, it does not score.** Each returns its own ordered list, and
fusion (V2.7) combines them by *rank* rather than by magnitude. Channels measure
incomparable things — a lexical overlap score and a dependency-edge distance
have no common unit — and pretending otherwise would mean inventing weights,
which on an 18-case corpus is overfitting with extra steps.

**Every ranked repository carries a justification.** Not for decoration: the
report has to be able to say *why* a repository was examined, and "ranked #1 by
dependency edge, #3 by lexical overlap, not seen by the symbol index" is a
sentence a reviewer can argue with. A ranking nobody can interrogate is a
ranking nobody should trust.

**Channels are independent and additive.** A channel may not read another's
output, and adding one must not change any other's. That is what makes the
evaluation harness able to attribute a change to the thing that caused it —
and it is why the equivalence test for the first channel matters so much.

A channel that has nothing to say returns an empty ranking. That is a normal
outcome, not a failure: the dependency-graph channel is silent for a Python
consumer of a TypeScript API, because no manifest anywhere records that edge.
Fusion has to survive silence, so silence is part of the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from panorama.intake import PullRequest
from panorama.workspace import Workspace


@dataclass(frozen=True)
class Hit:
    """A specific place in a sibling repository worth confirming.

    Lives here rather than with the lexical pass because it is the shared
    currency of *file-level leads*: the symbol index points at a definition,
    the lexical pass points at a matched line, and the report and the prompt
    treat them identically. ``token`` is whatever made the channel look —
    a matched identifier, an exported symbol name — and ``window`` is the few
    surrounding lines, carried so the model can orient without a second read.

    A lead is not evidence. Nothing here is ever cited directly: a finding's
    citation is validated against the live checkout, separately, every run.
    """

    repo: str
    path: str
    line: int
    token: str
    window: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedRepo:
    """One repository a channel considers relevant, and why.

    ``rank`` is competition ranking within this channel: ties share a rank, so
    a three-way tie for first is rank 1 three times. That keeps a tie visible
    instead of letting sort order silently break it — the same discipline the
    evaluation scorer uses, for the same reason.
    """

    repo: str
    rank: int
    score: float
    #: Plain-language reason this repository surfaced, for the report.
    justification: str


@dataclass(frozen=True)
class ChannelResult:
    """What one channel found."""

    channel: str
    ranked: tuple[RankedRepo, ...] = ()
    #: File-level leads, where the channel produces them. A channel that ranks
    #: repositories without pointing at lines returns none, which is fine.
    hits: tuple[Hit, ...] = ()
    #: Anything the report should say about how this run went — a cap that bit,
    #: a manifest that would not parse. Notes are for the human, not the model.
    notes: tuple[str, ...] = ()
    #: True when a cap stopped the channel early, so the report can say the
    #: search was incomplete rather than implying it was exhaustive.
    truncated: bool = False

    @property
    def repos(self) -> tuple[str, ...]:
        return tuple(entry.repo for entry in self.ranked)


@runtime_checkable
class RetrievalChannel(Protocol):
    """One way of deciding which siblings matter.

    A Protocol rather than a base class: channels share an interface, not an
    implementation, and there is no behaviour worth inheriting. It also keeps
    the door shut on a lifecycle — there is no setup hook, no teardown, and no
    ordering contract between channels.
    """

    #: Stable identifier, used in provenance and in evaluation output.
    name: str

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        """Rank sibling repositories for this pull request.

        Must not mutate the workspace, must not reach the network, and must not
        depend on any other channel having run.
        """
        ...


#: The standard reciprocal-rank-fusion constant. It is not tuned and should not
#: be: the only data available to fit it is an 18-case corpus, and a constant
#: fitted on 18 cases is overfitting with extra steps.
RRF_K = 60


@dataclass(frozen=True)
class ChannelVote:
    """One channel's opinion about one repository, kept for the report."""

    channel: str
    rank: int
    justification: str


@dataclass(frozen=True)
class FusedRepo:
    """A repository after every channel has had its say."""

    repo: str
    score: float
    votes: tuple[ChannelVote, ...]

    @property
    def provenance(self) -> tuple[str, ...]:
        """One readable line per channel that ranked this repository.

        This is what lets the report answer "why was this repository even
        looked at" — and a ranking nobody can interrogate is a ranking nobody
        should trust.
        """
        return tuple(
            f"{vote.channel} #{vote.rank}: {vote.justification}" for vote in self.votes
        )


def fuse(results: list[ChannelResult], *, k: int = RRF_K) -> list[FusedRepo]:
    """Combine channel rankings by reciprocal rank fusion.

    Each repository scores the sum of ``1 / (k + rank)`` over the channels that
    ranked it. Channels that did not rank it contribute nothing — they are
    silent about it, which is not the same as voting against it.

    **Why fusion by rank rather than by score.** Channels measure incomparable
    things: a lexical overlap score and a dependency-edge distance share no
    unit. Combining them numerically would mean choosing weights, and the only
    data available to choose them from is the evaluation corpus, which is small
    enough that any weight fitted on it describes the corpus rather than the
    problem. Rank fusion needs no weights, survives a channel being silent, and
    stays explainable.

    **What it costs.** Fusion throws away *magnitude*. A channel that is
    overwhelmingly confident about its top result cannot say so — first place is
    first place. The consequence is real and shows up as two second-places
    outweighing one first: agreement between channels is rewarded, sometimes
    more than one channel's strong conviction deserves. That is a deliberate
    trade of some ceiling for a lot of robustness, and the evaluation harness is
    what keeps it a testable choice rather than a permanent one.

    Ties break by repository name so the order is deterministic; the *rank* a
    repository is given downstream still shares with everything it ties with.
    """
    scores: dict[str, float] = {}
    votes: dict[str, list[ChannelVote]] = {}

    for result in results:
        for entry in result.ranked:
            scores[entry.repo] = scores.get(entry.repo, 0.0) + 1.0 / (k + entry.rank)
            votes.setdefault(entry.repo, []).append(
                ChannelVote(
                    channel=result.channel,
                    rank=entry.rank,
                    justification=entry.justification,
                )
            )

    return [
        FusedRepo(repo=repo, score=round(score, 6), votes=tuple(votes[repo]))
        for repo, score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def competition_ranked(
    scored: list[tuple[str, float]],
    *,
    justify,
) -> tuple[RankedRepo, ...]:
    """Turn ``(repo, score)`` pairs into a ranking where ties share a rank.

    Sorted by descending score then by name, so the *order* is deterministic —
    but the ``rank`` a repository is given depends only on how many repositories
    scored strictly higher. A repository that merely ties for the top is rank 1
    alongside the others rather than being promoted by alphabetical luck.

    ``justify`` is called with ``(repo, score)`` and returns the human-readable
    reason. It is a callback so each channel explains itself in its own terms
    without this function knowing anything about ranking mechanics.
    """
    ordered = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
    return tuple(
        RankedRepo(
            repo=repo,
            rank=1 + sum(1 for _, other in ordered if other > score),
            score=score,
            justification=justify(repo, score),
        )
        for repo, score in ordered
    )
