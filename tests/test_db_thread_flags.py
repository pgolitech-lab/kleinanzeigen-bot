"""Тесты для thread_flags — set_thread_flags / get_thread_flags."""
from __future__ import annotations
import sqlite3
import pytest
from unittest.mock import patch
from contextlib import contextmanager

import database  # loads database + modules.db_threads via re-exports (correct import order)


def make_in_memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE thread_flags (
            gmail_thread_id TEXT PRIMARY KEY,
            is_pinned       INTEGER NOT NULL DEFAULT 0,
            operator_unread INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


@pytest.fixture
def conn():
    c = make_in_memory_conn()
    yield c
    c.close()


@pytest.fixture
def patched_db(conn):
    @contextmanager
    def fake_get_conn():
        yield conn
    with patch("modules.db_threads.get_conn", fake_get_conn):
        yield conn


def test_set_flags_creates_row(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    row = get_thread_flags("t1")
    assert row is not None
    assert row["is_pinned"] == 1
    assert row["operator_unread"] == 0


def test_set_flags_updates_existing(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    set_thread_flags("t1", operator_unread=1)
    row = get_thread_flags("t1")
    assert row["is_pinned"] == 1
    assert row["operator_unread"] == 1


def test_set_flags_pin_then_unpin(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    set_thread_flags("t1", is_pinned=0)
    row = get_thread_flags("t1")
    assert row["is_pinned"] == 0


def test_get_flags_returns_none_for_unknown(patched_db):
    from modules.db_threads import get_thread_flags
    assert get_thread_flags("nonexistent") is None


def test_set_flags_noop_on_empty_thread_id(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("", is_pinned=1)
    assert get_thread_flags("") is None
