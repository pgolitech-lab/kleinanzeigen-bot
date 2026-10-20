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


def _stock(ad_id: str, qty: int) -> int:
    """Товар на складе с учётом остатка: приход qty шт., объявление ad_id привязано."""
    from modules import db_inventory as inv
    pid = inv.create_product(title=f"Товар {ad_id}")
    inv.upsert_listing(ad_id=ad_id, product_id=pid, link_method="operator")
    inv.add_movement(product_id=pid, kind="receipt", qty=qty, occurred_at="2020-01-01T00:00:00")
    return pid


def test_mark_thread_sold_close_other_threads_for_ad(tmp_db):
    """Последняя штука продана (склад: out_of_stock) → другие треды по ad свёрнуты."""
    _ensure_account(tmp_db)
    _stock("ad-1", 1)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-A", ad_id="ad-1")
    m_other_thread = _insert_msg(tmp_db, direction="in", status="pending",
                                  gmail_thread_id="thread-B", ad_id="ad-1")
    m_unrelated_ad = _insert_msg(tmp_db, direction="in", status="pending",
                                  gmail_thread_id="thread-C", ad_id="ad-2")

    result = tmp_db.mark_thread_sold(
        m_main, sold_price_eur=999.0, close_other_threads_for_ad=True,
    )
    assert result["stock"]["qty_after"] == 0 and result["stock"]["stock_tracked"] is True
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


def test_mark_thread_sold_closes_thread_for_pipeline(tmp_db):
    """Sold-тред должен исчезать из pipeline (через closed_threads)."""
    _ensure_account(tmp_db)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-X", ad_id="ad-9")
    assert tmp_db.is_thread_closed("thread-X") is False
    tmp_db.mark_thread_sold(mid, sold_price_eur=500.0)
    assert tmp_db.is_thread_closed("thread-X") is True


def test_mark_thread_sold_closes_other_threads_when_flag_set(tmp_db):
    _ensure_account(tmp_db)
    _stock("ad-1", 1)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-A", ad_id="ad-1")
    _insert_msg(tmp_db, direction="in", status="pending",
                gmail_thread_id="thread-B", ad_id="ad-1")
    tmp_db.mark_thread_sold(m_main, sold_price_eur=200.0,
                             close_other_threads_for_ad=True)
    assert tmp_db.is_thread_closed("thread-A") is True
    assert tmp_db.is_thread_closed("thread-B") is True


def test_close_other_threads_skipped_while_in_stock(tmp_db):
    """Товар ещё есть (2 шт. → 1) — другие покупатели не сворачиваются."""
    _ensure_account(tmp_db)
    _stock("ad-1", 2)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                         gmail_thread_id="thread-A", ad_id="ad-1")
    m_other = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-B", ad_id="ad-1")
    res = tmp_db.mark_thread_sold(m_main, sold_price_eur=10.0, close_other_threads_for_ad=True)
    assert res["closed_other_threads"] == [] and res["stock"]["qty_after"] == 1
    assert tmp_db.get_message(m_other)["status"] == "pending"
    assert tmp_db.is_thread_closed("thread-B") is False


def test_close_other_threads_skipped_when_stock_unknown(tmp_db):
    """Объявления нет на складе — остаток неизвестен, другие треды не трогаем."""
    _ensure_account(tmp_db)
    m_main = _insert_msg(tmp_db, direction="in", status="pending",
                         gmail_thread_id="thread-A", ad_id="ad-1")
    m_other = _insert_msg(tmp_db, direction="in", status="pending",
                          gmail_thread_id="thread-B", ad_id="ad-1")
    res = tmp_db.mark_thread_sold(m_main, sold_price_eur=10.0, close_other_threads_for_ad=True)
    assert res["closed_other_threads"] == [] and res["stock"] is None
    assert tmp_db.get_message(m_other)["status"] == "pending"


def test_stock_failure_does_not_roll_back_sale(tmp_db, monkeypatch):
    _ensure_account(tmp_db)
    from modules import inventory

    def boom(**kw):
        raise RuntimeError("склад упал")
    monkeypatch.setattr(inventory, "record_sale", boom)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    res = tmp_db.mark_thread_sold(mid, sold_price_eur=10.0)
    assert res["stock"] is None
    assert tmp_db.get_message(mid)["status"] == "skipped_sold"
    assert tmp_db.is_thread_closed("thread-A") is True


def test_sale_recorded_in_stock_once(tmp_db):
    _ensure_account(tmp_db)
    from modules import db_inventory as inv
    pid = _stock("ad-1", 3)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    tmp_db.mark_thread_sold(mid, sold_price_eur=10.0)
    tmp_db.mark_thread_sold(mid, sold_price_eur=10.0)  # повтор — не списывает второй раз
    assert inv.stock_qty(pid) == 2
    assert inv.sale_for_thread("thread-A")["source"] == "thread"


def test_sale_on_answered_thread_keeps_sent_status(tmp_db):
    """Тред уже отвечен: наш ответ остаётся sent, продажа — отдельной строкой того же треда."""
    _ensure_account(tmp_db)
    from modules import db_inventory as inv, inventory
    pid = _stock("ad-1", 3)
    mid = _insert_msg(tmp_db, direction="in", status="sent",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    with tmp_db.get_conn() as conn:
        conn.execute("UPDATE messages SET de_client='Hallo', de_answer='Ja', sent_at=? WHERE id=?",
                     (datetime.utcnow().isoformat(), mid))
    res = tmp_db.add_thread_sale("thread-A", 50.0, note="забрал сам")
    assert tmp_db.get_message(mid)["status"] == "sent"
    sale = tmp_db.get_message(res["sale_msg_id"])
    assert sale["id"] != mid and sale["status"] == "skipped_sold" and sale["sold_price_eur"] == 50.0
    assert sale["gmail_thread_id"] == "thread-A" and sale["ad_id"] == "ad-1"
    assert sale["direction"] == "out" and not sale["de_answer"]
    # «Последнее письмо клиента» по-прежнему настоящее входящее
    assert tmp_db.add_thread_sale("thread-A", 50.0)["sale_msg_id"] == res["sale_msg_id"]
    assert "забрал сам" in sale["extra_notes"]
    # Повтор (поправили цену) — та же строка, склад не списывает второй раз
    res2 = tmp_db.add_thread_sale("thread-A", 55.0)
    assert res2["sale_msg_id"] == res["sale_msg_id"]
    assert inv.stock_qty(pid) == 2
    sales = tmp_db.list_sales()
    assert len(sales) == 1 and sales[0]["sold_price_eur"] == 55.0 and sales[0]["source"] == "thread"
    # sync склада видит ту же продажу — без второго списания и без void
    inventory.sync_sales()
    assert inv.stock_qty(pid) == 2
    # В ленте треда строки продажи нет, наш ответ остаётся
    kinds = [e["kind"] for e in tmp_db.thread_events("thread-A")]
    assert kinds == ["in", "out"]


def test_add_thread_sale_on_draft_marks_row(tmp_db):
    _ensure_account(tmp_db)
    mid = _insert_msg(tmp_db, direction="in", status="pending",
                      gmail_thread_id="thread-A", ad_id="ad-1")
    res = tmp_db.add_thread_sale("thread-A", 20.0)
    assert res["sale_msg_id"] == mid and tmp_db.get_message(mid)["status"] == "skipped_sold"


def test_add_thread_sale_unknown_thread(tmp_db):
    with pytest.raises(ValueError):
        tmp_db.add_thread_sale("nope", 10.0)
