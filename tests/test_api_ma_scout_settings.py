"""Настройки разведки должны быть доступны из Mini App.

Без whitelist-записей авто-прогон нельзя было включить: scout_job молча
возвращал «auto disabled», данные протухали (инцидент 2026-07-21).
"""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN
from web.api_ma import ALLOWED_SETTING_KEYS, VALIDATORS

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.config") as mcfg:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mcfg.get.return_value = ""
        from web.app import app
        yield TestClient(app), mdb


@pytest.mark.parametrize("key", [
    "scout_auto_enabled", "scout_interval_hours",
    "scout_page_delay_sec", "scout_stale_days",
])
def test_scout_keys_are_whitelisted(key):
    assert key in ALLOWED_SETTING_KEYS


@pytest.mark.parametrize("value,ok", [("1", True), ("0", True), ("yes", False), ("", False)])
def test_auto_enabled_validator(value, ok):
    assert VALIDATORS["scout_auto_enabled"](value) is ok


@pytest.mark.parametrize("value,ok", [
    ("6", True), ("1", True), ("168", True), ("0", False), ("169", False), ("шесть", False),
])
def test_interval_validator(value, ok):
    assert VALIDATORS["scout_interval_hours"](value) is ok


@pytest.mark.parametrize("value,ok", [
    ("1.5", True), ("0,8", True), ("0.2", True), ("10", True),
    ("0.1", False), ("11", False), ("fast", False),
])
def test_page_delay_validator(value, ok):
    """Запятая как десятичный разделитель — оператор может ввести «1,5»."""
    assert VALIDATORS["scout_page_delay_sec"](value) is ok


@pytest.mark.parametrize("value,ok", [("7", True), ("90", True), ("0", False), ("91", False)])
def test_stale_days_validator(value, ok):
    assert VALIDATORS["scout_stale_days"](value) is ok


def test_toggle_auto_scout_via_api(client):
    """Тумблер в UI бьёт именно сюда."""
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings", json={"key": "scout_auto_enabled", "value": "1"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mdb.set_setting.assert_called_once_with("scout_auto_enabled", "1")


def test_invalid_scout_value_rejected(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings", json={"key": "scout_interval_hours", "value": "999"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mdb.set_setting.assert_not_called()
