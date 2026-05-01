"""Persistent state for incremental sync — tracks handled books in a SQLite database."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

from .models import Book

log = logging.getLogger(__name__)

REASON_IN_LIBRARY = "in_library"
REASON_SUBMITTED = "submitted"


class StateManager:
    """Persists handled-book state across sync runs in a SQLite database.

    Thread-safety: NOT thread-safe. The process is single-threaded so no
    locking is needed.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._dirty = False
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._setup_schema()
        log.info(
            "State loaded from %s (%d previously handled books)",
            db_path,
            self._count(),
        )

    # ---------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------

    def is_handled(self, book: Book) -> bool:
        """Return True if this book was handled in a prior run."""
        row = self._conn.execute(
            "SELECT 1 FROM handled_books WHERE key = ?",
            (book.normalized_key(),),
        ).fetchone()
        return row is not None

    def mark_handled(self, book: Book, reason: str) -> None:
        """Record book as handled. Deferred — call save() to commit."""
        self._conn.execute(
            "INSERT OR REPLACE INTO handled_books (key, reason, handled_at) VALUES (?, ?, ?)",
            (book.normalized_key(), reason, datetime.now().isoformat(timespec="seconds")),
        )
        self._dirty = True

    def save(self) -> None:
        """Commit pending marks to disk. No-op if nothing was marked this pass."""
        if not self._dirty:
            return
        try:
            self._conn.commit()
            self._dirty = False
            log.debug("State saved (%d total handled books)", self._count())
        except sqlite3.Error as exc:
            log.error("Failed to save state to %s: %s", self._path, exc)

    def get_last_full_sync(self) -> datetime | None:
        """Return the timestamp of the last full verification sync, or None."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'last_full_sync_at'",
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def set_last_full_sync(self) -> None:
        """Record the current time as the last full sync timestamp."""
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_full_sync_at', ?)",
                (datetime.now().isoformat(timespec="seconds"),),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.error("Failed to record full sync timestamp: %s", exc)

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def resolve_path() -> str:
        """Resolve state DB path: STATE_FILE env > /data/state.db > ./state.db."""
        env_val = os.environ.get("STATE_FILE", "").strip()
        if env_val:
            return env_val
        if os.path.isdir("/data"):
            return "/data/state.db"
        return "./state.db"

    # ---------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------

    def _setup_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS handled_books (
                key        TEXT PRIMARY KEY,
                reason     TEXT NOT NULL,
                handled_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def _count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM handled_books").fetchone()[0]
