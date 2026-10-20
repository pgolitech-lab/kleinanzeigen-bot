# Тесты скрипта разбора истории: промпт, запись уроков/досье, идемпотентность.
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "mine_lessons", Path(__file__).resolve().parent.parent / "scripts" / "mine_lessons.py")
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)


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


def _person(db, email="b@r"):
    return ml.people_to_scan(None, None, email)[0]


def test_people_grouped_by_account_and_name(tmp_db):
    tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="a1@r", buyer_display_name="Jan  K",
                       de_client="x", created_at="2026-09-01T10:00")
    tmp_db.add_message(1, "in", gmail_thread_id="t2", buyer_name="a2@r", buyer_display_name="jan k",
                       de_client="y", created_at="2026-09-02T10:00")
    tmp_db.add_message(1, "in", gmail_thread_id="t3", buyer_name="noname@r", de_client="z")
    people = ml.people_to_scan(None, None, None)
    jan = next(p for p in people if p["name_key"] == "jan k")
    assert jan["emails"] == ["a1@r", "a2@r"]
    assert [t for t, _ in ml.person_threads(jan)] == ["t1", "t2"]
    assert any(p["emails"] == ["noname@r"] for p in people)


def test_prompt_has_both_sides_dates_and_sold_hint(tmp_db):
    tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="b@r", ad_title="Konsole",
                       de_client="Preis?", de_answer="200 €", status="skipped_sold", sold_price_eur=180,
                       created_at="2026-09-01T10:00", sent_at="2026-09-01T11:00")
    prompt = ml.build_prompt(ml.person_threads(_person(tmp_db)))
    assert "### T1 (Адрес A) — «Konsole»" in prompt and "продажу за 180 €" in prompt
    assert "[2026-09-01 10:00] [Клиент]: Preis?" in prompt
    assert "[2026-09-01 11:00] [Продавец]: 200 €" in prompt


def test_write_result_lessons_and_dossier(tmp_db, monkeypatch):
    mid = tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="b@r", buyer_display_name="Jan",
                             de_client="x")
    rows = tmp_db.thread_history("t1")
    saved: list[dict] = []
    dossiers: list[tuple] = []
    monkeypatch.setattr(tmp_db, "upsert_client_dossier", lambda *a: dossiers.append(a), raising=False)
    monkeypatch.setattr("modules.llm_memory.save_lesson", lambda **f: saved.append(f) or len(saved))
    person = _person(tmp_db)
    res = {"person": person, "max_id": mid, "by_ref": {"T1": ("t1", rows)}, "model": "m",
           "data": {"dossier_ru": "Купил консоль", "facts": {"outcome": "sold"}, "threads": [
               {"ref": "T1", "outcome": "sold", "final_price_eur": 180,
                "lessons": [{"situation_ru": "торг", "lesson_ru": "Держи цену"}]},
               {"ref": "T9", "outcome": "open", "final_price_eur": None, "lessons": []}]}}
    assert ml.write_result(res) == 1
    assert saved[0]["source_type"] == "history_review" and saved[0]["source_ref"] == "t1#1"
    acc, name, summary, facts, tids, emails, model = dossiers[0]
    assert (acc, name, summary, tids, emails) == (1, "Jan", "Купил консоль", ["t1"], ["b@r"])
    assert '"sold"' in facts


def test_write_result_fails_loud_when_lesson_not_saved(tmp_db, monkeypatch):
    tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="b@r", de_client="x")
    monkeypatch.setattr("modules.llm_memory.save_lesson", lambda **f: None)
    monkeypatch.setattr(tmp_db, "upsert_client_dossier", lambda *a: None, raising=False)
    res = {"person": _person(tmp_db), "max_id": 1, "by_ref": {"T1": ("t1", tmp_db.thread_history("t1"))},
           "model": "m", "data": {"dossier_ru": "d", "facts": {}, "threads": [
               {"ref": "T1", "outcome": "open", "final_price_eur": None,
                "lessons": [{"situation_ru": "s", "lesson_ru": "l"}]}]}}
    with pytest.raises(RuntimeError, match="не сохранён"):
        ml.write_result(res)
