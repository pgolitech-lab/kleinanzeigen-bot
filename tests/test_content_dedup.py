"""Message-ID dedup misses Kleinanzeigen relay re-sends (new Message-ID, same
body). Content-level dedup catches them (Bug 9)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _ensure_account(db):
    with db.get_conn() as conn:
        if conn.execute("SELECT id FROM accounts WHERE id=1").fetchone():
            return
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'test', 't@x', 'p', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )


def _in(db, thread_id, email, body, created=None):
    _ensure_account(db)
    created = created or datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "buyer_name, de_client, created_at) VALUES (1, 'in', 'new', ?, ?, ?, ?)",
            (thread_id, email, body, created),
        )


def test_detects_identical_body_same_thread(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo, ist das noch verfügbar?")
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Hallo,   ist das noch  verfügbar?\n"
    ) is True


def test_different_body_not_duplicate(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo, ist das noch verfügbar?")
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Was ist der letzte Preis?"
    ) is False


def test_other_thread_not_duplicate(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo?")
    assert tmp_db.has_recent_identical_incoming("t2", "buyer@relay.de", "Hallo?") is False


def test_outside_window_not_duplicate(tmp_db):
    old = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo?", created=old)
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Hallo?", within_hours=12
    ) is False


def test_empty_inputs_false(tmp_db):
    assert tmp_db.has_recent_identical_incoming("", "x@y", "body") is False
    assert tmp_db.has_recent_identical_incoming("t1", "", "body") is False
    assert tmp_db.has_recent_identical_incoming("t1", "x@y", "") is False
