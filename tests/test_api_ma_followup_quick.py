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

    with patch("modules.tg_init_data.config") as mc:
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


def test_followup_via_llm(env):
    c, acc, mid = env
    with patch("modules.drafts.followup_ping", return_value={
            "ru_answer": "Напомнить", "client_answer": "Noch Interesse?", "ru_translation": "Ещё интересно?",
            "client_lang": "de", "days_silent": 3.0, "anchor_msg_id": mid}) as fp:
        r = c.post("/api/ma/threads/T1/followup", headers=H()).json()
    fp.assert_called_once_with("T1")
    assert r == {"translated": "Noch Interesse?", "back_ru": "Ещё интересно?", "target_lang": "de",
                 "ru_text": "Напомнить", "days_silent": 3.0, "anchor_msg_id": mid}
    from modules import claude
    with patch("modules.drafts.followup_ping", side_effect=claude.SuggestError("последним писал клиент")):
        assert c.post("/api/ma/threads/T1/followup", headers=H()).status_code == 409


def test_quick_replies_via_llm(env):
    c, acc, mid = env
    info = c.post(f"/api/ma/accounts/{acc}/quick-info", json={"pickup": "Köln, вечером"}, headers=H()).json()
    assert info == {"delivery": "", "pickup": "Köln, вечером", "payment": ""}
    with patch("modules.drafts.quick_reply", return_value={"client_answer": "Abholung in Köln", "ru_translation":
                                                          "Самовывоз в Кёльне", "client_lang": "de",
                                                          "kind": "pickup"}) as qr:
        r = c.post(f"/api/ma/messages/{mid}/quick", json={"kind": "pickup"}, headers=H()).json()
    assert qr.call_args.args == (mid, "pickup", {"delivery": "", "pickup": "Köln, вечером", "payment": ""})
    assert r["translated"] == "Abholung in Köln" and r["kind"] == "pickup"
    assert c.post(f"/api/ma/messages/{mid}/quick", json={"kind": "city"}, headers=H()).status_code == 422
