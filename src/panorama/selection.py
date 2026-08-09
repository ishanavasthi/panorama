"""Choosing which repositories are worth cloning.

V1 cloned every repository in the organisation and refused to run above fifty.
That ceiling was a symptom of cloning everything rather than a real limit on the
idea: the expensive part is the clone, not the reasoning.

So provisioning splits in two. First a **metadata phase** reads each
repository's manifest through the API without cloning anything — cheap enough
for hundreds of repositories. Then a **selection phase** ranks candidates and
clones only the top few. The fifty-repository ceiling becomes a *clone budget*.

## The failure mode this introduces

Selection can be wrong, and when it is wrong it is **silent**. A relevant
sibling that is never cloned cannot be searched, so it produces no lead, no
finding, and no warning — the review simply comes back thinner and nobody knows
why. That is the worst failure mode in the whole system, worse than a wrong
finding, because a wrong finding is visible.

Three things bound it, and all three matter:

- **The report always states considered-versus-cloned.** A review of an
  organisation where 12 of 140 repositories were examined says so, every time.
- **`--all-repos` restores V1 behaviour** for anyone who would rather wait.
- **Selection recall is measured separately from ranking** — the evaluation asks
  whether a labelled target survives selection at all, which is a different
  question from where it ranks afterwards.

## How candidates are ordered

1. The pull request's own repository, always, unconditionally.
2. Repositories connected by a **declared dependency edge**, nearest and most
   consequential first, using exactly the same resolution the retrieval channel
   uses. A selection that resolved edges differently from retrieval would drop
   repositories retrieval was about to ask for.
3. Repositories a **previous run's symbol index** says import a name this change
   removes, or export one it adds. This is the cache earning its keep: it makes
   the *second* review of an organisation better targeted than the first.
4. Whatever budget is left, filled in a stable alphabetical order.

That last step deserves defending, because filling a budget with arbitrary
repositories looks unprincipled. The alternative is leaving the budget unspent,
and an unspent budget is strictly worse: the lexical channel finds links through
shared vocabulary and needs no declared edge to do it, so an extra cloned
repository is an extra chance of a real hit at no additional risk. The ordering
is deterministic so two runs of the same review examine the same repositories.

Contains no fixture names (constraint #2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from panorama.dependencies import graph_from_manifests
from panorama.languages import ManifestFacts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from panorama.cache import Cache

#: How many repositories a review clones when it has to choose. Chosen to be
#: comfortably more than any single change plausibly touches, while small enough
#: that a first review of a large organisation finishes in a reasonable time.
DEFAULT_CLONE_BUDGET = 15

#: A listing larger than this is refused rather than paged forever. Not a
#: statement about what is reviewable — the budget handles that — only a bound
#: on how much enumerating one command will do.
MAX_LISTED_REPOS = 1000

#: Why a repository was chosen, strongest first. Ordering is the selection.
REASON_PR_REPO = "the pull request's own repository"
REASON_DEPENDENCY_EDGE = "connected by a declared dependency"
REASON_SYMBOL_MATCH = "a previous run indexed a matching exported or imported name"
REASON_BUDGET_FILL = "spare clone budget, in stable order"


@dataclass(frozen=True)
class Selection:
    """Which repositories a review will look at, and why."""

    selected: tuple[str, ...]
    considered: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict)
    budget: int = DEFAULT_CLONE_BUDGET
    unbounded: bool = False

    @property
    def n_considered(self) -> int:
        return len(self.considered)

    @property
    def n_selected(self) -> int:
        return len(self.selected)

    @property
    def complete(self) -> bool:
        """Whether every repository considered is also being examined."""
        return self.n_selected >= self.n_considered

    def summary(self) -> str:
        """One line for the report. Said on every run, not only when it bites."""
        if self.complete:
            return f"Examined all {self.n_considered} repositories in the organisation."
        return (
            f"Examined {self.n_selected} of {self.n_considered} repositories: "
            "the rest were not cloned, so anything they contain was not searched. "
            "Re-run with --all-repos to examine every repository."
        )


def select_repositories(
    pr_repo: str,
    manifests: dict[str, tuple[ManifestFacts, str | None]],
    *,
    budget: int = DEFAULT_CLONE_BUDGET,
    all_repos: bool = False,
    cache: Cache | None = None,
    removed_symbols: frozenset[str] = frozenset(),
    added_symbols: frozenset[str] = frozenset(),
) -> Selection:
    """Order the organisation by relevance and cut it to the clone budget."""
    considered = tuple(sorted(manifests))

    if all_repos or budget <= 0 or len(considered) <= budget:
        # Nothing to choose between: take everything, and say so rather than
        # implying a selection happened.
        reasons = {name: REASON_PR_REPO if name == pr_repo else "" for name in considered}
        return Selection(
            selected=considered,
            considered=considered,
            reasons=reasons,
            budget=budget,
            unbounded=True,
        )

    ordered: list[str] = []
    reasons: dict[str, str] = {}

    def take(name: str, reason: str) -> None:
        if name in reasons or name not in manifests:
            return
        reasons[name] = reason
        ordered.append(name)

    take(pr_repo, REASON_PR_REPO)

    # -- declared dependency edges, using retrieval's own resolution ---------
    graph = graph_from_manifests(manifests)
    if pr_repo in graph.nodes:
        for repo, _direction, _distance in graph.related(pr_repo):
            take(repo, REASON_DEPENDENCY_EDGE)

    # -- what a previous run already learned ---------------------------------
    if cache is not None and (removed_symbols or added_symbols):
        for name in considered:
            if name in reasons:
                continue
            if _cached_symbol_match(cache, name, removed_symbols, added_symbols):
                take(name, REASON_SYMBOL_MATCH)

    # -- spend what is left rather than leaving it idle ----------------------
    for name in considered:
        if len(ordered) >= budget:
            break
        take(name, REASON_BUDGET_FILL)

    selected = tuple(ordered[:budget])
    return Selection(
        selected=selected,
        considered=considered,
        reasons={name: reasons[name] for name in selected},
        budget=budget,
    )


def _cached_symbol_match(
    cache: Cache,
    repo: str,
    removed: frozenset[str],
    added: frozenset[str],
) -> bool:
    """Whether any cached index for ``repo`` touches the changed names.

    Deliberately reads *any* commit's index rather than a specific one. This is
    selection, not evidence: being wrong here means cloning a repository that
    turns out to be irrelevant, which costs a little time. The index that
    actually informs the review is rebuilt against the real checkout afterwards,
    where the commit does have to match.
    """
    from panorama.symbols import CACHE_KIND

    for head_sha in cache.known_shas(repo):
        payload = cache.get_facts(repo, head_sha, CACHE_KIND)
        if not isinstance(payload, dict):
            continue
        exports = payload.get("exports") or {}
        imports = payload.get("imports") or {}
        if not isinstance(exports, dict) or not isinstance(imports, dict):
            continue
        if removed & imports.keys() or added & exports.keys():
            return True
    return False
