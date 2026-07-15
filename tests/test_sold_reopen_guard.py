"""A new email into a SOLD thread must not auto-reopen it (Bug 6)."""
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


def test_close_reason_none_when_open(tmp_db):
    assert tmp_db.thread_close_reason("nope") is None


def test_close_reason_returns_closed_by(tmp_db):
    tmp_db.close_thread("t-sold", closed_by="sold")
    assert tmp_db.thread_close_reason("t-sold") == "sold"


def test_should_not_reopen_sold(tmp_db):
    tmp_db.close_thread("t-sold", closed_by="sold")
    assert tmp_db.should_reopen_closed_thread("t-sold") is False


def test_should_not_reopen_detected_sale(tmp_db):
    tmp_db.close_thread("t-det", closed_by="detected-sale")
    assert tmp_db.should_reopen_closed_thread("t-det") is False


def test_should_reopen_operator_closed(tmp_db):
    tmp_db.close_thread("t-op", closed_by="@user#1")
    assert tmp_db.should_reopen_closed_thread("t-op") is True


def test_should_not_reopen_when_not_closed(tmp_db):
    assert tmp_db.should_reopen_closed_thread("t-open") is False
