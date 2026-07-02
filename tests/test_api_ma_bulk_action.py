"""Тесты для POST /api/ma/threads/bulk-action."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        yield TestClient(app), mdb


def _post(c, body, user=TEST_USER):
    init = make_init_data(user)
    return c.post(
        "/api/ma/threads/bulk-action",
        json=body,
        headers={"X-Telegram-Init-Data": init},
    )


def test_bulk_close_calls_close_thread(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1", "t2"], "action": "close"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["affected"] == 2
    assert mdb.close_thread.call_count == 2


def test_bulk_pin_calls_set_thread_flags_with_pinned(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1"], "action": "pin"})
    assert res.status_code == 200
    mdb.set_thread_flags.assert_called_once_with("t1", is_pinned=1)


def test_bulk_unpin_calls_set_thread_flags_with_zero(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "unpin"})
    mdb.set_thread_flags.assert_called_once_with("t1", is_pinned=0)


def test_bulk_read_clears_operator_unread(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "read"})
    mdb.set_thread_flags.assert_called_once_with("t1", operator_unread=0)


def test_bulk_unread_sets_operator_unread(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "unread"})
    mdb.set_thread_flags.assert_called_once_with("t1", operator_unread=1)


def test_bulk_unknown_action_returns_400(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1"], "action": "nuke"})
    assert res.status_code == 400


def test_bulk_empty_thread_ids_returns_422(client):
    c, mdb = client
    res = _post(c, {"thread_ids": [], "action": "pin"})
    assert res.status_code == 422


def test_bulk_requires_auth(client):
    c, mdb = client
    res = c.post("/api/ma/threads/bulk-action", json={"thread_ids": ["t1"], "action": "pin"})
    assert res.status_code == 422
