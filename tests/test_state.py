"""Tests for src.state.StateManager."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from unittest.mock import patch

from src.models import Book
from src.state import REASON_IMPORTED, REASON_READ_STATUS_SET, REASON_SUBMITTED, StateManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_book(title: str = "Dark Matter", author: str = "Blake Crouch") -> Book:
    return Book(title, author)


def _new_state(tmp_path) -> StateManager:
    return StateManager(str(tmp_path / "state.db"))


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------

def test_resolve_path_uses_env_var():
    with patch.dict(os.environ, {"STATE_FILE": "/custom/state.db"}):
        assert StateManager.resolve_path() == "/custom/state.db"


def test_resolve_path_uses_data_dir_when_exists():
    with patch.dict(os.environ, {}, clear=True), \
         patch("os.path.isdir", return_value=True):
        assert StateManager.resolve_path() == "/data/state.db"


def test_resolve_path_falls_back_to_local():
    with patch.dict(os.environ, {}, clear=True), \
         patch("os.path.isdir", return_value=False):
        assert StateManager.resolve_path() == "./state.db"


def test_resolve_path_env_takes_priority_over_data_dir():
    with patch.dict(os.environ, {"STATE_FILE": "/override/state.db"}), \
         patch("os.path.isdir", return_value=True):
        assert StateManager.resolve_path() == "/override/state.db"


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

def test_schema_created_on_first_open(tmp_path):
    state = _new_state(tmp_path)
    state.close()

    conn = sqlite3.connect(str(tmp_path / "state.db"))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "handled_books" in tables
    assert "meta" in tables


# ---------------------------------------------------------------------------
# is_handled — fresh DB
# ---------------------------------------------------------------------------

def test_is_handled_returns_false_for_new_book(tmp_path):
    state = _new_state(tmp_path)
    assert state.is_handled(_make_book()) is False
    state.close()


# ---------------------------------------------------------------------------
# mark_handled + save + is_handled
# ---------------------------------------------------------------------------

def test_mark_and_save_submitted(tmp_path):
    book = _make_book()
    state = _new_state(tmp_path)
    state.mark_handled(book, REASON_SUBMITTED)
    state.save()
    assert state.is_handled(book) is True
    state.close()


def test_mark_and_save_imported(tmp_path):
    book = _make_book("Project Hail Mary", "Andy Weir")
    state = _new_state(tmp_path)
    state.mark_handled(book, REASON_IMPORTED)
    state.save()
    assert state.is_handled(book) is True
    state.close()


def test_is_handled_false_for_different_book(tmp_path):
    book_a = _make_book("Dark Matter", "Blake Crouch")
    book_b = _make_book("Project Hail Mary", "Andy Weir")
    state = _new_state(tmp_path)
    state.mark_handled(book_a, REASON_SUBMITTED)
    state.save()
    assert state.is_handled(book_b) is False
    state.close()


# ---------------------------------------------------------------------------
# Persistence across reload
# ---------------------------------------------------------------------------

def test_persists_across_reload(tmp_path):
    path = str(tmp_path / "state.db")
    book = _make_book()

    state1 = StateManager(path)
    state1.mark_handled(book, REASON_SUBMITTED)
    state1.save()
    state1.close()

    state2 = StateManager(path)
    assert state2.is_handled(book) is True
    state2.close()


def test_different_book_not_persisted_across_reload(tmp_path):
    path = str(tmp_path / "state.db")
    book_a = _make_book("Dark Matter", "Blake Crouch")
    book_b = _make_book("Project Hail Mary", "Andy Weir")

    state1 = StateManager(path)
    state1.mark_handled(book_a, REASON_SUBMITTED)
    state1.save()
    state1.close()

    state2 = StateManager(path)
    assert state2.is_handled(book_b) is False
    state2.close()


# ---------------------------------------------------------------------------
# INSERT OR REPLACE idempotency
# ---------------------------------------------------------------------------

def test_remark_updates_reason(tmp_path):
    path = str(tmp_path / "state.db")
    book = _make_book()
    state = StateManager(path)
    state.mark_handled(book, REASON_SUBMITTED)
    state.save()

    state.mark_handled(book, REASON_IMPORTED)
    state.save()

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT reason FROM handled_books WHERE key = ?",
        (book.normalized_key(),),
    ).fetchone()
    conn.close()
    assert row[0] == REASON_IMPORTED
    state.close()


# ---------------------------------------------------------------------------
# save() no-op when _dirty=False
# ---------------------------------------------------------------------------

def test_save_noop_when_nothing_marked(tmp_path):
    state = _new_state(tmp_path)
    state.save()  # _dirty is False — should not error
    state.close()

    conn = sqlite3.connect(str(tmp_path / "state.db"))
    count = conn.execute("SELECT COUNT(*) FROM handled_books").fetchone()[0]
    conn.close()
    assert count == 0


def test_save_noop_after_commit(tmp_path):
    """Second save() after a commit should be a no-op (mtime unchanged)."""
    path = str(tmp_path / "state.db")
    book = _make_book()
    state = StateManager(path)
    state.mark_handled(book, REASON_SUBMITTED)
    state.save()  # commits, _dirty → False

    mtime1 = os.path.getmtime(path)
    state.save()  # should be a no-op
    mtime2 = os.path.getmtime(path)
    assert mtime1 == mtime2
    state.close()


# ---------------------------------------------------------------------------
# Multiple books in one run
# ---------------------------------------------------------------------------

def test_multiple_books_saved_and_reloaded(tmp_path):
    path = str(tmp_path / "state.db")
    books = [
        Book("Dark Matter", "Blake Crouch"),
        Book("Project Hail Mary", "Andy Weir"),
        Book("The Martian", "Andy Weir"),
    ]

    state = StateManager(path)
    for book in books:
        state.mark_handled(book, REASON_SUBMITTED)
    state.save()
    state.close()

    state2 = StateManager(path)
    for book in books:
        assert state2.is_handled(book) is True
    state2.close()


# ---------------------------------------------------------------------------
# get_last_full_sync / set_last_full_sync
# ---------------------------------------------------------------------------

def test_get_last_full_sync_none_on_fresh_db(tmp_path):
    state = _new_state(tmp_path)
    assert state.get_last_full_sync() is None
    state.close()


def test_set_and_get_last_full_sync(tmp_path):
    state = _new_state(tmp_path)
    before = datetime.now().replace(microsecond=0)
    state.set_last_full_sync()
    after = datetime.now().replace(microsecond=0)

    result = state.get_last_full_sync()
    assert result is not None
    assert before <= result <= after
    state.close()


def test_last_full_sync_persists_across_reload(tmp_path):
    path = str(tmp_path / "state.db")
    state1 = StateManager(path)
    state1.set_last_full_sync()
    ts1 = state1.get_last_full_sync()
    state1.close()

    state2 = StateManager(path)
    ts2 = state2.get_last_full_sync()
    state2.close()

    assert ts1 == ts2


# ---------------------------------------------------------------------------
# is_imported
# ---------------------------------------------------------------------------

def test_is_imported_true(tmp_path):
    book = _make_book()
    state = _new_state(tmp_path)
    state.mark_handled(book, REASON_IMPORTED)
    state.save()
    assert state.is_imported(book) is True
    state.close()


def test_is_imported_false_for_submitted(tmp_path):
    book = _make_book()
    state = _new_state(tmp_path)
    state.mark_handled(book, REASON_SUBMITTED)
    state.save()
    assert state.is_imported(book) is False
    state.close()


def test_is_imported_false_for_unknown_book(tmp_path):
    state = _new_state(tmp_path)
    assert state.is_imported(_make_book()) is False
    state.close()


# ---------------------------------------------------------------------------
# Migration: "in_library" → "imported"
# ---------------------------------------------------------------------------

def test_migration_renames_in_library_to_imported(tmp_path):
    path = str(tmp_path / "state.db")
    book = _make_book()

    # Manually create DB with legacy "in_library" reason
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE handled_books "
        "(key TEXT PRIMARY KEY, reason TEXT NOT NULL, handled_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO handled_books (key, reason, handled_at) "
        "VALUES (?, 'in_library', '2024-01-01T00:00:00')",
        (book.normalized_key(),),
    )
    conn.commit()
    conn.close()

    # Opening StateManager should migrate the record
    state = StateManager(path)
    assert state.is_imported(book) is True
    assert state.is_handled(book) is True

    # Verify raw DB value was updated
    raw = state._conn.execute(
        "SELECT reason FROM handled_books WHERE key = ?", (book.normalized_key(),)
    ).fetchone()
    assert raw[0] == REASON_IMPORTED
    state.close()


# ---------------------------------------------------------------------------
# REASON_READ_STATUS_SET constant
# ---------------------------------------------------------------------------

def test_read_status_constant():
    assert REASON_READ_STATUS_SET == "read_status_set"


# ---------------------------------------------------------------------------
# is_read_status_set / mark_read_status_set
# ---------------------------------------------------------------------------

def test_is_read_status_set_false_initially(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    assert state.is_read_status_set(book) is False
    state.close()


def test_mark_read_status_set_and_retrieve(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    state.mark_read_status_set(book)
    state.save()
    assert state.is_read_status_set(book) is True
    state.close()


def test_read_status_does_not_affect_is_handled(tmp_path):
    # Marking read status must NOT cause is_handled() to return True
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    state.mark_read_status_set(book)
    state.save()
    assert state.is_handled(book) is False
    state.close()


# ---------------------------------------------------------------------------
# get_last_read_status_sync / set_last_read_status_sync
# ---------------------------------------------------------------------------

def test_get_last_read_status_sync_returns_none_initially(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    assert state.get_last_read_status_sync() is None
    state.close()


def test_set_and_get_last_read_status_sync(tmp_path):
    from datetime import datetime, timedelta
    state = StateManager(str(tmp_path / "state.db"))
    state.set_last_read_status_sync()
    result = state.get_last_read_status_sync()
    assert result is not None
    assert abs((result - datetime.now()).total_seconds()) < 5
    state.close()
