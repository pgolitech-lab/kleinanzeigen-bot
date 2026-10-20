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
    m["X-Conversation-ID"] = "49sml:conv:" + msg_id.strip("<>@x")
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


def test_find_orphans_known_read_after_fetch(fake_imap):
    """known как callable читается после IMAP FETCH — без гонки с очередью."""
    order: list[str] = []
    real_uid = FakeIMAP.uid

    def uid_spy(self, cmd, *args):
        order.append(cmd)
        return real_uid(self, cmd, *args)

    FakeIMAP.uid = uid_spy
    try:
        orphans = gmail.find_orphan_seen_uids(
            "me@gmail.com", "pw",
            known_message_ids=lambda: order.append("known") or {"<a@x>", "<b@x>"})
    finally:
        FakeIMAP.uid = real_uid
    assert orphans == ["12"] and order == ["SEARCH", "FETCH", "known"]


def test_uid_fetch_not_ok_raises(fake_imap, monkeypatch):
    """UID FETCH не OK — ошибка опроса, а не тихий пустой результат."""
    real_uid = FakeIMAP.uid
    monkeypatch.setattr(FakeIMAP, "uid", lambda self, cmd, *a: ("NO", [b"x"]) if cmd == "FETCH"
                        else real_uid(self, cmd, *a))
    with pytest.raises(gmail.imaplib.IMAP4.error):
        gmail.fetch_new("me@gmail.com", "pw")


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
    def fake_review(mid):
        # как настоящий: при доставке пишет telegram_message_id и возвращает его
        calls["review"].append(mid)
        tmp_db.update_message(mid, telegram_message_id=1000 + mid)
        return 1000 + mid

    monkeypatch.setattr(incoming.telegram_bot, "send_for_review", fake_review)
    incoming._fake_review = fake_review
    monkeypatch.setattr(incoming.telegram_bot, "send_autopilot_stop_notification",
                        lambda mid, reason: calls["stop"].append(reason))
    monkeypatch.setattr(incoming.telegram_bot, "send_autopilot_progress", lambda *a: None)
    calls["notify"] = []
    calls["detected"] = "de"  # что вернёт Haiku-детектор языка
    monkeypatch.setattr(incoming.claude, "detect_text_lang",
                        lambda text: {"lang": calls["detected"]})
    monkeypatch.setattr(incoming.telegram_bot, "notify",
                        lambda text, *a, **kw: calls["notify"].append(text))
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
    """Карточка не ушла (send_for_review вернул None — так он сообщает о сбое
    fanout) → исключение (очередь повторит); повтор доводит строку до оператора."""
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", lambda mid: None)
    with pytest.raises(RuntimeError):
        inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert row["status"] == "new"
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", inc._fake_review)
    inc._process_incoming(ACC, _email())
    assert inc._calls["review"] == [row["id"]]
    assert inc._calls["parse_ad"] == ["https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1"]


def test_delivered_card_not_duplicated_on_repeat(inc, tmp_db):
    """status='new' с доставленной карточкой (оператор ещё не открыл) — повторное
    попадание письма (сбой \\Seen, рестарт) не шлёт дубль, а уходит в skipped_dedup."""
    inc._process_incoming(ACC, _email())
    inc._process_incoming(ACC, _email())
    assert len(inc._calls["review"]) == 1
    with tmp_db.get_conn() as conn:
        r = conn.execute("SELECT reason FROM processed_messages WHERE gmail_message_id='<m1@x>'").fetchone()
    assert r["reason"] == "skipped_dedup"


def test_undelivered_card_deferred_when_thread_busy(inc, tmp_db, monkeypatch):
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", lambda mid: None)
    with pytest.raises(RuntimeError):
        inc._process_incoming(ACC, _email())
    monkeypatch.setattr(inc.telegram_bot, "thread_is_busy", lambda tid: True)
    inc._process_incoming(ACC, _email())
    assert tmp_db.find_by_gmail_message_id("<m1@x>")["status"] == "deferred"


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


def test_autopilot_sends_when_enabled(inc, tmp_db):
    """Включён на треде → отправляет сам; старое autopilot_shadow_mode=1 не мешает."""
    tmp_db.set_setting("autopilot_shadow_mode", "1")
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [mid] and inc._calls["review"] == []
    assert tmp_db.get_thread_autopilot("T1")["messages_sent"] == 1


def test_autopilot_cap_from_settings(inc, tmp_db):
    tmp_db.set_setting("autopilot_message_cap", "0")
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [] and inc._calls["stop"] == ["limit"]


@pytest.mark.parametrize("reply,reason,why", [
    (_reply(de_answer="Да, ещё есть"), "guard_language", "не на языке клиента"),
    (_reply(deal_brief={"negotiated_price_eur": 50}), "guard_floor", "ниже минимума"),
])
def test_autopilot_guards_hold_reply_keep_active(inc, tmp_db, reply, reason, why):
    """Guard задерживает только этот ответ: карточка + уведомление с причиной,
    автопилот на треде остаётся включённым."""
    mid, ap = _ap_msg(tmp_db, floor=100)
    inc._autopilot_dispatch(mid, ap, reply)
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]
    assert inc._calls["stop"] == []
    assert len(inc._calls["notify"]) == 1 and why in inc._calls["notify"][0]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 1


def test_autopilot_floor_checks_price_in_text(inc, tmp_db):
    """Бриф говорит 120, а в тексте клиенту 80 € — guard_floor по тексту."""
    mid, ap = _ap_msg(tmp_db, floor=100)
    inc._autopilot_dispatch(mid, ap, _reply(de_answer="Für 80 € gebe ich es ab. Viele Grüße"))
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]
    assert "ниже минимума" in inc._calls["notify"][0]


def test_prices_in_text():
    assert inc_prices("Für 80 € oder 1.200 EUR, € 95, 70,- Euro") == [80.0, 1200.0, 95.0, 70.0]
    assert inc_prices("Ich gebe es für 80 ab, VB 75, letzter Preis: 90 oder 85,-") == [80.0, 75.0, 90.0, 85.0]
    assert inc_prices("Baujahr 2015, 120 cm breit, für 2 Personen") == [2.0]


def inc_prices(t):
    from modules import incoming
    return incoming._prices_in_text(t)


def test_autopilot_language_vs_thread_lang(inc, tmp_db):
    """Модель заявила 'en', а клиент треда пишет по-немецки — стоп, не шлём."""
    tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1", de_client="Hallo",
                       client_lang="de", status="sent")
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply(client_lang="en", de_answer="Yes, still there."))
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 1


def test_autopilot_ukrainian_client_cyrillic_ok(inc, tmp_db):
    mid, ap = _ap_msg(tmp_db)
    inc._calls["detected"] = "uk"
    inc._autopilot_dispatch(mid, ap, _reply(client_lang="uk", de_answer="Так, ще є."))
    assert inc._calls["send"] == [mid]


def test_autopilot_detector_catches_wrong_latin_language(inc, tmp_db):
    """Модель заявила de, написала по-английски — алфавит не видит, Haiku ловит."""
    mid, ap = _ap_msg(tmp_db)
    inc._calls["detected"] = "en"
    inc._autopilot_dispatch(mid, ap, _reply(de_answer="Yes, it is still available."))
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]


def test_autopilot_detector_failure_is_fail_closed(inc, tmp_db, monkeypatch):
    mid, ap = _ap_msg(tmp_db)
    monkeypatch.setattr(inc.claude, "detect_text_lang",
                        lambda t: (_ for _ in ()).throw(RuntimeError("cli down")))
    # Короткий текст без явных признаков языка — эвристика не уверена, нужен детектор.
    inc._autopilot_dispatch(mid, ap, _reply(de_answer="Okay, passt."))
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid]


def test_autopilot_detector_und_passes(inc, tmp_db):
    mid, ap = _ap_msg(tmp_db)
    inc._calls["detected"] = "und"
    inc._autopilot_dispatch(mid, ap, _reply())
    assert inc._calls["send"] == [mid]


def test_autopilot_holds_while_previous_held_draft_open(inc, tmp_db):
    """Прошлый ответ задержан и висит у оператора — новый не шлём поверх."""
    mid1, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid1, ap, _reply(de_answer="Für 50 € gern. Viele Grüße"))
    tmp_db.update_message(mid1, de_answer="Für 50 € gern. Viele Grüße")
    mid2 = tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1",
                              de_client="und?", status="new")
    inc._autopilot_dispatch(mid2, tmp_db.get_thread_autopilot("T1"), _reply())
    assert inc._calls["send"] == [] and inc._calls["review"] == [mid1, mid2]
    assert "черновик" in inc._calls["notify"][-1]


def test_autopilot_threat_with_guard_only_stop_notification(inc, tmp_db):
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply(should_stop=True, stop_reason="threat",
                                            de_answer="Пожалуйста, без угроз", deal_brief={}))
    assert inc._calls["send"] == [] and inc._calls["stop"] == ["threat"]
    assert inc._calls["notify"] == []
    assert tmp_db.get_thread_autopilot("T1")["active"] == 0


def test_autopilot_threat_smtp_failure_goes_to_review(inc, tmp_db, monkeypatch):
    mid, ap = _ap_msg(tmp_db)
    monkeypatch.setattr(inc, "_send_reply", lambda row: {"kind": "error", "message": "smtp"})
    inc._autopilot_dispatch(mid, ap, _reply(should_stop=True, stop_reason="threat",
                                            deal_brief={}))
    assert inc._calls["review"] == [mid] and inc._calls["stop"] == ["threat"]
    assert tmp_db.get_thread_autopilot("T1")["active"] == 0


def test_autopilot_review_undelivered_raises(inc, tmp_db, monkeypatch):
    """Ответ задержан guard'ом, а карточка не дошла — исключение, а не тихий return."""
    mid, ap = _ap_msg(tmp_db)
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", lambda m: None)
    with pytest.raises(RuntimeError):
        inc._autopilot_dispatch(mid, ap, _reply(de_answer="Да, ещё есть"))


def test_history_excludes_current_by_id_not_text(inc, tmp_db, monkeypatch):
    """Одинаковое короткое «Ok» в прошлом письме не выпадает из истории."""
    seen: dict = {}
    body = "Ok https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1"
    same = inc.parser.clean_email_body(body)  # прошлое письмо с тем же текстом
    tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1", de_client=same,
                       status="sent", ad_id="123456789", ad_title="Sitzbank")
    tmp_db.start_thread_autopilot("T1", 100, "silent")

    def gen(**kw):
        seen["history"] = kw["history"]
        return _reply()

    monkeypatch.setattr(inc.claude, "generate_autopilot_reply", gen)
    inc._process_incoming(ACC, _email(body=body))
    assert [h["de_client"] for h in seen["history"]] == [same]


def test_history_hides_unsent_answer(inc, tmp_db, monkeypatch):
    """Задержанный/неотправленный de_answer клиент не видел — в истории его нет."""
    seen: dict = {}
    tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1", de_client="Preis?",
                       de_answer="Für 50 €", status="new", ad_id="123456789", ad_title="Sitzbank")
    tmp_db.add_message(account_id=1, direction="in", gmail_thread_id="T1", de_client="Hallo",
                       de_answer="Ja, da", status="sent", ad_id="123456789", ad_title="Sitzbank")
    tmp_db.start_thread_autopilot("T1", 100, "silent")
    monkeypatch.setattr(inc.claude, "generate_autopilot_reply",
                        lambda **kw: seen.update(kw) or _reply())
    inc._process_incoming(ACC, _email())
    assert [h["de_answer"] for h in seen["history"]] == [None, "Ja, da"]


def test_last_poll_ok_at(monkeypatch):
    import scheduler
    monkeypatch.setitem(scheduler.POLL_STATE, "last_ok_ts", None)
    assert scheduler.last_poll_ok_at() is None
    monkeypatch.setitem(scheduler.POLL_STATE, "last_ok_ts", 123.0)
    assert scheduler.last_poll_ok_at() == 123.0


def test_autopilot_smtp_failure_goes_to_review(inc, tmp_db, monkeypatch):
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


def test_flush_seen_failure_escalates_to_error(mq, monkeypatch, caplog):
    monkeypatch.setattr(mq, "_SEEN_FAILS", {})
    monkeypatch.setattr(mq.gmail, "_imap_retry", lambda f, *a, **kw: (_ for _ in ()).throw(OSError("imap")))
    import logging
    with caplog.at_level(logging.WARNING, logger=mq.logger.name):
        for _ in range(mq.SEEN_FAIL_ALERT):
            mq._flush_seen({1: (ACC, ["5"])})
    levels = [r.levelname for r in caplog.records if "mark_seen fail" in r.getMessage()]
    assert levels[-1] == "ERROR" and levels[0] == "WARNING"


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


# ---------------------------------------------------------------------------
# Возраст письма: «запоздало» и слишком старое
# ---------------------------------------------------------------------------

def _date_days_ago(days: float) -> str:
    from email.utils import format_datetime
    from datetime import timezone, timedelta as td
    return format_datetime(datetime.now(timezone.utc) - td(days=days))


def test_too_old_email_notifies_and_skips(inc, tmp_db):
    inc._process_incoming(ACC, _email(date=_date_days_ago(9)))
    assert inc._calls["review"] == [] and inc._calls["classify"] == []
    assert len(inc._calls["notify"]) == 1 and "Старое письмо" in inc._calls["notify"][0]
    with tmp_db.get_conn() as conn:
        r = conn.execute("SELECT reason FROM processed_messages WHERE gmail_message_id='<m1@x>'").fetchone()
    assert r["reason"] == "skipped_max_age"


def test_late_email_no_autopilot_card_and_notice(inc, tmp_db, monkeypatch):
    """Письмо 3-дневной давности в треде с автопилотом: не отвечаем, карточка + «запоздало»."""
    tmp_db.start_thread_autopilot("T1", 100, "silent")
    monkeypatch.setattr(inc.claude, "generate_autopilot_reply",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("автопилот не должен")))
    inc._process_incoming(ACC, _email(date=_date_days_ago(3)))
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert inc._calls["send"] == [] and inc._calls["review"] == [row["id"]]
    assert any("Запоздалое" in t for t in inc._calls["notify"])


def test_late_email_no_auto_ack(inc, tmp_db, monkeypatch):
    acked: list = []
    monkeypatch.setattr(inc, "_send_auto_ack", lambda *a, **kw: acked.append(1))
    inc._process_incoming({**ACC, "auto_ack_enabled": 1}, _email(date=_date_days_ago(2)))
    assert acked == []
    inc._process_incoming({**ACC, "auto_ack_enabled": 1},
                          _email(gmail_message_id="<m9@x>", gmail_thread_id="T9", uid="11",
                                 date=_date_days_ago(0.1)))
    assert acked == [1]


# ---------------------------------------------------------------------------
# Мониторинг ошибок
# ---------------------------------------------------------------------------

def test_monitor_errors_collects_dedups_realerts(monkeypatch):
    import logging
    import scheduler
    sent: list[str] = []
    monkeypatch.setattr(scheduler.telegram_bot, "notify", lambda text, *a, **kw: sent.append(text))
    monkeypatch.setattr(scheduler, "_REPORTED_ERROR_HASHES", {})
    scheduler._ERRORS.clear()
    scheduler.install_error_collector()
    log = logging.getLogger("modules.test_x")
    log.info("просто info")  # не ловится
    log.error("send fail msg=%s", 12)
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("упало msg=%s", 34)
    log.error("send fail msg=%s", 56)  # тот же тип, что 12
    assert "posted 2" in scheduler.monitor_errors_job()
    assert len(sent) == 1 and "×2" in sent[0] and "ValueError" in sent[0]
    log.error("send fail msg=%s", 78)
    assert "already reported" in scheduler.monitor_errors_job()
    for h in scheduler._REPORTED_ERROR_HASHES:
        scheduler._REPORTED_ERROR_HASHES[h] -= scheduler.ERROR_REALERT_SEC + 1
    log.error("send fail msg=%s", 90)
    assert "posted 1" in scheduler.monitor_errors_job()
    assert scheduler.monitor_errors_job() == "ok, no issues"


# ---------------------------------------------------------------------------
# Совместимость с opt/llm (через getattr / фолбэки)
# ---------------------------------------------------------------------------

def test_history_uses_llm_history_for_and_hides_unsent(inc, monkeypatch):
    got: dict = {}

    def history_for(tid, exclude_id=None, before=None):
        got["args"] = (tid, exclude_id)
        return [{"id": 1, "direction": "in", "de_client": "Preis?", "de_answer": "50 €", "status": "edited"},
                {"id": 2, "direction": "in", "de_client": "Hallo", "de_answer": "Ja", "status": "sent"},
                {"id": 3, "direction": "out", "de_client": None, "de_answer": "ack", "status": "skipped"}]

    monkeypatch.setattr(inc.claude, "history_for", history_for)
    h = inc._history_for_prompt("T1", 99)
    assert got["args"] == ("T1", 99)
    assert [x["de_answer"] for x in h] == [None, "Ja", None]
    assert inc._history_for_prompt("", 99) == []


def test_translation_failure_marks_card_keeps_lang_empty(inc, tmp_db, monkeypatch):
    monkeypatch.setattr(inc.claude, "detect_and_translate_to_ru",
                        lambda body: (_ for _ in ()).throw(RuntimeError("cli down")))
    inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert row["ru_client"] == inc.TRANSLATION_FAILED_RU
    assert not row["client_lang"]
    assert inc._calls["review"] == [row["id"]]


def test_auto_ack_uses_ack_text_ru_without_translate(inc, tmp_db, monkeypatch):
    tmp_db.set_setting("send_mode", "redirect")  # _send_reply подменён — реально ничего не шлётся
    monkeypatch.setattr(inc.claude, "generate_auto_ack", lambda **kw: {
        "ack_text": "Danke, melde mich gleich.", "ack_text_ru": "Спасибо, скоро отвечу.",
        "client_lang": "de", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0})
    monkeypatch.setattr(inc.claude, "translate_only",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("не нужен")))
    inc._process_incoming({**ACC, "auto_ack_enabled": 1}, _email())
    with tmp_db.get_conn() as conn:
        r = conn.execute("SELECT ru_translation FROM messages WHERE is_auto_ack=1").fetchone()
    assert r["ru_translation"] == "Спасибо, скоро отвечу."


def test_language_guard_prefers_llm_guard_when_present(inc, tmp_db, monkeypatch):
    seen: list = []
    monkeypatch.setattr(inc.guardrails, "outgoing_language_ok_llm",
                        lambda text, lang: seen.append(lang) or False, raising=False)
    mid, ap = _ap_msg(tmp_db)
    inc._autopilot_dispatch(mid, ap, _reply())
    assert seen == ["de"] and inc._calls["send"] == [] and inc._calls["review"] == [mid]


# ---------------------------------------------------------------------------
# X-Conversation-ID (id переписки на сайте Kleinanzeigen)
# ---------------------------------------------------------------------------

def test_fetch_new_extracts_conversation_id(fake_imap):
    res = gmail.fetch_new("me@gmail.com", "pw")
    assert res[0]["ka_conversation_id"] == "49sml:conv:a"


def test_conversation_id_saved_when_column_exists(inc, tmp_db, monkeypatch):
    # колонку создаёт миграция db (ka_conversation_id); id переписки сохраняется при приёме
    monkeypatch.setattr(inc, "_MESSAGES_COLUMNS", set())
    inc._process_incoming(ACC, _email(gmail_message_id="<m2@x>", gmail_thread_id="T2", uid="9",
                                      ka_conversation_id="49sml:1:2"))
    assert tmp_db.find_by_gmail_message_id("<m2@x>")["ka_conversation_id"] == "49sml:1:2"


# ---------------------------------------------------------------------------
# Уроки через llm_memory и лимит подписки LLM
# ---------------------------------------------------------------------------

def test_autopilot_uses_llm_memory_lessons(inc, tmp_db, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(inc.llm_memory, "lessons_for", lambda row: [{"lesson_ru": "урок", "id": row["id"]}])
    tmp_db.start_thread_autopilot("T1", 100, "silent")
    monkeypatch.setattr(inc.claude, "generate_autopilot_reply",
                        lambda **kw: seen.update(kw) or _reply())
    inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert seen["lessons"] == [{"lesson_ru": "урок", "id": row["id"]}]


def test_autopilot_llm_limit_card_and_note_keeps_active(inc, tmp_db, monkeypatch):
    tmp_db.start_thread_autopilot("T1", 100, "silent")
    monkeypatch.setattr(inc.claude, "generate_autopilot_reply",
                        lambda **kw: (_ for _ in ()).throw(inc.llm.LLMLimitError("429")))
    inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert inc._calls["review"] == [row["id"]] and inc._calls["send"] == []
    assert any("Лимит подписки" in t for t in inc._calls["notify"])
    assert tmp_db.get_thread_autopilot("T1")["active"] == 1


def test_manual_translation_llm_limit_marks_card(inc, tmp_db, monkeypatch):
    monkeypatch.setattr(inc.claude, "detect_and_translate_to_ru",
                        lambda body: (_ for _ in ()).throw(inc.llm.LLMLimitError("429")))
    inc._process_incoming(ACC, _email())
    row = tmp_db.find_by_gmail_message_id("<m1@x>")
    assert "Лимит подписки" in row["ru_client"] and not row["client_lang"]
    assert inc._calls["review"] == [row["id"]]
