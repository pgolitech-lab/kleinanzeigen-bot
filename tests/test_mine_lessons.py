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


def test_prompt_has_both_sides_dates_and_sold_hint(tmp_db):
    tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="b@r", ad_title="Konsole",
                       de_client="Preis?", de_answer="200 €", status="skipped_sold", sold_price_eur=180,
                       created_at="2026-09-01T10:00", sent_at="2026-09-01T11:00")
    prompt = ml.build_prompt(ml.buyer_threads("b@r"))
    assert "### T1 — «Konsole»" in prompt and "продажу за 180 €" in prompt
    assert "[2026-09-01 10:00] [Клиент]: Preis?" in prompt
    assert "[2026-09-01 11:00] [Продавец]: 200 €" in prompt


def test_write_result_idempotent(tmp_db, monkeypatch):
    mid = tmp_db.add_message(1, "in", gmail_thread_id="t1", buyer_name="b@r", de_client="x")
    rows = tmp_db.thread_history("t1")
    done: set[str] = set()
    saved: list[dict] = []
    dossiers: list[tuple] = []
    monkeypatch.setattr(tmp_db, "has_mined_lessons", lambda tid: tid in done, raising=False)
    monkeypatch.setattr(tmp_db, "set_client_dossier", lambda *a: dossiers.append(a), raising=False)
    monkeypatch.setattr("modules.llm_memory.save_lesson", lambda **f: saved.append(f) or done.add(f["gmail_thread_id"]))
    res = {"buyer": "b@r", "max_id": mid, "by_ref": {"T1": ("t1", rows)},
           "data": {"dossier_ru": "Купил консоль", "threads": [
               {"ref": "T1", "outcome": "sold", "final_price_eur": 180,
                "lessons": [{"situation_ru": "торг", "lesson_ru": "Держи цену"}]},
               {"ref": "T9", "outcome": "open", "final_price_eur": None, "lessons": []}]}}
    assert ml.write_result(res) == (1, True)
    assert saved[0]["source"] == "mined" and saved[0]["message_id"] == mid
    # повтор: тред уже разобран → уроков не добавляем
    assert ml.write_result(res)[0] == 0
    assert dossiers[0] == ("b@r", "Купил консоль", mid)
