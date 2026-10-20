"""Отложенная отправка: очередь, отмена, отправка по таймеру со всеми guard'ами, переживает рестарт."""
from __future__ import annotations
import json
import time
from unittest.mock import MagicMock, patch

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
    mid = database.add_message(acc, "in", gmail_thread_id="T1", de_client="Hallo", status="pending",
                               de_answer="Guten Tag, MfG", client_lang="de")
    from web import api_ma
    sent = []

    def fake_send_one(msg_id):
        sent.append(msg_id)
        database.update_message(msg_id, status="sent", sent_at="2026-10-20T10:00:00")
        return {"kind": "sent", "message": "ok"}

    with patch("modules.tg_init_data.config") as mc, patch.object(api_ma, "telegram_bot") as mtb, \
         patch.object(api_ma.scheduler, "send_one", fake_send_one), \
         patch.object(api_ma, "_LEARN_POOL", MagicMock()):
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mtb._claim_lock.return_value = None
        api_ma._send_queue.clear()
        from web.app import app
        yield TestClient(app), mid, sent, api_ma, mtb
    for job in api_ma._send_queue.values():
        job["timer"].cancel()
    api_ma._send_queue.clear()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_delayed_send_fires(env):
    c, mid, sent, api_ma, _ = env
    r = c.post(f"/api/ma/messages/{mid}/send", json={"delay_sec": 1}, headers=H()).json()
    assert r["queued"] is True and r["job_id"] == mid and r["status"] == "send_queued"
    assert database.get_message(mid)["status"] == "send_queued"
    assert json.loads(database.get_setting("ma_send_queue"))[str(mid)]["prev"] == "pending"
    # пока в очереди — не править и не слать второй раз
    assert c.post(f"/api/ma/messages/{mid}/edit-de", json={"text": "x"}, headers=H()).status_code == 409
    assert c.post(f"/api/ma/messages/{mid}/send", json={}, headers=H()).status_code == 409
    assert _wait(lambda: sent == [mid])
    assert database.get_message(mid)["status"] == "sent"
    assert json.loads(database.get_setting("ma_send_queue")) == {}


def test_cancel_returns_to_draft(env):
    c, mid, sent, api_ma, _ = env
    c.post(f"/api/ma/messages/{mid}/send", json={"delay_sec": 2}, headers=H())
    r = c.post(f"/api/ma/send/{mid}/cancel", headers=H())
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert database.get_message(mid)["status"] == "pending"
    time.sleep(2.3)
    assert sent == []
    assert c.post(f"/api/ma/send/{mid}/cancel", headers=H()).status_code == 409


def test_queued_send_keeps_language_guard(env):
    c, mid, sent, api_ma, _ = env
    database.update_message(mid, de_answer="Здравствуйте, ещё есть")
    assert c.post(f"/api/ma/messages/{mid}/send", json={"delay_sec": 1}, headers=H()).status_code == 409
    assert database.get_message(mid)["status"] == "pending"


def test_resume_after_restart(env):
    c, mid, sent, api_ma, mtb = env
    database.update_message(mid, status="send_queued")
    database.set_setting("ma_send_queue", json.dumps({str(mid): {"due": time.time() + 0.3, "prev": "edited",
                                                                   "actor": "@pgtest#999"}}))
    api_ma.resume_send_queue()
    assert _wait(lambda: sent == [mid])


def test_resume_long_overdue_returns_to_draft(env):
    c, mid, sent, api_ma, mtb = env
    database.update_message(mid, status="send_queued")
    database.set_setting("ma_send_queue", json.dumps({str(mid): {"due": time.time() - 3600, "prev": "edited",
                                                                   "actor": "@a"}}))
    api_ma.resume_send_queue()
    assert database.get_message(mid)["status"] == "edited" and sent == []
    mtb.run_in_background.assert_called_once()  # операторам сообщено


def test_approved_queued_as_edited_no_double_send(env):
    """Из approved в очередь встаём как edited — джоб send_approved_replies не подхватит дубль."""
    c, mid, sent, api_ma, _ = env
    database.update_message(mid, status="approved")
    c.post(f"/api/ma/messages/{mid}/send", json={"delay_sec": 5}, headers=H())
    assert api_ma._send_queue[mid]["prev"] == "edited"
    c.post(f"/api/ma/send/{mid}/cancel", headers=H())
    assert database.get_message(mid)["status"] == "edited"


def test_fire_failure_returns_to_draft_and_alerts(env):
    c, mid, sent, api_ma, mtb = env
    c.post(f"/api/ma/messages/{mid}/send", json={"delay_sec": 30}, headers=H())
    api_ma._send_queue[mid]["timer"].cancel()
    with patch.object(api_ma, "_persist_send_queue", side_effect=RuntimeError("database is locked")):
        api_ma._fire_queued_send(mid)
    assert database.get_message(mid)["status"] == "pending" and sent == []
    mtb.run_in_background.assert_called()  # оператору сообщено
