"""«Жду до…», напоминание молчуну, быстрые ответы из справки аккаунта."""
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
    mid = database.add_message(acc, "in", gmail_thread_id="T1", de_client="Hallo", status="sent",
                               client_lang="de", ad_title="Rückbank", buyer_name="b@x")
    from web import api_ma

    def fake_twb(text, target_lang, source_lang="ru", context=None):
        return {"translation": f"[{target_lang}] {text}", "back_translation": text, "target_lang": target_lang}

    with patch("modules.tg_init_data.config") as mc, \
         patch.object(api_ma.claude, "translate_with_back", fake_twb):
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        yield TestClient(app), acc, mid


def test_wait_until_days_sets_snooze(env):
    c, acc, mid = env
    r = c.post("/api/ma/threads/T1/wait", json={"days": 3}, headers=H())
    assert r.status_code == 200 and r.json()["wait_until"]
    assert database.get_message(mid)["reminder_snooze_until"] == r.json()["wait_until"]
    assert c.post("/api/ma/threads/T1/wait", json={"until": "завтра"}, headers=H()).status_code == 400
    assert c.post("/api/ma/threads/T1/wait", headers=H()).json()["wait_until"] is None  # как раньше


def test_followup_in_client_language(env):
    c, acc, mid = env
    r = c.post("/api/ma/threads/T1/followup", headers=H()).json()
    assert r["target_lang"] == "de" and r["translated"].startswith("[de] ") and "Rückbank" in r["ru_text"]


def test_quick_replies(env):
    c, acc, mid = env
    assert c.post(f"/api/ma/messages/{mid}/quick", json={"kind": "city"}, headers=H()).status_code == 400
    info = c.post(f"/api/ma/accounts/{acc}/quick-info", json={"city": "Гамбург, Альтона"}, headers=H()).json()
    assert info["city"] == "Гамбург, Альтона" and info["hours"] == ""
    assert c.get(f"/api/ma/accounts/{acc}/quick-info", headers=H()).json()["city"] == "Гамбург, Альтона"
    r = c.post(f"/api/ma/messages/{mid}/quick", json={"kind": "city"}, headers=H()).json()
    assert r["translated"].startswith("[de] ") and "Гамбург" in r["ru_text"]
    assert c.post(f"/api/ma/messages/{mid}/quick", json={"kind": "price"}, headers=H()).status_code == 422
