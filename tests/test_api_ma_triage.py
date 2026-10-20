"""«Разбор»: только «ждут нас» без автопилота, сортировка по баллу, черновик в ответе."""
from __future__ import annotations
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**over):
    d = dict(id=1, gmail_thread_id="t", account_id=1, buyer_display_name="B", ad_title="X", ad_price="1000 €",
             deal_brief_json=None, last_event_at=datetime.utcnow().isoformat(), last_event_kind="in",
             autopilot_active=0, autopilot_floor_price_eur=None, has_pending_draft=0, de_answer=None,
             ru_answer=None, ru_translation=None, ru_client="Привет", status="pending")
    d.update(over)
    r = MagicMock()
    r.__getitem__.side_effect = d.__getitem__
    r.keys.return_value = d.keys()
    return r


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        from web.api_ma import invalidate_pipeline_cache
        invalidate_pipeline_cache()
        yield TestClient(app), mdb
        invalidate_pipeline_cache()


def test_triage_order_and_filters(client):
    c, mdb = client
    old = (datetime.utcnow() - timedelta(hours=30)).isoformat()
    mdb.pipeline_threads.return_value = [
        _row(id=1, gmail_thread_id="fresh"),
        _row(id=2, gmail_thread_id="hot_old", last_event_at=old,
             deal_brief_json='{"client_assessment": "серьёзный, готов забрать", "negotiated_price_eur": 950}',
             has_pending_draft=1, de_answer="Ja, gerne", ru_translation="Да, конечно"),
        _row(id=3, gmail_thread_id="ours", last_event_kind="out"),
        _row(id=4, gmail_thread_id="auto", autopilot_active=1),
    ]
    body = c.get("/api/ma/triage", headers={"X-Telegram-Init-Data": make_init_data(TEST_USER)}).json()
    assert [i["thread_id"] for i in body["items"]] == ["hot_old", "fresh"]
    top = body["items"][0]
    assert top["draft"] == {"msg_id": 2, "status": "pending", "client_text": "Ja, gerne", "ru_text": "Да, конечно"}
    assert "горячий" in top["reasons"] and "цена почти полная" in top["reasons"]
    assert body["items"][1]["draft"] is None
