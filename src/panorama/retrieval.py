"""Deterministic cross-repository retrieval.

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
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from panorama.errors import PreflightError
from panorama.intake import PullRequest
from panorama.workspace import Workspace

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


@dataclass(frozen=True)
class Hit:
    """A sibling-repository line matched by a signal."""

    repo: str
    path: str
    line: int
    token: str
    window: tuple[str, ...]


@dataclass
class RepoRelevance:
    """A sibling repository ranked by how strongly the diff points at it."""

    repo: str
    score: float
    signals: list[str] = field(default_factory=list)


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


def retrieve(pr: PullRequest, workspace: Workspace) -> RetrievalResult:
    """Rank sibling repositories by how strongly the diff points at them."""
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
        ranked.append(RepoRelevance(repo=repo.name, score=round(score, 3), signals=sorted(tokens)))
    ranked.sort(key=lambda r: (-r.score, r.repo))

    convention_docs = {
        entry.name: entry.convention_docs
        for entry in workspace.org_entries()
        if entry.name != pr.repo and entry.convention_docs
    }

    return RetrievalResult(
        signals=signals,
        searched=searched,
        hits=hits,
        ranked_repos=ranked,
        convention_docs=convention_docs,
        truncated=truncated,
    )
