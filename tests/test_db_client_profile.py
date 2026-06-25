"""Unit-тесты для client_profiles: get/upsert."""
from __future__ import annotations
import json
import pytest
import database
import modules.db_threads as db_threads


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    return db_path


def test_get_client_profile_returns_none_when_missing(tmp_db):
    result = db_threads.get_client_profile("nobody@example.com")
    assert result is None


def test_upsert_creates_profile(tmp_db):
    db_threads.upsert_client_profile("buyer@test.com", ["Серьёзный"], "хороший клиент")
    row = db_threads.get_client_profile("buyer@test.com")
    assert row is not None
    assert json.loads(row["tags_json"]) == ["Серьёзный"]
    assert row["note"] == "хороший клиент"


def test_upsert_updates_existing_profile(tmp_db):
    db_threads.upsert_client_profile("buyer@test.com", ["Серьёзный"], "первая заметка")
    db_threads.upsert_client_profile("buyer@test.com", ["Торгуется", "Мошенник"], "вторая")
    row = db_threads.get_client_profile("buyer@test.com")
    assert json.loads(row["tags_json"]) == ["Торгуется", "Мошенник"]
    assert row["note"] == "вторая"


def test_upsert_empty_tags_and_note(tmp_db):
    db_threads.upsert_client_profile("x@y.com", [], "")
    row = db_threads.get_client_profile("x@y.com")
    assert json.loads(row["tags_json"]) == []
    assert row["note"] == ""
