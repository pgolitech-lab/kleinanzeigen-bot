# Тесты памяти LLM: уроки в промпте, память о покупателе, урок из правки оператора.
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from modules import llm_memory


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


def test_format_lessons_prefers_lesson_text_and_skips_noop_pairs():
    text = llm_memory.format_lessons([
        {"lesson_ru": "Не обещай доставку без цены", "client_situation_ru": "просит доставку"},
        {"bad_draft_ru": "одно", "good_answer_ru": "одно"},          # без исправления — пропуск
        {"bad_draft_ru": "Цена 1000", "good_answer_ru": "Цена 1200, окончательно", "client_situation_ru": "торг"},
    ])
    assert "Не обещай доставку без цены" in text
    assert "Оператор отправил (так правильно): Цена 1200" in text
    assert text.count("\n- ") == 2  # ровно два пункта
    assert llm_memory.format_lessons([]) == ""


def test_client_memory_includes_other_threads_of_buyer(tmp_db):
    tmp_db.add_message(1, "in", gmail_thread_id="old", buyer_name="b@relay", ad_title="Sitzbank Traveller",
                       de_client="Preis?", de_answer="1100 €", status="skipped_sold", sold_price_eur=1050,
                       created_at="2026-09-01T10:00")
    cur = tmp_db.add_message(1, "in", gmail_thread_id="new", buyer_name="b@relay", ad_title="Konsole",
                             de_client="Hallo", created_at="2026-10-01T10:00")
    text = llm_memory.client_memory_text(tmp_db.get_message(cur))
    assert "Sitzbank Traveller" in text and "1050" in text
    assert "Konsole" not in text  # текущий тред не дублируем


def test_client_memory_empty_for_new_buyer(tmp_db):
    cur = tmp_db.add_message(1, "in", gmail_thread_id="x", buyer_name="new@relay", de_client="Hi")
    assert llm_memory.client_memory_text(tmp_db.get_message(cur)) == ""


def test_save_lesson_drops_unknown_columns(tmp_db):
    llm_memory._COLS_CACHE.clear()
    lid = llm_memory.save_lesson(message_id=1, lesson_ru_future_col="x", good_answer_ru="ok")
    with tmp_db.get_conn() as conn:
        row = conn.execute("SELECT good_answer_ru FROM lessons WHERE id=?", (lid,)).fetchone()
    assert row[0] == "ok"


def _migrate_lessons(db):
    """Колонки уроков, которые добавляет миграция модуля db (в тесте — вручную, если их нет)."""
    with db.get_conn() as conn:
        have = {r[1] for r in conn.execute("PRAGMA table_info(lessons)")}
        for col in ("lesson_ru", "source", "gmail_thread_id", "buyer_email", "outcome", "category"):
            if col not in have:
                conn.execute(f"ALTER TABLE lessons ADD COLUMN {col} TEXT")
    llm_memory._COLS_CACHE.clear()


def test_save_lesson_refuses_without_lesson_column(tmp_db):
    with tmp_db.get_conn() as conn:
        has = "lesson_ru" in {r[1] for r in conn.execute("PRAGMA table_info(lessons)")}
    if has:
        pytest.skip("миграция db уже добавила lesson_ru")
    llm_memory._COLS_CACHE.clear()
    assert llm_memory.save_lesson(message_id=1, lesson_ru="урок") is None


def test_learn_from_sent_only_when_operator_rewrote(tmp_db):
    _migrate_lessons(tmp_db)
    mid = tmp_db.add_message(1, "in", gmail_thread_id="t", de_client="Letzter Preis?",
                             de_answer="Der Preis ist 1200 € fest. MfG", status="sent")
    with patch("modules.claude.extract_operator_lesson",
               return_value={"lesson_ru": "Держи цену, не предлагай скидку первым", "situation_ru": "торг"}) as ex:
        # почти тот же текст → урока нет, LLM не зовём
        assert llm_memory.learn_from_sent(mid, llm_draft_client="Der Preis ist 1200 € fest. MfG") is None
        ex.assert_not_called()
        lid = llm_memory.learn_from_sent(mid, llm_draft_client="Ich kann Ihnen 1000 € anbieten, passt das?")
    assert lid
    with tmp_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
    assert row["good_answer_de"].startswith("Der Preis") and row["bad_draft_de"].startswith("Ich kann")
    assert row["lesson_ru"].startswith("Держи цену") and row["source"] == "operator_edit"


def test_autopilot_derives_memory_from_history():
    from modules import claude
    hist = [{"id": 1, "direction": "in", "de_client": "Hallo", "status": "new", "buyer_name": "b@relay",
             "gmail_thread_id": "t", "account_id": 1}]
    with patch.object(llm_memory, "client_memory_text", return_value="=== ЧТО МЫ ЗНАЕМ ===\nкупил раньше") as cm, \
         patch("config.system_prompt", return_value="SYS"), \
         patch.object(claude, "smart_model", return_value="claude-sonnet-4-6"), \
         patch.object(claude, "call_json", side_effect=RuntimeError("stop")) as cj:
        with pytest.raises(RuntimeError):
            claude.generate_autopilot_reply("Noch da?", history=hist, floor_eur=100)
    cm.assert_called_once()
    assert "купил раньше" in cj.call_args.kwargs["user"]


def test_client_memory_marks_probable_same_person(tmp_db):
    tmp_db.add_message(1, "in", gmail_thread_id="other", buyer_name="x1@relay", buyer_display_name="Jan K",
                       ad_title="Konsole", de_client="Preis?", created_at="2026-09-01T10:00")
    cur = tmp_db.add_message(1, "in", gmail_thread_id="now", buyer_name="x2@relay", buyer_display_name="Jan K",
                             ad_title="Sitzbank", de_client="Hallo", created_at="2026-10-01T10:00")
    text = llm_memory.client_memory_text(tmp_db.get_message(cur))
    assert "ВОЗМОЖНО тот же человек" in text and "Konsole" in text
