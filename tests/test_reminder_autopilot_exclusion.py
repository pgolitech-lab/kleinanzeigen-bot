"""find_reminder_candidates must not offer a follow-up ping on a thread that
autopilot is actively driving (Bug 10)."""
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


def _insert_answered_incoming(db, thread_id):
    """One in-row that already has our sent reply, sent 2 days ago (last event=out)."""
    created = (datetime.utcnow() - timedelta(days=3)).isoformat()
    sent = (datetime.utcnow() - timedelta(days=2)).isoformat()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "de_client, de_answer, created_at, sent_at, reminder_state, is_reminder, "
            "is_auto_ack) VALUES (1, 'in', 'sent', ?, 'Hallo?', 'Antwort.', ?, ?, "
            "'none', 0, 0)",
            (thread_id, created, sent),
        )
        return cur.lastrowid


def _activate_autopilot(db, thread_id):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO thread_autopilot (gmail_thread_id, active, floor_price_eur, "
            "notify_mode, messages_sent, started_at) VALUES (?, 1, 1000, 'silent', 0, ?)",
            (thread_id, datetime.utcnow().isoformat()),
        )


def test_reminder_candidate_without_autopilot(tmp_db):
    _ensure_account(tmp_db)
    _insert_answered_incoming(tmp_db, "thread-R")
    ids = [r["gmail_thread_id"] for r in tmp_db.find_reminder_candidates(after_days=1)]
    assert "thread-R" in ids


def test_reminder_excludes_active_autopilot(tmp_db):
    _ensure_account(tmp_db)
    _insert_answered_incoming(tmp_db, "thread-R")
    _activate_autopilot(tmp_db, "thread-R")
    ids = [r["gmail_thread_id"] for r in tmp_db.find_reminder_candidates(after_days=1)]
    assert "thread-R" not in ids
