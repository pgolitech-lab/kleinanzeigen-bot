"""TestClient тесты для POST /api/ma/threads/{thread_id}/suggest-reply."""
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
         patch("web.api_ma.scheduler") as msched:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        # thread_history with at least one direction='in' row
        in_row = _row(id=42, direction="in", gmail_thread_id="abc",
                      ad_title="X", ad_price="100", ad_url=None, ad_id=None,
                      buyer_display_name="Buyer", buyer_name="b@x.com",
                      client_lang="de", de_client="Hello", ru_client="Привет",
                      ru_answer="", de_answer="", ru_translation="",
                      deal_brief_json=None, extra_notes=None, is_auto_ack=0,
                      account_id=1, status="pending", sent_at=None, sent_message_id=None)
        mdb.thread_history.return_value = [in_row]
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        msched.regenerate_draft.return_value = {"kind": "regenerated", "message": "ok"}
        from web.app import app
        yield TestClient(app), mdb, mtb, msched


def test_suggest_reply_success(client):
    c, mdb, mtb, msched = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/suggest-reply",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["header"]["thread_id"] == "abc"
    msched.regenerate_draft.assert_called_once_with(42, "regen")


def test_suggest_reply_404_no_incoming(client):
    c, mdb, mtb, msched = client
    # only outgoing rows
    out_row = _row(id=1, direction="out", gmail_thread_id="abc")
    mdb.thread_history.return_value = [out_row]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/suggest-reply",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_suggest_reply_404_empty_thread(client):
    c, mdb, mtb, msched = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/suggest-reply",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_suggest_reply_500_on_regen_error(client):
    c, mdb, mtb, msched = client
    msched.regenerate_draft.return_value = {"kind": "error", "message": "Claude API timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/suggest-reply",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    assert "Claude API timeout" in res.json()["detail"]


def test_suggest_reply_requires_auth(client):
    c, mdb, mtb, msched = client
    res = c.post("/api/ma/threads/abc/suggest-reply")
    assert res.status_code == 422


def test_suggest_reply_never_touches_sent_row(client):
    """Аудит №45: на треде без черновика «Предложить» затирал отправленный ответ (sent → edited)."""
    c, mdb, mtb, msched = client
    sent = _row(id=42, direction="in", gmail_thread_id="abc", status="sent",
                sent_at="2026-10-19T10:00:00", sent_message_id="<x@y>")
    mdb.thread_history.return_value = [sent]
    res = c.post("/api/ma/threads/abc/suggest-reply",
                 headers={"X-Telegram-Init-Data": make_init_data(TEST_USER)})
    assert res.status_code == 409
    msched.regenerate_draft.assert_not_called()
    mdb.update_message.assert_not_called()


def test_suggest_reply_skips_edited_after_send(client):
    """Уже испорченная строка (edited, но с sent_at) тоже не перегенерируется."""
    c, mdb, mtb, msched = client
    broken = _row(id=42, direction="in", gmail_thread_id="abc", status="edited",
                  sent_at="2026-10-19T10:00:00", sent_message_id=None)
    mdb.thread_history.return_value = [broken]
    res = c.post("/api/ma/threads/abc/suggest-reply",
                 headers={"X-Telegram-Init-Data": make_init_data(TEST_USER)})
    assert res.status_code == 409
    msched.regenerate_draft.assert_not_called()


def test_draft_endpoints_refuse_sent_row(client):
    """regenerate/price/instruction/edit-*/translate-draft тоже не трогают отправленное."""
    c, mdb, mtb, msched = client
    mdb.get_message.return_value = _row(id=42, status="sent", sent_at="x", sent_message_id="<m>",
                                        gmail_thread_id="abc", client_lang="de", de_answer="Hallo")
    h = {"X-Telegram-Init-Data": make_init_data(TEST_USER)}
    for path, body in [("regenerate", {"strategy": "regen"}), ("price", {"eur": 100}),
                       ("instruction", {"text": "x"}), ("edit-ru", {"text": "x"}),
                       ("edit-de", {"text": "x"}), ("translate-draft", {"text": "x"})]:
        r = c.post(f"/api/ma/messages/42/{path}", json=body, headers=h)
        assert r.status_code == 409, path
    mdb.update_message.assert_not_called()
