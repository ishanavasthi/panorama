"""Tests for the derived-facts cache.

The cache is the first persistent state V2 introduces, and constraint #7 says
it may only influence *which* repositories and files get looked at — never
whether a citation is real. Most of what is asserted here is therefore about
what the cache **refuses to do**: serve a fact for a commit that was not asked
for, survive a schema change, or hand back something it cannot parse.

The rest is ordinary durability: permissions, concurrency, and recovering from
a file somebody scribbled on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from panorama import cache as cache_mod
from panorama.cache import SCHEMA_VERSION, Cache, CacheError, owner_db_path

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "owner.db"


@pytest.fixture
def cache(db: Path) -> Cache:
    with Cache(db) as opened:
        yield opened


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


def test_facts_round_trip(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"exports": ["alpha"], "imports": []})
    assert cache.get_facts("repo", SHA_A, "symbols") == {
        "exports": ["alpha"],
        "imports": [],
    }


def test_a_miss_is_none_not_an_error(cache: Cache) -> None:
    assert cache.get_facts("repo", SHA_A, "symbols") is None


def test_writing_twice_replaces_rather_than_duplicates(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"exports": ["one"]})
    cache.put_facts("repo", SHA_A, "symbols", {"exports": ["two"]})
    assert cache.get_facts("repo", SHA_A, "symbols") == {"exports": ["two"]}
    assert cache.stats().n_facts == 1


def test_kinds_do_not_collide(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"a": 1})
    cache.put_facts("repo", SHA_A, "manifest", {"b": 2})
    assert cache.get_facts("repo", SHA_A, "symbols") == {"a": 1}
    assert cache.get_facts("repo", SHA_A, "manifest") == {"b": 2}


def test_repos_do_not_collide(cache: Cache) -> None:
    cache.put_facts("one", SHA_A, "symbols", {"n": 1})
    cache.put_facts("two", SHA_A, "symbols", {"n": 2})
    assert cache.get_facts("one", SHA_A, "symbols") == {"n": 1}
    assert cache.get_facts("two", SHA_A, "symbols") == {"n": 2}


# ---------------------------------------------------------------------------
# invalidation — the property constraint #7 rests on
# ---------------------------------------------------------------------------


def test_a_different_sha_is_a_miss(cache: Cache) -> None:
    """The whole invalidation story: a new commit simply does not match."""
    cache.put_facts("repo", SHA_A, "symbols", {"exports": ["stale"]})
    assert cache.get_facts("repo", SHA_B, "symbols") is None


def test_there_is_no_latest_entry_fallback(cache: Cache) -> None:
    """The tempting shortcut that would break the guarantee.

    If a lookup at an unknown SHA could fall back to the most recent entry for
    the repository, yesterday's symbol index would describe today's code and the
    cache would start influencing correctness rather than only effort.
    """
    cache.put_facts("repo", SHA_A, "symbols", {"exports": ["old"]})
    cache.put_facts("repo", SHA_B, "symbols", {"exports": ["new"]})
    assert cache.get_facts("repo", "c" * 40, "symbols") is None


def test_pruning_keeps_only_the_named_commit(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"n": 1})
    cache.put_facts("repo", SHA_B, "symbols", {"n": 2})
    cache.put_facts("other", SHA_A, "symbols", {"n": 3})

    removed = cache.prune_repo("repo", keep_sha=SHA_B)

    assert removed == 1
    assert cache.get_facts("repo", SHA_A, "symbols") is None
    assert cache.get_facts("repo", SHA_B, "symbols") == {"n": 2}
    # Pruning one repository must not touch another's history.
    assert cache.get_facts("other", SHA_A, "symbols") == {"n": 3}


def test_an_unreadable_payload_reads_as_a_miss(db: Path) -> None:
    """A corrupt row must degrade to recomputation, never to bad data."""
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"exports": ["fine"]})
        # Corrupt the stored payload behind the cache's back.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE repo_facts SET payload = '{not json'")
        conn.commit()
        conn.close()

    with Cache(db) as reopened:
        assert reopened.get_facts("repo", SHA_A, "symbols") is None
        # And the miss is repairable by writing over it.
        reopened.put_facts("repo", SHA_A, "symbols", {"exports": ["fixed"]})
        assert reopened.get_facts("repo", SHA_A, "symbols") == {"exports": ["fixed"]}


def test_a_payload_that_is_not_an_object_reads_as_a_miss(db: Path) -> None:
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"ok": True})
        conn = sqlite3.connect(db)
        conn.execute("UPDATE repo_facts SET payload = ?", (json.dumps([1, 2, 3]),))
        conn.commit()
        conn.close()

    with Cache(db) as reopened:
        assert reopened.get_facts("repo", SHA_A, "symbols") is None


# ---------------------------------------------------------------------------
# schema version and rebuild
# ---------------------------------------------------------------------------


def test_a_version_mismatch_drops_everything(db: Path, monkeypatch) -> None:
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"exports": ["from-v1"]})
        assert cache.stats().schema_version == SCHEMA_VERSION

    monkeypatch.setattr(cache_mod, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with Cache(db) as upgraded:
        assert upgraded.rebuilt is True
        assert upgraded.get_facts("repo", SHA_A, "symbols") is None
        assert upgraded.stats().schema_version == SCHEMA_VERSION + 1
        # And it is usable immediately, not left in a half-migrated state.
        upgraded.put_facts("repo", SHA_A, "symbols", {"exports": ["from-v2"]})
        assert upgraded.get_facts("repo", SHA_A, "symbols") == {"exports": ["from-v2"]}


def test_a_matching_version_keeps_the_contents(db: Path) -> None:
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"exports": ["kept"]})
    with Cache(db) as reopened:
        assert reopened.rebuilt is False
        assert reopened.get_facts("repo", SHA_A, "symbols") == {"exports": ["kept"]}


def test_a_missing_version_row_forces_a_rebuild(db: Path) -> None:
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"exports": ["orphan"]})

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with Cache(db) as reopened:
        assert reopened.rebuilt is True
        assert reopened.get_facts("repo", SHA_A, "symbols") is None


def test_a_corrupt_database_is_rebuilt_not_raised(db: Path) -> None:
    """A scribbled-on cache file must not be able to stop a review."""
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is emphatically not a sqlite database" * 50)

    with Cache(db) as cache:
        assert cache.rebuilt is True
        cache.put_facts("repo", SHA_A, "symbols", {"exports": ["recovered"]})
        assert cache.get_facts("repo", SHA_A, "symbols") == {"exports": ["recovered"]}


def test_an_empty_file_is_rebuilt(db: Path) -> None:
    """SQLite accepts a zero-byte file as an empty database, so this must not
    be mistaken for corruption *or* for a populated cache."""
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"n": 1})
        assert cache.get_facts("repo", SHA_A, "symbols") == {"n": 1}


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


def test_the_database_is_owner_only(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"n": 1})
    mode = stat.S_IMODE(os.stat(cache.path).st_mode)
    assert mode == 0o600, f"cache file is {oct(mode)}, expected 0600"


def test_the_cache_directory_is_owner_only(cache: Cache) -> None:
    mode = stat.S_IMODE(os.stat(cache.path.parent).st_mode)
    assert mode == 0o700, f"cache directory is {oct(mode)}, expected 0700"


def test_permissions_are_restated_on_reopen(db: Path) -> None:
    """A file created by an older version or restored from a backup gets fixed."""
    with Cache(db):
        pass
    os.chmod(db, 0o666)
    with Cache(db) as reopened:
        assert stat.S_IMODE(os.stat(reopened.path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


def test_two_writers_both_land(db: Path) -> None:
    """A watcher and a manual review on one machine is a normal situation.

    The loser of a write race has to wait and then succeed, not fail the review
    it is in the middle of.
    """
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        try:
            with Cache(db) as cache:
                for i in range(20):
                    cache.put_facts(name, SHA_A, f"kind-{i}", {"n": i})
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"repo-{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with Cache(db) as cache:
        assert cache.stats().n_facts == 40


def test_a_reader_sees_a_committed_write_from_another_connection(db: Path) -> None:
    with Cache(db) as writer:
        writer.put_facts("repo", SHA_A, "symbols", {"n": 1})
        with Cache(db) as reader:
            assert reader.get_facts("repo", SHA_A, "symbols") == {"n": 1}


# ---------------------------------------------------------------------------
# paths and operations
# ---------------------------------------------------------------------------


def test_owner_names_cannot_escape_the_cache_directory(tmp_path: Path) -> None:
    """The owner comes from a command line and becomes a filename."""
    path = owner_db_path("../../etc/passwd", root=tmp_path)
    assert path.parent == tmp_path
    assert ".." not in path.name


def test_owner_names_are_sanitised_but_still_distinct(tmp_path: Path) -> None:
    assert owner_db_path("acme", root=tmp_path) != owner_db_path("other", root=tmp_path)
    assert owner_db_path("acme", root=tmp_path).name == "acme.db"


def test_an_empty_owner_name_still_produces_a_path(tmp_path: Path) -> None:
    assert owner_db_path("...", root=tmp_path).name == "default.db"


def test_clear_empties_without_deleting_the_file(cache: Cache) -> None:
    cache.put_facts("repo", SHA_A, "symbols", {"n": 1})
    cache.clear()
    assert cache.stats().n_facts == 0
    assert cache.path.is_file()
    # Still usable afterwards.
    cache.put_facts("repo", SHA_A, "symbols", {"n": 2})
    assert cache.get_facts("repo", SHA_A, "symbols") == {"n": 2}


def test_stats_describe_what_is_stored(cache: Cache) -> None:
    cache.put_facts("beta", SHA_A, "symbols", {"n": 1})
    cache.put_facts("alpha", SHA_A, "symbols", {"n": 2})
    stats = cache.stats()
    assert stats.exists and stats.size_bytes > 0
    assert stats.n_facts == 2
    assert stats.repos == ("alpha", "beta")
    assert stats.schema_version == SCHEMA_VERSION


def test_an_unwritable_location_raises_rather_than_silently_degrading(
    tmp_path: Path,
) -> None:
    """Recovering from a bad *database* is right; inventing a place to put one
    is not. The user needs to know the cache is not working."""
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        with pytest.raises(CacheError):
            Cache(blocked / "sub" / "owner.db")
    finally:
        blocked.chmod(0o700)


def test_deleting_the_whole_cache_is_always_safe(db: Path) -> None:
    """Constraint #7, stated as a test: the file is a derived artifact."""
    with Cache(db) as cache:
        cache.put_facts("repo", SHA_A, "symbols", {"n": 1})
    db.unlink()
    with Cache(db) as rebuilt:
        assert rebuilt.get_facts("repo", SHA_A, "symbols") is None
        rebuilt.put_facts("repo", SHA_A, "symbols", {"n": 1})
        assert rebuilt.get_facts("repo", SHA_A, "symbols") == {"n": 1}
