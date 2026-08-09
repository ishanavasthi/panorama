"""What Panorama knows about a language, as a table rather than a framework.

The structural retrieval channels need three facts per language:

1. **Which manifest file declares the package**, and how to read the name it
   declares for itself plus the names it depends on. That is what lets V2.4
   build a dependency graph by resolving *declared name to declared name*,
   rather than by matching a dependency string against a directory name — the
   shortcut that works on tidy fixtures and fails on real organisations where
   ``github.com/example/api-server`` publishes ``@example/api``.
2. **Which files are source**, so the symbol index does not read images.
3. **What an export and an import look like**, as anchored regexes.

V1 explicitly deferred "a language-plugin framework". This narrowly reverses
that, and the distinction is worth being precise about, because "it's just a
table" is exactly what someone says right before shipping a plugin system.

What this is: a frozen tuple of frozen dataclasses, defined in one file, read
by name. What it deliberately is not: dynamically loaded, discoverable via
entry points, registrable at runtime, or extensible by a third party. There is
no lifecycle, no ordering contract between packs, and no way for one pack to
observe another. Adding a language means adding a literal to this file and its
tests. If any of those stop being true, the reversal has gone too far.

**Regex, not a parser.** No tree-sitter, no AST. The reasons are that it costs
no dependency, and that the evaluation harness can tell us empirically whether
recall is good enough. If V2.7's measurement shows regex recall is the binding
constraint, tree-sitter becomes a *measured* upgrade rather than a speculative
one. The honest limits of that choice are recorded at the bottom of this file.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: Nothing is read past this. A generated bundle or a vendored blob can be
#: megabytes of one line, and indexing it buys nothing — the symbols that
#: matter are the ones a human wrote.
MAX_SOURCE_BYTES = 512 * 1024

#: Directories that hold code nobody in this organisation wrote. Indexing a
#: vendored dependency would fill the index with symbols that belong to a
#: third party and are not owned by the repository containing them, which is
#: precisely the wrong answer to "who owns this".
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "out",
        "coverage",
        ".next",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


@dataclass(frozen=True)
class ManifestFacts:
    """What one package manifest says about itself and its dependencies."""

    #: The name the package declares for *itself*. ``None`` when the manifest
    #: is missing, malformed, or simply does not declare one — all of which are
    #: ordinary, and none of which are errors.
    declared_name: str | None = None
    #: Names this package depends on, as written. Resolution to repositories
    #: happens in the dependency-graph channel, not here.
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Symbol:
    """One exported or imported name, with where it was found."""

    name: str
    line: int


@dataclass(frozen=True)
class SymbolFacts:
    """The exported and imported names found in one source file."""

    exports: tuple[Symbol, ...] = ()
    imports: tuple[Symbol, ...] = ()


@dataclass(frozen=True)
class LanguagePack:
    """Everything Panorama knows about one language. Data, not behaviour."""

    name: str
    #: File extensions that count as source for this language.
    extensions: frozenset[str]
    #: Manifest filenames, most specific first.
    manifest_names: tuple[str, ...]
    #: Reads a manifest's text into facts. One small function per format,
    #: because JSON, TOML and go.mod are genuinely three different parsers —
    #: not an extension point.
    read_manifest: Callable[[str], ManifestFacts]
    #: Anchored patterns whose first group is an exported name.
    export_patterns: tuple[re.Pattern[str], ...] = ()
    #: Anchored patterns whose groups are imported names.
    import_patterns: tuple[re.Pattern[str], ...] = ()
    #: Whether a captured import group is a *list* of names to split apart.
    #: True for `import { a, b } from "mod"`. False for Go, where the captured
    #: group is a single module path: splitting `"encoding/json"` into two
    #: names would discard exactly the path the dependency graph needs.
    split_imports: bool = True
    #: Line-comment markers, stripped before matching.
    line_comments: tuple[str, ...] = ()
    #: Block-comment or docstring delimiters, as (open, close) pairs.
    block_comments: tuple[tuple[str, str], ...] = ()
    #: Names that are structure rather than symbols, dropped after matching.
    ignore_names: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# manifest readers
# ---------------------------------------------------------------------------

#: npm dependency sections that represent a real edge between packages.
#: ``optionalDependencies`` is included because an optional dependency that is
#: present is still a dependency; ``devDependencies`` because a build-time edge
#: still means a change can break the other repository's build.
_NPM_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

#: Strips a PEP 508 requirement down to its distribution name:
#: ``requests[socks]>=2.31 ; python_version < "3.12"`` -> ``requests``.
_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")

#: ``module github.com/example/service`` and the paths inside a require block.
_GO_MODULE = re.compile(r"^\s*module\s+(\S+)")
_GO_REQUIRE_ONE = re.compile(r"^\s*require\s+(\S+)\s+\S+")
_GO_REQUIRE_OPEN = re.compile(r"^\s*require\s*\(\s*$")
_GO_REQUIRE_ENTRY = re.compile(r"^\s*(\S+)\s+v\S+")


def read_npm_manifest(text: str) -> ManifestFacts:
    """``package.json``: the declared name and every dependency section."""
    try:
        data = json.loads(text)
    except ValueError:
        return ManifestFacts()
    if not isinstance(data, dict):
        return ManifestFacts()

    name = data.get("name")
    deps: list[str] = []
    for section in _NPM_DEPENDENCY_SECTIONS:
        block = data.get(section)
        if isinstance(block, dict):
            deps.extend(str(key) for key in block)

    return ManifestFacts(
        declared_name=name.strip() if isinstance(name, str) and name.strip() else None,
        dependencies=tuple(dict.fromkeys(deps)),
    )


def read_pyproject_manifest(text: str) -> ManifestFacts:
    """``pyproject.toml``: PEP 621 name and dependencies, plus optional extras."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return ManifestFacts()

    project = data.get("project")
    if not isinstance(project, dict):
        return ManifestFacts()

    raw: list[str] = []
    listed = project.get("dependencies")
    if isinstance(listed, list):
        raw.extend(str(item) for item in listed)
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        for group in optional.values():
            if isinstance(group, list):
                raw.extend(str(item) for item in group)

    deps: list[str] = []
    for requirement in raw:
        match = _PEP508_NAME.match(requirement)
        if match:
            deps.append(match.group(1))

    name = project.get("name")
    return ManifestFacts(
        declared_name=name.strip() if isinstance(name, str) and name.strip() else None,
        dependencies=tuple(dict.fromkeys(deps)),
    )


def read_go_manifest(text: str) -> ManifestFacts:
    """``go.mod``: the module path and its required module paths.

    Handwritten rather than regex-only for the require block, because go.mod
    has two forms — a single ``require x v1`` line and a parenthesised block —
    and conflating them silently loses every dependency in the block form.
    """
    declared: str | None = None
    deps: list[str] = []
    in_block = False

    for raw in text.splitlines():
        line = raw.split("//", 1)[0]
        if not line.strip():
            continue

        if in_block:
            if line.strip() == ")":
                in_block = False
                continue
            entry = _GO_REQUIRE_ENTRY.match(line)
            if entry:
                deps.append(entry.group(1))
            continue

        if declared is None:
            module = _GO_MODULE.match(line)
            if module:
                declared = module.group(1)
                continue

        if _GO_REQUIRE_OPEN.match(line):
            in_block = True
            continue

        single = _GO_REQUIRE_ONE.match(line)
        if single:
            deps.append(single.group(1))

    return ManifestFacts(declared_name=declared, dependencies=tuple(dict.fromkeys(deps)))


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

_TS_EXPORTS = (
    # export function f / export class C / export interface I / export type T ...
    re.compile(
        r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
        r"(?:function\*?|class|interface|type|enum|namespace)\s+([A-Za-z_$][\w$]*)"
    ),
    # export const x = ... / export let / export var
    re.compile(r"^\s*export\s+(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    # export { a, b as c } — every name in the brace list
    re.compile(r"^\s*export\s*\{([^}]*)\}"),
)
_TS_IMPORTS = (
    # import { a, b } from "mod" — the braces
    re.compile(r"^\s*import\s+(?:type\s+)?\{([^}]*)\}\s*from"),
    # import x from "mod" / import * as x from "mod"
    re.compile(r"^\s*import\s+(?:type\s+)?(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s*(?:,|from)"),
    # const { a } = require("mod")
    re.compile(r"^\s*(?:const|let|var)\s*\{([^}]*)\}\s*=\s*require\("),
)

_PY_EXPORTS = (
    re.compile(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^class\s+([A-Za-z_]\w*)"),
    # A module-level binding at column zero: constants and singletons.
    re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)"),
)
_PY_IMPORTS = (
    re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.*)$"),
    re.compile(r"^\s*import\s+([\w.]+)"),
)

_GO_EXPORTS = (
    # Only capitalised names are exported in Go — the language does the
    # visibility filtering for us, which is why this pack needs no heuristics.
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)"),
    re.compile(r"^type\s+([A-Z]\w*)"),
    re.compile(r"^(?:var|const)\s+([A-Z]\w*)"),
)
_GO_IMPORTS = (
    re.compile(r"^\s*(?:import\s+)?(?:[\w.]+\s+)?\"([^\"]+)\"\s*$"),
)

TYPESCRIPT = LanguagePack(
    name="typescript",
    extensions=frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"}),
    manifest_names=("package.json",),
    read_manifest=read_npm_manifest,
    export_patterns=_TS_EXPORTS,
    import_patterns=_TS_IMPORTS,
    line_comments=("//",),
    block_comments=(("/*", "*/"),),
    ignore_names=frozenset({"default", "type", "as", "from"}),
)

PYTHON = LanguagePack(
    name="python",
    extensions=frozenset({".py", ".pyi"}),
    manifest_names=("pyproject.toml",),
    read_manifest=read_pyproject_manifest,
    export_patterns=_PY_EXPORTS,
    import_patterns=_PY_IMPORTS,
    line_comments=("#",),
    block_comments=(('"""', '"""'), ("'''", "'''")),
    ignore_names=frozenset({"as"}),
)

GO = LanguagePack(
    name="go",
    extensions=frozenset({".go"}),
    manifest_names=("go.mod",),
    read_manifest=read_go_manifest,
    export_patterns=_GO_EXPORTS,
    import_patterns=_GO_IMPORTS,
    split_imports=False,
    line_comments=("//",),
    block_comments=(("/*", "*/"),),
)

#: The whole table. Order is stable so any derived output is deterministic.
LANGUAGE_PACKS: tuple[LanguagePack, ...] = (TYPESCRIPT, PYTHON, GO)

#: Every manifest filename any pack knows about, for a cheap "is this a
#: manifest" check without walking the table.
MANIFEST_NAMES: frozenset[str] = frozenset(
    name for pack in LANGUAGE_PACKS for name in pack.manifest_names
)


def pack_for_path(path: str | Path) -> LanguagePack | None:
    """The pack owning this file's extension, or ``None`` for anything else."""
    suffix = Path(path).suffix.lower()
    for pack in LANGUAGE_PACKS:
        if suffix in pack.extensions:
            return pack
    return None


def pack_for_manifest(name: str) -> LanguagePack | None:
    """The pack owning a manifest filename, or ``None``."""
    for pack in LANGUAGE_PACKS:
        if name in pack.manifest_names:
            return pack
    return None


def read_repo_manifest(repo_path: Path) -> tuple[LanguagePack | None, ManifestFacts]:
    """Find and read the manifest at a repository's root.

    Returns the pack that read it alongside the facts. A repository with no
    manifest — a documents repository, say — is an ordinary outcome, not an
    error: it simply contributes no dependency edges.
    """
    for pack in LANGUAGE_PACKS:
        for manifest_name in pack.manifest_names:
            manifest = repo_path / manifest_name
            if not manifest.is_file():
                continue
            try:
                text = manifest.read_text(errors="replace")
            except OSError:
                continue
            return pack, pack.read_manifest(text)
    return None, ManifestFacts()


# ---------------------------------------------------------------------------
# symbol extraction
# ---------------------------------------------------------------------------


def strip_comments(pack: LanguagePack, text: str) -> list[str]:
    """Blank out comment content, preserving line numbering.

    Lines are blanked rather than removed so a symbol's reported line number
    still matches the file on disk — a citation is only useful if it points at
    the right line.

    The limits are worth stating plainly, because they are the price of not
    having a parser. A delimiter inside a string literal is treated as opening
    a comment, so ``const marker = "/*"`` blinds the rest of the file until the
    next ``*/``. In practice that costs recall on a small number of files, and
    recall loss here is safe: a symbol Panorama fails to index is a lead it does
    not offer, never a citation it invents. If the measurement ever shows this
    is the binding constraint, that is the argument for tree-sitter.
    """
    lines = text.splitlines()
    out: list[str] = []
    open_delim: tuple[str, str] | None = None

    for raw in lines:
        line = raw

        if open_delim is not None:
            closer = open_delim[1]
            end = line.find(closer)
            if end < 0:
                out.append("")
                continue
            line = " " * (end + len(closer)) + line[end + len(closer) :]
            open_delim = None

        # Consume block openers left to right until the line is settled.
        while True:
            best: tuple[int, tuple[str, str]] | None = None
            for delim in pack.block_comments:
                found = line.find(delim[0])
                if found >= 0 and (best is None or found < best[0]):
                    best = (found, delim)
            if best is None:
                break
            start, delim = best
            after = line.find(delim[1], start + len(delim[0]))
            if after < 0:
                line = line[:start]
                open_delim = delim
                break
            end = after + len(delim[1])
            line = line[:start] + " " * (end - start) + line[end:]

        for marker in pack.line_comments:
            found = line.find(marker)
            if found >= 0:
                line = line[:found]

        out.append(line)

    return out


def _split_names(blob: str) -> list[str]:
    """Names out of a brace list or an import tail: ``a, b as c`` -> a, b, c."""
    names: list[str] = []
    for part in blob.split(","):
        for token in re.findall(r"[A-Za-z_$][\w$]*", part):
            names.append(token)
    return names


def extract_symbols(pack: LanguagePack, text: str) -> SymbolFacts:
    """Exported and imported names in one file, with line numbers.

    Comment content is removed first, so a symbol that appears only in a
    comment or a docstring is not indexed — otherwise a file that *documents* a
    function would look like a file that *exports* it, and the symbol index
    would point at prose.
    """
    exports: list[Symbol] = []
    imports: list[Symbol] = []
    seen_exports: set[str] = set()
    seen_imports: set[str] = set()

    for number, line in enumerate(strip_comments(pack, text), start=1):
        if not line.strip():
            continue

        for pattern in pack.export_patterns:
            match = pattern.match(line)
            if not match:
                continue
            for name in _split_names(match.group(1)):
                if name in pack.ignore_names or name in seen_exports:
                    continue
                seen_exports.add(name)
                exports.append(Symbol(name=name, line=number))

        for pattern in pack.import_patterns:
            match = pattern.match(line)
            if not match:
                continue
            for group in match.groups():
                if not group:
                    continue
                names = _split_names(group) if pack.split_imports else [group.strip()]
                for name in names:
                    if not name or name in pack.ignore_names or name in seen_imports:
                        continue
                    seen_imports.add(name)
                    imports.append(Symbol(name=name, line=number))
            break  # one import form per line is enough

    return SymbolFacts(exports=tuple(exports), imports=tuple(imports))


def iter_source_files(root: Path):
    """Every file under ``root`` that some pack claims, in a stable order.

    Sorted so anything derived from this walk is reproducible: an index whose
    contents depend on filesystem ordering would produce a cache that differs
    between machines for no reason.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts[:-1]):
            continue
        if pack_for_path(path) is None:
            continue
        yield path


def extract_file_symbols(path: Path) -> SymbolFacts:
    """Extract from a file on disk, skipping anything oversized or unreadable."""
    pack = pack_for_path(path)
    if pack is None:
        return SymbolFacts()
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return SymbolFacts()
        text = path.read_text(errors="replace")
    except OSError:
        return SymbolFacts()
    return extract_symbols(pack, text)
