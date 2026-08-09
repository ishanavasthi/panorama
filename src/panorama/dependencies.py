"""Who depends on whom, read from the manifests repositories already ship.

The lexical channel can only find a link that shares vocabulary. This one finds
links that share *nothing* — the case that motivates the whole channel is a
convention violated by an **absence**, where the diff and the document it breaks
have no word in common and no amount of ranking will ever connect them. A
declared dependency connects them anyway.

## Edges resolve declared name to declared name

The tempting implementation matches a dependency string against a directory in
the workspace. It works perfectly on a tidy fixture organisation and fails on
real ones, where the repository directory and the published package name are
routinely different things.

So instead: read every repository's own manifest, build a map from the name each
package **declares for itself** to the repository that declares it, and resolve
every dependency list through that map. No repository-name string matching
anywhere. A dependency that resolves to nothing is external — an ordinary
outcome, and worth counting rather than discarding, because "40 of 44
dependencies are outside this organisation" is useful context.

## Ranking is by what kind of edge it is, not by how many there are

A repository's rank in this channel comes from a fixed ordering of edge kinds,
nearest and most consequential first:

1. a **direct dependent** — something that declares a dependency on the pull
   request's repository, and can therefore be *broken* by it;
2. a **direct dependency** — something the pull request's repository declares a
   dependency on, which is where shared helpers and organisation conventions
   live;
3. the same two, one hop further out, and so on.

Two things follow from ranking by kind rather than by comparison.

**A provider never occupies rank 1.** A consumer can be broken by this change; a
provider can only be duplicated or contradicted by it. The first is a stronger
claim than the second, always, and that ordering holds whether or not any
consumer happens to exist. If ranks were assigned by comparing whatever this
channel found, a repository could be promoted to first place purely because
nothing better turned up — which would make the rank mean "best of a bad lot"
rather than "strong structural claim".

**The rank is comparable across runs**, because it encodes the kind of evidence
rather than a position in a list. That matters when these ranks are fused with
another channel's: fusion is only meaningful if a rank means the same thing on
both sides.

## What this channel cannot see

A Python client and a Go gateway can both depend utterly on a TypeScript API and
appear in no manifest anywhere, because the coupling is an HTTP contract. This
channel is *silent* for them, and silence is a correct answer rather than a
failure. About a third of the evaluation corpus is built to be exactly that
case, so that fusion is forced to survive abstention instead of being flattered
by a channel that always has an opinion.

Contains no fixture repository names, package names or expected findings
(constraint #2): everything here is derived from manifests found on disk.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from panorama.channels import ChannelResult, RankedRepo
from panorama.intake import PullRequest
from panorama.languages import ManifestFacts, read_repo_manifest
from panorama.workspace import Workspace

#: How many hops out the graph is walked. Beyond a couple of hops "depends on"
#: stops meaning anything actionable — in a connected organisation everything
#: reaches everything eventually, and a ranking that includes the whole org is
#: the same as no ranking at all.
MAX_DISTANCE = 3

#: Edge kinds in strength order. The index into this tuple *is* the base rank,
#: which is why it is a fixed list rather than something computed.
DEPENDENT = "dependent"
DEPENDENCY = "dependency"
_DIRECTIONS = (DEPENDENT, DEPENDENCY)


@dataclass(frozen=True)
class RepoNode:
    """One repository's manifest facts, as read from its own checkout."""

    repo: str
    declared_name: str | None
    dependencies: tuple[str, ...]
    language: str | None

    @property
    def declares_itself(self) -> bool:
        return bool(self.declared_name)


@dataclass
class DependencyGraph:
    """Resolved edges between repositories in one workspace."""

    nodes: dict[str, RepoNode] = field(default_factory=dict)
    #: repo -> repositories it declares a dependency on.
    depends_on: dict[str, set[str]] = field(default_factory=dict)
    #: repo -> repositories that declare a dependency on it.
    depended_on_by: dict[str, set[str]] = field(default_factory=dict)
    #: repo -> dependency names that resolved to nothing in this workspace.
    external: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.depends_on.values())

    def neighbours(self, repo: str, direction: str) -> set[str]:
        table = self.depended_on_by if direction == DEPENDENT else self.depends_on
        return table.get(repo, set())

    def related(self, repo: str, *, max_distance: int = MAX_DISTANCE) -> list[tuple[str, str, int]]:
        """``(repo, direction, distance)`` for everything reachable from ``repo``.

        Each direction is walked separately, breadth-first, so a repository that
        is both a consumer and a provider is reported as both — that is two
        genuinely different relationships and collapsing them would lose the
        stronger one. Visited sets make cycles terminate; a repository that
        depends on itself, directly or through a loop, is simply not its own
        neighbour.
        """
        found: list[tuple[str, str, int]] = []
        for direction in _DIRECTIONS:
            seen = {repo}
            frontier = deque([(repo, 0)])
            while frontier:
                current, distance = frontier.popleft()
                if distance >= max_distance:
                    continue
                for neighbour in sorted(self.neighbours(current, direction)):
                    if neighbour in seen:
                        continue
                    seen.add(neighbour)
                    found.append((neighbour, direction, distance + 1))
                    frontier.append((neighbour, distance + 1))
        return found


def _facts_for(repo_path: Path) -> tuple[ManifestFacts, str | None]:
    pack, facts = read_repo_manifest(repo_path)
    return facts, pack.name if pack else None


def build_graph(workspace: Workspace) -> DependencyGraph:
    """Read every repository's manifest from disk and resolve the edges."""
    return graph_from_manifests(
        {view.name: _facts_for(view.path) for view in workspace.repos()}
    )


def graph_from_manifests(
    manifests: dict[str, tuple[ManifestFacts, str | None]],
) -> DependencyGraph:
    """Resolve edges from already-read manifest facts.

    Separated from reading them so the graph can be built two ways that must
    agree: from checkouts on disk, and from manifests fetched over the API
    before anything has been cloned. Selection would otherwise reimplement
    resolution, and a selection that resolved edges differently from retrieval
    would drop repositories retrieval was about to ask for.
    """
    graph = DependencyGraph()

    for name, (facts, language) in manifests.items():
        graph.nodes[name] = RepoNode(
            repo=name,
            declared_name=facts.declared_name,
            dependencies=facts.dependencies,
            language=language,
        )

    # Declared name -> repository. Built from what packages call *themselves*,
    # never from directory names. A duplicate declaration is ambiguous, so the
    # first repository in sorted order wins and the situation is left visible in
    # the node table rather than silently resolved.
    by_declared: dict[str, str] = {}
    for name in sorted(graph.nodes):
        declared = graph.nodes[name].declared_name
        if declared and declared not in by_declared:
            by_declared[declared] = name

    for name in sorted(graph.nodes):
        node = graph.nodes[name]
        resolved: set[str] = set()
        unresolved: list[str] = []
        for dependency in node.dependencies:
            target = by_declared.get(dependency)
            if target is None:
                unresolved.append(dependency)
            elif target != name:  # a package depending on itself is not an edge
                resolved.add(target)
        graph.depends_on[name] = resolved
        graph.external[name] = tuple(unresolved)
        for target in resolved:
            graph.depended_on_by.setdefault(target, set()).add(name)

    return graph


def edge_rank(direction: str, distance: int) -> int:
    """The rank an edge of this kind earns, from the fixed ordering above.

    Distance dominates, then direction: a direct provider outranks a
    two-hop consumer, because two hops of "depends on" is already a weak claim
    about anything.
    """
    return (distance - 1) * len(_DIRECTIONS) + _DIRECTIONS.index(direction) + 1


class DependencyChannel:
    """Declared dependency edges between repositories.

    Finds links that share no vocabulary — which is the entire point, since a
    convention violated by an absence leaves nothing to match on. Silent for
    couplings no manifest records, which is most cross-language ones.
    """

    name = "dependency"

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        graph = build_graph(workspace)

        if pr.repo not in graph.nodes:
            # The pull request's repository is not in the workspace we were
            # given. Nothing to say, and inventing something would be worse.
            return ChannelResult(
                channel=self.name,
                notes=("pull request repository is not in the workspace",),
            )

        best: dict[str, tuple[int, str, int]] = {}
        for repo, direction, distance in graph.related(pr.repo):
            rank = edge_rank(direction, distance)
            current = best.get(repo)
            if current is None or rank < current[0]:
                best[repo] = (rank, direction, distance)

        ranked = tuple(
            RankedRepo(
                repo=repo,
                rank=rank,
                # The score is the reciprocal of the rank purely so that a
                # larger number means a stronger claim, matching every other
                # channel. Nothing consumes its magnitude.
                score=round(1.0 / rank, 6),
                justification=_justify(graph, pr.repo, repo, direction, distance),
            )
            for repo, (rank, direction, distance) in sorted(
                best.items(), key=lambda item: (item[1][0], item[0])
            )
        )

        return ChannelResult(channel=self.name, ranked=ranked, notes=_notes(graph))


def _justify(
    graph: DependencyGraph, pr_repo: str, repo: str, direction: str, distance: int
) -> str:
    """Why this repository surfaced, in terms a reviewer can check."""
    hops = "directly" if distance == 1 else f"{distance} hops away"
    if direction == DEPENDENT:
        declared = graph.nodes[pr_repo].declared_name or pr_repo
        return f"depends on this repository ({declared}), {hops}"
    declared = graph.nodes[repo].declared_name or repo
    return f"this repository depends on it ({declared}), {hops}"


def _notes(graph: DependencyGraph) -> tuple[str, ...]:
    """Facts about the graph worth reporting when it explains a silence."""
    notes: list[str] = []
    undeclared = sorted(name for name, node in graph.nodes.items() if not node.declares_itself)
    if undeclared:
        # The most common reason this channel says nothing useful, and the
        # least obvious from the outside.
        notes.append(
            f"{len(undeclared)} repository/repositories declare no package name, "
            "so no edge can resolve to them: " + ", ".join(undeclared)
        )
    if not graph.edge_count:
        notes.append("no dependency edges resolved inside this workspace")
    return tuple(notes)
