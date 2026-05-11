"""Tests for database.list_sales + api_ma /sales endpoint."""
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


def _seed_account(db, account_id=1, name="acc-1"):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (account_id, name, f"{name}@x", "p", datetime.utcnow().isoformat()),
        )


def _seed_sold(db, *, thread, ad_id, ad_title, ad_price, price, sold_at, account_id=1):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "ad_id, ad_title, ad_price, sold_price_eur, sold_at, created_at) "
            "VALUES (?, 'in', 'skipped_sold', ?, ?, ?, ?, ?, ?, ?)",
            (account_id, thread, ad_id, ad_title, ad_price, price, sold_at,
             datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def test_list_sales_empty(tmp_db):
    assert tmp_db.list_sales() == []


def test_list_sales_one_row_per_thread(tmp_db):
    _seed_account(tmp_db)
    # Same thread, two rows — но обе с разными priceами
    now = datetime.utcnow().isoformat()
    _seed_sold(tmp_db, thread="t-1", ad_id="a-1", ad_title="Foo",
               ad_price="500", price=400, sold_at=now)
    _seed_sold(tmp_db, thread="t-1", ad_id="a-1", ad_title="Foo",
               ad_price="500", price=350, sold_at=now)
    out = tmp_db.list_sales()
    assert len(out) == 1
    assert out[0]["sold_price_eur"] == 400  # MAX


def test_list_sales_filters_period(tmp_db):
    _seed_account(tmp_db)
    long_ago = (datetime.utcnow() - timedelta(days=365)).isoformat()
    yesterday = (datetime.utcnow() - timedelta(days=1)).isoformat()
    _seed_sold(tmp_db, thread="old", ad_id="a", ad_title="Old", ad_price="100",
               price=80, sold_at=long_ago)
    _seed_sold(tmp_db, thread="new", ad_id="b", ad_title="New", ad_price="200",
               price=150, sold_at=yesterday)

    week_start = (datetime.utcnow() - timedelta(days=7)).isoformat()
    out = tmp_db.list_sales(period_from=week_start)
    threads = [s["thread_id"] for s in out]
    assert threads == ["new"]


def test_list_sales_filter_account_and_query(tmp_db):
    _seed_account(tmp_db, 1, "acc-A")
    _seed_account(tmp_db, 2, "acc-B")
    now = datetime.utcnow().isoformat()
    _seed_sold(tmp_db, thread="a1", ad_id="x", ad_title="Sofa rot", ad_price="100",
               price=80, sold_at=now, account_id=1)
    _seed_sold(tmp_db, thread="a2", ad_id="y", ad_title="Tisch", ad_price="200",
               price=150, sold_at=now, account_id=2)

    out_a = tmp_db.list_sales(account_id=1)
    assert [s["thread_id"] for s in out_a] == ["a1"]

    out_q = tmp_db.list_sales(query="Tisch")
    assert [s["thread_id"] for s in out_q] == ["a2"]


def test_list_sales_returns_account_name(tmp_db):
    _seed_account(tmp_db, 1, "Magic Account")
    now = datetime.utcnow().isoformat()
    _seed_sold(tmp_db, thread="t", ad_id="a", ad_title="X", ad_price="10",
               price=5, sold_at=now)
    out = tmp_db.list_sales()
    assert out[0]["account_name"] == "Magic Account"


# ──────── price parsing ────────


def test_parse_price_handles_formats():
    from web.api_ma import _parse_listed_price_eur as p
    assert p("1500") == 1500.0
    assert p("1500 €") == 1500.0
    assert p("1.500 €") == 1500.0
    assert p("1,500 €") == 1500.0
    assert p("1.500,50 €") == 1500.5
    assert p("1,500.50 €") == 1500.5
    assert p("99,99 €") == 99.99
    assert p("VB") is None
    assert p("") is None
    assert p(None) is None
    assert p("VB 1500") == 1500.0
    assert p("Auf Anfrage") is None
