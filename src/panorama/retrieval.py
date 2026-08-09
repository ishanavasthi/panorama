"""Deterministic cross-repository retrieval — the lexical channel.

Given a normalized `PullRequest` and a `Workspace`, this produces the
orientation the review runs on: a small ranked set of generic *signals* pulled
from the diff (changed fields, added function/class names, routes, and other
identifiers), the sibling-repository lines those signals match, and a relevance
ranking of the siblings themselves.

It is deliberately lexical and explainable — no embeddings, no parser
framework. Every surfaced repository can be justified by *which signal* matched
*which line*. The code contains no fixture repository names, field names, or
expected findings (hard constraint #2): it keys entirely off the diff and the
workspace content.

Read-only: the only external calls are `git grep` and reading files.

**Where this sits in V2.** This is now *one* channel among several, implementing
the interface in `channels.py`. Its mechanics live in `LexicalChannel`; the
module-level `retrieve()` is the composition point that assembles what the
review actually consumes. Today there is one channel, so composition is the
identity — in V2.7 the same function fuses several rankings by reciprocal rank.

Its known weakness is worth stating where someone will read it: this channel
can only find a link that shares *vocabulary*. A convention violated by an
absence leaves no vocabulary at all, and a helper duplicated across a
naming-convention boundary — the same idea spelled in snake case in one
language and camel case in another — is two different strings. Those are not
tuning problems; they are what the structural channels exist to answer.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from panorama.cache import Cache
from panorama.channels import (
    ChannelResult,
    Hit,
    RetrievalChannel,
    competition_ranked,
    fuse,
)
from panorama.errors import PreflightError
from panorama.intake import PullRequest
from panorama.workspace import Workspace

__all__ = [
    "Hit",
    "LexicalChannel",
    "RepoRelevance",
    "RetrievalResult",
    "Signal",
    "active_channels",
    "extract_signals",
    "filter_diff",
    "retrieve",
]

# --- caps: retrieval is orientation, not an index. Keep it bounded. --------
MAX_SIGNALS_SEARCHED = 15
MAX_HITS_PER_SIGNAL_PER_REPO = 5
MAX_TOTAL_HITS = 60
WINDOW_RADIUS = 2

# --- signal weights by kind ------------------------------------------------
_KIND_WEIGHT = {
    "field": 5,      # a public-looking `name:` — the stuff contracts are made of
    "route": 4,      # an HTTP path literal
    "function": 4,   # an added/removed function or method name
    "class": 4,      # a class/interface/type/enum name
    "identifier": 2, # any other changed identifier
}

# Generic keywords, primitive types, and ubiquitous builtins. NONE of these are
# fixture-specific; they are the tokens that would add noise in any codebase.
_STOPWORDS = frozenset(
    {
        # control flow / declarations
        "const", "let", "var", "function", "return", "import", "export", "from",
        "class", "interface", "type", "enum", "struct", "def", "fn", "public",
        "private", "protected", "readonly", "static", "async", "await", "yield",
        "if", "else", "for", "while", "do", "switch", "case", "break", "continue",
        "new", "delete", "typeof", "instanceof", "in", "of", "as", "extends",
        "implements", "namespace", "module", "package", "throw", "try", "catch",
        "finally", "this", "super", "self", "with", "and", "or", "not", "pass",
        "lambda", "global", "nonlocal", "raise", "elif", "then", "end", "begin",
        # primitive / common types
        "string", "number", "boolean", "bool", "int", "float", "double", "char",
        "void", "null", "nil", "none", "undefined", "any", "unknown", "never",
        "object", "array", "list", "dict", "map", "set", "tuple", "true", "false",
        "date", "error", "exception", "promise", "future", "option", "result",
        # ubiquitous builtins / globals
        "console", "window", "document", "process", "require", "exports",
        "fetch", "json", "math", "print", "len", "range", "str", "repr",
        "value", "values", "key", "keys", "item", "items", "data",
        "res", "req", "request", "response", "params", "args", "kwargs", "opts",
        "props", "state", "context", "config", "options", "callback", "handler",
        "get", "post", "put", "patch", "head", "index", "main", "app", "test",
    }
)

_MIN_TOKEN_LEN = 3

# --- file filtering: generated / lock / binary content is noise ------------
_GENERATED_BASENAMES = frozenset(
    {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
        "cargo.lock", "poetry.lock", "gemfile.lock", "go.sum", "go.mod",
    }
)
_GENERATED_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".snap",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff",
    ".woff2", ".ttf", ".eot", ".wasm", ".class", ".o", ".so", ".dylib",
)
_GENERATED_DIR_SEGMENTS = frozenset(
    {"node_modules", "dist", "build", "vendor", "out", "coverage", ".next", ".git"}
)

# --- diff line classification ---------------------------------------------
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_FIELD = re.compile(r"^\s*(?:[A-Za-z_$][\w$]*\s+)*([A-Za-z_$][\w$]*)\s*[?!]?\s*:")
_FUNC = re.compile(r"\b(?:function|def|fn)\s+([A-Za-z_$][\w$]*)")
_TYPE = re.compile(r"\b(?:class|interface|type|enum|struct)\s+([A-Za-z_$][\w$]*)")
_ARROW = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\()"
)
_ROUTE = re.compile(r"""["'`](/[\w\-./:{}]*)["'`]""")
_COMMENT_PREFIXES = ("//", "#", "*", "/*", "<!--")


@dataclass(frozen=True)
class Signal:
    """One thing worth searching sibling repositories for."""

    token: str
    kind: str
    weight: int
    polarity: frozenset[str]  # {"added"} / {"removed"} / both

    @property
    def word(self) -> bool:
        """Whether the token is a bare word (so `git grep -w` is meaningful)."""
        return bool(re.fullmatch(r"\w+", self.token))


@dataclass
class RepoRelevance:
    """A sibling repository ranked by how strongly the change points at it.

    ``score`` is the fused score across every channel that ranked this
    repository: comparable within one run, meaningless between runs.
    ``signals`` are the lexical tokens that matched, kept separately because
    they are the part a reader can go and check by eye. ``provenance`` is one
    line per channel — the answer to "why is this repository on the list at
    all", which a bare number cannot give.
    """

    repo: str
    score: float
    signals: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    signals: list[Signal]
    searched: list[Signal]
    hits: list[Hit]
    ranked_repos: list[RepoRelevance]
    convention_docs: dict[str, list[str]]
    truncated: bool = False


# ---------------------------------------------------------------------------
# signal extraction
# ---------------------------------------------------------------------------


def _is_generated(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name in _GENERATED_BASENAMES:
        return True
    if any(name.endswith(suffix) for suffix in _GENERATED_SUFFIXES):
        return True
    return any(segment in _GENERATED_DIR_SEGMENTS for segment in path.lower().split("/"))


def _strip_prefix(path: str) -> str:
    path = path.strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def filter_diff(diff: str) -> tuple[str, list[str]]:
    """Drop generated, lock, and binary file sections from a unified diff.

    A pull request that also regenerates a lockfile or a minified bundle carries
    huge, low-signal hunks; feeding them to the model wastes attention and crowds
    out the real change. This removes whole per-file sections that are generated
    (by path) or binary (by the ``Binary files`` marker), and returns the trimmed
    diff plus the paths it dropped, so the omission can be stated rather than
    hidden. Host validation still runs against the *full* diff — this only shapes
    what the model reads.
    """
    sections: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    kept: list[str] = []
    dropped: list[str] = []
    for section in sections:
        header = _DIFF_HEADER.match(section[0].rstrip("\n")) if section else None
        path = header.group("b") if header else None
        is_binary = any(line.startswith("Binary files") for line in section)
        if (path is not None and _is_generated(path)) or is_binary:
            dropped.append(path or "(binary)")
            continue
        kept.extend(section)
    return "".join(kept), dropped


def _admit(token: str) -> bool:
    return (
        len(token) >= _MIN_TOKEN_LEN
        and token.lower() not in _STOPWORDS
        and not token.isdigit()
        and not token.startswith("_")  # non-public / private by convention
    )


def _extract_from_line(content: str, polarity: str, acc: dict[str, dict]) -> None:
    stripped = content.lstrip()
    if stripped.startswith(_COMMENT_PREFIXES):
        return  # a comment or prose line contributes no code signals

    def add(token: str, kind: str) -> None:
        if not _admit(token):
            return
        weight = _KIND_WEIGHT[kind]
        entry = acc.get(token)
        if entry is None:
            entry = acc[token] = {"kind": kind, "weight": weight, "polarity": set()}
        # Polarity accumulates across every sighting; a later high-weight kind
        # upgrades the classification without discarding what we already saw.
        entry["polarity"].add(polarity)
        if weight > entry["weight"]:
            entry["weight"] = weight
            entry["kind"] = kind

    # Structural signals run on the raw line (routes/strings must stay intact).
    field_match = _FIELD.match(content)
    if field_match:
        add(field_match.group(1), "field")
    for match in _FUNC.finditer(content):
        add(match.group(1), "function")
    for match in _TYPE.finditer(content):
        add(match.group(1), "class")
    for match in _ARROW.finditer(content):
        add(match.group(1), "function")
    for match in _ROUTE.finditer(content):
        token = match.group(1)
        if len(token) > 1:  # ignore a bare "/"
            add(token, "route")

    # Plain identifiers run on a comment-stripped copy to avoid prose tokens.
    code = content.split("//", 1)[0]
    for match in _IDENT.finditer(code):
        add(match.group(0), "identifier")


def extract_signals(diff: str) -> list[Signal]:
    """Extract and rank generic signals from a unified diff."""
    acc: dict[str, dict] = {}
    skip = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            skip = False
            continue
        if line.startswith(("+++ ", "--- ")):
            path = _strip_prefix(line[4:])
            if path != "/dev/null" and _is_generated(path):
                skip = True
            continue
        if line.startswith("Binary files"):
            skip = True
            continue
        if skip or not line or line[0] not in "+-":
            continue
        _extract_from_line(line[1:], "added" if line[0] == "+" else "removed", acc)

    signals = [
        Signal(token=token, kind=e["kind"], weight=e["weight"], polarity=frozenset(e["polarity"]))
        for token, e in acc.items()
    ]
    # Ranked most specific first: heavier kinds, then longer (more distinctive)
    # tokens, then alphabetical for a fully deterministic order.
    signals.sort(key=lambda s: (-s.weight, -len(s.token), s.token))
    return signals


# ---------------------------------------------------------------------------
# sibling search
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise PreflightError(
            "git was not found on PATH; install git to run retrieval."
        ) from exc


def _git_grep(repo_path: Path, token: str, *, word: bool) -> list[tuple[str, int]]:
    args = ["grep", "-n", "-I", "-F"]
    if word:
        args.append("-w")
    args += ["-e", token, "--", "."]
    proc = _run_git(repo_path, *args)
    if proc.returncode not in (0, 1):  # 1 == no match, which is not an error
        return []
    hits: list[tuple[str, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        hits.append((parts[0], int(parts[1])))
    return hits


def _window(repo_path: Path, rel: str, line: int, radius: int = WINDOW_RADIUS) -> tuple[str, ...]:
    try:
        lines = (repo_path / rel).read_text(errors="replace").splitlines()
    except OSError:
        return ()
    lo = max(0, line - 1 - radius)
    hi = min(len(lines), line + radius)
    return tuple(f"{i + 1}: {lines[i]}" for i in range(lo, hi))


@dataclass(frozen=True)
class LexicalPass:
    """Everything one lexical search produced, before it is shaped for a caller.

    Exists so the channel view and the pipeline view are built from *one*
    computation rather than two that have to be kept agreeing. That is not
    tidiness — it is the reason the refactor could be proven not to change
    behaviour.
    """

    signals: list[Signal]
    searched: list[Signal]
    hits: list[Hit]
    ranked: list[RepoRelevance]
    truncated: bool


#: How many matched tokens a justification names before summarising the rest.
_MAX_JUSTIFIED_TOKENS = 5


class LexicalChannel:
    """Shared vocabulary between the diff and its siblings.

    The original V1 retrieval pass, unchanged in behaviour, now wearing the
    channel interface. Strong on renames and duplicated helpers, because both
    leave a distinctive token in two places. Blind to anything that shares no
    vocabulary — see the module docstring.
    """

    name = "lexical"

    def search(self, pr: PullRequest, workspace: Workspace) -> LexicalPass:
        """Run the search and return everything it found."""
        signals = extract_signals(pr.diff)
        searched = signals[:MAX_SIGNALS_SEARCHED]

        siblings = [r for r in workspace.repos() if r.name != pr.repo]
        weight_by_token = {s.token: s.weight for s in searched}

        hits: list[Hit] = []
        signal_repo_matches: dict[str, set[str]] = defaultdict(set)
        truncated = False

        for signal in searched:
            for repo in siblings:
                found = _git_grep(repo.path, signal.token, word=signal.word)
                if not found:
                    continue
                signal_repo_matches[signal.token].add(repo.name)
                for path, line in found[:MAX_HITS_PER_SIGNAL_PER_REPO]:
                    if len(hits) >= MAX_TOTAL_HITS:
                        truncated = True
                        break
                    hits.append(
                        Hit(
                            repo=repo.name,
                            path=path,
                            line=line,
                            token=signal.token,
                            window=_window(repo.path, path, line),
                        )
                    )
                if truncated:
                    break
            if truncated:
                break

        # Score each sibling by the DISTINCT signals that matched it, each signal
        # weighted and discounted by how many repositories it hit — a token that
        # matches everywhere is far less discriminating than one that matches once.
        repo_signals: dict[str, set[str]] = defaultdict(set)
        for hit in hits:
            repo_signals[hit.repo].add(hit.token)

        ranked: list[RepoRelevance] = []
        for repo in siblings:
            tokens = repo_signals.get(repo.name, set())
            if not tokens:
                continue
            score = sum(
                weight_by_token[t] / len(signal_repo_matches[t]) for t in tokens
            )
            ranked.append(
                RepoRelevance(repo=repo.name, score=round(score, 3), signals=sorted(tokens))
            )
        ranked.sort(key=lambda r: (-r.score, r.repo))

        return LexicalPass(
            signals=signals,
            searched=searched,
            hits=hits,
            ranked=ranked,
            truncated=truncated,
        )

    def rank(self, pr: PullRequest, workspace: Workspace) -> ChannelResult:
        """The channel-interface view of the same search."""
        return _lexical_result(self, self.search(pr, workspace))


def _lexical_result(channel: LexicalChannel, found: LexicalPass) -> ChannelResult:
    """Shape one lexical search as a channel result.

    Free-standing so the composition point can reuse a search it has already
    paid for rather than running every `git grep` a second time.
    """
    signals_by_repo = {entry.repo: entry.signals for entry in found.ranked}

    def justify(repo: str, _score: float) -> str:
        tokens = signals_by_repo.get(repo, [])
        shown = ", ".join(tokens[:_MAX_JUSTIFIED_TOKENS])
        extra = len(tokens) - _MAX_JUSTIFIED_TOKENS
        if extra > 0:
            shown = f"{shown} (+{extra} more)"
        return f"shares {len(tokens)} identifier(s) with the diff: {shown}"

    return ChannelResult(
        channel=channel.name,
        ranked=competition_ranked(
            [(entry.repo, entry.score) for entry in found.ranked], justify=justify
        ),
        hits=tuple(found.hits),
        notes=("hit cap reached; the search stopped early",) if found.truncated else (),
        truncated=found.truncated,
    )


#: Channels that exist but are not on by default, keyed by name. A channel lands
#: here when it is built but its value has not been *measured* — shipping an
#: unmeasured channel enabled would be making a quality claim nobody checked.
EXPERIMENTAL_CHANNELS = ("cochange",)


def active_channels(
    cache: Cache | None = None, *, experimental: tuple[str, ...] = ()
) -> tuple[RetrievalChannel, ...]:
    """The channels that contribute to a review, in a stable order.

    A function rather than a constant so the import graph stays one-directional
    — each channel imports the shared interface, and only this composition point
    imports the channels — and so the cache can be handed in rather than reached
    for. ``cache=None`` means every channel computes from scratch, which is what
    the evaluation harness and the tests use: a run that depends on state left
    behind by an earlier run is not a measurement.
    """
    from panorama.dependencies import DependencyChannel
    from panorama.symbols import SymbolChannel

    channels: list[RetrievalChannel] = [
        LexicalChannel(),
        DependencyChannel(),
        SymbolChannel(cache),
    ]

    unknown = sorted(set(experimental) - set(EXPERIMENTAL_CHANNELS))
    if unknown:
        # A typo'd channel name that silently enabled nothing would look
        # exactly like a channel that helped nothing.
        raise ValueError(
            f"unknown experimental channel(s): {', '.join(unknown)}. "
            f"Known: {', '.join(EXPERIMENTAL_CHANNELS)}"
        )
    if "cochange" in experimental:
        from panorama.cochange import CoChangeChannel

        channels.append(CoChangeChannel())

    return tuple(channels)


def retrieve(
    pr: PullRequest,
    workspace: Workspace,
    *,
    cache: Cache | None = None,
    experimental: tuple[str, ...] = (),
) -> RetrievalResult:
    """Rank sibling repositories by how strongly the change points at them.

    The composition point: every channel ranks independently, and their
    rankings are fused. Leads come from the channels that match *locations* —
    the lexical pass, which matches lines, and the symbol index, which matches
    declarations. The dependency graph contributes ranking only, because a
    manifest edge names a repository and never a place inside it.

    ``cache`` is an optimisation and nothing more: passing one changes how much
    work the run does, never what it concludes. Every citation is validated
    against the live checkout regardless.
    """
    lexical_channel = LexicalChannel()
    lexical = lexical_channel.search(pr, workspace)
    signals_by_repo = {entry.repo: entry.signals for entry in lexical.ranked}

    results = [
        channel.rank(pr, workspace)
        for channel in active_channels(cache, experimental=experimental)
        # The lexical pass has already run; re-running it would double the
        # `git grep` cost of every review to produce the same answer.
        if channel.name != lexical_channel.name
    ]
    results.insert(0, _lexical_result(lexical_channel, lexical))

    ranked = [
        RepoRelevance(
            repo=fused.repo,
            score=fused.score,
            signals=signals_by_repo.get(fused.repo, []),
            provenance=list(fused.provenance),
        )
        for fused in fuse(results)
    ]

    hits = list(lexical.hits)
    seen = {(hit.repo, hit.path, hit.line) for hit in hits}
    for result in results:
        if result.channel == lexical_channel.name:
            continue
        for hit in result.hits:
            key = (hit.repo, hit.path, hit.line)
            if key not in seen:
                seen.add(key)
                hits.append(hit)

    convention_docs = {
        entry.name: entry.convention_docs
        for entry in workspace.org_entries()
        if entry.name != pr.repo and entry.convention_docs
    }

    return RetrievalResult(
        signals=lexical.signals,
        searched=lexical.searched,
        hits=hits,
        ranked_repos=ranked,
        convention_docs=convention_docs,
        truncated=lexical.truncated or any(r.truncated for r in results),
    )
