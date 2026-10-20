"""Чипы цены: варианты параллельно, кэш по версии треда, применение выбранного."""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database
from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}
H = lambda: {"X-Telegram-Init-Data": make_init_data(TEST_USER)}  # noqa: E731


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    acc = database.add_account("a", "a@gmail.com", "pw")
    mid = database.add_message(acc, "in", gmail_thread_id="T1", de_client="Letzter Preis?", status="pending",
                               de_answer="Hallo", client_lang="de", ad_price="400 €")
    from web import api_ma
    api_ma._variant_cache.clear()
    calls = []

    def fake_regen(row, instruction, **kw):
        calls.append(instruction)
        return {"client_answer": f"DE:{instruction[:12]}", "ru_answer": "RU", "ru_translation": "RT",
                "deal_brief": {"summary_ru": "торг"}, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0}

    with patch("modules.tg_init_data.config") as mc, \
         patch.object(api_ma.claude, "regenerate_with_instruction", fake_regen), \
         patch.object(api_ma, "telegram_bot") as mtb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mtb._claim_lock.return_value = None
        from web.app import app
        yield TestClient(app), mid, calls


def test_variants_prices_and_cache(env):
    c, mid, calls = env
    v = c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).json()["variants"]
    by = {x["tone"]: x for x in v}
    assert [x["tone"] for x in v] == ["hold", "minus5", "floor", "decline"]
    assert by["hold"]["price_eur"] == 400 and by["minus5"]["price_eur"] == 380
    assert by["floor"]["price_eur"] == 360  # −10 % по умолчанию
    assert by["decline"]["price_eur"] is None and all(x["error"] is None for x in v)
    assert len(calls) == 4
    v2 = c.post(f"/api/ma/messages/{mid}/variants", json={"tones": ["hold"]}, headers=H()).json()["variants"]
    assert v2[0]["cached"] is True and len(calls) == 4
    assert database.get_message(mid)["de_answer"] == "Hallo"  # варианты в БД не пишутся


def test_apply_variant_and_stale(env):
    c, mid, calls = env
    c.post(f"/api/ma/messages/{mid}/variants", json={"tones": ["minus5", "hold"]}, headers=H())
    r = c.post(f"/api/ma/messages/{mid}/variants/minus5/apply", headers=H())
    assert r.status_code == 200
    row = database.get_message(mid)
    assert row["status"] == "edited" and row["de_answer"].startswith("DE:")
    # тред изменился → старый вариант не применяется
    assert c.post(f"/api/ma/messages/{mid}/variants/hold/apply", headers=H()).status_code == 409


def test_variants_refused_for_sent(env):
    c, mid, calls = env
    database.update_message(mid, status="sent", sent_at="x")
    assert c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).status_code == 409
    assert calls == []


def test_minus5_never_below_min_price(env):
    c, mid, calls = env
    database.update_message(mid, ad_id="AD1")
    from modules.db_sales import set_ad_min_price
    set_ad_min_price("AD1", 390)
    by = {x["tone"]: x for x in c.post(f"/api/ma/messages/{mid}/variants", json={}, headers=H()).json()["variants"]}
    assert by["floor"]["price_eur"] == 390 and by["minus5"]["price_eur"] == 390
