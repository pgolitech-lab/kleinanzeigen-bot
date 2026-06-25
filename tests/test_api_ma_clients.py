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
    row.get.side_effect = fields.get
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


def test_client_history_includes_deal_brief(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="800€",
             msg_count=2, last_at="2026-06-24T10:00:00", last_status="skipped_sold",
             deal_brief_json='{"summary_ru":"Договорились","negotiated_price_eur":750,"client_assessment":"серьёзный"}'),
    ]
    mdb.get_client_profile.return_value = None
    mdb.get_conn = None  # заглушка — display_name и cost придут из отдельных queries
    init = make_init_data(TEST_USER)
    # Замокать get_conn чтобы display_name/cost queries вернули пустое
    with patch("web.api_ma.db.get_conn") as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = mock_cursor
        res = c.get(
            f"/api/ma/clients/{quote('osman@x.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert len(body["threads"]) == 1
    brief = body["threads"][0].get("deal_brief")
    assert brief is not None
    assert brief["summary_ru"] == "Договорились"
    assert brief["negotiated_price_eur"] == 750
