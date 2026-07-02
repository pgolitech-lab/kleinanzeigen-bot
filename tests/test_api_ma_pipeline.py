"""TestClient тесты для GET /api/ma/pipeline."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**overrides):
    """Helper: создать MagicMock что ведёт себя как sqlite3.Row для конкретных колонок."""
    defaults = {
        "id": 1,
        "gmail_thread_id": "thread_abc",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://www.kleinanzeigen.de/s-anzeige/x/123",
        "buyer_display_name": "Osman",
        "deal_brief_json": '{"summary_ru":"торгуется"}',
        "last_event_at": "2026-05-10T10:00:00",
        "last_event_kind": "in",
        "pending_drafts_count": 1,
        "any_sent_count": 0,
        "real_sent_count": 0,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__.side_effect = defaults.__getitem__
    row.keys.return_value = defaults.keys()
    return row


@pytest.fixture
def client():
    """TestClient + замоканные config + db."""
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        # Default: pipeline_threads пустой; per-test переопределяет.
        mdb.pipeline_threads.return_value = []
        mdb.list_accounts.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_message.return_value = None
        from web.app import app
        yield TestClient(app), mdb


def test_pipeline_empty_returns_empty_sections(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body == {"pinned": [], "red": [], "green": [], "accounts": []}


def test_pipeline_splits_by_last_event_kind(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row(id=1, gmail_thread_id="t1", last_event_kind="in", last_event_at="2026-05-10T10:00:00"),
        _row(id=2, gmail_thread_id="t2", last_event_kind="out", last_event_at="2026-05-10T11:00:00"),
        _row(id=3, gmail_thread_id="t3", last_event_kind="in", last_event_at="2026-05-10T09:00:00"),
    ]
    mdb.get_message.return_value = MagicMock()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert len(body["red"]) == 2
    assert len(body["green"]) == 1
    # Сортировка DESC внутри красной — t1 (10:00) перед t3 (09:00)
    assert body["red"][0]["thread_id"] == "t1"
    assert body["red"][1]["thread_id"] == "t3"
    assert body["green"][0]["thread_id"] == "t2"


def test_pipeline_serialises_required_fields(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row(id=42, gmail_thread_id="abc", ad_title="Test", ad_price="1000€",
             buyer_display_name="Jane", pending_drafts_count=2),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    item = res.json()["red"][0]
    assert item["thread_id"] == "abc"
    assert item["msg_id"] == 42
    assert item["ad_title"] == "Test"
    assert item["ad_price"] == "1000€"
    assert item["buyer_display_name"] == "Jane"
    assert item["pending_drafts_count"] == 2
    assert item["last_event_at"] == "2026-05-10T10:00:00"
    assert item["last_event_kind"] == "in"
    assert item["is_autopilot"] is False


def test_pipeline_autopilot_flag_true_when_row_active(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [_row(gmail_thread_id="ap_thread")]
    autopilot_row = MagicMock()
    autopilot_row.__getitem__.side_effect = {"active": 1}.__getitem__
    autopilot_row.keys.return_value = ["active"]
    mdb.get_thread_autopilot.return_value = autopilot_row
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["red"][0]["is_autopilot"] is True


def test_pipeline_requires_auth(client):
    c, mdb = client
    res = c.get("/api/ma/pipeline")  # no header
    assert res.status_code == 422


def test_pipeline_rejects_bad_hash(client):
    c, mdb = client
    res = c.get(
        "/api/ma/pipeline",
        headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=99&hash=fake"},
    )
    assert res.status_code == 401


def _row_with_flags(**overrides):
    """Как _row(), но включает is_pinned и operator_unread."""
    defaults = {
        "id": 1,
        "gmail_thread_id": "thread_abc",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": None,
        "buyer_display_name": "Osman",
        "deal_brief_json": None,
        "last_event_at": "2026-05-10T10:00:00",
        "last_event_kind": "in",
        "pending_drafts_count": 0,
        "any_sent_count": 1,
        "real_sent_count": 1,
        "is_pinned": 0,
        "operator_unread": 0,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__.side_effect = defaults.__getitem__
    row.keys.return_value = list(defaults.keys())
    return row


def test_pipeline_includes_is_pinned_and_operator_unread(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row_with_flags(gmail_thread_id="t1", is_pinned=0, operator_unread=1),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    item = res.json()["red"][0]
    assert item["is_pinned"] is False
    assert item["operator_unread"] is True


def test_pipeline_pinned_thread_goes_to_pinned_section(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row_with_flags(gmail_thread_id="t1", is_pinned=1, last_event_kind="in"),
        _row_with_flags(gmail_thread_id="t2", is_pinned=0, last_event_kind="in"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    body = res.json()
    assert "pinned" in body
    assert len(body["pinned"]) == 1
    assert body["pinned"][0]["thread_id"] == "t1"
    assert len(body["red"]) == 1
    assert body["red"][0]["thread_id"] == "t2"
