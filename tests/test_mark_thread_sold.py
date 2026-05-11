"""Unit tests for database.mark_thread_sold.

Uses a temp SQLite DB so we can verify multi-row UPDATE behavior end-to-end.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Re-import database with DB_PATH pointed to a fresh tmp file."""
    db_file = tmp_path / "test.db"
    # Patch DB_PATH BEFORE init_db runs
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _insert_msg(db, *, direction, status, gmail_thread_id, ad_id,
                account_id=1, sold_price=None):
    now = datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, "
            "gmail_thread_id, ad_id, created_at, sold_price_eur) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, direction, status, gmail_thread_id, ad_id, now, sold_price),
        )
        return cur.lastrowid


def _ensure_account(db):
    with db.get_conn() as conn:
        existing = conn.execute("SELECT id FROM accounts WHERE id=1").fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'test', 't@x', 'p', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )


def test_mark_thread_sold_basic(tmp_db):
    _ensure_account(tmp_db)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    result = tmp_db.mark_thread_sold(mid, sold_price_eur=1300.0)
    row = tmp_db.get_message(mid)
    assert row["status"] == "skipped_sold"
    assert row["sold_price_eur"] == 1300.0
    assert result["thread_id"] == "thread-A"
    assert result["ad_id"] == "ad-1"
    assert result["closed_other_threads"] == []


def test_mark_thread_sold_closes_thread_pending_rows(tmp_db):
    _ensure_account(tmp_db)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-A", ad_id="ad-1")
    m_other_pending = _insert_msg(tmp_db, direction="in", status="pending",
                                   gmail_thread_id="thread-A", ad_id="ad-1")
    # Out-row in same thread should NOT be touched
    m_out = _insert_msg(tmp_db, direction="out", status="pending",
                        gmail_thread_id="thread-A", ad_id="ad-1")
    tmp_db.mark_thread_sold(m_main, sold_price_eur=500.0)

    assert tmp_db.get_message(m_main)["status"] == "skipped_sold"
    assert tmp_db.get_message(m_other_pending)["status"] == "skipped_sold"
    # Out-row untouched
    assert tmp_db.get_message(m_out)["status"] == "pending"
    # Only main row has price
    assert tmp_db.get_message(m_other_pending)["sold_price_eur"] is None


def test_mark_thread_sold_close_other_threads_for_ad(tmp_db):
    _ensure_account(tmp_db)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-A", ad_id="ad-1")
    m_other_thread = _insert_msg(tmp_db, direction="in", status="pending",
                                  gmail_thread_id="thread-B", ad_id="ad-1")
    m_unrelated_ad = _insert_msg(tmp_db, direction="in", status="pending",
                                  gmail_thread_id="thread-C", ad_id="ad-2")

    result = tmp_db.mark_thread_sold(
        m_main, sold_price_eur=999.0, close_other_threads_for_ad=True,
    )
    assert result["closed_other_threads"] == ["thread-B"]
    assert tmp_db.get_message(m_other_thread)["status"] == "skipped_sold"
    # Different ad — untouched
    assert tmp_db.get_message(m_unrelated_ad)["status"] == "pending"


def test_mark_thread_sold_does_not_touch_other_ads_when_close_off(tmp_db):
    _ensure_account(tmp_db)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-A", ad_id="ad-1")
    m_other = _insert_msg(tmp_db, direction="in", status="pending",
                           gmail_thread_id="thread-B", ad_id="ad-1")
    tmp_db.mark_thread_sold(m_main, sold_price_eur=400.0,
                             close_other_threads_for_ad=False)
    assert tmp_db.get_message(m_other)["status"] == "pending"


def test_mark_thread_sold_ad_not_marked_sold(tmp_db):
    """Объявление НЕ помечается sold (товаров много)."""
    _ensure_account(tmp_db)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    tmp_db.mark_thread_sold(mid, sold_price_eur=800.0,
                             close_other_threads_for_ad=True)
    assert tmp_db.is_ad_sold("ad-1") is False


def test_mark_thread_sold_invalid_msg_id(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        tmp_db.mark_thread_sold(99999, sold_price_eur=100.0)
