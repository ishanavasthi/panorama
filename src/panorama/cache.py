"""A local, derived-facts cache keyed by repository and commit.

V2 adds persistent state for the first time, and one constraint governs all of
it: **the cache can never make a review wrong.** It may only influence *which*
repositories and files get looked at. Every citation in a finished review is
still validated against the live checkout at the reviewed SHA, on every run,
with no cache anywhere in that path. A stale or corrupt cache must be able to
produce a *worse* review, never an *unsupported* one.

Three design choices follow directly from that, and each is enforced by tests
rather than by discipline:

**Keyed by ``(repo, head_sha)``.** Nothing is stored against a repository alone.
A commit that changes anything changes the SHA, so a lookup at the new SHA
simply misses and the fact is recomputed. Invalidation is not a policy that
could be wrong — it is the shape of the key. There is deliberately no "close
enough" lookup and no fallback to the newest entry for a repository.

**A schema version with a drop-and-rebuild migration.** Everything here is
derived from repository content, so throwing it away is always a legal repair.
That makes migration a non-problem: on a version mismatch the tables are dropped
and recreated, and the next run pays to recompute. Writing incremental migrations
for a cache you are allowed to delete is work that buys nothing and can itself
be buggy.

**Corruption is recovered from, not raised.** A database that will not open is
deleted and rebuilt. The alternative — failing the review — would mean a
scribbled-on cache file could stop Panorama working, which is a worse failure
than doing the work again.

Stdlib ``sqlite3``, so this adds no dependency. The file lives at
``~/.panorama/cache/<owner>.db`` with mode 0600 inside a 0700 directory: it holds
derived facts about private repositories — symbol names, file paths, dependency
edges — and those are not for other local users to read.

One consequence worth stating rather than discovering. Later milestones store
things here that are *not* derived from repository content: watch cursors
(V2.9) and finding suppressions (V2.10). A schema rebuild drops those too. For
cursors that means re-reviewing a pull request; for suppressions it means a
dismissed finding can come back. Both are recoverable and neither can make a
review *wrong* — a returning finding is still fully validated, and constraint #8
means suppression can only ever subtract. ``panorama suppressions list`` exists
so the state is visible instead of mysterious.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from panorama.config import CACHE_ROOT, ensure_dir
from panorama.errors import PanoramaError

#: Bump when the table layout changes. Any mismatch drops and rebuilds, so this
#: never needs to be a sequence anyone migrates *through* — only a value that
#: differs from what is on disk.
SCHEMA_VERSION = 3

#: Owner read/write only. The cache describes private repository content.
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600

#: How long a writer waits for another process's lock before giving up. Two
#: Panorama runs on one machine is normal (a watcher plus a manual review), and
#: the loser of a race should wait rather than fail the review it is doing.
_BUSY_TIMEOUT_SECONDS = 10.0

#: Owner names become filenames, so they are constrained to something that
#: cannot escape the cache directory or collide across cases.
_SAFE_OWNER = re.compile(r"[^A-Za-z0-9._-]+")


class CacheError(PanoramaError):
    """Raised only for a cache problem that is *not* safely recoverable —
    an unwritable directory, say. A corrupt or outdated database is rebuilt
    rather than raised, because rebuilding is always correct here."""


@dataclass(frozen=True)
class CacheStats:
    """What ``panorama status`` reports about the cache."""

    path: Path
    exists: bool
    size_bytes: int
    schema_version: int | None
    n_facts: int
    repos: tuple[str, ...]


def owner_db_path(owner: str, root: Path | None = None) -> Path:
    """Where one owner's cache lives.

    The owner name is sanitised because it comes from a command line and ends
    up as a filename. ``../../etc/passwd`` is not a GitHub owner, and it is not
    going to become a path here either.
    """
    safe = _SAFE_OWNER.sub("-", owner).strip("-.") or "default"
    return Path(root or CACHE_ROOT) / f"{safe}.db"


class Cache:
    """Derived facts about repositories, keyed by repository and commit.

    Open with :meth:`for_owner` in production. The constructor takes an explicit
    path so tests never touch the real home directory.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.rebuilt = False
        """True when opening had to discard an existing database — a version
        mismatch or a corrupt file. Surfaced so a run can *say* it started cold
        rather than leaving someone wondering why it was slow."""
        self._connect()

    @classmethod
    def for_owner(cls, owner: str, root: Path | None = None) -> Cache:
        return cls(owner_db_path(owner, root))

    # -- lifecycle ----------------------------------------------------------

    def _connect(self) -> None:
        try:
            ensure_dir(self.path.parent)
        except OSError as exc:
            raise CacheError(f"cannot create cache directory {self.path.parent}: {exc}") from exc

        try:
            self._open()
        except sqlite3.DatabaseError:
            # Not a database, truncated, or scribbled on. Deleting and starting
            # again is always a legal repair for derived data, and failing the
            # review instead would let a damaged file block real work.
            self._discard()
            self._open()

        if self._version_on_disk != SCHEMA_VERSION:
            self._rebuild()

    def _open(self) -> None:
        self._conn = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,  # explicit transactions; see _write()
        )
        self._conn.row_factory = sqlite3.Row
        self._harden_permissions()
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Force a read of the header so a corrupt file fails *here*, inside the
        # recovery path, rather than on the first unrelated query later.
        self._conn.execute("PRAGMA schema_version").fetchone()
        # Must happen before any table is created — see _existing_version.
        self._version_on_disk = self._existing_version()
        self._create_tables()

    def _existing_version(self) -> int | None:
        """The schema version already on disk, read *before* setup runs.

        Reading it afterwards would be self-fulfilling: creating the tables
        stamps the current version, so a database whose version row had gone
        missing would present as current and keep its incompatible rows. A
        genuinely empty file reports the current version, because there is
        nothing there to be incompatible.
        """
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables:
            return SCHEMA_VERSION  # fresh file, nothing to migrate
        if "meta" not in tables:
            return None  # tables but no version: not ours, or damaged
        return self._stored_version()

    def _harden_permissions(self) -> None:
        """0600 on the database file, restated on every open.

        Restated rather than set once because the file may have been created by
        an older version, restored from a backup, or copied with a permissive
        umask. It costs one syscall and removes a whole class of "it was fine
        when I made it" reasoning.
        """
        try:
            os.chmod(self.path, _FILE_MODE)
        except OSError:  # pragma: no cover - platform-dependent
            pass

    def _discard(self) -> None:
        try:
            self._conn.close()
        except (AttributeError, sqlite3.Error):
            pass
        self.path.unlink(missing_ok=True)
        # SQLite's sidecar files belong to the database that just went away.
        for suffix in ("-wal", "-shm", "-journal"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
        self.rebuilt = True

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- One row per (repository, commit, kind of fact). The primary key
            -- IS the invalidation rule: there is no way to ask for a fact
            -- without naming the exact commit it was derived from.
            CREATE TABLE IF NOT EXISTS repo_facts (
                repo       TEXT NOT NULL,
                head_sha   TEXT NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (repo, head_sha, kind)
            );

            CREATE INDEX IF NOT EXISTS repo_facts_by_repo ON repo_facts (repo);

            -- Where the watcher got to. One row per pull request, holding the
            -- head commit it was last reviewed at, so a restart resumes instead
            -- of re-reviewing everything it has already seen.
            CREATE TABLE IF NOT EXISTS watch_cursors (
                repo        TEXT    NOT NULL,
                number      INTEGER NOT NULL,
                head_sha    TEXT    NOT NULL,
                reviewed_at TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (repo, number)
            );

            -- Every review the watcher has performed, for the hourly cap. Kept
            -- on disk rather than in memory so a crash-looping watcher cannot
            -- reset its own budget by restarting.
            CREATE TABLE IF NOT EXISTS watch_reviews (
                repo        TEXT    NOT NULL,
                number      INTEGER NOT NULL,
                head_sha    TEXT    NOT NULL,
                reviewed_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS watch_reviews_by_time
                ON watch_reviews (reviewed_at);

            -- Findings a human has dismissed. The only state here that is not
            -- derived from repository content, and the only one whose loss a
            -- reader would notice: a rebuild makes a dismissed finding come
            -- back. That is recoverable and visible, and it can never make a
            -- review *wrong* — a returning finding is still fully validated.
            CREATE TABLE IF NOT EXISTS suppressions (
                fingerprint TEXT PRIMARY KEY,
                title       TEXT NOT NULL DEFAULT '',
                source      TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def _stored_version(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return None

    def _rebuild(self) -> None:
        """Drop everything and recreate at the current schema version."""
        self._conn.executescript(
            "DROP TABLE IF EXISTS repo_facts;"
            "DROP TABLE IF EXISTS watch_cursors;"
            "DROP TABLE IF EXISTS watch_reviews;"
            "DROP TABLE IF EXISTS suppressions;"
            "DROP TABLE IF EXISTS meta;"
        )
        self._create_tables()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.rebuilt = True

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self):
        """One immediate transaction, so a concurrent writer waits rather than
        reading a half-applied change."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- derived facts ------------------------------------------------------

    def get_facts(self, repo: str, head_sha: str, kind: str) -> dict | None:
        """Facts of ``kind`` for ``repo`` **at exactly ``head_sha``**.

        A miss returns ``None`` and the caller recomputes. There is deliberately
        no nearest-SHA or latest-entry fallback: that is precisely the shortcut
        that would let yesterday's symbol index describe today's code.
        """
        row = self._conn.execute(
            "SELECT payload FROM repo_facts WHERE repo = ? AND head_sha = ? AND kind = ?",
            (repo, head_sha, kind),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            # A row we cannot read is a row we do not have. Same outcome as a
            # miss: recompute, and let the write below overwrite it.
            return None
        return payload if isinstance(payload, dict) else None

    def put_facts(self, repo: str, head_sha: str, kind: str, payload: dict) -> None:
        """Record facts derived from ``repo`` at ``head_sha``."""
        blob = json.dumps(payload, sort_keys=True)
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO repo_facts (repo, head_sha, kind, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (repo, head_sha, kind)
                DO UPDATE SET payload = excluded.payload,
                              created_at = datetime('now')
                """,
                (repo, head_sha, kind, blob),
            )

    def known_shas(self, repo: str) -> tuple[str, ...]:
        """Commits this cache holds facts for, newest first.

        Only for *selection*, which asks the loose question "has this repository
        ever looked relevant" before anything has been cloned. Nothing that
        informs a finding may use this: `get_facts` and its exact-commit key
        remain the only way to read a fact that reaches a review.
        """
        return tuple(
            row["head_sha"]
            for row in self._conn.execute(
                "SELECT DISTINCT head_sha FROM repo_facts WHERE repo = ? "
                "ORDER BY created_at DESC",
                (repo,),
            )
        )

    def prune_repo(self, repo: str, keep_sha: str) -> int:
        """Drop every stored commit for ``repo`` except ``keep_sha``.

        Without this the cache grows one generation per commit forever. Called
        after a repository's facts are refreshed, so the cost stays proportional
        to the size of the organisation rather than to its history.
        """
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM repo_facts WHERE repo = ? AND head_sha != ?",
                (repo, keep_sha),
            )
            return cursor.rowcount or 0

    # -- watch state --------------------------------------------------------

    def get_cursor(self, repo: str, number: int) -> str | None:
        """The head commit this pull request was last reviewed at."""
        row = self._conn.execute(
            "SELECT head_sha FROM watch_cursors WHERE repo = ? AND number = ?",
            (repo, number),
        ).fetchone()
        return row["head_sha"] if row else None

    def set_cursor(self, repo: str, number: int, head_sha: str) -> None:
        """Record that this pull request has been reviewed at ``head_sha``.

        Also appends to the review log, which is what the hourly cap counts.
        Both happen in one transaction: a cursor advanced without a logged
        review would let a restart-loop review forever inside its budget.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO watch_cursors (repo, number, head_sha)
                VALUES (?, ?, ?)
                ON CONFLICT (repo, number)
                DO UPDATE SET head_sha = excluded.head_sha,
                              reviewed_at = datetime('now')
                """,
                (repo, number, head_sha),
            )
            conn.execute(
                "INSERT INTO watch_reviews (repo, number, head_sha) VALUES (?, ?, ?)",
                (repo, number, head_sha),
            )

    def reviews_since(self, seconds: int) -> int:
        """How many reviews the watcher has run in the last ``seconds``."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM watch_reviews "
            "WHERE reviewed_at > datetime('now', ?)",
            (f"-{int(seconds)} seconds",),
        ).fetchone()
        return int(row["n"])

    def cursors(self) -> tuple[tuple[str, int, str], ...]:
        """Every cursor, for `panorama status`."""
        return tuple(
            (row["repo"], row["number"], row["head_sha"])
            for row in self._conn.execute(
                "SELECT repo, number, head_sha FROM watch_cursors "
                "ORDER BY repo, number"
            )
        )

    # -- suppressions -------------------------------------------------------

    def suppress(self, fingerprint: str, *, title: str = "", source: str = "") -> None:
        """Remember that a human dismissed this finding.

        ``title`` and ``source`` are stored so `panorama suppressions list`
        shows something a person recognises. A suppression nobody can read is a
        suppression nobody can undo.
        """
        with self._write() as conn:
            conn.execute(
                """
                INSERT INTO suppressions (fingerprint, title, source)
                VALUES (?, ?, ?)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    title = excluded.title,
                    source = excluded.source
                """,
                (fingerprint, title, source),
            )

    def is_suppressed(self, fingerprint: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM suppressions WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def suppressions(self) -> tuple[tuple[str, str, str, str], ...]:
        """Every suppression as ``(fingerprint, title, source, created_at)``."""
        return tuple(
            (row["fingerprint"], row["title"], row["source"], row["created_at"])
            for row in self._conn.execute(
                "SELECT fingerprint, title, source, created_at FROM suppressions "
                "ORDER BY created_at DESC, fingerprint"
            )
        )

    def clear_suppressions(self) -> int:
        with self._write() as conn:
            return conn.execute("DELETE FROM suppressions").rowcount or 0

    # -- operations ---------------------------------------------------------

    def clear(self) -> None:
        """Empty the cache without deleting the file. Backs `panorama cache clear`."""
        with self._write() as conn:
            conn.execute("DELETE FROM repo_facts")

    def stats(self) -> CacheStats:
        n_facts = self._conn.execute("SELECT COUNT(*) AS n FROM repo_facts").fetchone()["n"]
        repos = tuple(
            row["repo"]
            for row in self._conn.execute(
                "SELECT DISTINCT repo FROM repo_facts ORDER BY repo"
            )
        )
        return CacheStats(
            path=self.path,
            exists=self.path.is_file(),
            size_bytes=self.path.stat().st_size if self.path.is_file() else 0,
            schema_version=self._stored_version(),
            n_facts=n_facts,
            repos=repos,
        )
