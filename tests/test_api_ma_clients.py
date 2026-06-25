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
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.list_threads_for_client.return_value = []
        mdb.get_client_profile.return_value = None
        # Настроить get_conn как контекстный менеджер по умолчанию
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mdb.get_conn.return_value = ctx
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
    assert body["buyer_email"] == "user@example.com"
    assert body["threads"] == []
    assert body["sold_count"] == 0
    assert body["total_negotiated_eur"] == 0
    assert body["last_active_thread_id"] is None


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


def test_client_history_returns_aggregates(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="800€",
             msg_count=3, last_at="2026-06-24T10:00:00", last_status="sent",
             deal_brief_json=None),
        _row(thread_id="t2", ad_title="B", ad_id="2", ad_price="500€",
             msg_count=1, last_at="2026-06-20T10:00:00", last_status="skipped_sold",
             deal_brief_json='{"summary_ru":"ok","negotiated_price_eur":500,"client_assessment":"серьёзный"}'),
    ]
    mdb.get_client_profile.return_value = None
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('buyer@test.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert "display_name" in body
    assert "tags" in body
    assert "note" in body
    assert "sold_count" in body
    assert "total_negotiated_eur" in body
    assert "last_active_thread_id" in body
    assert body["sold_count"] == 1
    assert body["total_negotiated_eur"] == 500
    assert body["last_active_thread_id"] == "t1"  # t1 статус sent — активный


def test_client_history_last_active_none_when_all_closed(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="100€",
             msg_count=1, last_at="2026-06-24T10:00:00", last_status="skipped",
             deal_brief_json=None),
        _row(thread_id="t2", ad_title="B", ad_id="2", ad_price="200€",
             msg_count=1, last_at="2026-06-20T10:00:00", last_status="skipped_sold",
             deal_brief_json=None),
    ]
    mdb.get_client_profile.return_value = None
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('buyer@test.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    assert res.json()["last_active_thread_id"] is None


def test_client_history_returns_tags_from_profile(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = []
    profile_row = _row(tags_json='["Серьёзный","Торгуется"]', note="заметка теста")
    mdb.get_client_profile.return_value = profile_row
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('x@y.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["tags"] == ["Серьёзный", "Торгуется"]
    assert body["note"] == "заметка теста"

def test_client_profile_post_saves_tags_and_note(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post(
        f"/api/ma/clients/{quote('buyer@test.com')}/profile",
        headers={"X-Telegram-Init-Data": init},
        json={"tags": ["Серьёзный", "Торгуется"], "note": "важный клиент"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    mdb.upsert_client_profile.assert_called_once_with(
        "buyer@test.com", ["Серьёзный", "Торгуется"], "важный клиент"
    )


def test_client_profile_post_filters_invalid_tags(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post(
        f"/api/ma/clients/{quote('x@y.com')}/profile",
        headers={"X-Telegram-Init-Data": init},
        json={"tags": ["Серьёзный", "НеизвестныйТег", "Мошенник"], "note": ""},
    )
    assert res.status_code == 200
    # НеизвестныйТег отфильтрован, сохранены только допустимые в том же порядке
    mdb.upsert_client_profile.assert_called_once_with(
        "x@y.com", ["Серьёзный", "Мошенник"], ""
    )


def test_client_profile_post_requires_auth(client):
    c, mdb = client
    res = c.post(
        f"/api/ma/clients/{quote('x@y.com')}/profile",
        json={"tags": [], "note": ""},
    )
    assert res.status_code == 422
