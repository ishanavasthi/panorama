"""Tests for the symbol index and its channel.

This channel makes the strongest claim in retrieval — "that repository imports a
name this change deletes" — so the tests concentrate on the two ways a strong
claim goes wrong: asserting it when it is not true, and quietly failing to
notice it when it is.

The cache gets its own attention here for a different reason. It exists purely
to avoid re-reading files, and the property that has to hold is that using it
changes *how much work happens* and never *what is concluded*.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from panorama import symbols as symbols_mod
from panorama.cache import Cache
from panorama.channels import RetrievalChannel
from panorama.intake import PullRequest
from panorama.symbols import (
    BREAKS,
    DUPLICATES,
    RepoSymbolIndex,
    SymbolChannel,
    SymbolIndexer,
    build_index,
    claim_rank,
    diff_symbols,
)
from panorama.workspace import Workspace


def make_org(root: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    for name, files in repos.items():
        repo = root / name
        repo.mkdir(parents=True, exist_ok=True)
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for args in (
            ["init", "-b", "main"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "seed"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    return Workspace(root)


def pull_request(repo: str, diff: str = "") -> PullRequest:
    return PullRequest(
        owner="o",
        repo=repo,
        number=1,
        url="https://example.invalid/1",
        base_sha="0" * 40,
        head_sha="1" * 40,
        base_ref="main",
        head_ref="branch",
        title="t",
        body="",
        diff=diff,
    )


def diff_for(path: str, removed: list[str], added: list[str]) -> str:
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n{body}"


# ---------------------------------------------------------------------------
# indexing a repository
# ---------------------------------------------------------------------------


def test_an_index_records_exports_with_their_location(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {"lib": {"src/a.ts": "// leading comment\nexport function alpha() {}\n"}},
    )
    index = build_index(workspace.repo("lib"))
    assert "alpha" in index.exports
    assert index.exports["alpha"][0].path == "src/a.ts"
    assert index.exports["alpha"][0].line == 2


def test_an_index_records_imports(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org", {"app": {"src/a.ts": 'import { alpha } from "lib";\n'}}
    )
    index = build_index(workspace.repo("app"))
    assert "alpha" in index.imports


def test_indexing_spans_every_language(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "poly": {
                "src/a.ts": "export const tsName = 1;\n",
                "src/b.py": "def py_name():\n    return 1\n",
                "src/c.go": "package c\n\nfunc GoName() {}\n",
            }
        },
    )
    index = build_index(workspace.repo("poly"))
    assert {"tsName", "py_name", "GoName"} <= index.exports.keys()


def test_a_symbol_only_mentioned_in_a_comment_is_not_indexed(tmp_path: Path) -> None:
    """Otherwise a repository that *documents* a function looks like one that
    exports it, and the index starts pointing at prose."""
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {
                "src/a.ts": "// export function ghost() {}\n/* export const alsoGhost = 1 */\n"
                "export const real = 1;\n"
            }
        },
    )
    index = build_index(workspace.repo("lib"))
    assert "real" in index.exports
    assert "ghost" not in index.exports
    assert "alsoGhost" not in index.exports


def test_vendored_directories_are_not_indexed(tmp_path: Path) -> None:
    """A vendored dependency's symbols belong to a third party, and indexing
    them would answer "who owns this" with the wrong repository."""
    workspace = make_org(
        tmp_path / "org",
        {
            "app": {
                "src/a.ts": "export const mine = 1;\n",
                "node_modules/pkg/index.ts": "export const theirs = 1;\n",
            }
        },
    )
    index = build_index(workspace.repo("app"))
    assert "mine" in index.exports and "theirs" not in index.exports


def test_the_file_cap_is_enforced_and_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(symbols_mod, "MAX_INDEXED_FILES", 3)
    workspace = make_org(
        tmp_path / "org",
        {"big": {f"src/f{i}.ts": f"export const s{i} = 1;\n" for i in range(10)}},
    )
    index = build_index(workspace.repo("big"))
    assert index.files_indexed == 3
    assert index.truncated is True


def test_a_repository_with_no_source_indexes_to_nothing(tmp_path: Path) -> None:
    workspace = make_org(tmp_path / "org", {"docs": {"README.md": "# docs\n"}})
    index = build_index(workspace.repo("docs"))
    assert index.exports == {} and index.imports == {}


# ---------------------------------------------------------------------------
# reading the diff
# ---------------------------------------------------------------------------


def test_a_removed_export_is_detected() -> None:
    changed = diff_symbols(diff_for("src/a.ts", ["export function gone() {}"], []))
    assert changed.removed_exports == {"gone"}
    assert changed.added_exports == frozenset()


def test_an_added_export_is_detected() -> None:
    changed = diff_symbols(diff_for("src/a.ts", [], ["export function fresh() {}"]))
    assert changed.added_exports == {"fresh"}


def test_a_rename_is_one_removal_and_one_addition() -> None:
    changed = diff_symbols(
        diff_for("src/a.ts", ["export function before() {}"], ["export function after() {}"])
    )
    assert changed.removed_exports == {"before"}
    assert changed.added_exports == {"after"}


def test_a_symbol_present_on_both_sides_is_neither_added_nor_removed() -> None:
    """The subtraction that stops a reformat reading as a contract break.

    Re-indenting or re-wrapping a file re-emits its declarations on both sides
    of the diff. Without cancelling those out, every formatting change would
    look like deleting and re-declaring the entire file.
    """
    changed = diff_symbols(
        diff_for(
            "src/a.ts",
            ["export function same() {}", "export const other = 1;"],
            ["export function same() {}", "export const other = 2;"],
        )
    )
    assert changed.removed_exports == frozenset()
    assert changed.added_exports == frozenset()


def test_a_deleted_file_still_yields_its_removed_declarations() -> None:
    """A deleted file has no `+++` path, and reading only that path would make
    the largest possible contract break invisible."""
    diff = (
        "diff --git a/src/a.ts b/src/a.ts\n"
        "deleted file mode 100644\n"
        "--- a/src/a.ts\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-export function gone() {}\n"
    )
    assert diff_symbols(diff).removed_exports == {"gone"}


def test_a_file_in_an_unknown_language_is_skipped() -> None:
    assert diff_symbols(diff_for("README.md", ["export function ghost() {}"], [])).empty


def test_an_empty_diff_yields_nothing() -> None:
    assert diff_symbols("").empty


# ---------------------------------------------------------------------------
# the channel's claims
# ---------------------------------------------------------------------------


def test_a_break_outranks_a_duplicate() -> None:
    assert claim_rank(BREAKS) < claim_rank(DUPLICATES)


def test_a_sibling_that_imports_a_removed_name_is_reported_as_broken(
    tmp_path: Path,
) -> None:
    """The strongest claim in retrieval, and the reason this channel exists."""
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {"src/a.ts": "export function alpha() {}\n"},
            "app": {"src/use.ts": 'import { alpha } from "lib";\n'},
            "bystander": {"src/x.ts": "export const unrelated = 1;\n"},
        },
    )
    diff = diff_for("src/a.ts", ["export function alpha() {}"], [])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)

    assert [r.repo for r in result.ranked] == ["app"]
    assert result.ranked[0].rank == claim_rank(BREAKS)
    assert "imports" in result.ranked[0].justification


def test_a_sibling_that_already_exports_an_added_name_is_reported_as_duplication(
    tmp_path: Path,
) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "app": {"src/a.ts": "export const x = 1;\n"},
            "lib": {"src/helper.ts": "export function shared() {}\n"},
        },
    )
    diff = diff_for("src/new.ts", [], ["export function shared() {}"])
    result = SymbolChannel().rank(pull_request("app", diff), workspace)

    assert [r.repo for r in result.ranked] == ["lib"]
    assert result.ranked[0].rank == claim_rank(DUPLICATES)
    assert "already exports" in result.ranked[0].justification


def test_a_break_wins_when_a_repository_matches_both_ways(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {"src/a.ts": "export function alpha() {}\n"},
            "app": {
                "src/use.ts": 'import { alpha } from "lib";\n',
                "src/own.ts": "export function beta() {}\n",
            },
        },
    )
    diff = diff_for("src/a.ts", ["export function alpha() {}"], ["export function beta() {}"])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)
    assert result.ranked[0].rank == claim_rank(BREAKS)


def test_the_channel_points_at_the_exact_consuming_line(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {"src/a.ts": "export function alpha() {}\n"},
            "app": {"src/use.ts": "// header\n// header\nimport { alpha } from 'lib';\n"},
        },
    )
    diff = diff_for("src/a.ts", ["export function alpha() {}"], [])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)

    assert result.hits
    hit = result.hits[0]
    assert (hit.repo, hit.path, hit.line, hit.token) == ("app", "src/use.ts", 3, "alpha")


def test_the_channel_is_silent_when_nothing_is_declared_or_undeclared(
    tmp_path: Path,
) -> None:
    workspace = make_org(tmp_path / "org", {"lib": {"src/a.ts": "export const x = 1;\n"}})
    diff = diff_for("src/a.ts", ["  x = 1"], ["  x = 2"])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)
    assert result.ranked == () and result.hits == ()
    assert any("declares and removes no exported names" in n for n in result.notes)


def test_a_removed_name_nobody_imports_surfaces_nothing(tmp_path: Path) -> None:
    """The private-rename control, in miniature: a name with no reader anywhere
    must produce no claim at all."""
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {"src/a.ts": "export function alpha() {}\n"},
            "app": {"src/use.ts": "export const unrelated = 1;\n"},
        },
    )
    diff = diff_for("src/a.ts", ["function privateHelper() {}"], ["function renamedHelper() {}"])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)
    assert result.ranked == () and result.hits == ()


def test_the_pull_requests_own_repository_is_never_ranked(tmp_path: Path) -> None:
    workspace = make_org(
        tmp_path / "org",
        {
            "lib": {
                "src/a.ts": "export function alpha() {}\n",
                "src/self.ts": 'import { alpha } from "./a";\n',
            },
            "app": {"src/x.ts": "export const y = 1;\n"},
        },
    )
    diff = diff_for("src/a.ts", ["export function alpha() {}"], [])
    result = SymbolChannel().rank(pull_request("lib", diff), workspace)
    assert "lib" not in [r.repo for r in result.ranked]


def test_the_channel_satisfies_the_protocol() -> None:
    assert isinstance(SymbolChannel(), RetrievalChannel)
    assert SymbolChannel().name == "symbols"


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


@pytest.fixture
def org(tmp_path: Path) -> Workspace:
    return make_org(
        tmp_path / "org",
        {
            "lib": {"src/a.ts": "export function alpha() {}\n"},
            "app": {"src/use.ts": 'import { alpha } from "lib";\n'},
        },
    )


def test_a_second_run_is_served_from_the_cache(org: Workspace, tmp_path: Path) -> None:
    with Cache(tmp_path / "c" / "o.db") as cache:
        first = SymbolIndexer(cache)
        first.index_for(org.repo("lib"))
        assert (first.hits, first.misses) == (0, 1)

        second = SymbolIndexer(cache)
        second.index_for(org.repo("lib"))
        assert (second.hits, second.misses) == (1, 0)


def test_a_warm_cache_reads_no_files(org: Workspace, tmp_path: Path, monkeypatch) -> None:
    """The exit criterion, asserted directly rather than by timing.

    A timing assertion on a six-repository fixture would be noise; "it did not
    open a single file" is the property that actually makes it faster.
    """
    with Cache(tmp_path / "c" / "o.db") as cache:
        SymbolIndexer(cache).index_for(org.repo("lib"))

        def explode(_view):
            raise AssertionError("a warm cache must not rebuild the index")

        monkeypatch.setattr(symbols_mod, "build_index", explode)
        warm = SymbolIndexer(cache).index_for(org.repo("lib"))
        assert "alpha" in warm.exports


def test_a_moved_commit_invalidates_the_index(org: Workspace, tmp_path: Path) -> None:
    """Invalidation is the shape of the key, not a policy — so a new commit
    simply misses."""
    with Cache(tmp_path / "c" / "o.db") as cache:
        SymbolIndexer(cache).index_for(org.repo("lib"))

        lib = org.repo("lib").path
        (lib / "src" / "b.ts").write_text("export const added = 1;\n")
        for args in (
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "more"],
        ):
            subprocess.run(["git", "-C", str(lib), *args], capture_output=True, check=True)

        indexer = SymbolIndexer(cache)
        refreshed = indexer.index_for(org.repo("lib"))
        assert (indexer.hits, indexer.misses) == (0, 1)
        assert "added" in refreshed.exports


def test_a_stale_generation_is_pruned(org: Workspace, tmp_path: Path) -> None:
    """Without pruning the cache grows one generation per commit, forever."""
    with Cache(tmp_path / "c" / "o.db") as cache:
        SymbolIndexer(cache).index_for(org.repo("lib"))
        lib = org.repo("lib").path
        (lib / "src" / "b.ts").write_text("export const added = 1;\n")
        for args in (
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "more"],
        ):
            subprocess.run(["git", "-C", str(lib), *args], capture_output=True, check=True)
        SymbolIndexer(cache).index_for(org.repo("lib"))

        assert cache.stats().n_facts == 1


def test_a_corrupt_payload_reads_as_a_miss(org: Workspace, tmp_path: Path) -> None:
    with Cache(tmp_path / "c" / "o.db") as cache:
        view = org.repo("lib")
        cache.put_facts(view.name, view.head_sha, "symbols", {"version": 999, "junk": True})
        indexer = SymbolIndexer(cache)
        rebuilt = indexer.index_for(view)
        assert indexer.misses == 1
        assert "alpha" in rebuilt.exports


def test_an_index_round_trips_through_its_payload(org: Workspace) -> None:
    original = build_index(org.repo("lib"))
    restored = RepoSymbolIndex.from_payload("lib", original.head_sha, original.to_payload())
    assert restored is not None
    assert restored.exports.keys() == original.exports.keys()
    assert restored.exports["alpha"][0].line == original.exports["alpha"][0].line


def test_a_payload_of_the_wrong_shape_is_refused() -> None:
    assert RepoSymbolIndex.from_payload("r", "s", {"version": 1}) is None
    assert RepoSymbolIndex.from_payload("r", "s", {"version": 1, "exports": 5}) is None
    assert RepoSymbolIndex.from_payload("r", "s", []) is None


def test_using_a_cache_does_not_change_what_is_concluded(
    org: Workspace, tmp_path: Path
) -> None:
    """Constraint #7 at this layer: the cache changes effort, never conclusions."""
    diff = diff_for("src/a.ts", ["export function alpha() {}"], [])
    pr = pull_request("lib", diff)

    cold = SymbolChannel().rank(pr, org)
    with Cache(tmp_path / "c" / "o.db") as cache:
        SymbolChannel(cache).rank(pr, org)  # warm it
        warm = SymbolChannel(cache).rank(pr, org)

    assert [r.repo for r in cold.ranked] == [r.repo for r in warm.ranked]
    assert [r.rank for r in cold.ranked] == [r.rank for r in warm.ranked]
    assert [(h.repo, h.path, h.line) for h in cold.hits] == [
        (h.repo, h.path, h.line) for h in warm.hits
    ]


def test_a_poisoned_cache_can_mislead_but_not_fabricate(
    org: Workspace, tmp_path: Path
) -> None:
    """A stale or tampered index may waste attention; it may not invent evidence.

    The leads it produces are places to look. Whether anything is *cited* is
    decided later, against the live checkout, with no cache in that path — so
    the worst a poisoned index can do is send the model somewhere useless.
    """
    with Cache(tmp_path / "c" / "o.db") as cache:
        view = org.repo("app")
        cache.put_facts(
            view.name,
            view.head_sha,
            "symbols",
            {
                "version": symbols_mod.INDEX_VERSION,
                "files_indexed": 1,
                "truncated": False,
                "exports": {},
                "imports": {"alpha": [["does/not/exist.ts", 999]]},
            },
        )
        diff = diff_for("src/a.ts", ["export function alpha() {}"], [])
        result = SymbolChannel(cache).rank(pull_request("lib", diff), org)

    # The bogus lead is offered...
    assert any(h.path == "does/not/exist.ts" for h in result.hits)
    # ...but it is only ever a lead: nothing here is a citation, and the file it
    # names does not exist, which host validation is what catches.
    assert not (org.repo("app").path / "does/not/exist.ts").exists()


def test_indexing_the_corpus_twice_is_faster_warm(tmp_path: Path) -> None:
    """The measured claim, kept honest by comparing work rather than wall clock
    alone: the warm pass must both be quicker and rebuild nothing."""
    from panorama.fixtures import bootstrap as bootstrap_fixtures

    root = tmp_path / "demo-org"
    bootstrap_fixtures(dest_root=root)
    workspace = Workspace(root)

    with Cache(tmp_path / "c" / "o.db") as cache:
        cold_indexer = SymbolIndexer(cache)
        started = time.perf_counter()
        for view in workspace.repos():
            cold_indexer.index_for(view)
        cold = time.perf_counter() - started

        warm_indexer = SymbolIndexer(cache)
        started = time.perf_counter()
        for view in workspace.repos():
            warm_indexer.index_for(view)
        warm = time.perf_counter() - started

    assert cold_indexer.misses == len(workspace.repos())
    assert warm_indexer.hits == len(workspace.repos())
    assert warm_indexer.misses == 0
    assert warm < cold
