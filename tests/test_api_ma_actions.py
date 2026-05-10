"""TestClient тесты для action endpoints под /api/ma/messages/{id}/*."""
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


def _full_msg_row(**overrides):
    defaults = {
        "id": 123,
        "gmail_thread_id": "abc",
        "status": "pending",
        "direction": "in",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://x/2812",
        "ad_id": "2812",
        "buyer_display_name": "Osman",
        "buyer_name": "osman@gmx.de",
        "client_lang": "de",
        "de_client": "Hallo",
        "ru_client": "Привет",
        "ru_answer": "ответ",
        "de_answer": "Antwort",
        "ru_translation": "ответ-back",
        "deal_brief_json": None,
        "extra_notes": None,
        "is_auto_ack": 0,
    }
    defaults.update(overrides)
    return _row(**defaults)


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol, \
         patch("web.api_ma.scheduler") as msched, \
         patch("web.api_ma.claude") as mclaude:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.get_message.return_value = _full_msg_row()
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mtb._check_lock.return_value = None
        mtb._acquire_lock.return_value = None
        mtb._release_lock.return_value = None
        mtb.broadcast_after_external_action.return_value = None
        mol.state.return_value = ("@pgtest#999", 1700000000.0)
        mol.remaining_min.return_value = 5
        from web.app import app
        yield TestClient(app), mdb, mtb, msched, mclaude


def test_send_success(client):
    c, mdb, mtb, msched, mclaude = client
    msched.send_one.return_value = {"kind": "ok", "message": "sent"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    msched.send_one.assert_called_once_with(123)
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    mtb._release_lock.assert_called_once_with(123)


def test_send_409_when_locked_by_other(client):
    c, mdb, mtb, msched, mclaude = client
    mtb._check_lock.return_value = "@other#222"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409
    msched.send_one.assert_not_called()


def test_send_500_on_smtp_failure(client):
    c, mdb, mtb, msched, mclaude = client
    msched.send_one.return_value = {"kind": "error", "message": "SMTP timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    body = res.json()
    assert "SMTP timeout" in body["detail"]
    # Lock не освобождаем при ошибке — оператор может попробовать ещё
    mtb._release_lock.assert_not_called()


def test_skip_success(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/skip",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == "skipped"
    mdb.update_message.assert_called_once_with(123, status="skipped")
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    mtb._release_lock.assert_called_once_with(123)


def test_sold_success(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/sold",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "skipped_sold"
    mdb.update_message.assert_called_once_with(123, status="skipped_sold")


def test_action_endpoints_require_auth(client):
    c, mdb, mtb, msched, mclaude = client
    for path in ["/api/ma/messages/123/send", "/api/ma/messages/123/skip",
                 "/api/ma/messages/123/sold"]:
        res = c.post(path)
        assert res.status_code == 422
