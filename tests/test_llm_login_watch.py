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
