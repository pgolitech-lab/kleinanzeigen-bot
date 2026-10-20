"""check_llm_login_job: уведомление при потере входа в подписку — один раз, потом напоминание, и при восстановлении."""
from unittest.mock import patch

from modules import telegram_bot as tb


def _run(ok, now):
    with patch("modules.llm.status", return_value=(ok, "msg")), \
         patch.object(tb, "notify") as n, patch.object(tb.time, "time", return_value=now):
        tb.check_llm_login_job()
    return n.call_count


def test_login_watch_dedup_and_recovery():
    tb._llm_login_state.update(ok=True, alerted_at=0.0)
    assert _run(True, 1000) == 0          # всё хорошо — тишина
    assert _run(False, 1000) == 1         # вход пропал — алерт
    assert _run(False, 2000) == 0         # дедуп
    assert _run(False, 1000 + tb.LLM_LOGIN_REMIND_SEC) == 1  # напоминание
    assert _run(True, 99999) == 1         # восстановилось — сообщение
    assert _run(True, 99999) == 0


def _run_msg(ok, msg, now, sent=True):
    with patch("modules.llm.status", return_value=(ok, msg)), \
         patch.object(tb, "notify", return_value={"message_id": 1} if sent else {}) as n, \
         patch.object(tb.time, "time", return_value=now):
        tb.check_llm_login_job()
    return n.call_count


def test_check_failure_alerts_only_after_repeats():
    tb._llm_login_state.update(ok=True, alerted_at=0.0, check_fails=0)
    for _ in range(tb.LLM_CHECK_FAILS_ALERT - 1):
        assert _run_msg(False, "Ошибка проверки: timeout", 1000) == 0
    assert _run_msg(False, "Ошибка проверки: timeout", 1000) == 1


def test_undelivered_alert_is_retried():
    tb._llm_login_state.update(ok=True, alerted_at=0.0, check_fails=0)
    assert _run_msg(False, "CLI не авторизован", 1000, sent=False) == 1
    assert _run_msg(False, "CLI не авторизован", 1060) == 1  # не дошло — пробуем снова, а не ждём 6 ч
    assert _run_msg(False, "CLI не авторизован", 1120) == 0
