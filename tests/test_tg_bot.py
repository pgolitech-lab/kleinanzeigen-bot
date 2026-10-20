"""Telegram-бот: авторизация поллера, карточка письма по-русски, недоставка, «⚙️» один раз, лог tg sent."""
from unittest.mock import MagicMock, patch

import pytest

from modules import telegram_bot as tb


def _row(**d):
    r = MagicMock()
    r.__getitem__.side_effect = d.__getitem__
    r.keys.return_value = d.keys()
    return r


@pytest.fixture
def cfg():
    with patch.object(tb, "config") as c:
        c.telegram_authorized_ids.return_value = {"111"}
        c.telegram_operator_dm_ids.return_value = ["111", "222"]
        c.telegram_chat_id.return_value = "111"
        c.get.return_value = ""
        yield c


def test_unauthorized_command_and_button_ignored(cfg, caplog):
    sent = []
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), \
         patch.object(tb, "db") as mdb:
        tb._handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "from": {"id": 999}, "text": "/tasks"}})
        tb._handle_update({"update_id": 2, "callback_query": {"id": "c", "from": {"id": 999}, "data": "close_t1",
                                                              "message": {"chat": {"id": 999}}}})
    assert sent == []
    mdb.close_thread.assert_not_called()
    mdb.pipeline_threads.assert_not_called()
    assert "неавторизованного" in caplog.text


def test_authorized_inbox_lists_threads(cfg):
    sent = []
    t = _row(id=5, gmail_thread_id="t1", has_pending_draft=1, buyer_display_name="Klaus M.",
             buyer_name="k@x", ru_client="Скамья ещё есть?", de_client="Ist die Bank noch da?")
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), \
         patch.object(tb, "db") as mdb:
        mdb.pipeline_threads.return_value = [t]
        tb._handle_update({"update_id": 1, "message": {"chat": {"id": 111}, "from": {"id": 111}, "text": "/inbox"}})
    (method, payload), = sent
    assert "Ждут ответа: 1" in payload["text"]
    buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[0]["text"].startswith("Klaus M. · Скамья")
    assert not any("🗑" in b["text"] for b in buttons)


def test_old_buttons_answer_outdated(cfg):
    sent = []
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), \
         patch.object(tb, "db") as mdb:
        tb._handle_update({"update_id": 1, "callback_query": {"id": "c", "from": {"id": 111}, "data": "close_t1",
                                                              "message": {"chat": {"id": 111}}}})
    mdb.close_thread.assert_not_called()
    assert sent[0][0] == "answerCallbackQuery" and "устарела" in sent[0][1]["text"]


def test_review_card_in_russian_short():
    msg = _row(id=7, gmail_thread_id="t1", buyer_display_name="Klaus M.", buyer_name="k@x",
               ad_title="Rückbank Peugeot", ad_price="450 €", ru_client="Скамья ещё есть? Последняя цена 300?",
               de_client="Ist die Bank noch da? Letzter Preis 300?", ru_translation="Да, есть.", ru_answer="")
    with patch.object(tb, "db") as mdb:
        mdb.thread_history.return_value = [_row(direction="in", de_client="a"), _row(direction="in", de_client="b")]
        text = tb._build_review_text(msg)
    lines = text.split("\n")
    assert len(lines) <= 6 and "Скамья ещё есть" in text and "Ist die Bank" not in text
    assert "#7" not in text and "Ещё активных" not in text and "Черновик" in text
    kb = tb._review_kb(msg)["inline_keyboard"]
    assert [b["text"] for b in kb[0]] == ["✅ Ответить", "💬 Переписка"]


def test_fanout_all_failed_raises_partial_ok(cfg):
    def boom(m, p):
        raise RuntimeError("Forbidden: bot was blocked")
    with patch.object(tb, "_http_post_single", boom):
        with pytest.raises(RuntimeError, match="ни одному"):
            tb.notify("x")

    def half(m, p):
        if p["chat_id"] == "222":
            raise RuntimeError("blocked")
        return {"message_id": 1}
    with patch.object(tb, "_http_post_single", half):
        assert tb.notify("x") == {"message_id": 1}


def test_clear_reply_keyboards_only_once(cfg):
    calls = []
    with patch.object(tb, "_http_post_single", lambda m, p: calls.append(m) or {"message_id": 5}), \
         patch.object(tb, "db") as mdb:
        tb.clear_reply_keyboards()
        mdb.set_setting.assert_called_once_with(tb._REPLY_KB_FLAG, "1")
        cfg.get.return_value = "1"
        calls.clear()
        tb.clear_reply_keyboards()
    assert calls == []


def test_tg_sent_log_line(cfg, caplog):
    import io, json, logging
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps({"ok": True, "result": {"message_id": 3}}).encode()
    cfg.telegram_bot_token.return_value = "t"
    with patch("urllib.request.urlopen", return_value=resp) as uo, caplog.at_level(logging.INFO):
        tb._http_post_single("sendMessage", {"chat_id": 111, "text": "hi", "_kind": "review", "_thread": "t1"})
    assert "tg sent: review chat=111 thread=t1" in caplog.text
    body = json.loads(uo.call_args.args[0].data)
    assert "_kind" not in body and "_thread" not in body


def test_notify_admin_without_chat_is_not_sent(cfg):
    with patch.object(tb, "_http_post_single") as p:
        assert tb.notify_admin("ошибка") is False
    p.assert_not_called()


def test_first_letter_with_auto_ack_is_new():
    msg = _row(id=7, gmail_thread_id="t1", buyer_display_name="Anna", buyer_name="a@x", ad_title="X",
               ad_price=None, ru_client="Привет", de_client="Hallo", ru_translation="", ru_answer="")
    with patch.object(tb, "db") as mdb:
        mdb.thread_history.return_value = [_row(direction="in", de_client="Hallo"),
                                           _row(direction="out", de_client=None)]  # auto-ack
        assert tb._build_review_text(msg).startswith("🆕")


def test_release_other_card_keeps_thread_busy():
    """Уход с экрана старого письма треда не снимает «занят» по живому локу другого оператора."""
    from modules import operator_lock
    with patch.object(tb, "_msg_thread_id", return_value="T"), patch.object(tb, "run_in_background"):
        operator_lock.forget(11); operator_lock.forget(10)
        assert tb._claim_lock(11, "@a") is None
        assert tb.thread_is_busy("T")
        assert tb._release_lock(10, "@b") is True
        assert tb.thread_is_busy("T")  # A ещё работает
        assert tb._release_lock(11, "@a") is True
        assert not tb.thread_is_busy("T")


def test_evening_summary(tmp_path, monkeypatch):
    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    assert tb.evening_summary_text() is None  # пустой день
    acc = database.add_account("a", "a@gmail.com", "pw")
    database.add_message(acc, "in", gmail_thread_id="T", de_client="Hallo", status="pending")
    text = tb.evening_summary_text()
    assert "Писем: 1" in text and "Ждут ответа сейчас: 1" in text
    with patch.object(tb, "notify") as n:
        assert tb.send_evening_summary() == "итог дня отправлен"
    assert n.call_args.kwargs["kind"] == "evening_summary"
