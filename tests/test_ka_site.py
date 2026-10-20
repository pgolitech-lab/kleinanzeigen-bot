"""Чтение переписки с сайта Kleinanzeigen: парсинг, вход-препятствия, синхронизация.

Без браузера и сети: сессия сайта подменяется фейком, Gmail — monkeypatch.
Фикстура conversation.json — синтетическая (формат messagebox API); login_identifier.html —
реальная страница входа Auth0 (state-токен вырезан)."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from modules import ka_site, ka_site_sync
from modules.ka_site import KaBlocked, KaFormatError, KaNotFound

FIX = Path(__file__).parent / "fixtures" / "ka_site"
CONV = "49sml:2cxlxdm:2pxg5bw9w"


def _conv_json() -> dict:
    return json.loads((FIX / "conversation.json").read_text())


# --- парсинг -------------------------------------------------------------------------

def test_parse_conversation_fixture():
    msgs = ka_site.parse_conversation(_conv_json(), CONV)
    assert [m["external_id"] for m in msgs] == [f"{CONV}:m{i}" for i in (1, 2, 3, 4)]  # m5 без текста
    assert [m["ours"] for m in msgs] == [False, True, False, True]
    assert [m["sender"] for m in msgs] == ["buyer", "seller", "buyer", "seller"]
    assert msgs[1]["sent_at"] == "2026-10-19T08:30:00"  # +0200 → UTC
    assert msgs[3]["text"] == "60 geht, darunter nicht.\nViele Grüße"


def test_parse_conversation_we_are_buyer_and_epoch_ms():
    data = {"role": "Buyer", "messages": [
        {"id": 7, "boundness": "OUTBOUND", "receivedDate": 1760860800000, "text": "Hi"}]}
    (m,) = ka_site.parse_conversation(data, "a1:b2")
    assert (m["sender"], m["ours"], m["sent_at"]) == ("buyer", True, "2025-10-19T08:00:00")


def test_parse_conversation_sorts_by_time():
    data = {"messages": [
        {"messageId": "b", "boundness": "INBOUND", "receivedDate": "2026-01-02T10:00:00Z", "text": "2"},
        {"messageId": "a", "boundness": "OUTBOUND", "receivedDate": "2026-01-01T10:00:00Z", "text": "1"}]}
    assert [m["text"] for m in ka_site.parse_conversation(data, "c1:d2")] == ["1", "2"]


@pytest.mark.parametrize("data", [
    [], {"conversations": []},
    {"messages": [{"messageId": "1", "receivedDate": "2026-01-01T00:00:00Z", "text": "x"}]},
    {"messages": [{"messageId": "1", "boundness": "INBOUND", "text": "x"}]},
    {"messages": [{"messageId": "1", "boundness": "INBOUND", "receivedDate": "gestern", "text": "x"}]},
])
def test_parse_conversation_unknown_format_raises(data):
    with pytest.raises(KaFormatError):
        ka_site.parse_conversation(data, "c1:d2")


def test_conversation_id_validation():
    assert ka_site.is_valid_conversation_id(CONV)
    for bad in ("", None, "abc", "a:b/../x", "49sml:2cx?x=1", "x" * 30 + ":y"):
        assert not ka_site.is_valid_conversation_id(bad)


def test_user_id_from_token():
    def jwt(payload):
        b = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"Bearer h.{b}.s"
    assert ka_site.user_id_from_token(jwt({"userId": 12345})) == "12345"
    assert ka_site.user_id_from_token(jwt({"sub": "auth0|987"})) == "987"
    assert ka_site.user_id_from_token("Bearer nope") is None
    assert ka_site.user_id_from_token("") is None


def test_detect_login_obstacle():
    real = (FIX / "login_identifier.html").read_text()
    assert ka_site.detect_login_obstacle(
        "https://login.kleinanzeigen.de/u/login/identifier?state=X", real) is None
    assert ka_site.detect_login_obstacle("https://login.kleinanzeigen.de/u/mfa-email-challenge", "") == "mfa"
    assert ka_site.detect_login_obstacle("u", '<form><input id="code" name="code"></form>') == "mfa"
    assert ka_site.detect_login_obstacle(
        "u", '<iframe src="https://client-api.arkoselabs.com/fc/x"></iframe>') == "captcha"
    assert ka_site.detect_login_obstacle(
        "u", '<span id="error-element-password">Falsches Passwort</span>') == "bad_credentials"


def test_cookies_to_storage_state():
    st = ka_site.cookies_to_storage_state([
        {"name": "a", "value": "1", "domain": ".kleinanzeigen.de", "expirationDate": 2e9,
         "sameSite": "no_restriction", "secure": True, "httpOnly": True},
        {"name": "x", "value": "2", "domain": ".google.com"}])
    assert [c["name"] for c in st["cookies"]] == ["a"]
    assert st["cookies"][0]["sameSite"] == "None"
    with pytest.raises(ValueError):
        ka_site.cookies_to_storage_state([{"name": "x", "value": "2", "domain": "g.com"}])


# --- синхронизация -------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "q.db")
    db_mod.init_db()
    sdir = tmp_path / "state"
    monkeypatch.setattr(ka_site, "STATE_DIR", sdir)
    monkeypatch.setattr(ka_site_sync, "STATE_FILE", sdir / "ka_site_state.json")
    monkeypatch.setattr(ka_site_sync, "LOCK_FILE", sdir / "ka_site.lock")
    notes: list[str] = []
    from modules import telegram_bot
    monkeypatch.setattr(telegram_bot, "notify", lambda text, *a, **k: notes.append(text) or {})
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, kleinanzeigen_email, "
            "kleinanzeigen_password, is_active, created_at) "
            "VALUES (3, 'acc', 'g@x', 'gp', 'k@x', 'kp', 1, '2026-01-01')")
    return db_mod, notes


def _thread(db, tid="t1", conv=CONV, at=None, gmid="<m1@x>"):
    at = at or (datetime.utcnow() - timedelta(hours=1)).isoformat()
    return db.add_message(3, "in", status="pending", gmail_thread_id=tid, de_client="Letzter Preis 50?",
                          buyer_name="b@relay", ad_id="ad1", client_lang="de",
                          ka_conversation_id=conv, gmail_message_id=gmid, created_at=at)


class FakeSession:
    def __init__(self, convs=None, raise_on_get=None, calls=None):
        self.convs = convs or {}
        self.raise_on_get = raise_on_get
        self.calls = calls if calls is not None else []
        self.saved = False

    def __call__(self, account, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_conversation(self, cid):
        self.calls.append(cid)
        if self.raise_on_get:
            raise self.raise_on_get
        if cid not in self.convs:
            raise KaNotFound(cid)
        return self.convs[cid]

    def save_state(self):
        self.saved = True


def _sync(**kw):
    kw.setdefault("translate", False)
    kw.setdefault("sleep", lambda s: None)
    return ka_site_sync.sync_unanswered(**kw)


def test_sync_imports_our_site_replies_and_is_idempotent(env):
    db, _ = env
    _thread(db, at="2026-10-19T09:00:00")  # = m3 (11:00 Берлин), наш m4 позже
    fake = FakeSession({CONV: _conv_json()})
    rep = _sync(session_factory=fake)
    assert rep["imported"] == 2 and not rep["errors"] and fake.saved
    outs = [r for r in db.thread_history("t1") if r["status"] == "sent_external"]
    assert [r["de_answer"] for r in outs] == ["Hallo, ja, noch da.\n\nMfG", "60 geht, darunter nicht.\nViele Grüße"]
    assert outs[0]["sent_at"] == "2026-10-19T08:30:00" and outs[0]["external_id"] == f"{CONV}:m2"
    # повтор: тред теперь «отвечен» (последнее событие — наш ответ) → на сайт не идём
    fake2 = FakeSession({CONV: _conv_json()})
    rep2 = _sync(session_factory=fake2, force=True)
    assert rep2["threads"] == 0 and fake2.calls == []


def test_sync_skips_reply_already_sent_by_mail(env):
    db, _ = env
    _thread(db)
    # бот отправил m2 по почте (тот же текст, Kleinanzeigen срезал «MfG»-хвост? — нет, точный)
    db.add_message(3, "out", status="sent", gmail_thread_id="t1",
                   de_answer="Hallo,  ja, noch da.\n\nMfG", sent_at="2026-10-19T08:29:00")
    # и m4 — нечётко: на сайте без подписи, отправлен в пределах окна
    db.add_message(3, "out", status="sent", gmail_thread_id="t1",
                   de_answer="60 geht, darunter nicht.\nViele Grüße\nMax Mustermann",
                   sent_at="2026-10-19T10:14:00")
    _thread(db, tid="t1", conv=None, gmid="<m3@x>")  # свежий входящий → тред снова «ждёт нас»
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}))
    assert rep["known"] == 2 and rep["imported"] == 0


def test_recheck_interval_per_thread(env):
    db, _ = env
    _thread(db)  # входящее свежее наших реплик → тред остаётся «ждёт нас»
    _sync(session_factory=FakeSession({CONV: _conv_json()}))
    st = ka_site_sync._load_state()
    st["accounts"]["3"]["last_run_at"] = "2000-01-01T00:00:00"  # интервал аккаунта прошёл
    ka_site_sync._save_state(st)
    calls: list = []
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}, calls=calls))
    assert rep["accounts"][3]["skipped"] == "nothing_due" and calls == []
    _thread(db, gmid="<new@x>")  # новое письмо покупателя → проверяем сразу
    st = ka_site_sync._load_state()
    st["accounts"]["3"]["last_run_at"] = "2000-01-01T00:00:00"
    ka_site_sync._save_state(st)
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}, calls=calls))
    assert calls == [CONV] and rep["already"] == 0 and rep["known"] == 2


def test_dry_run_writes_nothing(env):
    db, _ = env
    _thread(db)
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}), dry_run=True)
    assert rep["imported"] == 0 and len(rep["would_import"]) == 2
    assert not [r for r in db.thread_history("t1") if r["status"] == "sent_external"]


def test_gmail_backfill_of_conversation_id(env, monkeypatch):
    db, _ = env
    mid = _thread(db, conv=None, gmid="<old@x>")
    seen = {}

    def fake_gmail(em, pw, mids):
        seen["args"] = (em, pw, mids)
        return {"<old@x>": CONV}
    monkeypatch.setattr(ka_site_sync, "conversation_ids_from_gmail", fake_gmail)
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}))
    assert seen["args"] == ("g@x", "gp", ["<old@x>"])
    assert rep["conv_backfilled"] == 1 and rep["imported"] == 2
    assert db.get_message(mid)["ka_conversation_id"] == CONV


def test_gmail_backfill_dry_run_does_not_store(env, monkeypatch):
    db, _ = env
    mid = _thread(db, conv=None, gmid="<old@x>")
    monkeypatch.setattr(ka_site_sync, "conversation_ids_from_gmail", lambda *a: {"<old@x>": CONV})
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}), dry_run=True)
    assert rep["conv_backfilled"] == 1 and db.get_message(mid)["ka_conversation_id"] is None


def test_gmail_error_is_reported_not_silent(env, monkeypatch):
    db, _ = env
    _thread(db, conv=None)

    def boom(*a):
        raise OSError("imap down")
    monkeypatch.setattr(ka_site_sync, "conversation_ids_from_gmail", boom)
    rep = _sync(session_factory=FakeSession())
    assert rep["no_conv_id"] == 1 and rep["errors"] and "imap down" in rep["errors"][0]


def test_block_on_captcha_notifies_once_and_stops(env):
    db, notes = env
    _thread(db)
    rep = _sync(session_factory=FakeSession(raise_on_get=KaBlocked("captcha")))
    assert rep["accounts"][3]["skipped"] == "blocked:captcha" and len(notes) == 1
    assert "капчу" in notes[0]
    calls: list = []
    rep2 = _sync(session_factory=FakeSession({CONV: _conv_json()}, calls=calls), force=True)
    assert rep2["accounts"][3]["skipped"] == "blocked:captcha" and calls == [] and len(notes) == 1


def test_bad_credentials_block_lifts_on_password_change(env):
    db, notes = env
    _thread(db)
    _sync(session_factory=FakeSession(raise_on_get=KaBlocked("bad_credentials")))
    assert ka_site_sync.block_status(3, ("k@x", "kp")) == "bad_credentials"
    assert ka_site_sync.block_status(3, ("k@x", "new")) is None
    db.update_account(3, kleinanzeigen_password="new")
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}), force=True)
    assert rep["imported"] == 2


def test_captcha_block_expires(env):
    state = {}
    ka_site_sync._set_block(state, 3, "captcha", ("k", "p"))
    assert ka_site_sync.block_status(3, ("k", "p"), state) == "captcha"
    state["accounts"]["3"]["blocked_until"] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    assert ka_site_sync.block_status(3, ("k", "p"), state) is None


def test_interval_between_scheduled_runs(env):
    db, _ = env
    _thread(db)
    ka_site_sync._save_state({"accounts": {"3": {"last_run_at": datetime.utcnow().isoformat()}}})
    calls: list = []
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}, calls=calls))
    assert rep["accounts"][3]["skipped"] == "interval" and calls == []


def test_not_found_and_format_error(env):
    db, _ = env
    _thread(db)
    rep = _sync(session_factory=FakeSession({}))
    assert rep["not_found"] == 1 and not rep["errors"]
    rep2 = _sync(session_factory=FakeSession({CONV: {"weird": 1}}), force=True)
    assert rep2["errors"] and rep2["errors"][0].startswith("format:")


def test_account_without_credentials_or_session_is_skipped(env):
    db, _ = env
    db.update_account(3, kleinanzeigen_password="")
    _thread(db)
    calls: list = []
    rep = _sync(session_factory=FakeSession({CONV: _conv_json()}, calls=calls))
    assert rep["accounts"][3]["skipped"] == "no_credentials" and calls == []


def test_busy_lock(env):
    db, _ = env
    _thread(db)
    with ka_site_sync._single_run() as mine:
        assert mine
        rep = _sync(session_factory=FakeSession({CONV: _conv_json()}))
    assert rep["skipped"] == "busy"


def test_state_file_permissions(env):
    ka_site_sync._save_state({"x": 1})
    assert (ka_site_sync.STATE_FILE.stat().st_mode & 0o777) == 0o600


def test_run_scheduled_disabled_by_default(env, monkeypatch):
    monkeypatch.setattr(ka_site_sync, "sync_unanswered", lambda **k: pytest.fail("не должен идти"))
    assert ka_site_sync.run_scheduled() is None


def test_all_mail_box_parsing():
    class I:
        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"',
                          b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"']
    assert ka_site_sync._all_mail_box(I()) == "[Gmail]/Alle Nachrichten"
