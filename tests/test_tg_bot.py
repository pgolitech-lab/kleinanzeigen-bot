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
    t = _row(id=5, gmail_thread_id="t1", has_pending_draft=1, last_event_kind="in", client_lang="de", buyer_display_name="Klaus M.",
             buyer_name="k@x", ru_client="Скамья ещё есть?", de_client="Ist die Bank noch da?")
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), \
         patch.object(tb, "db") as mdb:
        mdb.pipeline_threads.return_value = [t]
        tb._handle_update({"update_id": 1, "message": {"chat": {"id": 111}, "from": {"id": 111}, "text": "/inbox"}})
    (method, payload), = sent
    assert "Ждут ответа: 1" in payload["text"] and "🇩🇪" in payload["text"]
    buttons = [b for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons[0]["text"].startswith("✅ Ответить") and buttons[1]["callback_data"] == "skip:5:p"
    assert "Скамья ещё есть" in payload["text"]
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
    assert [b["text"] for b in kb[0]] == ["✅ Ответить", "⏭ Пропустить"]
    assert kb[0][1]["callback_data"] == "skip:7" and "💬 Переписка" in [b["text"] for b in kb[2]]


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
    assert "Писем: 1" in text and "Ждут ответа сейчас: <b>1</b>" in text
    with patch.object(tb, "notify") as n:
        assert tb.send_evening_summary() == "итог дня отправлен"
    assert n.call_args.kwargs["kind"] == "evening_summary"


def test_hot_buyer_card_and_loud_at_night(cfg):
    msg = _row(id=7, gmail_thread_id="t1", buyer_display_name="Klaus", buyer_name="k@x", ad_title="Bank",
               ad_price="450 €", ru_client="Беру сегодня", de_client="x", ru_translation="", ru_answer="",
               is_hot=1, hot_reason="готов забрать сегодня")
    with patch.object(tb, "db") as mdb:
        mdb.thread_history.return_value = []
        text = tb._build_review_text(msg)
        assert text.startswith("🔥 <b>горячий покупатель</b>: готов забрать сегодня")
        sent = []
        mdb.get_message.return_value = msg
        with patch.object(tb, "quiet_now", return_value=True), \
             patch.object(tb, "_http_post_single", lambda m, p: sent.append(p) or {"message_id": 1}):
            tb.send_for_review(7)
    assert all(p["disable_notification"] is False for p in sent)


def test_quiet_hours_and_urgent(cfg):
    from datetime import datetime
    assert tb.quiet_now(datetime(2026, 10, 20, 23)) and tb.quiet_now(datetime(2026, 10, 20, 3))
    assert not tb.quiet_now(datetime(2026, 10, 20, 12))
    sent = []
    with patch.object(tb, "quiet_now", return_value=True), \
         patch.object(tb, "_http_post_single", lambda m, p: sent.append(p) or {"message_id": 1}):
        tb.notify("x")
        tb.notify("🔥", urgent=True)
    assert [p["disable_notification"] for p in sent] == [True, True, False, False]


def _live_env(tmp_path, monkeypatch):
    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    sent = []

    def fake(method, p):
        sent.append((method, p))
        if method == "editMessageText" and p["message_id"] == 404:
            raise RuntimeError("Telegram API ошибка: Bad Request: message to edit not found")
        return {"message_id": 100 + len(sent)}
    monkeypatch.setattr(tb, "_http_post_single", fake)
    monkeypatch.setattr(tb.config, "telegram_operator_dm_ids", lambda: ["1"])
    monkeypatch.setattr(tb, "quiet_now", lambda *a: False)
    return database, sent


def test_live_card_edits_in_place_and_resends_when_gone(tmp_path, monkeypatch):
    database, sent = _live_env(tmp_path, monkeypatch)
    tb._card_put("T", "a", "thread_T", "x", kind="k")
    tb._card_put("T", "b", "thread_T", "x", kind="k")
    assert [m for m, _ in sent] == ["sendMessage", "editMessageText"]  # вторая — правка, не новое сообщение
    assert sent[1][1]["message_id"] == 101
    tb._card_record("T", "1", 404)  # карточку в чате удалили
    tb._card_put("T", "c", "thread_T", "x", kind="k")
    assert [m for m, _ in sent][-2:] == ["editMessageText", "sendMessage"]
    # replace: новая со звуком, старая удаляется после неё (urgent)
    tb._card_put("T", "d", "thread_T", "x", kind="k", urgent=True, replace=True)
    assert [m for m, _ in sent][-2:] == ["sendMessage", "deleteMessage"]


def test_live_card_all_failed_raises(tmp_path, monkeypatch):
    _live_env(tmp_path, monkeypatch)
    monkeypatch.setattr(tb, "_http_post_single", lambda m, p: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        tb._card_put("T", "a", "thread_T", "x", kind="k")


def test_autopilot_one_card_and_batch_reminders_and_morning(tmp_path, monkeypatch):
    database, sent = _live_env(tmp_path, monkeypatch)
    acc = database.add_account("a", "a@gmail.com", "pw")
    m1 = database.add_message(acc, "in", gmail_thread_id="T", de_client="Hallo", status="pending",
                              buyer_display_name="Klaus", ad_title="Bank", de_answer="Gern")
    tb.send_autopilot_start_notification(m1, 300.0, "@pg")
    tb.send_autopilot_progress(m1, 1)
    tb.send_autopilot_stop_notification(m1, "limit")        # не срочная — правка
    assert [m for m, _ in sent] == ["sendMessage", "editMessageText", "editMessageText"]
    assert "Автопилот остановлен" in sent[-1][1]["text"]
    tb.send_autopilot_stop_notification(m1, "threat")       # срочная — новая со звуком
    assert [m for m, _ in sent][-2:] == ["sendMessage", "deleteMessage"]
    assert sent[-2][1]["disable_notification"] is False
    sent.clear()
    m2 = database.add_message(acc, "in", gmail_thread_id="U", de_client="x", status="sent",
                              buyer_display_name="Anna", ad_title="Tür")
    assert tb.send_reminder_batch([], 2) is None
    tb.send_reminder_batch([m1, m2], 2)
    assert len(sent) == 1 and "Пора напомнить: 2" in sent[0][1]["text"] and "Anna" in sent[0][1]["text"]
    sent.clear()
    assert tb.send_morning_summary() == "утренняя сводка отправлена"
    assert "Ждут ответа" in sent[0][1]["text"] and sent[0][1]["reply_markup"]


def test_card_has_flag_and_autopilot_status():
    msg = _row(id=7, gmail_thread_id="t1", buyer_display_name="Klaus", buyer_name="k@x", client_lang="de",
               ad_title="Bank", ad_price="450 €", ru_client="Ещё есть?", de_client="Noch da?",
               ru_translation="", ru_answer="")
    with patch.object(tb, "db") as mdb, patch.object(tb, "config") as mc:
        mdb.thread_history.return_value = [_row(direction="in", de_client="a")]
        mdb.get_thread_autopilot.return_value = {"active": 1, "messages_sent": 3}
        mc.autopilot_message_cap.return_value = 20
        text = tb._build_review_text(msg)
    assert "🇩🇪 <b>Klaus</b>" in text and "🤖 автопилот 3/20" in text and "🏷 Bank · 450" in text


def test_waiting_count_is_miniapp_definition():
    rows = [_row(last_event_kind="in", is_pinned=1), _row(last_event_kind="in", is_pinned=0),
            _row(last_event_kind="out", is_pinned=0)]
    assert len(tb.waiting_threads(rows)) == 2


def test_skip_callback_edits_panel_and_unskip(cfg):
    sent = []
    t = _row(id=5, gmail_thread_id="t1", last_event_kind="in", buyer_display_name="Klaus", buyer_name="k@x",
             client_lang="de", ad_title="Bank", ru_client="Ещё есть?", de_client="x", is_pinned=0,
             last_event_at="2026-10-20T10:00:00")
    cb = {"id": "cb1", "from": {"id": 111, "username": "pg"}, "data": "skip:5:p",
          "message": {"chat": {"id": 111}, "message_id": 9, "text": "панель"}}
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), \
         patch.object(tb, "db") as mdb, patch.object(tb, "_claim_lock", return_value=None), \
         patch.object(tb, "_release_lock"):
        mdb.get_message.return_value = _row(id=5, status="pending", gmail_thread_id="t1")
        mdb.pipeline_threads.return_value = []
        mdb.get_thread_autopilot.return_value = None
        tb._handle_update({"update_id": 1, "callback_query": cb})
        mdb.update_message.assert_called_with(5, status="skipped")
        assert [m for m, _ in sent] == ["answerCallbackQuery", "editMessageText"]
        assert "Никто не ждёт" in sent[1][1]["text"]
        # уже отправленное — не трогаем, объясняем человеческим текстом
        mdb.update_message.reset_mock(); sent.clear()
        mdb.get_message.return_value = _row(id=5, status="sent", gmail_thread_id="t1")
        tb._handle_update({"update_id": 2, "callback_query": cb})
        mdb.update_message.assert_not_called()
        assert sent[0][1]["show_alert"] and "уже обработано" in sent[0][1]["text"]


def test_skip_callback_unauthorized_ignored(cfg):
    sent = []
    cb = {"id": "cb1", "from": {"id": 666}, "data": "skip:5", "message": {"chat": {"id": 1}, "message_id": 9}}
    with patch.object(tb, "_http_post_single", lambda m, p: sent.append((m, p)) or {}), patch.object(tb, "db") as mdb:
        tb._handle_update({"update_id": 1, "callback_query": cb})
    assert sent == [] and not mdb.update_message.called
