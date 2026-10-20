"""«Горячий покупатель» и SLA-напоминания. Тексты выдуманные — по мотивам типичных
писем покупателей (без реальной переписки). Ничего не отправляется."""
from datetime import datetime, timedelta
from typing import Any

import pytest

from modules import hot_buyer as hb
from tests.test_mail_pipeline import ACC, _email, inc, tmp_db  # noqa: F401 — фикстуры

HOT = [
    "Hallo, wir würden die Bank gerne kaufen! VG",
    "Für 250 würde ich es nehmen, Geld kann ich sofort überweisen.",
    "Guten Tag, 90€ ist für mich okay. Wann könnte ich es abholen kommen?",
    "Danke! Geld habe ich gerade überwiesen. Passt Ihnen Donnerstag?",
    "HALLO 700€ IST OK",
    "Ich biete 600€ und würde es heute noch abholen.",
    "Alles klar. Wann kann ich vorbeikommen?",
    "Passt genau zu meinem Bus, also nehme ich es.",
    "Hallo. Ich nehme den Tisch. Danke.",
    "Ich könnte heute Abend vorbeikommen, wäre das ok?",
    "Wir würden die Sitze auf jeden Fall nehmen.",
    "Deal! Ich komme morgen.",
    "Ok, I'll take it. Can I pick it up tomorrow?",
    "Dzień dobry, biorę. Kiedy mogę odebrać?",
    "Здравствуйте, беру за 500, заберу в субботу",
]
NOT_HOT = [
    "Hallo, gibt es die Sitze auch einzeln?",
    "Wäre es möglich nur die Schienen zu kaufen?",
    "Ist das noch aktuell?",
    "Klingt gut, wir entscheiden nächste Woche. Wo müsste man abholen?",
    "PayPal teilt mir mit, dass Ihr Konto gesperrt ist.",
    "Könnten Sie das Teil verpacken und schicken?",
    "Ich frage morgen bei der Post nach und melde mich dann.",
    "Warum verkaufen Sie die Sitze? Aus welchem Auto sind sie?",
    "Ist das ein guter Deal?",
    "Damit bin ich nicht einverstanden.",
    "Die nehme ich leider nicht.",
    "Czy mogę zrobić przelew?",
    "",
]


@pytest.mark.parametrize("text", HOT)
def test_heuristic_hot(text):
    assert hb.heuristic(text)[0] == "hot", hb.heuristic(text)


@pytest.mark.parametrize("text", NOT_HOT)
def test_heuristic_not_hot(text):
    assert hb.heuristic(text)[0] != "hot", hb.heuristic(text)


def test_unsure_goes_to_haiku_only_then(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(hb, "_ask_haiku", lambda t: calls.append(t) or (True, "договаривается о встрече"))
    assert hb.classify("Hallo, ich nehme es.")[0] is True and calls == []       # уверенно — без LLM
    assert hb.classify("Hallo, wie alt sind die Sitze?")[0] is False and calls == []
    hot, why = hb.classify("Können Sie morgen um 16 Uhr kommen?")             # неясно → Haiku
    assert hot is True and len(calls) == 1 and "Haiku" in why


def test_haiku_failure_is_not_hot(monkeypatch):
    monkeypatch.setattr(hb, "_ask_haiku", lambda t: (_ for _ in ()).throw(RuntimeError("limit")))
    assert hb.classify("Können Sie morgen um 16 Uhr kommen?")[0] is False


def test_hot_schema_has_no_additional_properties():
    assert hb._HOT_SCHEMA["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Интеграция в incoming
# ---------------------------------------------------------------------------

AD = " https://www.kleinanzeigen.de/s-anzeige/sitzbank/123456789-223-1"


def test_hot_flag_saved_and_fallback_notice(inc, tmp_db, monkeypatch):
    monkeypatch.setattr(inc, "_MESSAGES_COLUMNS", set())
    body = "Hallo, ich nehme es! Wann kann ich abholen?" + AD
    inc._process_incoming(ACC, _email(body=body))           # колонок нет → отдельное 🔥-уведомление
    assert any("Горячий покупатель" in t for t in inc._calls["notify"])
    with tmp_db.get_conn() as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN is_hot INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE messages ADD COLUMN hot_reason TEXT")
    n = len(inc._calls["notify"])
    inc._process_incoming(ACC, _email(gmail_message_id="<m2@x>", gmail_thread_id="T2", uid="9", body=body))
    row = tmp_db.find_by_gmail_message_id("<m2@x>")
    assert row["is_hot"] == 1 and "nehme" in row["hot_reason"]
    assert len(inc._calls["notify"]) == n                   # колонка есть → пометка в карточке (api)


def test_unsure_checked_by_haiku_after_card(inc, tmp_db, monkeypatch):
    """Сомнительное письмо: карточка уходит до Haiku, 🔥 — отдельным уведомлением после."""
    order: list[str] = []
    real_review = inc.telegram_bot.send_for_review
    monkeypatch.setattr(inc.telegram_bot, "send_for_review", lambda mid: order.append("card") or real_review(mid))
    monkeypatch.setattr(inc.hot_buyer, "_ask_haiku", lambda t: order.append("haiku") or (True, "встреча"))
    inc._process_incoming(ACC, _email(body="Können Sie morgen um 16 Uhr kommen?" + AD))
    assert order == ["card", "haiku"]
    assert any("Горячий покупатель" in t for t in inc._calls["notify"])


def test_hot_notice_when_card_deferred(inc, tmp_db, monkeypatch):
    monkeypatch.setattr(inc, "_MESSAGES_COLUMNS", set())
    monkeypatch.setattr(inc.telegram_bot, "thread_is_busy", lambda tid: True)
    inc._process_incoming(ACC, _email(body="Ich nehme es!" + AD))
    assert any("Горячий покупатель" in t for t in inc._calls["notify"])


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------

@pytest.fixture()
def sla_env(tmp_db, monkeypatch):
    from modules import incoming, sla, telegram_bot
    monkeypatch.setattr(incoming, "_MESSAGES_COLUMNS", set())
    monkeypatch.setattr(sla, "_REMINDED", set())
    monkeypatch.setattr(sla, "_LAST_RUN", None)
    sent: list[str] = []
    monkeypatch.setattr(telegram_bot, "notify", lambda text, *a, **kw: sent.append(text))
    sla._sent = sent
    return sla


def _in(tmp_db, minutes_ago: float, now: datetime, **kw: Any) -> int:
    return tmp_db.add_message(account_id=1, direction="in", gmail_thread_id=kw.pop("thread", "T1"),
                              de_client="x", status=kw.pop("status", "new"),
                              created_at=(now - timedelta(minutes=minutes_ago)).isoformat(), **kw)


NOON_UTC = datetime(2026, 10, 20, 10, 0)    # 12:00 Berlin — день
NIGHT_UTC = datetime(2026, 10, 20, 1, 0)    # 03:00 Berlin — ночь


def test_sla_reminds_once_when_due(sla_env, tmp_db):
    a = _in(tmp_db, 33, NOON_UTC, thread="A")               # срок наступил 3 мин назад
    _in(tmp_db, 10, NOON_UTC, thread="B")                   # ещё рано
    assert "напомнили про 1" in sla_env.sla_reminders_job(NOON_UTC)
    assert len(sla_env._sent) == 1 and f"#{a}" in sla_env._sent[0]
    later = NOON_UTC + timedelta(minutes=5)
    assert sla_env.sla_reminders_job(later) == "sla: ok"    # второй раз — без спама


def test_sla_no_repeat_after_restart(sla_env, tmp_db):
    """Рестарт: память сброшена, но письмо со сроком час назад не всплывает снова."""
    _in(tmp_db, 90, NOON_UTC, thread="A")
    assert sla_env.sla_reminders_job(NOON_UTC) == "sla: ok"


def test_sla_skips_answered(sla_env, tmp_db):
    _in(tmp_db, 33, NOON_UTC, thread="A")
    tmp_db.add_message(account_id=1, direction="out", gmail_thread_id="A", status="sent",
                       sent_at=(NOON_UTC - timedelta(minutes=5)).isoformat())
    assert sla_env.sla_reminders_job(NOON_UTC) == "sla: ok"


def test_sla_night_only_hot_and_morning_shift(sla_env, tmp_db):
    with tmp_db.get_conn() as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN is_hot INTEGER DEFAULT 0")
    a = _in(tmp_db, 33, NIGHT_UTC, thread="A")              # обычное ночью — молчим
    h = _in(tmp_db, 12, NIGHT_UTC, thread="H", is_hot=1)    # горячее, порог 10 мин
    assert "напомнили про 1" in sla_env.sla_reminders_job(NIGHT_UTC)
    assert f"🔥 #{h}" in sla_env._sent[0]
    morning = datetime(2026, 10, 20, 6, 2)                  # 08:02 Berlin — срок обычного перенесён на 08:00
    assert "напомнили про 1" in sla_env.sla_reminders_job(morning) and f"#{a}" in sla_env._sent[-1]


def test_sla_persists_reminded_column(sla_env, tmp_db):
    with tmp_db.get_conn() as conn:
        conn.execute("ALTER TABLE messages ADD COLUMN sla_reminded_at TEXT")
    a = _in(tmp_db, 33, NOON_UTC, thread="A")
    sla_env.sla_reminders_job(NOON_UTC)
    assert tmp_db.get_message(a)["sla_reminded_at"]
