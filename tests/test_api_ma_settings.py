"""TestClient тесты для GET/POST /api/ma/settings."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.config") as mc2:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mc2.get.side_effect = lambda k: {
            "send_mode": "disabled",
            "gmail_poll_interval_sec": "60",
            "anthropic_api_key": "sk-ant-secret-12345",
            "telegram_bot_token": "secret-bot-token-67890",
            "polling_paused": "0",
        }.get(k, "")
        from web.app import app
        yield TestClient(app), mdb, mc2


def test_settings_get_returns_dict(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/settings", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, dict)
    assert "send_mode" in body
    assert body["send_mode"] == "disabled"


def test_settings_get_masks_secrets(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/settings", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    # Secrets must be masked
    assert "secret" not in body.get("anthropic_api_key", "").lower()
    assert "secret" not in body.get("telegram_bot_token", "").lower()
    assert body["anthropic_api_key"].startswith("•")
    assert body["telegram_bot_token"].startswith("•")


def test_settings_post_valid_update(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "send_mode", "value": "production"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    mdb.set_setting.assert_called_once_with("send_mode", "production")


def test_settings_post_invalid_key_400(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "evil_key", "value": "x"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mdb.set_setting.assert_not_called()


def test_settings_post_invalid_send_mode_value_400(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "send_mode", "value": "evil"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mdb.set_setting.assert_not_called()


def test_settings_endpoints_require_auth(client):
    c, mdb, mc2 = client
    res = c.get("/api/ma/settings")
    assert res.status_code == 422
    res = c.post("/api/ma/settings", json={"key": "send_mode", "value": "disabled"})
    assert res.status_code == 422
