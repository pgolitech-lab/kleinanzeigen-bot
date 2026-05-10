"""TestClient тесты для POST /api/ma/threads/{id}/autopilot/preview + extended /autopilot/start."""
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
         patch("web.api_ma.operator_lock") as mol, \
         patch("web.api_ma.claude") as mclaude:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        in_row = _row(id=42, direction="in", gmail_thread_id="abc",
                      ad_title="Sitzbank", ad_price="1500€", ad_url=None, ad_id=None,
                      ad_description="Used Peugeot seat",
                      buyer_display_name="Osman", buyer_name="osman@x.com",
                      seller_name="Peter Block",
                      client_lang="de", de_client="Können Sie 1300?",
                      ru_client="Можете 1300?",
                      ru_answer="", de_answer="", ru_translation="",
                      deal_brief_json=None, extra_notes=None, is_auto_ack=0,
                      account_id=1, status="pending")
        mdb.thread_history.return_value = [in_row]
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        mclaude.generate_autopilot_reply.return_value = {
            "ru_answer": "Минимум 1400, согласен на самовывоз",
            "client_answer": "Mindestens 1400€, bei Selbstabholung. MfG",
            "ru_translation": "Минимум 1400€, при самовывозе. С уважением",
            "deal_summary_ru": "торгуется",
            "expected_next": "ждём ответ",
            "negotiated_price_eur": 1400,
            "client_assessment": "серьёзный",
            "should_stop": False,
        }
        from web.app import app
        yield TestClient(app), mdb, mtb, mclaude


def test_preview_success(client):
    c, mdb, mtb, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/preview",
                 json={"floor_eur": 1200, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["preview"]["ru_text"].startswith("Минимум 1400")
    assert body["preview"]["client_text"].startswith("Mindestens")
    assert body["preview"]["ru_translation"].startswith("Минимум 1400€")
    assert body["preview"]["deal_brief"]["summary_ru"] == "торгуется"
    mclaude.generate_autopilot_reply.assert_called_once()
    args, kwargs = mclaude.generate_autopilot_reply.call_args
    assert kwargs.get("floor_eur") == 1200 or (len(args) >= 1)


def test_preview_404_no_incoming(client):
    c, mdb, mtb, mclaude = client
    out_row = _row(id=1, direction="out", gmail_thread_id="abc")
    mdb.thread_history.return_value = [out_row]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/preview",
                 json={"floor_eur": 1200, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_preview_invalid_floor_422(client):
    c, mdb, mtb, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/preview",
                 json={"floor_eur": -100, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_start_with_preview_applies_to_in_row(client):
    c, mdb, mtb, mclaude = client
    init = make_init_data(TEST_USER)
    preview_payload = {
        "ru_text": "Preview RU",
        "client_text": "Preview DE",
        "ru_translation": "Preview Back",
        "deal_brief": {"summary_ru": "preview brief"},
    }
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent",
                       "preview": preview_payload},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    # update_message called with status='approved' + preview text
    update_calls = mdb.update_message.call_args_list
    assert len(update_calls) >= 1
    # find call with msg_id=42 and status='approved'
    found = False
    for call in update_calls:
        args, kwargs = call
        if args and args[0] == 42 and kwargs.get("status") == "approved":
            assert kwargs.get("de_answer") == "Preview DE"
            assert kwargs.get("ru_answer") == "Preview RU"
            found = True
            break
    assert found, f"expected update_message(42, status='approved', ...) — got {update_calls}"
    mdb.start_thread_autopilot.assert_called_once()


def test_start_without_preview_works_as_before(client):
    c, mdb, mtb, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    # update_message NOT called for the in-row (no preview)
    update_calls = mdb.update_message.call_args_list
    for call in update_calls:
        args, kwargs = call
        if args and args[0] == 42:
            assert kwargs.get("status") != "approved", "should not set approved without preview"
    mdb.start_thread_autopilot.assert_called_once()


def test_preview_endpoint_requires_auth(client):
    c, mdb, mtb, mclaude = client
    res = c.post("/api/ma/threads/abc/autopilot/preview",
                 json={"floor_eur": 1200, "notify_mode": "silent"})
    assert res.status_code == 422
