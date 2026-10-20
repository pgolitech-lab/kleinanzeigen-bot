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


def _thread(mdb, brief=None, ad_price="450 €", status="pending", sent_at=None):
    mdb.thread_history.return_value = [_row(id=7, direction="in", deal_brief_json=brief, ad_price=ad_price,
                                            extra_notes=None, status=status, sent_at=sent_at,
                                            sent_message_id=None, account_id=3, ad_title="Bank",
                                            buyer_display_name="Klaus", buyer_name="k@x")]
    conn = mdb.get_conn.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = None  # в очереди ничего


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


def test_sold_on_answered_thread_keeps_sent_row(client):
    """Последнее письмо уже отвечено: наш отправленный ответ не превращается в skipped_sold —
    продажа отдельной строкой, тред закрыт."""
    c, mdb = client
    _thread(mdb, status="sent", sent_at="2026-10-19T10:00:00")
    mdb.add_manual_sale.return_value = {"msg_id": 99, "thread_id": "manual-x"}
    r = c.post("/api/ma/threads/t1/sold", json={"price_eur": 300}, headers=H())
    assert r.status_code == 200 and r.json()["separate_sale"] == {"msg_id": 99, "thread_id": "manual-x"}
    mdb.mark_thread_sold.assert_not_called()
    assert mdb.add_manual_sale.call_args.kwargs["sold_price_eur"] == 300.0
    mdb.close_thread.assert_called_once()


def test_sold_refused_while_letter_queued(client):
    c, mdb = client
    _thread(mdb)
    mdb.get_conn.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"id": 7}
    assert c.post("/api/ma/threads/t1/sold", json={"price_eur": 1}, headers=H()).status_code == 409
    mdb.mark_thread_sold.assert_not_called()


def test_sold_without_any_price_400(client):
    c, mdb = client
    _thread(mdb, ad_price="VB")
    assert c.post("/api/ma/threads/t1/sold", json={}, headers=H()).status_code == 400
    mdb.mark_thread_sold.assert_not_called()


def test_accept_confident(client):
    c, mdb = client
    mdb.accept_detected_bulk.return_value = {"applied": [{"detection_id": 1}], "skipped": []}
    r = c.post("/api/ma/detected-sales/accept-confident", json={"min_confidence": "medium"}, headers=H())
    assert r.json()["applied"] == [{"detection_id": 1}]
    mdb.accept_detected_bulk.assert_called_once_with("medium")
    assert c.post("/api/ma/detected-sales/accept-confident", json={"min_confidence": "low"},
                  headers=H()).status_code == 422


def test_dashboard_sold_today_from_db_sales_summary(client):
    """«Продано сегодня» — db.sales_summary(сегодня по Берлину), как экран «Продажи» и бот."""
    c, mdb = client
    mdb.pipeline_threads.return_value = []
    conn = mdb.get_conn.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"c": 0, "s": 0}
    mdb.berlin_day_bounds_utc.return_value = ("2026-10-19T22:00:00", "2026-10-20T22:00:00")
    mdb.sales_summary.return_value = {"count": 2, "total_eur": 500.0}
    from web import api_ma
    from modules.db_sales import today_berlin
    api_ma.invalidate_pipeline_cache()
    with patch("web.api_ma.config") as mcfg:
        mcfg.get.return_value = ""
        body = c.get("/api/ma/dashboard", headers=H()).json()
    assert body["today"]["sold"] == 2
    assert mdb.sales_summary.call_args_list[0].args == (today_berlin(), today_berlin())


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
