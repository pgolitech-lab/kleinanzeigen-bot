"""Почтовый конвейер (opt/mail): IMAP только UID-командами, очередь обработки,
порядок фильтров до платного классификатора, guard'ы автопилота, сброс 'sending'."""
from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from typing import Any

import pytest

from modules import gmail


# ---------------------------------------------------------------------------
# Фейковый IMAP: понимает только UID-команды (seq-num команды — ошибка теста)
# ---------------------------------------------------------------------------

def _raw(msg_id: str, subject: str = "Nutzer-Anfrage", body: str = "Hallo") -> bytes:
    m = EmailMessage()
    m["From"] = "Hans über Kleinanzeigen <abc@mail.kleinanzeigen.de>"
    m["To"] = "me@gmail.com"
    m["Subject"] = subject
    m["Message-ID"] = msg_id
    m["Date"] = "Tue, 20 Oct 2026 10:00:00 +0200"
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    instances: list["FakeIMAP"] = []
    mailbox: dict[str, bytes] = {}

    def __init__(self, *a: Any, **kw: Any) -> None:
        self.calls: list[tuple] = []
        FakeIMAP.instances.append(self)

    def __enter__(self) -> "FakeIMAP":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def login(self, *a: Any) -> None:
        pass

    def select(self, *a: Any, **kw: Any) -> None:
        pass

    def search(self, *a: Any) -> None:
        raise AssertionError("seq-num SEARCH запрещён — только UID")

    def fetch(self, *a: Any) -> None:
        raise AssertionError("seq-num FETCH запрещён — только UID")

    def store(self, *a: Any) -> None:
        raise AssertionError("seq-num STORE запрещён — только UID")

    def uid(self, cmd: str, *args: Any):
        self.calls.append((cmd, *args))
        if cmd == "SEARCH":
            return "OK", [" ".join(sorted(FakeIMAP.mailbox, key=int)).encode()]
        if cmd == "FETCH":
            uid_set, what = args
            data: list[Any] = []
            for i, uid in enumerate(uid_set.split(",")):
                raw = FakeIMAP.mailbox[uid]
                if "MESSAGE-ID" in what:
                    msgid = [ln for ln in raw.splitlines() if ln.lower().startswith(b"message-id:")][0]
                    raw = msgid + b"\r\n\r\n"
                head = f"{i + 1} (UID {uid} X-GM-THRID 77{uid} BODY[] {{{len(raw)}}}".encode()
                data += [(head, raw), b")"]
            return "OK", data
        if cmd == "STORE":
            return "OK", [b""]
        raise AssertionError(cmd)


@pytest.fixture()
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.mailbox = {"5": _raw("<a@x>"), "9": _raw("<b@x>"), "12": _raw("<c@x>")}
    monkeypatch.setattr(gmail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


def test_fetch_new_uses_uid_and_one_batched_fetch(fake_imap):
    res = gmail.fetch_new("me@gmail.com", "pw", from_filter="kleinanzeigen.de", skip_uids={"9"})
    assert [r["uid"] for r in res] == ["5", "12"]
    assert [r["gmail_message_id"] for r in res] == ["<a@x>", "<c@x>"]
    assert res[0]["gmail_thread_id"] == "775"
    assert res[0]["from_name"] == "Hans über Kleinanzeigen"
    (imap,) = fake_imap.instances  # одна IMAP-сессия
    fetches = [c for c in imap.calls if c[0] == "FETCH"]
    assert len(fetches) == 1 and fetches[0][1] == "5,12"
    search = [c for c in imap.calls if c[0] == "SEARCH"][0]
    assert "UNSEEN" in search and "FROM" in search


def test_uid_after_literal_and_broken_mime(fake_imap, monkeypatch):
    """UID может прийти ПОСЛЕ литерала; битое письмо не роняет весь fetch."""
    real_uid = FakeIMAP.uid

    def uid_late(self, cmd, *args):
        typ, data = real_uid(self, cmd, *args)
        if cmd != "FETCH":
            return typ, data
        out = []
        for item in data:
            if isinstance(item, tuple):
                head = item[0].replace(b"UID ", b"XUID-")  # убрать UID из заголовка
                uid = head.split(b"XUID-")[1].split(b" ")[0]
                out += [(head, item[1]), b" UID " + uid + b")"]
        return typ, out

    monkeypatch.setattr(FakeIMAP, "uid", uid_late)
    real_parse = gmail._parse_message
    monkeypatch.setattr(gmail, "_parse_message",
                        lambda uid, h, r: (_ for _ in ()).throw(ValueError("mime")) if uid == "9" else real_parse(uid, h, r))
    res = gmail.fetch_new("me@gmail.com", "pw")
    assert [r["uid"] for r in res] == ["5", "9", "12"]
    assert res[1].get("_broken") and not res[0].get("_broken")


def test_mark_seen_single_uid_store(fake_imap):
    gmail.mark_seen("me@gmail.com", "pw", ["5", b"12"])
    (imap,) = fake_imap.instances
    assert imap.calls == [("STORE", "5,12", "+FLAGS", "(\\Seen)")]


def test_find_orphans_and_unmark(fake_imap):
    orphans = gmail.find_orphan_seen_uids("me@gmail.com", "pw", known_message_ids={"<a@x>"})
    assert orphans == ["9", "12"]
    gmail.unmark_seen("me@gmail.com", "pw", orphans)
    assert fake_imap.instances[-1].calls == [("STORE", "9,12", "-FLAGS", "(\\Seen)")]


def test_smtp_has_timeout(monkeypatch):
    seen: dict[str, Any] = {}

    class FakeSMTP:
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def login(self, *a: Any) -> None:
            pass

        def send_message(self, *a: Any) -> None:
            pass

    monkeypatch.setattr(gmail.smtplib, "SMTP_SSL", FakeSMTP)
    gmail.send_reply("me@gmail.com", "pw", "Me", "to@x.de", "Re: x", "Hallo")
    assert seen.get("timeout") == gmail.SMTP_TIMEOUT


# ---------------------------------------------------------------------------
# БД-фикстура и общие заглушки
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'acc', 'me@gmail.com', 'pw', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )
    return db_mod


@pytest.fixture()
def inc(tmp_db, monkeypatch):
    """incoming с заглушками внешних эффектов (LLM, Telegram, Playwright, SMTP)."""
    from modules import incoming
    calls: dict[str, list] = {"classify": [], "review": [], "parse_ad": [], "send": [], "stop": []}

    def classify(**kw):
        calls["classify"].append(kw)
        return {"is_inquiry": True, "cost_usd": 0.0, "reason": "ok"}

    monkeypatch.setattr(incoming.claude, "classify_email_is_inquiry", classify)
    monkeypatch.setattr(incoming.claude, "detect_and_translate_to_ru",
                        lambda body: {"translation_ru": "привет", "lang": "de", "cost_usd": 0.0})
    monkeypatch.setattr(incoming.claude, "history_for", lambda tid: [])
    monkeypatch.setattr(incoming.telegram_bot, "send_for_review", lambda mid: calls["review"].append(mid))
    monkeypatch.setattr(incoming.telegram_bot, "send_autopilot_stop_notification",
                        lambda mid, reason: calls["stop"].append(reason))
    monkeypatch.setattr(incoming.telegram_bot, "send_autopilot_progress", lambda *a: None)
    monkeypatch.setattr(incoming.telegram_bot, "thread_is_busy", lambda tid: False)
    monkeypatch.setattr(incoming.parser, "parse_ad", lambda url: calls["parse_ad"].append(url) or {
        "title": "Sitzbank", "price": "100 €", "description": "gut", "seller_name": "S"})
    monkeypatch.setattr(incoming.ad_brief, "generate_brief", lambda **kw: (_ for _ in ()).throw(RuntimeError("no")))

    def fake_send(row):
        calls["send"].append(row["id"])
        tmp_db.update_message(row["id"], status="sent")
        return {"kind": "sent"}

    monkeypatch.setattr(incoming, "_send_reply", fake_send)
    incoming._calls = calls
    return incoming


def _email(**over: Any) -> dict[str, Any]:
    em = {
        "gmail_message_id": "<m1@x>", "gmail_thread_id": "T1", "uid": "5",
        "from_email": "abc@mail.kleinanzeigen.de", "from_name": "Hans über Kleinanzeigen",
        "subject": "Nutzer-Anfrage zu deiner Anzeige Sitzbank",
        "body": "Ist das noch da? https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1",
        "body_html": "", "date": "",
    }
    em.update(over)
    return em


ACC = {"id": 1, "gmail_email": "me@gmail.com", "gmail_app_password": "pw", "auto_ack_enabled": 0}


# ---------------------------------------------------------------------------
# Порядок фильтров и Playwright
# ---------------------------------------------------------------------------

def test_no_ad_ref_skipped_before_classifier(inc, tmp_db):
    inc._process_incoming(ACC, _email(body="Hallo, wie geht's?"))
    assert inc._calls["classify"] == []
    assert inc._already_processed("<m1@x>")


def test_already_processed_not_reclassified(inc, tmp_db):
    tmp_db.mark_processed("<m1@x>", 1, "skipped_classifier")
    inc._process_incoming(ACC, _email())
    assert inc._calls["classify"] == []
    assert inc._calls["review"] == []


def test_new_thread_parses_ad_followup_reuses_db(inc, tmp_db):
    inc._process_incoming(ACC, _email())
    assert len(inc._calls["parse_ad"]) == 1
    assert len(inc._calls["review"]) == 1
    inc._process_incoming(ACC, _email(gmail_message_id="<m2@x>", uid="9", body="Letzter Preis? https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1"))
    assert len(inc._calls["parse_ad"]) == 1  # второй раз — из БД, без Playwright
    row = tmp_db.find_by_gmail_message_id("<m2@x>")
    assert row["ad_title"] == "Sitzbank" and row["ad_description"] == "gut"


def test_review_failure_raises_and_retry_resumes_card(inc, tmp_db, monkeypatch):
    """Карточка не ушла → исключение (очередь повторит); повтор не глотается
    дедупом, а доводит сохранённую строку до оператора."""
    monkeypatch.setattr(inc.telegram_bot, "send_for_review",
                        lambda mid: (_ for _ in ()).throw(RuntimeError("tg down")))
    with pytest.raises(RuntimeError):
        inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert row["status"] == "new"
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", lambda mid: inc._calls["review"].append(mid))
    inc._process_incoming(ACC, _email())
    assert inc._calls["review"] == [row["id"]]
    assert inc._calls["parse_ad"] == ["https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1"]


# ---------------------------------------------------------------------------
# Автопилот
# ---------------------------------------------------------------------------

def _reply(**over: Any) -> dict[str, Any]:
    r = {"client_lang": "de", "ru_client": "ещё есть?", "ru_answer": "да",
         "de_answer": "Ja, noch da. Viele Grüße", "ru_translation": "да",
         "deal_brief": {"negotiated_price_eur": 120}, "should_stop": False, "stop_reason": ""}
    r.update(over)
    return r


def _ap_msg(tmp_db, floor: float = 100) -> tuple[int, Any]:
    tmp_db.start_thread_autopilot("T1", floor, "silent")
    mid = tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1",
                             buyer_name="abc@mail.kleinanzeigen.de", de_client="x", status="new")
    return mid, tmp_db.get_thread_autopilot("T1")


def test_autopilot_shadow_does_not_send(inc, tmp_db):
    mid, ap = _ap_msg(tmp_db)  # autopilot_shadow_mode по умолчанию = 1
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 1


def test_autopilot_sends_when_shadow_off(inc, tmp_db):
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [mid]
    assert tmp_db.get_thread_autopilot("T1")["messages_sent"] == 1


def test_autopilot_cap_from_settings(inc, tmp_db):
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    tmp_db.set_setting("autopilot_message_cap", "0")
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [] and inc._calls["stop"] == ["limit"]


@pytest.mark.parametrize("reply,reason", [
    (_reply(de_answer="Да, ещё есть"), "guard_language"),
    (_reply(deal_brief={"negotiated_price_eur": 50}), "guard_floor"),
])
def test_autopilot_guards_block_send(inc, tmp_db, reply, reason):
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    mid, ap = _ap_msg(tmp_db, floor=100)
    inc._autopilot_dispatch(mid, ap, reply)
    assert inc._calls["send"] == []
    assert inc._calls["stop"] == [reason] and inc._calls["review"] == [mid]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 0


def test_autopilot_smtp_failure_goes_to_review(inc, tmp_db, monkeypatch):
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    mid, ap = _ap_msg(tmp_db)
    monkeypatch.setattr(inc, "_send_reply", lambda row: {"kind": "error", "message": "smtp"})
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["review"] == [mid]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 1


def test_autopilot_llm_failure_goes_to_review(inc, tmp_db, monkeypatch):
    tmp_db.start_thread_autopilot("T1", 100, "silent")
    monkeypatch.setattr(inc.claude, "generate_autopilot_reply",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("cli down")))
    inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert inc._calls["review"] == [row["id"]]


# ---------------------------------------------------------------------------
# Очередь обработки
# ---------------------------------------------------------------------------

@pytest.fixture()
def mq(monkeypatch):
    from modules import mail_queue
    monkeypatch.setattr(mail_queue, "_INFLIGHT", {})
    monkeypatch.setattr(mail_queue, "_ATTEMPTS", {})
    monkeypatch.setattr(mail_queue, "_RETRY_AT", {})
    import queue
    monkeypatch.setattr(mail_queue, "_Q", queue.Queue())
    alerts: list[str] = []
    monkeypatch.setattr(mail_queue.telegram_bot, "notify", lambda text, *a, **kw: alerts.append(text))
    mail_queue._alerts = alerts
    return mail_queue


def test_enqueue_dedups_inflight(mq):
    assert mq.enqueue(ACC, [_email(uid="5"), _email(uid="9")]) == 2
    assert mq.enqueue(ACC, [_email(uid="5")]) == 0
    assert mq.inflight_uids(1) == {"5", "9"}


def test_handle_retries_then_gives_up(mq, tmp_db, monkeypatch):
    monkeypatch.setattr(mq, "_process_incoming", lambda acc, em: (_ for _ in ()).throw(ValueError("boom")))
    em = _email()
    results = [mq._handle(ACC, em) for _ in range(mq.MAX_ATTEMPTS)]
    assert results == [False] * (mq.MAX_ATTEMPTS - 1) + [True]
    with tmp_db.get_conn() as conn:
        r = conn.execute("SELECT reason FROM processed_messages WHERE gmail_message_id='<m1@x>'").fetchone()
    assert r["reason"] == "error_processing"
    assert len(mq._alerts) == 1 and "не обработано" in mq._alerts[0]


def test_failed_uid_held_until_backoff_expires(mq, monkeypatch):
    monkeypatch.setattr(mq, "_process_incoming", lambda acc, em: (_ for _ in ()).throw(ValueError("boom")))
    mq.enqueue(ACC, [_email(uid="5")])
    assert mq._handle(ACC, _email(uid="5")) is False
    assert mq.release_due_retries() == 0 and "5" in mq.inflight_uids(1)  # пауза ещё идёт
    for k in mq._RETRY_AT:
        mq._RETRY_AT[k] = 0
    assert mq.release_due_retries() == 1 and mq.inflight_uids(1) == set()


def test_broken_message_gives_up_with_alert(mq):
    assert mq._handle(ACC, {"uid": "7", "gmail_message_id": "", "_broken": True}) is True
    assert len(mq._alerts) == 1


def test_flush_seen_batches_and_releases(mq, monkeypatch):
    marked: list = []
    monkeypatch.setattr(mq.gmail, "mark_seen", lambda e, p, uids: marked.append(list(uids)))
    mq.enqueue(ACC, [_email(uid="5"), _email(uid="9")])
    mq._flush_seen({1: (ACC, ["5", "9"])})
    assert marked == [["5", "9"]]
    assert mq.inflight_uids(1) == set()


# ---------------------------------------------------------------------------
# Сброс зависших 'sending'
# ---------------------------------------------------------------------------

def test_reset_stuck_sending(tmp_db):
    from modules import outgoing
    a = tmp_db.add_message(account_id=1, direction="out", status="sending")
    b = tmp_db.add_message(account_id=1, direction="out", status="sent")
    assert outgoing.reset_stuck_sending() == 1
    assert tmp_db.get_message(a)["status"] == "error_send_failed"
    assert tmp_db.get_message(b)["status"] == "sent"
