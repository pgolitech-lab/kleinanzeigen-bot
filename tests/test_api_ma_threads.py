"""TestClient тесты для GET /api/ma/threads/{thread_id}."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**fields):
    row = MagicMock()
    row.__getitem__.side_effect = fields.__getitem__
    row.keys.return_value = list(fields.keys())
    return row


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = []
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        from web.app import app
        yield TestClient(app), mdb


def test_thread_not_found_returns_404(client):
    c, mdb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/nonexistent", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_thread_returns_header_events_related(client):
    c, mdb = client
    last_in = _row(
        id=42, gmail_thread_id="t1", direction="in",
        ad_title="Sitzbank", ad_price="1500€",
        ad_url="https://kleinanzeigen.de/x/123",
        buyer_display_name="Osman", buyer_name="osman@x.com",
        account_id=1,
    )
    mdb.thread_history.return_value = [last_in]
    mdb.thread_events.return_value = [
        {"ts": "2026-05-10T10:00:00", "kind": "in", "text": "Hallo",
         "ru_text": "Привет", "is_auto_ack": False,
         "row": _row(id=42, status="pending")},
        {"ts": "2026-05-10T11:00:00", "kind": "out", "text": "MfG",
         "ru_text": None, "is_auto_ack": False,
         "row": _row(id=42, status="sent")},
    ]
    mdb.find_related_inquiries.return_value = [
        _row(gmail_thread_id="t2", ad_title="Other", ad_price="500€",
             sent_at="2026-05-09T10:00:00", created_at="2026-05-09T09:00:00"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/t1", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()

    assert body["header"]["thread_id"] == "t1"
    assert body["header"]["ad_title"] == "Sitzbank"
    assert body["header"]["ad_price"] == "1500€"
    assert body["header"]["buyer_display_name"] == "Osman"
    assert body["header"]["buyer_email"] == "osman@x.com"
    assert body["header"]["account_name"] == "main"
    assert body["header"]["account_email"] == "us@gmail.com"
    assert body["header"]["is_autopilot"] is False

    assert len(body["events"]) == 2
    assert body["events"][0]["kind"] == "in"
    assert body["events"][0]["text"] == "Hallo"
    assert body["events"][0]["ru_text"] == "Привет"
    assert body["events"][1]["kind"] == "out"

    assert body["related"]["buyer_display_name"] == "Osman"
    assert len(body["related"]["matches"]) == 1
    assert body["related"]["matches"][0]["thread_id"] == "t2"


def test_thread_autopilot_flag(client):
    c, mdb = client
    mdb.thread_history.return_value = [
        _row(id=1, direction="in", buyer_display_name="X", buyer_name="x@y.com",
             ad_title="A", ad_price="100", ad_url="u", account_id=1),
    ]
    mdb.thread_events.return_value = []
    autopilot = MagicMock()
    autopilot.__getitem__.side_effect = {"active": 1}.__getitem__
    autopilot.keys.return_value = ["active"]
    mdb.get_thread_autopilot.return_value = autopilot
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/t1", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["header"]["is_autopilot"] is True


def test_thread_requires_auth(client):
    c, mdb = client
    res = c.get("/api/ma/threads/abc")
    assert res.status_code == 422
