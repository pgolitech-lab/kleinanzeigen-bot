"""Чипы цены через drafts.variants (llm): кэш по версии треда, применение, карта ошибок."""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database
from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}
H = lambda: {"X-Telegram-Init-Data": make_init_data(TEST_USER)}  # noqa: E731

RES = {"variants": [{"tone": "hold", "price_eur": 400, "ru_answer": "держим", "client_answer": "Bleibt 400",
                     "ru_translation": "Остаётся 400"},
                    {"tone": "minus5", "price_eur": 380, "ru_answer": "уступим", "client_answer": "380 ok",
                     "ru_translation": "380 ок"}],
       "dropped": [{"tone": "floor", "reason": "минимум не задан"}], "base_price_eur": 400, "floor_eur": None}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    acc = database.add_account("a", "a@gmail.com", "pw")
    mid = database.add_message(acc, "in", gmail_thread_id="T1", de_client="Letzter Preis?", status="pending",
                               de_answer="Hallo", client_lang="de", ad_price="400 €")
    from web import api_ma
    api_ma._variant_cache.clear()
    with patch("modules.tg_init_data.config") as mc, patch.object(api_ma, "telegram_bot") as mtb, \
         patch("modules.drafts.variants", return_value=RES) as dv:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mtb._claim_lock.return_value = None
        from web.app import app
        yield TestClient(app), mid, dv


def test_variants_cached_and_shape(env):
    c, mid, dv = env
    r = c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).json()
    assert [v["tone"] for v in r["variants"]] == ["hold", "minus5"] and r["dropped"][0]["tone"] == "floor"
    assert r["variants"][1] == {"tone": "minus5", "price_eur": 380, "client_text": "380 ok", "ru_text": "уступим",
                                "ru_translation": "380 ок", "error": None, "cached": False}
    r2 = c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).json()
    assert r2["variants"][0]["cached"] is True and dv.call_count == 1
    assert database.get_message(mid)["de_answer"] == "Hallo"


def test_apply_and_stale(env):
    c, mid, dv = env
    c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H())
    assert c.post(f"/api/ma/messages/{mid}/variants/minus5/apply", headers=H()).status_code == 200
    row = database.get_message(mid)
    assert row["status"] == "edited" and row["de_answer"] == "380 ok"
    assert c.post(f"/api/ma/messages/{mid}/variants/hold/apply", headers=H()).status_code == 409


@pytest.mark.parametrize("exc,code", [("suggest", 409), ("lookup", 404), ("limit", 503), ("llm", 502)])
def test_error_mapping(env, exc, code):
    c, mid, dv = env
    from modules import claude, llm
    dv.side_effect = {"suggest": claude.SuggestError("нет цены"), "lookup": LookupError("нет письма"),
                      "limit": llm.LLMLimitError("limit"), "llm": llm.LLMError("timeout")}[exc]
    r = c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H())
    assert r.status_code == code


def test_variants_refused_for_sent(env):
    c, mid, dv = env
    database.update_message(mid, status="sent", sent_at="x")
    assert c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).status_code == 409
    dv.assert_not_called()
