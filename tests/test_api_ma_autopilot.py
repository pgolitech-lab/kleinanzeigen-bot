"""TestClient тесты для POST /api/ma/threads/{id}/autopilot/{start,stop}."""
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
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = [
            _row(id=42, direction="in", gmail_thread_id="abc", status="pending",
                 ad_title="Sitzbank", ad_price="1500€", ad_url=None, ad_id=None,
                 buyer_display_name="Osman", buyer_name="osman@x.com",
                 client_lang="de", de_client="", ru_client="",
                 ru_answer="", de_answer="", ru_translation="",
                 deal_brief_json=None, extra_notes=None, is_auto_ack=0,
                 account_id=1),
        ]
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        from web.app import app
        yield TestClient(app), mdb, mtb


def test_autopilot_start_silent(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1200, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["header"]["thread_id"] == "abc"
    mdb.start_thread_autopilot.assert_called_once()
    args, kwargs = mdb.start_thread_autopilot.call_args
    # signature: (thread_id, floor_price_eur, notify_mode, started_by=...)
    assert args[0] == "abc"
    assert args[1] == 1200
    assert args[2] == "silent"
    # Silent → notification NOT sent
    mtb.send_autopilot_start_notification.assert_not_called()


def test_autopilot_start_notify_sends_notification(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "notify"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mtb.send_autopilot_start_notification.assert_called_once()
    args, kwargs = mtb.send_autopilot_start_notification.call_args
    # signature: (msg_id, floor, actor)
    assert args[0] == 42  # latest msg_id из thread_history
    assert args[1] == 1500
    assert args[2] == "@pgtest#999"


def test_autopilot_start_invalid_floor_negative_422(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": -100, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_autopilot_start_invalid_notify_mode_422(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "loud"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_autopilot_start_404_thread_missing(client):
    c, mdb, mtb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_autopilot_stop_success(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/stop",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mdb.stop_thread_autopilot.assert_called_once_with("abc", "manual")


def test_autopilot_stop_404_thread_missing(client):
    c, mdb, mtb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/autopilot/stop",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_autopilot_endpoints_require_auth(client):
    c, mdb, mtb = client
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent"})
    assert res.status_code == 422
    res = c.post("/api/ma/threads/abc/autopilot/stop")
    assert res.status_code == 422
