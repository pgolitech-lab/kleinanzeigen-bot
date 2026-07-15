"""find_related_inquiries must group by email, not display_name (Bug 8):
two different buyers who happen to share a first name must NOT be merged."""
from __future__ import annotations

from datetime import datetime

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


def _in(db, thread_id, email, display):
    now = datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "buyer_name, buyer_display_name, de_client, created_at) "
            "VALUES (1, 'in', 'sent', ?, ?, ?, 'x', ?)",
            (thread_id, email, display, now),
        )


def test_same_email_is_related(tmp_db):
    _ensure_account(tmp_db)
    _in(tmp_db, "t1", "hans@relay.de", "Hans")
    _in(tmp_db, "t2", "hans@relay.de", "Hans")
    rows = tmp_db.find_related_inquiries("hans@relay.de", exclude_thread_id="t1")
    assert [r["gmail_thread_id"] for r in rows] == ["t2"]


def test_same_name_different_email_not_related(tmp_db):
    _ensure_account(tmp_db)
    _in(tmp_db, "t1", "hans1@relay.de", "Hans")
    _in(tmp_db, "t2", "hans2@relay.de", "Hans")  # namesake, different person
    rows = tmp_db.find_related_inquiries("hans1@relay.de", exclude_thread_id="t1")
    assert rows == []


def test_empty_email_returns_empty(tmp_db):
    assert tmp_db.find_related_inquiries("") == []
