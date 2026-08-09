"""What each repository exports, what it imports, and who breaks when that moves.

The lexical channel asks "does this word appear over there". That question has a
well-known failure: a word appears in a repository that *defines* it, one that
*documents* it, and one that *uses* it, and lexical matching cannot tell those
apart. On the evaluation corpus that is not hypothetical — it is exactly why a
conventions document and a shared library outrank the service that actually
breaks.

This channel asks a sharper question: **does another repository import a name
this change removed?** An import is a declaration of consumption. A repository
that imports a symbol the pull request deletes or renames is broken *by
construction*, not by resemblance, and the citation is a specific line rather
than a lead.

## Two claims, in strength order

1. **A sibling imports a name this change removed.** That is a contract break.
   It is the strongest structural claim available anywhere in retrieval, because
   it is a statement about a dependency the sibling wrote down itself.
2. **A sibling exports a name this change adds.** That is duplication: the
   organisation already owns this idea somewhere else.

As with the dependency channel, rank encodes *which* of those claims applies
rather than a position among whatever happened to turn up. A duplication signal
never outranks a break, whether or not any break exists in this particular
change — otherwise the rank would silently change meaning depending on how the
run went, and fusing it with another channel's ranks would be comparing
different things.

## The index is cached, and the cache cannot make it wrong

Building the index means reading every source file in every sibling, which is
the most expensive thing retrieval does. It is cached per repository *at a
specific commit*, so a repository that has not moved is not re-read.

The safety argument is the same one the cache module makes, and it holds here
because of what this channel produces: **leads, not evidence**. A stale index
can point at a line that has moved or vanished. That wastes some of the model's
attention and can make a review worse. It cannot make one wrong, because every
citation in the finished review is validated against the live checkout at the
reviewed commit, with no cache in that path.

## What it cannot see

Only what a language's syntax declares. A Python client that reads a JSON field
over HTTP imports nothing from the service it depends on, and this channel is
silent about it — the same silence the dependency channel has, for the same
reason, on the same third of the corpus. And the same idea spelled in snake case
in one language and camel case in another remains two different strings: this
channel matches names exactly and does not attempt to normalise across naming
conventions.

Contains no fixture names (constraint #2): everything is derived from files on
disk and from the diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from panorama.cache import Cache
from panorama.channels import ChannelResult, Hit, RankedRepo
from panorama.intake import PullRequest
from panorama.languages import (
    extract_symbols,
    iter_source_files,
    pack_for_path,
)
from panorama.workspace import RepoView, Workspace

#: The kind of fact this channel stores in the cache.
CACHE_KIND = "symbols"

#: Bump when the stored index shape changes in a way that would misread older
#: rows. Kept separate from the database schema version so an index change does
#: not throw away unrelated cached facts.
INDEX_VERSION = 1

#: A repository with more source files than this is indexed up to the cap and
#: the shortfall is reported. Retrieval is orientation, not an IDE: an index
#: that takes a minute to build has already cost more than it can repay.
MAX_INDEXED_FILES = 2000

#: How many leads this channel contributes at most, so one very popular symbol
#: cannot crowd the prompt out.
MAX_HITS = 20

#: Claim kinds, strongest first. The index into this tuple *is* the rank.
BREAKS = "breaks"
DUPLICATES = "duplicates"
_CLAIMS = (BREAKS, DUPLICATES)


@dataclass(frozen=True)
class Occurrence:
    """Where a name was seen."""

    path: str
    line: int


@dataclass
class RepoSymbolIndex:
    """What one repository declares and what it consumes, at one commit."""

    repo: str
    head_sha: str
    exports: dict[str, list[Occurrence]] = field(default_factory=dict)
    imports: dict[str, list[Occurrence]] = field(default_factory=dict)
    files_indexed: int = 0
    truncated: bool = False

    def to_payload(self) -> dict:
        """A JSON-safe form for the cache."""
        return {
            "version": INDEX_VERSION,
            "files_indexed": self.files_indexed,
            "truncated": self.truncated,
            "exports": {
                name: [[o.path, o.line] for o in places]
                for name, places in self.exports.items()
            },
            "imports": {
                name: [[o.path, o.line] for o in places]
                for name, places in self.imports.items()
            },
        }

    @classmethod
    def from_payload(cls, repo: str, head_sha: str, payload: dict) -> RepoSymbolIndex | None:
        """Rebuild from a cached payload, or ``None`` if it cannot be trusted.

        Anything unexpected reads as a miss rather than an error. The cost of a
        miss is recomputation; the cost of accepting a malformed payload is an
        index that quietly describes something other than the code.
        """
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            return None
        try:
            return cls(
                repo=repo,
                head_sha=head_sha,
                exports=_places(payload["exports"]),
                imports=_places(payload["imports"]),
                files_indexed=int(payload.get("files_indexed", 0)),
                truncated=bool(payload.get("truncated", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _places(raw: object) -> dict[str, list[Occurrence]]:
    if not isinstance(raw, dict):
        raise TypeError("expected a mapping of name to occurrences")
    return {
        str(name): [Occurrence(path=str(p), line=int(line)) for p, line in places]
        for name, places in raw.items()
    }


def build_index(view: RepoView) -> RepoSymbolIndex:
    """Read a repository's source and record what it declares and consumes."""
    index = RepoSymbolIndex(repo=view.name, head_sha=view.head_sha)

    for path in iter_source_files(view.path):
        if index.files_indexed >= MAX_INDEXED_FILES:
            index.truncated = True
            break
        pack = pack_for_path(path)
        if pack is None:  # pragma: no cover - iter_source_files already filtered
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        index.files_indexed += 1

        relative = path.relative_to(view.path).as_posix()
        facts = extract_symbols(pack, text)
        for symbol in facts.exports:
            index.exports.setdefault(symbol.name, []).append(
                Occurrence(path=relative, line=symbol.line)
            )
        for symbol in facts.imports:
            index.imports.setdefault(symbol.name, []).append(
                Occurrence(path=relative, line=symbol.line)
            )

    return index


class SymbolIndexer:
    """Builds indexes, reusing cached ones for repositories that have not moved."""

    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache
        self.hits = 0
        """Repositories served from the cache — surfaced so a run can report
        whether it started warm rather than leaving someone guessing at why it
        was slow."""
        self.misses = 0

    def index_for(self, view: RepoView) -> RepoSymbolIndex:
        if self.cache is not None:
            payload = self.cache.get_facts(view.name, view.head_sha, CACHE_KIND)
            if payload is not None:
                cached = RepoSymbolIndex.from_payload(view.name, view.head_sha, payload)
                if cached is not None:
                    self.hits += 1
                    return cached

        self.misses += 1
        index = build_index(view)

        if self.cache is not None:
            self.cache.put_facts(view.name, view.head_sha, CACHE_KIND, index.to_payload())
            # Older commits of this repository will never be asked for again.
            self.cache.prune_repo(view.name, keep_sha=view.head_sha)
        return index


# ---------------------------------------------------------------------------
# reading the diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffSymbols:
    """Names whose declaration or consumption the diff changes."""

    removed_exports: frozenset[str] = frozenset()
    added_exports: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.removed_exports or self.added_exports)


def diff_symbols(diff: str) -> DiffSymbols:
    """Exported names the diff removes and adds, per file, by language.

    Added and removed lines are collected separately and each side is run
    through its language's patterns. A name on both sides was only moved or
    reformatted, so it is neither removed nor added — without that subtraction,
    re-indenting a file would look like deleting and re-declaring everything in
    it, and every formatting change would read as a contract break.
    """
    removed: set[str] = set()
    added: set[str] = set()

    for path, minus, plus in _changed_files(diff):
        pack = pack_for_path(path)
        if pack is None:
            continue
        before = {s.name for s in extract_symbols(pack, "\n".join(minus)).exports}
        after = {s.name for s in extract_symbols(pack, "\n".join(plus)).exports}
        removed |= before - after
        added |= after - before

    return DiffSymbols(removed_exports=frozenset(removed), added_exports=frozenset(added))


def _changed_files(diff: str):
    """``(path, removed_lines, added_lines)`` per file section of a unified diff."""
    path: str | None = None
    minus: list[str] = []
    plus: list[str] = []

    def flush():
        if path is not None and (minus or plus):
            return (path, list(minus), list(plus))
        return None

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            done = flush()
            if done:
                yield done
            path, minus, plus = None, [], []
            continue
        # `---` precedes `+++`, so the old path is read first and the new one
        # replaces it when there is one. A *deleted* file has `+++ /dev/null`,
        # and letting that overwrite the path would make the removed
        # declarations of an entire deleted file invisible — which is the
        # largest contract break there is.
        if line.startswith("--- "):
            candidate = line[4:].strip()
            if candidate.startswith("a/"):
                candidate = candidate[2:]
            if candidate != "/dev/null":
                path = candidate
            continue
        if line.startswith("+++ "):
            candidate = line[4:].strip()
            if candidate.startswith("b/"):
                candidate = candidate[2:]
            if candidate != "/dev/null":
                path = candidate
            continue
        if line.startswith("+"):
            plus.append(line[1:])
        elif line.startswith("-"):
            minus.append(line[1:])

    done = flush()
    if done:
        yield done


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


def claim_rank(claim: str) -> int:
    """The rank a claim of this kind earns, from the fixed ordering above."""
    return _CLAIMS.index(claim) + 1


class SymbolChannel:
    """Exported and imported names, matched exactly across repositories."""

    name = "symbols"

    def __init__(self, cache: Cache | None = None) -> None:
        self.indexer = SymbolIndexer(cache)

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        changed = diff_symbols(pr.diff)
        if changed.empty:
            # Nothing was declared or undeclared, so there is nothing this
            # channel can say. The commonest case, and a correct answer.
            return ChannelResult(
                channel=self.name,
                notes=("the change declares and removes no exported names",),
            )

        best: dict[str, tuple[int, str, set[str]]] = {}
        hits: list[Hit] = []
        truncated = False

        for view in workspace.repos():
            if view.name == pr.repo:
                continue
            index = self.indexer.index_for(view)

            broken = sorted(changed.removed_exports & index.imports.keys())
            duplicated = sorted(changed.added_exports & index.exports.keys())

            for claim, names, places in (
                (BREAKS, broken, index.imports),
                (DUPLICATES, duplicated, index.exports),
            ):
                if not names:
                    continue
                rank = claim_rank(claim)
                current = best.get(view.name)
                if current is None or rank < current[0]:
                    best[view.name] = (rank, claim, set(names))
                elif rank == current[0]:
                    current[2].update(names)

                for symbol in names:
                    for occurrence in places[symbol]:
                        if len(hits) >= MAX_HITS:
                            truncated = True
                            break
                        hits.append(
                            Hit(
                                repo=view.name,
                                path=occurrence.path,
                                line=occurrence.line,
                                token=symbol,
                            )
                        )
                    if truncated:
                        break

        ranked = tuple(
            RankedRepo(
                repo=repo,
                rank=rank,
                score=round(1.0 / rank, 6),
                justification=_justify(claim, sorted(names)),
            )
            for repo, (rank, claim, names) in sorted(
                best.items(), key=lambda item: (item[1][0], item[0])
            )
        )

        return ChannelResult(
            channel=self.name,
            ranked=ranked,
            hits=tuple(hits),
            notes=_notes(self.indexer, truncated),
            truncated=truncated,
        )


def _justify(claim: str, names: list[str]) -> str:
    shown = ", ".join(names[:5])
    extra = len(names) - 5
    if extra > 0:
        shown = f"{shown} (+{extra} more)"
    if claim == BREAKS:
        return f"imports {len(names)} name(s) this change removes: {shown}"
    return f"already exports {len(names)} name(s) this change adds: {shown}"


def _notes(indexer: SymbolIndexer, truncated: bool) -> tuple[str, ...]:
    notes: list[str] = []
    if indexer.hits:
        notes.append(
            f"symbol index reused for {indexer.hits} repository/repositories, "
            f"rebuilt for {indexer.misses}"
        )
    if truncated:
        notes.append("symbol lead cap reached; more matches exist")
    return tuple(notes)


def index_workspace(
    workspace: Workspace, *, cache: Cache | None = None, skip: str | None = None
) -> dict[str, RepoSymbolIndex]:
    """Index every repository, for tooling that wants the whole picture."""
    indexer = SymbolIndexer(cache)
    return {
        view.name: indexer.index_for(view)
        for view in workspace.repos()
        if view.name != skip
    }


def cache_for_owner(owner: str, root: Path | None = None) -> Cache:
    """Open the cache a review of ``owner``'s repositories should use."""
    return Cache.for_owner(owner, root)
