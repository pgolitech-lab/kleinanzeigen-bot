"""TestClient тесты для POST /api/ma/threads/{thread_id}/wait."""
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
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        in_row = _row(id=42, direction="in", gmail_thread_id="abc",
                      ad_title="X", ad_price="100", ad_url=None, ad_id=None,
                      buyer_display_name="B", buyer_name="b@x.com",
                      client_lang="de", de_client="", ru_client="",
                      ru_answer="", de_answer="", ru_translation="",
                      deal_brief_json=None, extra_notes=None, is_auto_ack=0,
                      account_id=1, status="sent")
        mdb.thread_history.return_value = [in_row]
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        from web.app import app
        yield TestClient(app), mdb


def test_wait_success(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/wait", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["header"]["thread_id"] == "abc"
    mdb.mark_thread_waiting.assert_called_once()
    args, kwargs = mdb.mark_thread_waiting.call_args
    assert args[0] == "abc"


def test_wait_404_thread_missing(client):
    c, mdb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/x/wait", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_wait_requires_auth(client):
    c, mdb = client
    res = c.post("/api/ma/threads/abc/wait")
    assert res.status_code == 422
