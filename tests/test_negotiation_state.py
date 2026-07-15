"""negotiation_state table + helpers (Track B Increment 2 foundation)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def test_get_missing_returns_none(tmp_db):
    assert tmp_db.get_negotiation_state("nope") is None


def test_upsert_creates_then_updates(tmp_db):
    tmp_db.upsert_negotiation_state("t1", floor_eur=1000.0, list_price_eur=1500.0)
    row = tmp_db.get_negotiation_state("t1")
    assert row is not None
    assert row["floor_eur"] == 1000.0
    assert row["list_price_eur"] == 1500.0
    assert row["phase"] == "opening"
    assert row["mode"] == "manual"
    assert row["updated_at"]

    tmp_db.upsert_negotiation_state("t1", phase="negotiating")
    row2 = tmp_db.get_negotiation_state("t1")
    assert row2["phase"] == "negotiating"
    assert row2["floor_eur"] == 1000.0


def test_upsert_ignores_unknown_fields(tmp_db):
    tmp_db.upsert_negotiation_state("t2", floor_eur=500.0, bogus_col=1)
    assert tmp_db.get_negotiation_state("t2")["floor_eur"] == 500.0


def test_record_our_offer(tmp_db):
    tmp_db.record_our_offer("t3", 1420.0)
    assert tmp_db.get_negotiation_state("t3")["our_last_offer_eur"] == 1420.0
    tmp_db.record_our_offer("t3", 1400.0)
    assert tmp_db.get_negotiation_state("t3")["our_last_offer_eur"] == 1400.0


def test_empty_thread_id_is_noop(tmp_db):
    tmp_db.upsert_negotiation_state("", floor_eur=1.0)
    tmp_db.record_our_offer("", 1.0)
    assert tmp_db.get_negotiation_state("") is None
