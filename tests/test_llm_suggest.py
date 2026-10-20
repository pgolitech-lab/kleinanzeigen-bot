# Тесты подсказок оператору: варианты под тон, быстрые ответы, напоминание молчуну.
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules import claude, claude_utils, drafts


def _resp(data):
    usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, subscription=True)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data, ensure_ascii=False))],
                           usage=usage, stop_reason="end_turn")


class _Fake:
    calls: list = []
    response = None

    def __init__(self, *a, **k):
        self.messages = self

    def create(self, **kw):
        _Fake.calls.append(kw)
        return _Fake.response


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("INSERT INTO accounts (id, name, gmail_email, gmail_app_password, is_active, created_at) "
                     "VALUES (1, 'a', 'a@x', 'p', 1, ?)", (datetime.utcnow().isoformat(),))
    _Fake.calls = []
    with patch.object(claude_utils.llm, "Client", _Fake), \
         patch.object(claude, "smart_model", return_value="claude-sonnet-4-6"), \
         patch("config.system_prompt", return_value="SYS"):
        yield db


def _msg(db, **kw):
    fields = dict(gmail_thread_id="t", ad_id="A1", ad_title="Sitzbank", ad_price="1.000 €",
                  de_client="Geht 800?", client_lang="de", created_at="2026-10-01T10:00")
    fields.update(kw)
    return db.add_message(1, "in", **fields)


def _brief(db, min_eur):
    db.upsert_ad_brief(ad_id="A1", ad_title="Sitzbank", ad_url="", ad_price="1.000 €", brief_md="b",
                       key_facts_json=json.dumps({"price_eur": 1000, "min_acceptable_eur": min_eur}))


def test_variants_prices_by_code_and_floor_enforced(env):
    _brief(env, 900)
    mid = _msg(env)
    _Fake.response = _resp({"client_lang": "de", "variants": [
        {"tone": "hold", "price_eur": 1000, "ru_answer": "1000", "client_answer": "Der Preis bleibt 1.000 €. MfG",
         "ru_translation": "x"},
        {"tone": "minus5", "price_eur": 950, "ru_answer": "950", "client_answer": "Ich kann 950 € anbieten. MfG",
         "ru_translation": "x"},
        {"tone": "floor", "price_eur": 850, "ru_answer": "850", "client_answer": "Letztes Angebot 850 €. MfG",
         "ru_translation": "x"},
        {"tone": "decline", "price_eur": None, "ru_answer": "нет", "client_answer": "Leider nein, danke. MfG",
         "ru_translation": "x"}]})
    res = drafts.variants(mid)
    user = _Fake.calls[-1]["messages"][0]["content"]
    assert "держим цену 1000 €" in user and "до 950 €" in user and "900 €" in user
    assert [v["tone"] for v in res["variants"]] == ["hold", "minus5", "decline"]
    assert res["dropped"] == [{"tone": "floor", "reason": "цена ниже минимума 900 €"}]
    assert res["floor_eur"] == 900 and res["base_price_eur"] == 1000
    assert len(_Fake.calls) == 1


def test_variants_minus5_not_below_floor_and_floor_skipped_without_min(env):
    mid = _msg(env, ad_id=None, ad_price="100 €")
    _Fake.response = _resp({"client_lang": "de", "variants": [
        {"tone": "hold", "price_eur": 100, "ru_answer": "a", "client_answer": "Preis bleibt 100 €. MfG",
         "ru_translation": "x"}]})
    res = drafts.variants(mid, ["hold", "floor"])
    assert {"tone": "floor", "reason": "минимум цены по объявлению не задан"} in res["dropped"]
    assert [v["tone"] for v in res["variants"]] == ["hold"]


def test_quick_reply_needs_seller_fact(env):
    mid = _msg(env)
    with pytest.raises(ValueError, match="delivery"):
        drafts.quick_reply(mid, "delivery", {})
    assert _Fake.calls == []
    _Fake.response = _resp({"client_answer": "Versand mit DHL für 10 € möglich. MfG",
                            "ru_translation": "Доставка DHL за 10 € возможна."})
    res = drafts.quick_reply(mid, "delivery", {"delivery": "DHL 10 €"})
    assert res["client_answer"].startswith("Versand") and "haiku" in _Fake.calls[-1]["model"]
    assert "DHL 10 €" in _Fake.calls[-1]["messages"][0]["content"]


def test_quick_reply_rejects_wrong_language(env):
    mid = _msg(env)
    _Fake.response = _resp({"client_answer": "Да, товар ещё есть, можно забрать.", "ru_translation": "x"})
    with pytest.raises(claude_utils.LLMResponseError):
        drafts.quick_reply(mid, "available")


def test_followup_ping_only_when_we_wrote_last(env):
    _msg(env, de_answer="Preis 1.000 €", status="sent", sent_at="2026-10-01T11:00")
    _Fake.response = _resp({"ru_answer": "Актуально?", "client_answer": "Ist das noch aktuell? MfG",
                            "ru_translation": "Это ещё актуально?"})
    res = drafts.followup_ping("t")
    assert res["client_answer"].startswith("Ist") and res["days_silent"] > 1
    assert "[Продавец]: Preis 1.000 €" in _Fake.calls[-1]["messages"][0]["content"]
    _msg(env, de_client="Hallo?", created_at="2026-10-02T10:00")
    with pytest.raises(ValueError, match="последним писал клиент"):
        drafts.followup_ping("t")
