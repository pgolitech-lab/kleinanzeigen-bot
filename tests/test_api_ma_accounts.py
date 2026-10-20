"""Экран «Аккаунты»: список без паролей, правка (пароли — только запись), test-imap, ручной бэкап."""
from __future__ import annotations
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}
SECRET_GMAIL = "abcd efgh ijkl mnop"
SECRET_KA = "Ka-Secret-123"


def _acc(**over):
    d = {"id": 3, "name": "main", "is_active": 1, "gmail_email": "us@gmail.com",
         "gmail_app_password": "abcdefghijklmnop", "kleinanzeigen_email": "ka@x.de",
         "kleinanzeigen_password": SECRET_KA, "auto_ack_enabled": 0, "created_at": "2026-01-01"}
    d.update(over)
    r = MagicMock()
    r.__getitem__.side_effect = d.__getitem__
    r.keys.return_value = d.keys()
    return r


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.list_accounts.return_value = [_acc()]
        mdb.get_account.return_value = _acc()
        from web.app import app
        yield TestClient(app), mdb


H = lambda: {"X-Telegram-Init-Data": make_init_data(TEST_USER)}  # noqa: E731


def test_list_has_no_passwords(client):
    c, mdb = client
    res = c.get("/api/ma/accounts", headers=H())
    text = res.text
    assert "abcdefghijklmnop" not in text and SECRET_KA not in text
    a = res.json()["accounts"][0]
    assert a["gmail_app_password_set"] is True and a["kleinanzeigen_password_set"] is True
    assert a["active"] is True and a["auto_ack_enabled"] is False and a["gmail_email"] == "us@gmail.com"


def test_update_flags_and_passwords_write_only(client, caplog):
    c, mdb = client
    res = c.post("/api/ma/accounts/3", headers=H(), json={
        "is_active": False, "auto_ack_enabled": True,
        "gmail_app_password": SECRET_GMAIL, "kleinanzeigen_password": "new-ka"})
    assert res.status_code == 200
    kwargs = mdb.update_account.call_args.kwargs
    assert kwargs == {"is_active": 0, "auto_ack_enabled": 1,
                      "gmail_app_password": "abcdefghijklmnop", "kleinanzeigen_password": "new-ka"}
    assert "new-ka" not in res.text and "abcdefghijklmnop" not in res.text
    assert "new-ka" not in caplog.text and "abcd" not in caplog.text


def test_update_empty_password_keeps_old(client):
    c, mdb = client
    res = c.post("/api/ma/accounts/3", headers=H(), json={"gmail_app_password": "  ", "is_active": True})
    assert res.status_code == 200
    assert mdb.update_account.call_args.kwargs == {"is_active": 1}


def test_update_nothing_400_and_unknown_404(client):
    c, mdb = client
    assert c.post("/api/ma/accounts/3", headers=H(), json={}).status_code == 400
    mdb.get_account.return_value = None
    assert c.post("/api/ma/accounts/9", headers=H(), json={"is_active": True}).status_code == 404


def test_test_imap_uses_new_or_saved_password(client):
    c, mdb = client
    with patch("modules.gmail.test_connection", return_value=(True, "IMAP подключение OK")) as tc:
        r1 = c.post("/api/ma/accounts/3/test-imap", headers=H(), json={})
        r2 = c.post("/api/ma/accounts/3/test-imap", headers=H(), json={"gmail_app_password": "zz yy"})
    assert r1.json() == {"ok": True, "message": "IMAP подключение OK"}
    assert tc.call_args_list[0].args == ("us@gmail.com", "abcdefghijklmnop")
    assert tc.call_args_list[1].args == ("us@gmail.com", "zzyy")
    mdb.update_account.assert_not_called()  # проверка ничего не сохраняет


def test_backup_run_once_and_status(client, tmp_path, monkeypatch):
    c, mdb = client
    from web import api_ma
    marker = tmp_path / "last_backup_ok"
    marker.write_text(f"{int(time.time()) - 120}\nkleinanzeigen.db.zst\nonedrive\n")
    monkeypatch.setattr(api_ma, "BACKUP_MARKER", marker)
    started = []
    monkeypatch.setattr(api_ma.threading, "Thread",
                        lambda **kw: MagicMock(start=lambda: started.append(kw["target"])))
    api_ma._backup_state.update(running=False, exit_code=None)
    r1 = c.post("/api/ma/backup/run", headers=H()).json()
    r2 = c.post("/api/ma/backup/run", headers=H()).json()
    assert r1["started"] is True and r2["started"] is False and len(started) == 1
    assert 119 <= r1["backup_age_sec"] <= 125
    # завершение фона
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ok", stderr="")):
        api_ma._run_backup()
    st = c.get("/api/ma/backup/status", headers=H()).json()
    assert st["running"] is False and st["ok"] is True and st["exit_code"] == 0


def test_backup_age_none_without_marker(tmp_path, monkeypatch):
    from web import api_ma
    monkeypatch.setattr(api_ma, "BACKUP_MARKER", tmp_path / "nope")
    assert api_ma.backup_age_sec() is None
