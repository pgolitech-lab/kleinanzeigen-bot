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

def test_regenerate_valid_strategy(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_strategy.return_value = {
        "ru_answer": "новый RU",
        "client_answer": "neue DE",
        "ru_translation": "новый back",
        "deal_summary_ru": "обновлено",
        "expected_next": "ждём",
        "negotiated_price_eur": 1400,
        "client_assessment": "серьёзный",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "harsh"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    # Возвращается полный review payload (как GET /messages/{id})
    assert body["msg_id"] == 123
    mclaude.regenerate_with_strategy.assert_called_once()
    args, kwargs = mclaude.regenerate_with_strategy.call_args
    assert args[1] == "harsh"
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    # Lock остаётся (intermediate)
    mtb._release_lock.assert_not_called()


def test_regenerate_invalid_strategy_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "evil"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422
    mclaude.regenerate_with_strategy.assert_not_called()


def test_regenerate_409_when_locked(client):
    c, mdb, mtb, msched, mclaude = client
    mtb._check_lock.return_value = "@other#222"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "harsh"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409


def test_regenerate_all_5_valid_strategies(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_strategy.return_value = {
        "ru_answer": "x", "client_answer": "y", "ru_translation": "z",
        "deal_summary_ru": "", "expected_next": "", "negotiated_price_eur": None,
        "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    for strategy in ["fest", "harsh", "friend", "short", "regen"]:
        res = c.post("/api/ma/messages/123/regenerate",
                     json={"strategy": strategy},
                     headers={"X-Telegram-Init-Data": init})
        assert res.status_code == 200, f"strategy={strategy} failed"


def test_edit_ru_success(client):
    c, mdb, mtb, msched, mclaude = client
    # translate_only вызывается дважды: forward (ru→de) и back (de→ru)
    mclaude.translate_only.side_effect = ["Neue DE Text", "Новый back"]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": "Новый RU текст"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["msg_id"] == 123
    assert mclaude.translate_only.call_count == 2
    args, kwargs = mdb.update_message.call_args
    assert kwargs["ru_answer"] == "Новый RU текст"
    assert kwargs["de_answer"] == "Neue DE Text"
    assert kwargs["ru_translation"] == "Новый back"
    assert kwargs["status"] == "edited"
    mtb.broadcast_after_external_action.assert_called_once_with(123)


def test_edit_de_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.translate_only.return_value = "Новый back-translation"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-de",
                 json={"text": "Neuer DE Text"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    # translate_only вызывается ОДИН раз — только back-translate
    assert mclaude.translate_only.call_count == 1
    args, kwargs = mdb.update_message.call_args
    assert kwargs["de_answer"] == "Neuer DE Text"
    assert kwargs["ru_translation"] == "Новый back-translation"
    assert "ru_answer" not in kwargs  # ru_answer НЕ трогаем
    assert kwargs["status"] == "edited"


def test_edit_ru_empty_text_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_edit_ru_too_long_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": "x" * 5000},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_price_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_price.return_value = {
        "ru_answer": "согласен на 1400",
        "client_answer": "akzeptiere 1400",
        "ru_translation": "принимаю 1400",
        "deal_summary_ru": "", "expected_next": "",
        "negotiated_price_eur": 1400, "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": 1400},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    args, kwargs = mclaude.regenerate_with_price.call_args
    assert args[1] == 1400
    mtb.broadcast_after_external_action.assert_called_once_with(123)


def test_price_invalid_negative_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": -10},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_price_invalid_too_high_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": 999999},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_instruction_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_instruction.return_value = {
        "ru_answer": "x", "client_answer": "y", "ru_translation": "z",
        "deal_summary_ru": "", "expected_next": "",
        "negotiated_price_eur": None, "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/instruction",
                 json={"text": "Скажи что доставим завтра"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    args, kwargs = mclaude.regenerate_with_instruction.call_args
    assert args[1] == "Скажи что доставим завтра"


def test_instruction_empty_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/instruction",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422
