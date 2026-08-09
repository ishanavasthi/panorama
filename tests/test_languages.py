"""Tests for the language packs.

Two things are being checked, and they fail in different ways.

**Manifest reading** feeds the dependency graph. Getting it wrong produces a
*missing edge*, which is a silent recall loss — the worst failure mode in
retrieval, because nothing reports it. So the awkward shapes get tests: a
declared name that differs from the directory, a malformed file, a missing
section, both forms of Go's require block.

**Symbol extraction** feeds the symbol index. Getting it wrong mostly produces
noise, and the specific noise worth preventing is indexing a name that appears
only in a comment — a file that *documents* a function would otherwise look
like a file that *exports* one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panorama.languages import (
    GO,
    LANGUAGE_PACKS,
    MANIFEST_NAMES,
    PYTHON,
    TYPESCRIPT,
    extract_file_symbols,
    extract_symbols,
    pack_for_manifest,
    pack_for_path,
    read_go_manifest,
    read_npm_manifest,
    read_pyproject_manifest,
    read_repo_manifest,
    strip_comments,
)


def export_names(pack, text) -> list[str]:
    return [s.name for s in extract_symbols(pack, text).exports]


def import_names(pack, text) -> list[str]:
    return [s.name for s in extract_symbols(pack, text).imports]


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_every_pack_is_complete() -> None:
    for pack in LANGUAGE_PACKS:
        assert pack.name and pack.extensions and pack.manifest_names
        assert callable(pack.read_manifest)
        assert pack.export_patterns and pack.import_patterns


def test_no_two_packs_claim_the_same_extension() -> None:
    """An overlap would make `pack_for_path` order-dependent, which is the kind
    of thing that works until someone reorders the table."""
    seen: set[str] = set()
    for pack in LANGUAGE_PACKS:
        assert not (seen & pack.extensions), f"{pack.name} overlaps another pack"
        seen |= pack.extensions


def test_no_two_packs_claim_the_same_manifest() -> None:
    seen: set[str] = set()
    for pack in LANGUAGE_PACKS:
        assert not (seen & set(pack.manifest_names))
        seen |= set(pack.manifest_names)


def test_pack_lookup_by_path_and_manifest() -> None:
    assert pack_for_path("src/a.ts") is TYPESCRIPT
    assert pack_for_path("src/a.py") is PYTHON
    assert pack_for_path("cmd/main.go") is GO
    assert pack_for_path("README.md") is None
    assert pack_for_path("Makefile") is None
    assert pack_for_manifest("go.mod") is GO
    assert pack_for_manifest("Cargo.toml") is None
    assert MANIFEST_NAMES == {"package.json", "pyproject.toml", "go.mod"}


def test_extension_matching_is_case_insensitive() -> None:
    assert pack_for_path("SRC/Main.GO") is GO


# ---------------------------------------------------------------------------
# manifests — npm
# ---------------------------------------------------------------------------


def test_npm_declared_name_and_dependencies() -> None:
    facts = read_npm_manifest(
        """
        {
          "name": "@scope/thing",
          "dependencies": {"left": "^1.0.0"},
          "devDependencies": {"right": "^2.0.0"},
          "peerDependencies": {"middle": "*"}
        }
        """
    )
    assert facts.declared_name == "@scope/thing"
    assert set(facts.dependencies) == {"left", "right", "middle"}


def test_npm_declared_name_can_differ_from_any_directory_name() -> None:
    """The case the dependency graph must resolve by declared name.

    A repository directory called `api-server` publishing `@acme/api` is normal,
    and matching dependency strings against directory names would miss every
    edge into it.
    """
    facts = read_npm_manifest('{"name": "@acme/api", "dependencies": {"@acme/lib": "1"}}')
    assert facts.declared_name == "@acme/api"
    assert facts.dependencies == ("@acme/lib",)


def test_npm_malformed_is_empty_not_an_error() -> None:
    assert read_npm_manifest("{not json").declared_name is None
    assert read_npm_manifest("").dependencies == ()
    # Valid JSON of the wrong shape is just as ordinary.
    assert read_npm_manifest("[1, 2, 3]").declared_name is None
    assert read_npm_manifest('"a string"').dependencies == ()


def test_npm_missing_sections_are_fine() -> None:
    facts = read_npm_manifest('{"name": "solo"}')
    assert facts.declared_name == "solo" and facts.dependencies == ()


def test_npm_blank_name_is_treated_as_undeclared() -> None:
    assert read_npm_manifest('{"name": "   "}').declared_name is None


def test_npm_duplicate_dependency_across_sections_appears_once() -> None:
    facts = read_npm_manifest(
        '{"dependencies": {"x": "1"}, "devDependencies": {"x": "1"}}'
    )
    assert facts.dependencies == ("x",)


# ---------------------------------------------------------------------------
# manifests — python
# ---------------------------------------------------------------------------


def test_pyproject_name_and_dependencies() -> None:
    facts = read_pyproject_manifest(
        """
        [project]
        name = "acme-analytics"
        dependencies = ["requests>=2.31", "urllib3 (>=2)"]

        [project.optional-dependencies]
        dev = ["pytest>=8"]
        """
    )
    assert facts.declared_name == "acme-analytics"
    assert set(facts.dependencies) == {"requests", "urllib3", "pytest"}


def test_pyproject_strips_extras_and_markers() -> None:
    facts = read_pyproject_manifest(
        """
        [project]
        name = "x"
        dependencies = ["requests[socks]>=2.31 ; python_version < '3.12'"]
        """
    )
    assert facts.dependencies == ("requests",)


def test_pyproject_without_a_project_table_is_empty() -> None:
    facts = read_pyproject_manifest('[build-system]\nrequires = ["hatchling"]\n')
    assert facts.declared_name is None and facts.dependencies == ()


def test_pyproject_malformed_is_empty_not_an_error() -> None:
    assert read_pyproject_manifest("[project\nname =").declared_name is None


# ---------------------------------------------------------------------------
# manifests — go
# ---------------------------------------------------------------------------


def test_go_module_path_and_block_requires() -> None:
    facts = read_go_manifest(
        """
        module github.com/acme/gateway

        go 1.22

        require (
            github.com/google/uuid v1.6.0
            golang.org/x/sync v0.7.0 // indirect
        )
        """
    )
    assert facts.declared_name == "github.com/acme/gateway"
    assert facts.dependencies == ("github.com/google/uuid", "golang.org/x/sync")


def test_go_single_line_require() -> None:
    """The other form. Handling only the block form loses every edge here."""
    facts = read_go_manifest("module m\n\nrequire github.com/pkg/errors v0.9.1\n")
    assert facts.dependencies == ("github.com/pkg/errors",)


def test_go_both_require_forms_together() -> None:
    facts = read_go_manifest(
        """
        module m
        require solo/one v1.0.0
        require (
            block/two v2.0.0
        )
        """
    )
    assert set(facts.dependencies) == {"solo/one", "block/two"}


def test_go_module_with_no_requires() -> None:
    facts = read_go_manifest("module github.com/acme/gateway\n\ngo 1.22\n")
    assert facts.declared_name == "github.com/acme/gateway"
    assert facts.dependencies == ()


def test_go_empty_manifest_is_empty() -> None:
    assert read_go_manifest("").declared_name is None


# ---------------------------------------------------------------------------
# reading a repository's manifest from disk
# ---------------------------------------------------------------------------


def test_read_repo_manifest_finds_the_right_pack(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "thing"\n')
    pack, facts = read_repo_manifest(tmp_path)
    assert pack is PYTHON and facts.declared_name == "thing"


def test_a_repository_with_no_manifest_is_not_an_error(tmp_path: Path) -> None:
    """A documents repository is a normal member of an organisation."""
    (tmp_path / "README.md").write_text("# docs\n")
    pack, facts = read_repo_manifest(tmp_path)
    assert pack is None
    assert facts.declared_name is None and facts.dependencies == ()


def test_a_malformed_manifest_on_disk_yields_empty_facts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{oh no")
    pack, facts = read_repo_manifest(tmp_path)
    assert pack is TYPESCRIPT and facts.declared_name is None


# ---------------------------------------------------------------------------
# symbols — typescript
# ---------------------------------------------------------------------------


TS_SOURCE = """\
import { fetchLink, type LinkView } from "./api/links";
import formatter from "acme-shared";

export interface LinkResponse {
  id: string;
}

export enum LinkStatus {
  Active = "active",
}

export const MAX_ITEMS = 50;

export function buildLink(id: string): string {
  return helper(id);
}

function helper(value: string): string {
  return value.trim();
}

export { helper };
"""


def test_typescript_exports() -> None:
    names = export_names(TYPESCRIPT, TS_SOURCE)
    assert "LinkResponse" in names
    assert "LinkStatus" in names
    assert "MAX_ITEMS" in names
    assert "buildLink" in names
    # Re-exported via a brace list.
    assert "helper" in names


def test_typescript_does_not_export_a_module_private_function() -> None:
    """`function helper` on its own is not exported; it appears only because of
    the explicit `export { helper }` line. Without that line it must not."""
    source = TS_SOURCE.replace("export { helper };", "")
    assert "helper" not in export_names(TYPESCRIPT, source)


def test_typescript_imports() -> None:
    names = import_names(TYPESCRIPT, TS_SOURCE)
    assert "fetchLink" in names
    assert "LinkView" in names
    assert "formatter" in names


def test_typescript_require_destructuring_is_an_import() -> None:
    names = import_names(TYPESCRIPT, 'const { readFile } = require("fs");\n')
    assert "readFile" in names


def test_typescript_export_keywords_are_not_symbols() -> None:
    assert "default" not in export_names(TYPESCRIPT, "export default function run() {}\n")
    assert "run" in export_names(TYPESCRIPT, "export default function run() {}\n")


# ---------------------------------------------------------------------------
# symbols — python
# ---------------------------------------------------------------------------


PY_SOURCE = '''\
"""Module docstring mentioning def ghost_function and class GhostClass."""

from .status import STATUS_ACTIVE, is_reportable
import requests

API_BASE = "https://example.invalid"
TIMEOUT: int = 10


def fetch_link(link_id):
    """Docstring naming def another_ghost."""
    local = link_id
    return local


class Reporter:
    def render(self):  # indented: a method, not a module-level export
        return None
'''


def test_python_exports() -> None:
    names = export_names(PYTHON, PY_SOURCE)
    assert "fetch_link" in names
    assert "Reporter" in names
    assert "API_BASE" in names
    assert "TIMEOUT" in names


def test_python_indented_names_are_not_module_level_exports() -> None:
    names = export_names(PYTHON, PY_SOURCE)
    assert "render" not in names
    assert "local" not in names


def test_python_imports() -> None:
    names = import_names(PYTHON, PY_SOURCE)
    assert "STATUS_ACTIVE" in names
    assert "is_reportable" in names
    assert "requests" in names


def test_python_docstrings_are_not_indexed() -> None:
    """The specific noise worth preventing: a module that *documents* a
    function must not look like a module that defines one."""
    names = export_names(PYTHON, PY_SOURCE)
    assert "ghost_function" not in names
    assert "GhostClass" not in names
    assert "another_ghost" not in names


def test_python_comparison_is_not_an_assignment() -> None:
    assert export_names(PYTHON, "CONST == 5\n") == []


# ---------------------------------------------------------------------------
# symbols — go
# ---------------------------------------------------------------------------


GO_SOURCE = """\
package client

import (
	"encoding/json"
	"net/http"
)

// ErrMissing describes a link the API would not serve. Mentions parseCode.
var ErrMissing = errors.New("missing")

const codeNotFound = "not_found"

type Link struct {
	ID string
}

func Fetch(base string) (*Link, error) {
	return nil, nil
}

func (l *Link) Describe() string {
	return l.ID
}

func parseCode(path string) string {
	return path
}
"""


def test_go_exports_only_capitalised_names() -> None:
    names = export_names(GO, GO_SOURCE)
    assert "Fetch" in names
    assert "Link" in names
    assert "ErrMissing" in names
    assert "Describe" in names  # method with a receiver


def test_go_unexported_names_are_not_exports() -> None:
    """Go's own visibility rule does the filtering, so this needs no heuristic."""
    names = export_names(GO, GO_SOURCE)
    assert "parseCode" not in names
    assert "codeNotFound" not in names


def test_go_imports_keep_the_whole_module_path() -> None:
    """A Go import is a module path, and the path is the point.

    Splitting `"encoding/json"` into `encoding` and `json` would throw away
    exactly what the dependency graph resolves edges on, and would invent two
    names that are not symbols in any repository.
    """
    names = import_names(GO, GO_SOURCE)
    assert "encoding/json" in names
    assert "net/http" in names
    assert "encoding" not in names and "net" not in names


def test_go_comment_mentions_are_not_indexed() -> None:
    assert "parseCode" not in export_names(GO, GO_SOURCE)


# ---------------------------------------------------------------------------
# files that yield nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack", LANGUAGE_PACKS, ids=lambda p: p.name)
def test_an_empty_file_yields_nothing(pack) -> None:
    facts = extract_symbols(pack, "")
    assert facts.exports == () and facts.imports == ()


@pytest.mark.parametrize("pack", LANGUAGE_PACKS, ids=lambda p: p.name)
def test_a_file_of_only_comments_yields_nothing(pack) -> None:
    marker = pack.line_comments[0]
    text = f"{marker} export function ghost() {{}}\n{marker} def ghost2():\n"
    facts = extract_symbols(pack, text)
    assert facts.exports == (), f"{pack.name} indexed a commented-out symbol"


@pytest.mark.parametrize("pack", LANGUAGE_PACKS, ids=lambda p: p.name)
def test_prose_yields_nothing(pack) -> None:
    facts = extract_symbols(pack, "the quick brown fox\njumps over\n")
    assert facts.exports == () and facts.imports == ()


def test_a_block_comment_spanning_lines_is_removed() -> None:
    text = "/*\nexport function ghost() {}\n*/\nexport const real = 1;\n"
    names = export_names(TYPESCRIPT, text)
    assert names == ["real"]


def test_line_numbers_survive_comment_stripping() -> None:
    """A citation is only useful if it points at the right line."""
    text = "// a\n/* b\n c */\nexport const real = 1;\n"
    facts = extract_symbols(TYPESCRIPT, text)
    assert facts.exports[0].line == 4


def test_strip_comments_preserves_line_count() -> None:
    text = "one\n/* two\nthree */\nfour\n"
    assert len(strip_comments(TYPESCRIPT, text)) == 4


# ---------------------------------------------------------------------------
# reading from disk
# ---------------------------------------------------------------------------


def test_extract_file_symbols_reads_a_real_file(tmp_path: Path) -> None:
    source = tmp_path / "mod.ts"
    source.write_text("export function alpha() {}\n")
    assert [s.name for s in extract_file_symbols(source).exports] == ["alpha"]


def test_an_unknown_extension_yields_nothing(tmp_path: Path) -> None:
    other = tmp_path / "notes.md"
    other.write_text("export function alpha() {}\n")
    assert extract_file_symbols(other).exports == ()


def test_an_oversized_file_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """A generated bundle can be megabytes of one line and holds nothing worth
    indexing, so there is a cap rather than a promise to be careful."""
    from panorama import languages

    monkeypatch.setattr(languages, "MAX_SOURCE_BYTES", 32)
    big = tmp_path / "bundle.js"
    big.write_text("export function alpha() {}\n" * 50)
    assert languages.extract_file_symbols(big).exports == ()


def test_a_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert extract_file_symbols(tmp_path / "absent.ts").exports == ()
