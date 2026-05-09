"""TestClient тесты для GET /api/ma/clients/{email}/history."""
from __future__ import annotations
from unittest.mock import patch, MagicMock
from urllib.parse import quote

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
    with patch("modules.tg_init_data.config") as mc,          patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.list_threads_for_client.return_value = []
        from web.app import app
        yield TestClient(app), mdb


def test_client_history_empty_returns_empty_threads(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('user@example.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    body = res.json()
    assert body == {"buyer_email": "user@example.com", "threads": []}


def test_client_history_returns_threads(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="123", ad_price="1500€",
             msg_count=3, last_at="2026-05-10T10:00:00", last_status="sent"),
        _row(thread_id="t2", ad_title="B", ad_id="456", ad_price="500€",
             msg_count=1, last_at="2026-05-09T10:00:00", last_status="pending"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('osman@x.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["buyer_email"] == "osman@x.com"
    assert len(body["threads"]) == 2
    assert body["threads"][0]["thread_id"] == "t1"
    assert body["threads"][0]["msg_count"] == 3
    assert body["threads"][0]["last_status"] == "sent"


def test_client_history_uses_email_as_lookup_key(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('foo@bar.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    mdb.list_threads_for_client.assert_called_once_with("foo@bar.com")


def test_client_history_requires_auth(client):
    c, mdb = client
    res = c.get(f"/api/ma/clients/{quote('x@y.com')}/history")
    assert res.status_code == 422
