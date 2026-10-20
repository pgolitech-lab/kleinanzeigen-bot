"""QA «ленивый продажник»: продажа с любого треда (S-01), «сегодня» по Берлину одним источником (S-03),
принятие уверенных находок разом (S-04)."""
from __future__ import annotations
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}
H = lambda: {"X-Telegram-Init-Data": make_init_data(TEST_USER)}  # noqa: E731


def _row(**d):
    r = MagicMock()
    r.__getitem__.side_effect = d.__getitem__
    r.keys.return_value = d.keys()
    r.get.side_effect = d.get
    return r


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mtb._claim_lock.return_value = None
        mdb.mark_thread_sold.return_value = {"thread_id": "t1", "ad_id": "a", "closed_other_threads": []}
        from web.app import app
        yield TestClient(app), mdb


def _thread(mdb, brief=None, ad_price="450 €"):
    mdb.thread_history.return_value = [_row(id=7, direction="in", deal_brief_json=brief,
                                            ad_price=ad_price, extra_notes=None, status="sent")]


def test_sold_any_thread_price_from_deal_brief(client):
    c, mdb = client
    _thread(mdb, brief='{"negotiated_price_eur": 380}')
    r = c.post("/api/ma/threads/t1/sold", json={}, headers=H())
    assert r.status_code == 200 and r.json()["sold_price_eur"] == 380 and r.json()["price_source"] == "deal_brief"
    assert mdb.mark_thread_sold.call_args.kwargs["sold_price_eur"] == 380.0


def test_sold_any_thread_price_from_ad_and_explicit(client):
    c, mdb = client
    _thread(mdb)
    assert c.post("/api/ma/threads/t1/sold", json={}, headers=H()).json()["sold_price_eur"] == 450
    r = c.post("/api/ma/threads/t1/sold", json={"price_eur": 300, "note": "забрал сам"}, headers=H())
    assert r.json()["price_source"] == "operator"
    assert "забрал сам" in mdb.update_message.call_args.kwargs["extra_notes"]


def test_sold_without_any_price_400(client):
    c, mdb = client
    _thread(mdb, ad_price="VB")
    assert c.post("/api/ma/threads/t1/sold", json={}, headers=H()).status_code == 400
    mdb.mark_thread_sold.assert_not_called()


def test_accept_confident(client):
    c, mdb = client
    mdb.list_pending_detections.return_value = [_row(id=1, confidence="high"), _row(id=2, confidence="medium"),
                                                _row(id=3, confidence="low")]
    r = c.post("/api/ma/detected-sales/accept-confident", json={}, headers=H())
    assert r.json()["applied"] == [1]
    mdb.apply_detection_to_messages.reset_mock()
    r = c.post("/api/ma/detected-sales/accept-confident", json={"min_confidence": "medium"}, headers=H())
    assert r.json()["applied"] == [1, 2]
    assert c.post("/api/ma/detected-sales/accept-confident", json={"min_confidence": "low"},
                  headers=H()).status_code == 422


def test_berlin_day_bounds_cover_local_day():
    from web.api_ma import berlin_day_bounds
    t0, t1 = berlin_day_bounds()
    a, b = datetime.fromisoformat(t0), datetime.fromisoformat(t1)
    assert 23 * 3600 <= (b - a).total_seconds() <= 25 * 3600
    assert a.hour in (22, 23)  # полночь по Берлину = 22:00/23:00 UTC
    assert a <= datetime.utcnow() < b


def test_dashboard_sold_today_from_list_sales(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = []
    conn = mdb.get_conn.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"c": 0, "s": 0}
    mdb.list_sales.return_value = [{"sold_price_eur": 300}, {"sold_price_eur": 200}]
    from web import api_ma
    api_ma.invalidate_pipeline_cache()
    with patch("web.api_ma.config") as mcfg:
        mcfg.get.return_value = ""
        body = c.get("/api/ma/dashboard", headers=H()).json()
    assert body["today"]["sold"] == 2
    t0, t1 = api_ma.berlin_day_bounds()
    assert mdb.list_sales.call_args_list[0].kwargs["period_from"] == t0


def test_detected_sales_evidence_translated_and_cached(client):
    c, mdb = client
    from web import api_ma
    api_ma._EVIDENCE_RU.clear()
    det = dict(id=11, gmail_thread_id="t", confidence="high", sold_price_eur=300, detected_ad_title="X",
               detected_sold_at=None, evidence="Ich nehme es für 300", thread_ad_title="X",
               thread_ad_price="350 €", buyer_display_name="B", buyer_name="b", account_name="a")
    det_ru = dict(det, id=12, evidence="Беру за 300")
    mdb.list_pending_detections.return_value = [_row(**det), _row(**det_ru)]
    with patch("web.api_ma.claude") as mcl:
        mcl.translate_only.return_value = {"translation": "Беру за 300"}
        items = c.get("/api/ma/detected-sales", headers=H()).json()["items"]
        c.get("/api/ma/detected-sales", headers=H())
    assert [i["evidence_ru"] for i in items] == ["Беру за 300", "Беру за 300"]
    assert mcl.translate_only.call_count == 1  # кириллица не переводится, повтор — из кэша
