# Тесты LLM-слоя: история для промпта, единый call_json, ключи результатов, черновики.
# LLM замокан на уровне llm.Client — реальных вызовов нет.
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules import claude, claude_utils


def _resp(data, stop="end_turn", texts=None):
    """Фейковый ответ llm.Client().messages.create."""
    blocks = [SimpleNamespace(type="text", text=t) for t in (texts or [json.dumps(data, ensure_ascii=False)])]
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, subscription=True)
    return SimpleNamespace(content=blocks, usage=usage, stop_reason=stop)


class _FakeClient:
    """Запоминает kwargs вызова и отдаёт заготовленный ответ."""
    calls: list = []
    response = None

    def __init__(self, *a, **k):
        self.messages = self

    def create(self, **kwargs):
        _FakeClient.calls.append(kwargs)
        return _FakeClient.response


@pytest.fixture
def fake_llm():
    _FakeClient.calls = []
    with patch.object(claude_utils.llm, "Client", _FakeClient), \
         patch.object(claude_utils.config, "claude_model", return_value="claude-sonnet-4-6"), \
         patch("config.system_prompt", return_value="SYS"):
        yield _FakeClient


DRAFT = {
    "client_lang": "de", "ru_answer": "Да, в наличии", "client_answer": "Ja, noch da. MfG",
    "ru_translation": "Да, ещё есть. С ув.", "deal_summary_ru": "спрашивает наличие",
    "expected_next": "ответа", "negotiated_price_eur": 500, "client_assessment": "спрашивает",
}


# ─────────────── format_history ───────────────

def test_format_history_keeps_our_sent_reply_from_in_row():
    hist = [
        {"id": 1, "direction": "in", "de_client": "Ist das noch da?", "de_answer": "Ja, noch da.",
         "status": "sent", "created_at": "2026-10-01T10:00", "sent_at": "2026-10-01T11:00"},
        {"id": 2, "direction": "in", "de_client": "Letzter Preis?", "de_answer": "Draft nicht gesendet",
         "status": "pending", "created_at": "2026-10-01T12:00", "sent_at": ""},
    ]
    text, has_ours = claude.format_history(hist)
    assert text.splitlines() == [
        "[Клиент]: Ist das noch da?",
        "[Продавец]: Ja, noch da.",
        "[Клиент]: Letzter Preis?",
    ]
    assert has_ours


def test_format_history_out_rows_only_sent_and_ack_marked():
    hist = [
        {"id": 1, "direction": "in", "de_client": "Hallo", "de_answer": None, "status": "new",
         "created_at": "2026-10-01T10:00"},
        {"id": 2, "direction": "out", "de_client": None, "de_answer": "Guten Tag, melde mich gleich",
         "status": "sent", "is_auto_ack": True, "created_at": "2026-10-01T10:01", "sent_at": "2026-10-01T10:01"},
        {"id": 3, "direction": "out", "de_client": None, "de_answer": "nie gesendet",
         "status": "error_send_failed", "created_at": "2026-10-01T10:05"},
    ]
    text, has_ours = claude.format_history(hist)
    assert "[Продавец, авто-приветствие]: Guten Tag" in text
    assert "nie gesendet" not in text
    assert has_ours


def test_format_history_orders_reply_after_later_client_followup():
    # Ответ на письмо 1 ушёл ПОСЛЕ follow-up клиента — порядок по времени отправки
    hist = [
        {"id": 1, "direction": "in", "de_client": "A", "de_answer": "Antwort A", "status": "sent",
         "created_at": "2026-10-01T10:00", "sent_at": "2026-10-01T12:00"},
        {"id": 2, "direction": "in", "de_client": "B", "de_answer": None, "status": "new",
         "created_at": "2026-10-01T11:00"},
    ]
    text, _ = claude.format_history(hist)
    assert text.splitlines() == ["[Клиент]: A", "[Клиент]: B", "[Продавец]: Antwort A"]


def test_format_history_trims_oldest_when_too_long():
    hist = [{"id": i, "direction": "in", "de_client": f"msg{i} " + "x" * 500, "de_answer": None,
             "status": "new", "created_at": f"2026-10-01T10:{i:02d}"} for i in range(30)]
    text, _ = claude.format_history(hist, max_chars=2000)
    assert len(text) < 2600
    assert "msg29" in text and "msg0 " not in text
    assert text.startswith("(… ещё")


# ─────────────── history_for (временная БД) ───────────────

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, is_active, created_at) "
            "VALUES (1, 'test', 't@x', 'p', 1, ?)", (datetime.utcnow().isoformat(),))
    return db_mod


def test_history_for_excludes_current_by_id_and_later_rows(tmp_db):
    t = "thr-1"
    a = tmp_db.add_message(1, "in", gmail_thread_id=t, de_client="Hallo", created_at="2026-10-01T10:00")
    b = tmp_db.add_message(1, "in", gmail_thread_id=t, de_client="Hallo", created_at="2026-10-01T11:00")
    tmp_db.add_message(1, "in", gmail_thread_id=t, de_client="später", created_at="2026-10-01T12:00")
    hist = claude.history_for(t, exclude_id=b, before="2026-10-01T11:00")
    # Одинаковый текст «Hallo» у прошлого письма не теряется (раньше фильтр был по тексту)
    assert [h["id"] for h in hist] == [a]


# ─────────────── call_json ───────────────

def test_call_json_rejects_truncated(fake_llm):
    fake_llm.response = _resp(DRAFT, stop="max_tokens")
    with pytest.raises(claude_utils.LLMResponseError, match="обрезан"):
        claude.generate_draft("Hallo")


def test_call_json_rejects_missing_and_empty_fields(fake_llm):
    fake_llm.response = _resp({k: v for k, v in DRAFT.items() if k != "ru_translation"})
    with pytest.raises(claude_utils.LLMResponseError, match="ru_translation"):
        claude.generate_draft("Hallo")
    fake_llm.response = _resp({**DRAFT, "client_answer": "  "})
    with pytest.raises(claude_utils.LLMResponseError, match="client_answer"):
        claude.generate_draft("Hallo")


def test_call_json_rejects_non_json(fake_llm):
    fake_llm.response = _resp(None, texts=["Извините, не могу"])
    with pytest.raises(claude_utils.LLMResponseError, match="не JSON"):
        claude.generate_draft("Hallo")


def test_call_json_takes_json_after_narrative(fake_llm):
    fake_llm.response = _resp(None, texts=["Сейчас проверю…", "Итог: " + json.dumps(DRAFT)])
    assert claude.generate_draft("Hallo")["client_answer"] == "Ja, noch da. MfG"


def test_fast_model_without_effort_and_thinking(fake_llm):
    fake_llm.response = _resp({"translation": "Hallo"})
    claude.translate_only("Привет", target_lang="de", source_lang="ru")
    kw = fake_llm.calls[-1]
    assert "haiku" in kw["model"]
    assert "thinking" not in kw and "effort" not in kw["output_config"]
    assert "MfG" in kw["system"]  # правило прощания для немецкого


# ─────────────── результаты для потребителей ───────────────

def test_generate_draft_prompt_has_ad_brief_and_history(fake_llm):
    fake_llm.response = _resp(DRAFT)
    hist = [{"id": 1, "direction": "in", "de_client": "Preis?", "de_answer": "500 €", "status": "sent",
             "created_at": "2026-10-01T10:00", "sent_at": "2026-10-01T10:30"}]
    res = claude.generate_draft("Geht 400?", ad_title="Sitzbank Traveller", ad_price="500 €",
                                ad_description="Gebraucht, gut", history=hist,
                                brief_text="=== БРИФ ===\nМин 450", client_lang="de")
    user = fake_llm.calls[-1]["messages"][0]["content"]
    for frag in ("Sitzbank Traveller", "Gebraucht, gut", "Мин 450", "[Продавец]: 500 €", "Geht 400?",
                 "НЕ повторяй приветствие"):
        assert frag in user
    assert res["de_answer"] == res["client_answer"] == "Ja, noch da. MfG"
    assert res["deal_brief"]["summary_ru"] == "спрашивает наличие"
    assert res["deal_summary_ru"] == "спрашивает наличие"


def test_autopilot_reply_keys_for_api_and_incoming(fake_llm):
    fake_llm.response = _resp({**DRAFT, "ru_client": "Ещё есть?", "should_stop": False,
                               "stop_reason": "", "client_pressing_below_floor": False})
    res = claude.generate_autopilot_reply("Noch da?", floor_eur=400)
    # api_ma (превью) читает client_answer и плоский бриф, incoming — de_answer и deal_brief
    for key in ("client_answer", "de_answer", "ru_answer", "ru_translation", "deal_summary_ru",
                "expected_next", "negotiated_price_eur", "client_assessment", "deal_brief",
                "should_stop", "stop_reason", "used_web_search", "ru_client"):
        assert key in res
    assert fake_llm.calls[-1]["tools"]
    assert "400" in fake_llm.calls[-1]["messages"][0]["content"]


def test_regenerate_with_price_loads_context_when_not_given(fake_llm):
    fake_llm.response = _resp(DRAFT)
    row = {"id": 5, "gmail_thread_id": "t", "de_client": "400?", "ad_title": "Sitzbank",
           "ad_description": "Beschreibung", "client_lang": "de", "ru_answer": "", "de_answer": ""}
    with patch.object(claude, "draft_context", return_value=([], "=== БРИФ X ===")) as dc:
        res = claude.regenerate_with_price(row, 450)
    dc.assert_called_once()
    user = fake_llm.calls[-1]["messages"][0]["content"]
    assert "БРИФ X" in user and "Beschreibung" in user and "450€" in user
    assert res["deal_summary_ru"]  # api _apply_regenerate_result читает плоские ключи


def test_translate_with_back_single_call(fake_llm):
    fake_llm.response = _resp({"translation": "Ja, noch da. MfG", "back_translation_ru": "Да, ещё есть. С ув."})
    res = claude.translate_with_back("Да, есть", target_lang="de")
    assert res["translation"] == "Ja, noch da. MfG"
    assert res["back_translation"] == "Да, ещё есть. С ув."
    assert len(fake_llm.calls) == 1


def test_auto_ack_returns_ru_mirror(fake_llm):
    fake_llm.response = _resp({"client_lang": "de", "ack_text": "Guten Tag! MfG", "ack_text_ru": "Добрый день!"})
    res = claude.generate_auto_ack("Hans", "Hallo", 10, "за рулём")
    assert res["ack_text_ru"] == "Добрый день!"


# ─────────────── drafts ───────────────

def test_regenerate_regen_without_draft_makes_first_draft(tmp_db, fake_llm):
    from modules import drafts
    fake_llm.response = _resp(DRAFT)
    t = "thr-d"
    tmp_db.add_message(1, "in", gmail_thread_id=t, de_client="Preis?", de_answer="500 €",
                       status="sent", created_at="2026-10-01T10:00", sent_at="2026-10-01T10:30")
    mid = tmp_db.add_message(1, "in", gmail_thread_id=t, de_client="Geht 400?", ad_title="Sitzbank",
                             client_lang="de", created_at="2026-10-01T11:00")
    res = drafts.regenerate_draft(mid, "regen")
    assert res["kind"] == "generated"
    user = fake_llm.calls[-1]["messages"][0]["content"]
    assert "[Продавец]: 500 €" in user and "ТЕКУЩИЙ ЧЕРНОВИК" not in user
    row = tmp_db.get_message(mid)
    assert row["de_answer"] == "Ja, noch da. MfG" and row["status"] == "edited"
    assert json.loads(row["deal_brief_json"])["summary_ru"] == "спрашивает наличие"


def test_regenerate_refuses_sent_row(tmp_db, fake_llm):
    from modules import drafts
    mid = tmp_db.add_message(1, "in", gmail_thread_id="t2", de_client="x", de_answer="y", status="sent")
    res = drafts.regenerate_draft(mid, "regen")
    assert res["kind"] == "error"
    assert tmp_db.get_message(mid)["de_answer"] == "y"
    assert fake_llm.calls == []
