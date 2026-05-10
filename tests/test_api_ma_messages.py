"""TestClient тесты для GET /api/ma/messages/{id} и /lock."""
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
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.get_message.return_value = None
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mol.state.return_value = None
        mol.remaining_min.return_value = 0
        from web.app import app
        yield TestClient(app), mdb, mol


def _full_msg_row(**overrides):
    """Helper для создания row с реалистичным набором колонок review-карточки."""
    defaults = {
        "id": 123,
        "gmail_thread_id": "abc",
        "status": "pending",
        "direction": "in",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://kleinanzeigen.de/x/2812",
        "ad_id": "2812",
        "buyer_display_name": "Osman",
        "buyer_name": "osman@gmx.de",
        "client_lang": "de",
        "de_client": "Können Sie 1300?",
        "ru_client": "Можете уступить до 1300?",
        "ru_answer": "Минимум 1400, при самовывозе...",
        "de_answer": "Mindestens 1400€... MfG",
        "ru_translation": "Минимум 1400€... С уважением",
        "deal_brief_json": '{"summary_ru":"торгуется","negotiated_price_eur":1300,"client_assessment":"серьёзный","expected_next":"ждём ответ"}',
        "extra_notes": None,
        "is_auto_ack": 0,
    }
    defaults.update(overrides)
    return _row(**defaults)


def test_message_review_404_when_missing(client):
    c, mdb, mol = client
    mdb.get_message.return_value = None
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/999", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_message_review_returns_full_payload(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["msg_id"] == 123
    assert body["thread_id"] == "abc"
    assert body["status"] == "pending"
    assert body["ad"]["title"] == "Sitzbank"
    assert body["ad"]["price"] == "1500€"
    assert body["ad"]["buyer_email"] == "osman@gmx.de"
    assert body["client_lang"] == "de"
    assert body["client_message"]["raw"] == "Können Sie 1300?"
    assert body["client_message"]["ru"] == "Можете уступить до 1300?"
    assert body["draft"]["ru_answer"].startswith("Минимум 1400")
    assert body["draft"]["de_answer"].startswith("Mindestens")
    assert body["draft"]["ru_translation"].startswith("Минимум 1400€")
    assert body["lock"]["holder"] is None
    assert body["lock"]["remaining_min"] == 0
    assert body["autopilot"]["active"] is False


def test_message_review_deal_brief_parses_json(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    brief = res.json()["deal_brief"]
    assert brief["summary_ru"] == "торгуется"
    assert brief["negotiated_price_eur"] == 1300
    assert brief["client_assessment"] == "серьёзный"
    assert brief["expected_next"] == "ждём ответ"


def test_message_review_invalid_deal_brief_json_returns_null(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row(deal_brief_json="{not_valid_json")
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["deal_brief"] is None


def test_message_review_null_deal_brief_returns_null(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row(deal_brief_json=None)
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["deal_brief"] is None


def test_message_review_lock_holder_when_held(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mol.state.return_value = ("@other#111", 1700000000.0)
    mol.remaining_min.return_value = 4
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["lock"]["holder"] == "@other#111"
    assert body["lock"]["remaining_min"] == 4


def test_message_review_autopilot_active(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    autopilot = MagicMock()
    autopilot.__getitem__.side_effect = {
        "active": 1, "messages_sent": 5, "floor_eur": 1200, "notify_mode": "silent",
    }.__getitem__
    autopilot.keys.return_value = ["active", "messages_sent", "floor_eur", "notify_mode"]
    mdb.get_thread_autopilot.return_value = autopilot
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["autopilot"]["active"] is True
    assert body["autopilot"]["messages_sent"] == 5
    assert body["autopilot"]["floor_eur"] == 1200
    assert body["autopilot"]["notify_mode"] == "silent"


def test_message_review_includes_related(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mdb.find_related_inquiries.return_value = [
        _row(gmail_thread_id="t2", ad_title="Other", ad_price="500€",
             sent_at="2026-05-09T10:00:00", created_at="2026-05-09T09:00:00"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    related = res.json()["related"]
    assert related["buyer_display_name"] == "Osman"
    assert len(related["matches"]) == 1
    assert related["matches"][0]["thread_id"] == "t2"


def test_message_review_requires_auth(client):
    c, mdb, mol = client
    res = c.get("/api/ma/messages/123")
    assert res.status_code == 422


def test_message_review_rejects_bad_hash(client):
    c, mdb, mol = client
    res = c.get(
        "/api/ma/messages/123",
        headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=99&hash=fake"},
    )
    assert res.status_code == 401
