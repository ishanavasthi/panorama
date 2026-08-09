"""Couplings that exist only on the wire, which nothing declares anywhere.

The two structural channels both ask a question the *source language* can
answer. The dependency graph asks who wrote whose name in a manifest; the symbol
index asks who imported whose declaration. Between them they cover every
coupling a compiler could see.

They are both silent for the coupling that dominates a service organisation. A
Python reporting client and a Go edge gateway can each depend utterly on a
TypeScript API — break instantly when it moves — and appear in no manifest and
no import statement, because the only thing joining them is an HTTP request and
the JSON that comes back. Roughly a third of the evaluation corpus is that
shape, and for those cases only the lexical channel speaks, so nothing can break
a tie against a repository that merely *mentions* the same words.

This channel reads the wire itself.

## What counts as the contract surface

Four kinds of token, all of which travel over HTTP rather than living in a
language's namespace:

- **route segments** — the static parts of a path literal, so ``"/links/:id"``
  and ``base + "/links/" + code`` both contribute ``links``;
- **payload fields** — JSON object keys, response-type members, ``json:"..."``
  struct tags, and dictionary subscripts;
- **payload values** — the lowercase, wordless string literals that error codes
  and enum members are made of;
- **status codes** — a three-digit number in a status position.

Both sides are read the same way: out of the diff to learn what the change
*moves*, and out of every sibling's source to learn who *speaks* it.

## Two things stop this from being the lexical channel again

**Only string-literal and wire positions count.** A route segment must come out
of a quoted path, a value out of a quoted literal, a field out of a key or tag.
An identifier that merely happens to be spelled the same is not a match. That
is what separates a repository that *calls* an endpoint from one that names a
variable after it.

**Only repositories that actually speak HTTP are eligible.** A repository
qualifies when some source file of its own makes an HTTP call, serves a route,
or decodes a response body. A shared library that defines an error-code constant
is not coupled to the wire by defining it — the coupling belongs to whoever
sends or reads it. A documentation repository has no source at all and is
therefore never eligible, which is the point: documenting a contract and
consuming one are different relationships, and lexical matching cannot tell them
apart.

Both filters cost recall in the same safe direction as everything else here: a
repository speaking HTTP through a library this module does not recognise is
invisible to the channel, which means a lead not offered — never a citation
invented.

## Claims, in strength order

1. **``endpoint``** — the sibling names a route segment this change moves. The
   endpoint itself is the strongest thing on the wire.
2. **``payload``** — the sibling names a field, value or status this change
   moves. The endpoint still exists; what travels over it changed.

As in the other structural channels, the rank *is* the claim kind rather than a
position among whatever this run happened to turn up. That keeps a rank meaning
the same thing between runs, which is the only condition under which fusing it
with another channel's ranks is meaningful.

## The surface index is cached, and the cache cannot make it wrong

Reading every sibling's source is the same expense the symbol index pays, so it
is cached the same way — per repository at a specific commit, under its own kind
so neither index invalidates the other. The safety argument is unchanged: this
channel produces *leads*, and every citation in the finished review is validated
against the live checkout at the reviewed commit with no cache in that path.

Contains no fixture repository names, routes, field names or expected findings
(constraint #2): every token on both sides is derived from a diff or from files
on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from panorama.cache import Cache
from panorama.channels import ChannelResult, Hit, RankedRepo
from panorama.intake import PullRequest
from panorama.languages import (
    LanguagePack,
    iter_source_files,
    pack_for_path,
    strip_comments,
)
from panorama.workspace import RepoView, Workspace

#: The kind of fact this channel stores in the cache.
CACHE_KIND = "http"

#: Bump when the stored surface shape changes in a way that would misread older
#: rows. Separate from the database schema version, and separate from the symbol
#: index's version, so one index changing does not discard the other.
INDEX_VERSION = 1

#: A repository with more source files than this is read up to the cap and the
#: shortfall reported. Retrieval is orientation, not an index.
MAX_INDEXED_FILES = 2000

#: How many leads this channel contributes at most, so one popular field name
#: cannot crowd every other channel out of the prompt.
MAX_HITS = 20

#: Claim kinds, strongest first. The index into this tuple *is* the rank.
ENDPOINT = "endpoint"
PAYLOAD = "payload"
_CLAIMS = (ENDPOINT, PAYLOAD)

#: Shortest useful route segment and payload field. Below this a token is
#: structure rather than contract — an ``id``, a ``v1``, an ``ok``.
_MIN_SEGMENT_LEN = 3
#: Values are held to a higher bar than fields: a field name is anchored by its
#: syntactic position, a bare string literal is not.
_MIN_VALUE_LEN = 4

#: Tokens that appear on the wire of essentially every HTTP service, so matching
#: one says nothing about whether two repositories share a contract. Generic to
#: HTTP and to serialisation, not to any organisation or corpus.
_WIRE_STOPWORDS = frozenset(
    {
        # ubiquitous path furniture
        "api", "www", "http", "https", "index", "static", "public", "assets",
        # ubiquitous envelope and pagination members
        "data", "error", "errors", "result", "results", "items", "item", "list",
        "meta", "value", "values", "key", "keys", "type", "kind", "name",
        "page", "size", "limit", "offset", "cursor", "total", "count",
        "status", "code", "message", "detail", "details", "reason",
        # request/response plumbing
        "req", "res", "request", "response", "body", "params", "query",
        "headers", "header", "method", "path", "url", "uri", "host", "port",
        "json", "text", "html", "content", "encoding", "charset",
    }
)


# ---------------------------------------------------------------------------
# reading wire tokens out of a line of source
# ---------------------------------------------------------------------------

#: A quoted string literal in any of the three languages, capturing its content.
#: Escapes are consumed so a backslashed quote does not end the literal early.
_STRING = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\])*)\1""")

#: A response-type member or a key on its own line. The leading alternatives
#: absorb modifiers (``readonly foo:``) so the captured group is the member name
#: rather than the modifier.
_MEMBER = re.compile(
    r"""^\s*(?:[A-Za-z_$][\w$]*\s+)*["']?([A-Za-z_$][\w$]*)["']?\s*[?!]?\s*:"""
)

#: Every key of an object or dictionary literal written inline, anchored on the
#: brace or comma that precedes it. A line-anchored pattern alone would see only
#: the *first* key of ``{ a: 1, b: 2 }``, which matters more than it sounds: a
#: reformat that splits a one-line literal across several lines would then look
#: like the second key being added, and every formatting change would read as a
#: payload change.
_INLINE_KEY = re.compile(r"""[{,]\s*["']?([A-Za-z_$][\w$]*)["']?\s*[?!]?\s*:""")

#: A Go struct tag naming the field's wire spelling: `json:"expires_at,omitempty"`.
_STRUCT_TAG = re.compile(r"""\bjson:"([^",]+)""")

#: Dictionary and map access by literal key: ``payload["id"]``, ``.get("id")``.
_SUBSCRIPT = re.compile(r"""(?:\[|\.get\(|\.getString\()\s*["']([A-Za-z_][\w]*)["']""")

#: A three-digit number in a status position — ``res.status(410)``,
#: ``StatusCode == 404``, ``status_code=204``. Requires the word so an ordinary
#: integer in the diff is not mistaken for a status.
_STATUS = re.compile(r"""\bstatus[_A-Za-z]*\s*(?:\(|==|!=|=|:|,)\s*(\d{3})\b""", re.I)

#: A payload value: one lowercase word-ish token, the shape error codes and
#: enum members take on the wire. Anything with a space, a slash or a capital
#: is prose, a path, or an identifier, and is left alone.
_VALUE_SHAPE = re.compile(r"^[a-z][a-z0-9_.-]*$")

#: A static path segment. Interpolation (``:id``, ``{id}``, ``${id}``, ``%s``)
#: fails this and is dropped, which is what makes two spellings of the same
#: route agree.
_SEGMENT_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

#: A line that opens with one of these is prose. Comment stripping handles the
#: general case; this catches continuation lines of a block comment, which have
#: no delimiter of their own and so cannot be recognised one line at a time.
_COMMENT_PREFIXES = ("//", "#", "*", "/*", "<!--")


@dataclass(frozen=True)
class WireTokens:
    """The contract surface of a diff or of a file, by kind."""

    routes: frozenset[str] = frozenset()
    fields: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    statuses: frozenset[int] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.routes or self.fields or self.values or self.statuses)


def _admit(token: str, *, minimum: int) -> bool:
    return (
        len(token) >= minimum
        and not token.startswith("_")
        and not token.isdigit()
        and token.lower() not in _WIRE_STOPWORDS
    )


def _is_import_line(pack: LanguagePack | None, line: str) -> bool:
    """Whether this line is an import, whose string literal is a module path.

    Reuses each pack's own import patterns rather than inventing a second
    notion of what an import looks like. It matters because a module path is
    shaped exactly like a route — ``"./handlers/stats"`` would otherwise
    contribute a route segment naming a file rather than an endpoint.
    """
    if pack is None:
        return False
    return any(pattern.match(line) for pattern in pack.import_patterns)


def scan_line(pack: LanguagePack | None, line: str) -> WireTokens:
    """Every wire token one line of source contributes."""
    if _is_import_line(pack, line):
        return WireTokens()

    routes: set[str] = set()
    fields: set[str] = set()
    values: set[str] = set()
    statuses: set[int] = set()

    for _quote, content in _STRING.findall(line):
        if "/" in content:
            # A path literal, possibly with an interpolated prefix or suffix.
            for segment in content.split("/"):
                if _SEGMENT_SHAPE.match(segment) and _admit(
                    segment, minimum=_MIN_SEGMENT_LEN
                ):
                    routes.add(segment)
        elif _VALUE_SHAPE.match(content) and _admit(content, minimum=_MIN_VALUE_LEN):
            values.add(content)

    member = _MEMBER.match(line)
    if member and _admit(member.group(1), minimum=_MIN_SEGMENT_LEN):
        fields.add(member.group(1))
    for key in _INLINE_KEY.findall(line):
        if _admit(key, minimum=_MIN_SEGMENT_LEN):
            fields.add(key)
    for tag in _STRUCT_TAG.findall(line):
        if _admit(tag, minimum=_MIN_SEGMENT_LEN):
            fields.add(tag)
    for name in _SUBSCRIPT.findall(line):
        if _admit(name, minimum=_MIN_SEGMENT_LEN):
            fields.add(name)

    for number in _STATUS.findall(line):
        statuses.add(int(number))

    return WireTokens(
        routes=frozenset(routes),
        fields=frozenset(fields),
        values=frozenset(values),
        statuses=frozenset(statuses),
    )


# ---------------------------------------------------------------------------
# the diff side: what this change moves
# ---------------------------------------------------------------------------


def contract_surface(diff: str) -> WireTokens:
    """The wire tokens a diff *changes*, by kind.

    Added and removed lines are read separately and a token present on both
    sides is discarded. Without that subtraction a reformat would re-emit every
    field name in the file it touched and read as a wholesale contract change —
    the same trap the symbol channel avoids for the same reason.

    Only files a language pack claims are read, so a documentation edit and a
    manifest bump contribute nothing at all.
    """
    added: dict[str, set] = {"routes": set(), "fields": set(), "values": set(), "statuses": set()}
    removed: dict[str, set] = {"routes": set(), "fields": set(), "values": set(), "statuses": set()}

    pack: LanguagePack | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            pack = None
            continue
        if line.startswith(("--- ", "+++ ")):
            candidate = line[4:].strip()
            if candidate.startswith(("a/", "b/")):
                candidate = candidate[2:]
            if candidate != "/dev/null":
                pack = pack_for_path(candidate)
            continue
        if not line or line[0] not in "+-":
            continue
        if pack is None:
            continue

        content = line[1:]
        if content.lstrip().startswith(_COMMENT_PREFIXES):
            continue
        # A comment carries prose, not contract. Stripping per pack keeps this
        # honest about which delimiter belongs to which language.
        stripped = strip_comments(pack, content)
        if not stripped or not stripped[0].strip():
            continue

        found = scan_line(pack, stripped[0])
        side = added if line[0] == "+" else removed
        side["routes"] |= found.routes
        side["fields"] |= found.fields
        side["values"] |= found.values
        side["statuses"] |= found.statuses

    return WireTokens(
        routes=frozenset(added["routes"] ^ removed["routes"]),
        fields=frozenset(added["fields"] ^ removed["fields"]),
        values=frozenset(added["values"] ^ removed["values"]),
        statuses=frozenset(added["statuses"] ^ removed["statuses"]),
    )


# ---------------------------------------------------------------------------
# the sibling side: who speaks HTTP, and what they say
# ---------------------------------------------------------------------------

#: Evidence that a file participates in HTTP rather than merely mentioning it.
#: A deliberately small, generic list covering the common client, server and
#: response-decoding idioms of the languages in the table. It is a heuristic and
#: it is incomplete by construction: a repository reaching the network through
#: an unrecognised library is invisible to this channel, which costs recall in
#: the safe direction and never invents a coupling.
_HTTP_PARTICIPATION = (
    # client calls
    re.compile(r"\bfetch\s*\(", re.I),
    re.compile(r"\baxios\s*[.(]", re.I),
    re.compile(r"\bXMLHttpRequest\b"),
    re.compile(r"\brequests\s*\.\s*(?:get|post|put|patch|delete|head|request)\s*\("),
    re.compile(r"\bhttpx\s*\.\s*(?:get|post|put|patch|delete|head|request|Client)\s*\("),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\bhttp\s*\.\s*(?:Get|Post|Head|PostForm|NewRequest|Do)\s*\("),
    re.compile(r"\bhttp\s*\.\s*Client\b"),
    re.compile(r"\bHttpClient\b"),
    # served routes
    re.compile(r"\b(?:app|router|api|srv|server|mux|r)\s*\.\s*(?:get|post|put|patch|delete|head|options|use|route|handle)\s*\(\s*['\"`]/"),
    re.compile(r"@\s*(?:app|router|blueprint|bp)\s*\.\s*(?:route|get|post|put|patch|delete)\s*\("),
    re.compile(r"\bhttp\s*\.\s*(?:HandleFunc|Handle)\s*\("),
    re.compile(r"\bListenAndServe\s*\("),
    # decoding a response body
    re.compile(r"\b(?:resp|response|res|r)\s*\.\s*json\s*\(\s*\)"),
    re.compile(r"\bjson\s*\.\s*(?:NewDecoder|Unmarshal)\s*\("),
    re.compile(r"\bStatusCode\b"),
    re.compile(r"\braise_for_status\s*\(", re.I),
)


@dataclass(frozen=True)
class Occurrence:
    """Where a wire token was seen."""

    path: str
    line: int


@dataclass
class RepoHTTPSurface:
    """What one repository says on the wire, at one commit."""

    repo: str
    head_sha: str
    #: True when some source file of this repository calls, serves or decodes
    #: HTTP. A repository where this is false is not eligible at all.
    participates: bool = False
    routes: dict[str, list[Occurrence]] = field(default_factory=dict)
    fields: dict[str, list[Occurrence]] = field(default_factory=dict)
    values: dict[str, list[Occurrence]] = field(default_factory=dict)
    statuses: dict[int, list[Occurrence]] = field(default_factory=dict)
    files_indexed: int = 0
    truncated: bool = False

    def to_payload(self) -> dict:
        """A JSON-safe form for the cache."""
        return {
            "version": INDEX_VERSION,
            "participates": self.participates,
            "files_indexed": self.files_indexed,
            "truncated": self.truncated,
            "routes": _dump(self.routes),
            "fields": _dump(self.fields),
            "values": _dump(self.values),
            "statuses": _dump(self.statuses),
        }

    @classmethod
    def from_payload(cls, repo: str, head_sha: str, payload: dict) -> RepoHTTPSurface | None:
        """Rebuild from a cached payload, or ``None`` if it cannot be trusted.

        Anything unexpected reads as a miss. The cost of a miss is recomputing;
        the cost of accepting a malformed payload is a surface that quietly
        describes something other than the code.
        """
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            return None
        try:
            return cls(
                repo=repo,
                head_sha=head_sha,
                participates=bool(payload["participates"]),
                routes=_load(payload["routes"], str),
                fields=_load(payload["fields"], str),
                values=_load(payload["values"], str),
                statuses=_load(payload["statuses"], int),
                files_indexed=int(payload.get("files_indexed", 0)),
                truncated=bool(payload.get("truncated", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _dump(places: dict) -> dict:
    return {str(token): [[o.path, o.line] for o in seen] for token, seen in places.items()}


def _load(raw: object, cast) -> dict:
    if not isinstance(raw, dict):
        raise TypeError("expected a mapping of token to occurrences")
    return {
        cast(token): [Occurrence(path=str(p), line=int(line)) for p, line in seen]
        for token, seen in raw.items()
    }


def build_surface(view: RepoView) -> RepoHTTPSurface:
    """Read a repository's source and record what it says on the wire."""
    surface = RepoHTTPSurface(repo=view.name, head_sha=view.head_sha)

    for path in iter_source_files(view.path):
        if surface.files_indexed >= MAX_INDEXED_FILES:
            surface.truncated = True
            break
        pack = pack_for_path(path)
        if pack is None:  # pragma: no cover - iter_source_files already filtered
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        surface.files_indexed += 1

        relative = path.relative_to(view.path).as_posix()
        for number, line in enumerate(strip_comments(pack, text), start=1):
            if not line.strip():
                continue
            if not surface.participates and any(
                pattern.search(line) for pattern in _HTTP_PARTICIPATION
            ):
                surface.participates = True

            found = scan_line(pack, line)
            where = Occurrence(path=relative, line=number)
            for token in found.routes:
                surface.routes.setdefault(token, []).append(where)
            for token in found.fields:
                surface.fields.setdefault(token, []).append(where)
            for token in found.values:
                surface.values.setdefault(token, []).append(where)
            for status in found.statuses:
                surface.statuses.setdefault(status, []).append(where)

    return surface


class SurfaceIndexer:
    """Builds surfaces, reusing cached ones for repositories that have not moved."""

    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache
        self.hits = 0
        self.misses = 0

    def surface_for(self, view: RepoView) -> RepoHTTPSurface:
        if self.cache is not None:
            payload = self.cache.get_facts(view.name, view.head_sha, CACHE_KIND)
            if payload is not None:
                cached = RepoHTTPSurface.from_payload(view.name, view.head_sha, payload)
                if cached is not None:
                    self.hits += 1
                    return cached

        self.misses += 1
        surface = build_surface(view)

        if self.cache is not None:
            self.cache.put_facts(view.name, view.head_sha, CACHE_KIND, surface.to_payload())
            self.cache.prune_repo(view.name, keep_sha=view.head_sha)
        return surface


# ---------------------------------------------------------------------------
# the channel
# ---------------------------------------------------------------------------


def claim_rank(claim: str) -> int:
    """The rank a claim of this kind earns, from the fixed ordering above."""
    return _CLAIMS.index(claim) + 1


class HTTPContractChannel:
    """Repositories that speak the same HTTP contract this change moves."""

    name = "httpcontract"

    def __init__(self, cache: Cache | None = None) -> None:
        self.indexer = SurfaceIndexer(cache)

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        changed = contract_surface(pr.diff)
        if changed.empty:
            # The change moves nothing that travels over HTTP. The commonest
            # case, and a correct answer.
            return ChannelResult(
                channel=self.name,
                notes=("the change moves no routes, payload fields or status codes",),
            )

        best: dict[str, tuple[int, str, list[str]]] = {}
        hits: list[Hit] = []
        truncated = False
        silent: list[str] = []
        eligible: list[tuple[str, RepoHTTPSurface]] = []

        for view in workspace.repos():
            if view.name == pr.repo:
                continue
            surface = self.indexer.surface_for(view)
            if not surface.participates:
                # Naming a contract is not consuming one. A shared library that
                # defines an error code, or a repository with no source at all,
                # is not coupled to the wire by saying the word.
                silent.append(view.name)
                continue
            eligible.append((view.name, surface))

        changed, ubiquitous = _discriminating(changed, eligible)

        for name, surface in eligible:
            matched: list[tuple[str, str, list[Occurrence]]] = []
            for token in sorted(changed.routes & surface.routes.keys()):
                matched.append((ENDPOINT, f"/{token}", surface.routes[token]))
            for token in sorted(changed.fields & surface.fields.keys()):
                matched.append((PAYLOAD, token, surface.fields[token]))
            for token in sorted(changed.values & surface.values.keys()):
                matched.append((PAYLOAD, token, surface.values[token]))
            for status in sorted(changed.statuses & surface.statuses.keys()):
                matched.append((PAYLOAD, str(status), surface.statuses[status]))

            for claim, label, places in matched:
                rank = claim_rank(claim)
                current = best.get(name)
                if current is None or rank < current[0]:
                    best[name] = (rank, claim, [label])
                elif rank == current[0]:
                    current[2].append(label)

                for occurrence in places:
                    if len(hits) >= MAX_HITS:
                        truncated = True
                        break
                    hits.append(
                        Hit(
                            repo=name,
                            path=occurrence.path,
                            line=occurrence.line,
                            token=label,
                        )
                    )
                if truncated:
                    break

        ranked = tuple(
            RankedRepo(
                repo=repo,
                rank=rank,
                # Reciprocal of the rank so a larger number is a stronger claim,
                # matching every other channel. Nothing consumes its magnitude.
                score=round(1.0 / rank, 6),
                justification=_justify(claim, sorted(set(labels))),
            )
            for repo, (rank, claim, labels) in sorted(
                best.items(), key=lambda item: (item[1][0], item[0])
            )
        )

        return ChannelResult(
            channel=self.name,
            ranked=ranked,
            hits=tuple(hits),
            notes=_notes(self.indexer, silent, ubiquitous, truncated),
            truncated=truncated,
        )


def _discriminating(
    changed: WireTokens, eligible: list[tuple[str, RepoHTTPSurface]]
) -> tuple[WireTokens, list[str]]:
    """Drop wire tokens that every eligible repository names.

    A ranking that puts every candidate in the same place is the same as no
    ranking. If a route segment or a field name is spoken by *all* the
    repositories that speak HTTP at all, it is the organisation's shared
    vocabulary — the resource everything is about — and matching it identifies
    nobody. This is the same argument the co-change channel's density guard
    makes: a signal that fires for most of the field carries no information.

    Parameter-free on purpose. The condition is "matched by every eligible
    repository", not "matched by more than some fraction", because the moment
    it becomes a fraction it is a number fitted to the corpus it is scored on.

    The cost is real and worth stating: a change that genuinely breaks *every*
    HTTP consumer in the organisation has its broadest token discarded here, so
    this channel is quietest exactly when a change is most sweeping. That case
    is the one lexical matching already handles well — the token is everywhere,
    so everything surfaces — and this channel exists for the discriminating
    case lexical matching cannot resolve.
    """
    if len(eligible) < 2:
        # With a single candidate there is nothing to discriminate *between*,
        # and a match still separates it from every ineligible repository.
        return changed, []

    dropped: list[str] = []

    def keep(tokens, attribute: str, render) -> frozenset:
        surviving = set()
        for token in tokens:
            everywhere = all(
                token in getattr(surface, attribute) for _, surface in eligible
            )
            if everywhere:
                dropped.append(render(token))
            else:
                surviving.add(token)
        return frozenset(surviving)

    return (
        WireTokens(
            routes=keep(changed.routes, "routes", lambda t: f"/{t}"),
            fields=keep(changed.fields, "fields", str),
            values=keep(changed.values, "values", str),
            statuses=keep(changed.statuses, "statuses", str),
        ),
        sorted(dropped),
    )


def _justify(claim: str, labels: list[str]) -> str:
    shown = ", ".join(labels[:5])
    extra = len(labels) - 5
    if extra > 0:
        shown = f"{shown} (+{extra} more)"
    if claim == ENDPOINT:
        return f"speaks HTTP and names {len(labels)} route(s) this change moves: {shown}"
    return (
        f"speaks HTTP and names {len(labels)} payload token(s) this change moves: {shown}"
    )


def _notes(
    indexer: SurfaceIndexer,
    silent: list[str],
    ubiquitous: list[str],
    truncated: bool,
) -> tuple[str, ...]:
    notes: list[str] = []
    if ubiquitous:
        # A dropped signal has to say so. "It found nothing" and "it discarded
        # what it found" are different answers and look identical from outside.
        notes.append(
            f"{len(ubiquitous)} wire token(s) named by every repository that "
            "speaks HTTP, so they discriminate nothing and were discarded: "
            + ", ".join(ubiquitous)
        )
    if silent:
        # The commonest reason this channel says nothing about a repository
        # that obviously shares vocabulary, and the least obvious from outside.
        notes.append(
            f"{len(silent)} repository/repositories make no HTTP call, serve no "
            "route and decode no response, so they cannot share a wire contract: "
            + ", ".join(sorted(silent))
        )
    if indexer.hits:
        notes.append(
            f"HTTP surface reused for {indexer.hits} repository/repositories, "
            f"rebuilt for {indexer.misses}"
        )
    if truncated:
        notes.append("HTTP lead cap reached; more matches exist")
    return tuple(notes)


def surface_workspace(
    workspace: Workspace, *, cache: Cache | None = None, skip: str | None = None
) -> dict[str, RepoHTTPSurface]:
    """Read every repository's HTTP surface, for tooling that wants the picture."""
    indexer = SurfaceIndexer(cache)
    return {
        view.name: indexer.surface_for(view)
        for view in workspace.repos()
        if view.name != skip
    }
