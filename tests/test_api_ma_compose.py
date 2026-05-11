"""TestClient тесты для POST /api/ma/threads/{thread_id}/compose."""
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
         patch("web.api_ma.scheduler") as msched, \
         patch("web.api_ma.claude") as mclaude:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = [
            _row(id=42, direction="in", gmail_thread_id="abc", status="sent"),
        ]
        msched.send_manual_compose.return_value = {"kind": "ok", "message_id": 99}
        # db.get_conn() context manager returning a MagicMock conn whose
        # execute(...).fetchall() yields an empty list (no pending drafts to skip).
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value.execute.return_value.fetchall.return_value = []
        mdb.get_conn.return_value = conn_cm
        from web.app import app
        yield TestClient(app), mdb, msched


def test_compose_success(client):
    c, mdb, msched = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "Здравствуйте, у меня вопрос..."},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["closed_drafts"] == []
    msched.send_manual_compose.assert_called_once_with(42, "Здравствуйте, у меня вопрос...")


def test_compose_closes_pending_drafts(client):
    """После успешного compose — pending in-rows треда → status='skipped'."""
    c, mdb, msched = client
    # Simulate two pending drafts to be closed
    conn_cm = MagicMock()
    pending_rows = [_row(id=1965), _row(id=1970)]
    conn_cm.__enter__.return_value.execute.return_value.fetchall.return_value = pending_rows
    mdb.get_conn.return_value = conn_cm

    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "manual reply"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert set(body["closed_drafts"]) == {1965, 1970}


def test_compose_404_thread_missing(client):
    c, mdb, msched = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/compose",
                 json={"text": "test"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_compose_empty_text_422(client):
    c, mdb, msched = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_compose_smtp_failure_500(client):
    c, mdb, msched = client
    msched.send_manual_compose.return_value = {"kind": "error", "message": "SMTP timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "test"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    assert "SMTP timeout" in res.json()["detail"]


def test_compose_requires_auth(client):
    c, mdb, msched = client
    res = c.post("/api/ma/threads/abc/compose", json={"text": "test"})
    assert res.status_code == 422
