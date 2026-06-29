"""Telegram-бот: уведомления оператору + интерактивная сводка задач (sync, urllib stdlib).

Архитектура (2026-06-23): Вся основная работа оператора — в Mini App / веб-морде.
Бот шлёт уведомления-ссылки в MA и обрабатывает команду /tasks (сводка висячих задач
с кнопками «открыть» → MA и «🗑 закрыть тред»).

Lock/thread-busy обёртки оставлены здесь (их зовут scheduler/api_ma) — они про
in-memory concurrency поверх modules/operator_lock, к Telegram отношения не имеют.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

import config
import database as db
from modules import operator_lock

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Mini App URL для deep-link / web_app кнопок. Pages serves SPA из main:/web-app/.
MA_BASE_URL = "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/"


def _ma_deep_link(start_param: str) -> str:
    """URL для web_app кнопки. start_param: 'review_<id>' / 'thread_<id>' / 'dashboard'."""
    return f"{MA_BASE_URL}?tgWebAppStartParam={start_param}"


def _safe(row: Any, key: str, default: Any = None) -> Any:
    """Безопасный доступ к sqlite3.Row (нет .get())."""
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _html(s: Optional[str]) -> str:
    """HTML-escape для parse_mode=HTML."""
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_html_safe(text: str, limit: int = 4090) -> str:
    """Грубая, но безопасная обрезка под лимит Telegram 4096.

    Режет по последней «безопасной» границе (перенос/пробел) и, если в хвосте
    остался незакрытый тег ('<' без '>'), отбрасывает его. Уведомления короткие —
    срабатывает редко.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n\n", "\n", " "):
        i = cut.rfind(sep)
        if i > limit * 0.6:
            cut = cut[:i]
            break
    lt = cut.rfind("<")
    if lt > cut.rfind(">"):
        cut = cut[:lt]
    return cut.rstrip() + "\n\n<i>…</i>"


# ============================================================
# HTTP API (sync)
# ============================================================

def _http_post_single(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Сырой POST к Telegram Bot API. Длинный HTML auto-truncate."""
    token = config.telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан Telegram bot token в настройках")
    if method in ("sendMessage", "editMessageText"):
        text = payload.get("text")
        if isinstance(text, str) and len(text) > 4096:
            payload = dict(payload)
            payload["text"] = _truncate_html_safe(text)
    url = f"{TELEGRAM_API}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            raise RuntimeError(f"Telegram API ошибка: {err_body.get('description', e.reason)}")
        except json.JSONDecodeError:
            raise RuntimeError(f"Telegram API HTTP {e.code}: {e.reason}")
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API ошибка: {body.get('description')}")
    return body.get("result", {})


def _http_post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST + DM-fanout: если operator_dm_ids непуст и chat_id == primary —
    рассылаем sendMessage каждому DM-id. Возвращаем первый result."""
    if method == "sendMessage":
        dm_ids = config.telegram_operator_dm_ids()
        primary = config.telegram_chat_id()
        target = str(payload.get("chat_id", ""))
        if dm_ids and target == str(primary):
            first_result: dict[str, Any] = {}
            for dm_id in dm_ids:
                p = dict(payload)
                p["chat_id"] = dm_id
                try:
                    r = _http_post_single(method, p)
                    if not first_result:
                        first_result = r
                except Exception:
                    logger.exception("DM-fanout: send to %s failed", dm_id)
            return first_result
    return _http_post_single(method, payload)


# ============================================================
# Уведомления
# ============================================================

def _open_kb(start_param: str, label: str = "📋 Открыть в MA") -> dict:
    """Inline-клавиатура из одной web_app-кнопки → экран MA."""
    return {"inline_keyboard": [[{
        "text": label, "web_app": {"url": _ma_deep_link(start_param)},
    }]]}


def _review_kb(message_id: int) -> dict:
    """Keyboard для review-уведомления: открыть MA + кнопка Прочитал."""
    return {"inline_keyboard": [[
        {"text": "📋 Открыть MA", "web_app": {"url": _ma_deep_link(f"review_{message_id}")}},
        {"text": "👁 Прочитал", "callback_data": f"read_{message_id}"},
    ]]}


def _build_review_text(message_id: int, reads: list[str]) -> str:
    """Строит текст уведомления: событие + список активных задач + read-метки."""
    msg = db.get_message(message_id)
    if not msg:
        return f"Сообщение #{message_id}"
    thread_id = _safe(msg, "gmail_thread_id") or ""
    is_followup = False
    try:
        is_followup = len(db.thread_history(thread_id)) > 1 if thread_id else False
    except Exception:
        pass
    buyer = _html(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент")
    ad = _html(_safe(msg, "ad_title") or "—")
    price = _safe(msg, "ad_price")
    snippet = _html((_safe(msg, "de_client") or "").replace("\n", " ").strip()[:150])
    head = "💬 <b>Ответил клиент</b>" if is_followup else "🆕 <b>Новое обращение</b>"
    price_s = f" · {_html(str(price))}" if price else ""
    text = (f"{head} · #{message_id}\n"
            f"👤 {buyer} · 🏷 {ad}{price_s}\n"
            f"<i>{snippet}</i>")
    # Список других активных задач
    try:
        all_threads = [t for t in db.pipeline_threads() if t["has_pending_draft"]]
        others = [t for t in all_threads if _safe(t, "gmail_thread_id") != thread_id]
        if others:
            lines = []
            for t in others[:5]:
                b = (_safe(t, "buyer_display_name") or _safe(t, "buyer_name") or "?")[:14]
                sn = (_safe(t, "de_client") or "").replace("\n", " ").strip()[:28]
                lines.append(f"• {_html(b)} — {_html(sn)}")
            extra = f" (+{len(others) - 5})" if len(others) > 5 else ""
            text += f"\n\n📋 Ещё активных: {len(all_threads)}{extra}\n" + "\n".join(lines)
    except Exception:
        pass
    if reads:
        text += "\n\n👁 " + " · ".join(reads)
    return text


def notify(text: str, start_param: Optional[str] = None,
           label: str = "📋 Открыть в MA") -> dict[str, Any]:
    """Универсальное уведомление: текст + (если задан target) кнопка «Открыть»."""
    payload: dict[str, Any] = {
        "chat_id": config.telegram_chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if start_param:
        payload["reply_markup"] = _open_kb(start_param, label)
    return _http_post("sendMessage", payload)


def set_menu_button() -> None:
    """Нативная Menu Button у поля ввода → открывает MA-дашборд. Ставится 1 раз при старте."""
    dm_ids = config.telegram_operator_dm_ids() or [config.telegram_chat_id()]
    btn = {"type": "web_app", "text": "📋 MA",
           "web_app": {"url": _ma_deep_link("dashboard")}}
    for chat in dm_ids:
        try:
            _http_post_single("setChatMenuButton",
                              {"chat_id": int(chat), "menu_button": btn})
        except Exception:
            logger.exception("set_menu_button fail chat=%s", chat)


def send_for_review(message_id: int) -> Optional[int]:
    """Уведомление о входящем: список активных + кнопки MA + Прочитал. Fanout в все DM."""
    if not db.get_message(message_id):
        logger.warning("send_for_review: сообщение %s не найдено", message_id)
        return None
    text = _build_review_text(message_id, reads=[])
    kb = _review_kb(message_id)
    dm_ids = config.telegram_operator_dm_ids() or [str(config.telegram_chat_id())]
    db.clear_card_dispatches(message_id)
    first_mid: Optional[int] = None
    for dm_id in dm_ids:
        try:
            r = _http_post_single("sendMessage", {
                "chat_id": dm_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": kb,
            })
            tg_mid = r.get("message_id") if isinstance(r, dict) else None
            if tg_mid:
                db.add_card_dispatch(message_id, str(dm_id), tg_mid)
                if first_mid is None:
                    first_mid = tg_mid
        except Exception:
            logger.exception("send_for_review fanout to %s failed", dm_id)
    if first_mid:
        try:
            db.update_message(message_id, telegram_message_id=first_mid)
        except Exception:
            pass
    return first_mid


def _edit_all_dispatches(message_id: int, text: str, kb: dict) -> None:
    """Обновить все fanout-копии уведомления (для read-receipt и пр.)."""
    for d in db.list_card_dispatches(message_id):
        try:
            _http_post_single("editMessageText", {
                "chat_id": d["chat_id"],
                "message_id": d["tg_msg_id"],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": kb,
            })
        except RuntimeError as e:
            if "message is not modified" not in str(e):
                logger.error("edit dispatch %s/%s fail: %s", d["chat_id"], d["tg_msg_id"], e)
        except Exception:
            logger.exception("edit dispatch %s/%s fail", d["chat_id"], d["tg_msg_id"])


def send_reminder_offer(out_message_id: int, days_silent: int) -> Optional[int]:
    """Уведомление: исходящее висит без ответа N дней — пора напомнить. Кнопка → тред MA."""
    msg = db.get_message(out_message_id)
    if not msg:
        return None
    buyer = _html(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент")
    ad = _html(_safe(msg, "ad_title") or "—")
    text = (f"⏰ <b>Пора напомнить</b> · #{out_message_id}\n"
            f"👤 {buyer} · 🏷 {ad}\n"
            f"<i>Клиент молчит {days_silent} дн. — открой тред и напиши follow-up.</i>")
    thread_id = _safe(msg, "gmail_thread_id") or ""
    r = notify(text, f"thread_{thread_id}", label="📋 Открыть тред")
    return r.get("message_id") if isinstance(r, dict) else None


# ---- Autopilot notifications (зовутся из scheduler._autopilot_dispatch / api_ma) ----

_AUTOPILOT_STOP_REASONS = {
    "limit": ("🛑", "исчерпан лимит 20 сообщений"),
    "ready_to_buy": ("🎯", "клиент готов покупать, иди закрывай сделку"),
    "wants_contact": ("📞", "просит контакты/реквизиты, передаёт человеку"),
    "threat": ("⚠️", "клиент угрожает, бот предупредил"),
    "manual": (None, None),  # silent
    "stopped_by_sonnet": ("🤖", "Sonnet решил что нужно остановиться"),
}


def send_autopilot_stop_notification(msg_id: int, reason: str) -> None:
    """Пинг при остановке автопилота. reason='manual' — silent."""
    info = _AUTOPILOT_STOP_REASONS.get(reason, ("🛑", reason))
    if info[0] is None:
        return
    msg = db.get_message(msg_id)
    if not msg:
        return
    ap = db.get_thread_autopilot(_safe(msg, "gmail_thread_id") or "")
    sent_count = ap["messages_sent"] if ap else 0
    text = (f"🛑 <b>АВТОПИЛОТ ОСТАНОВЛЕН</b> · #{msg_id}\n"
            f"Причина: {info[0]} {info[1]}\n"
            f"Отправлено: <code>{sent_count}/20</code>")
    notify(text, f"thread_{_safe(msg, 'gmail_thread_id') or ''}", label="📋 Открыть тред")


def send_autopilot_progress(msg_id: int, count: int) -> None:
    """Короткий пинг на каждый авто-ответ (notify_mode='notify')."""
    msg = db.get_message(msg_id)
    if not msg:
        return
    excerpt = _html((_safe(msg, "de_answer") or "").replace("\n", " ").strip()[:80])
    notify(f"🤖 <code>#{msg_id}</code> · автопилот {count}/20: <i>{excerpt}</i>",
           f"thread_{_safe(msg, 'gmail_thread_id') or ''}", label="📋 Открыть тред")


def send_autopilot_start_notification(msg_id: int, floor: float, actor: str) -> None:
    """Пинг при включении автопилота (notify mode)."""
    msg = db.get_message(msg_id)
    thread_id = _safe(msg, "gmail_thread_id") or "" if msg else ""
    notify(f"🚀 <b>АВТОПИЛОТ ВКЛЮЧЁН</b> · #{msg_id}\n"
           f"floor: <code>{int(floor)}€</code> · запустил {_html(actor)}",
           f"thread_{thread_id}", label="📋 Открыть тред")


# ============================================================
# Lock + thread-busy обёртки (concurrency, не Telegram).
# Зовутся из scheduler/api_ma. Поверх modules/operator_lock.
# ============================================================

# Per-thread «занят»: пока оператор работает с row треда (active lock) ИЛИ идёт
# SMTP-отправка — новые incoming для треда откладываются (status 'deferred').
# kinds: {'operator', 'sending'}.
_THREAD_BUSY: dict[str, dict[str, Any]] = {}
# thread_id -> msg_id of the operator currently holding the lock (for expiry detection)
_THREAD_OP_MSG: dict[str, int] = {}


def thread_is_busy(thread_id: Optional[str]) -> bool:
    if not thread_id:
        return False
    entry = _THREAD_BUSY.get(thread_id)
    if not entry or not entry.get("kinds"):
        return False
    # Auto-expire stale "operator" kind when the actual lock has expired
    if "operator" in entry["kinds"]:
        mid = _THREAD_OP_MSG.get(thread_id)
        if mid is not None and operator_lock.state(mid) is None:
            entry["kinds"].discard("operator")
            _THREAD_OP_MSG.pop(thread_id, None)
            if not entry["kinds"]:
                _THREAD_BUSY.pop(thread_id, None)
                return False
    return bool(entry and entry.get("kinds"))


def mark_thread_busy(thread_id: Optional[str], kind: str, by: str = "?") -> None:
    if not thread_id:
        return
    entry = _THREAD_BUSY.setdefault(
        thread_id, {"kinds": set(), "by": by, "since": datetime.utcnow().isoformat()})
    entry["kinds"].add(kind)
    entry["by"] = by


def clear_thread_busy(thread_id: Optional[str], kind: str) -> None:
    if not thread_id:
        return
    entry = _THREAD_BUSY.get(thread_id)
    if not entry:
        return
    entry["kinds"].discard(kind)
    if not entry["kinds"]:
        _THREAD_BUSY.pop(thread_id, None)


def _msg_thread_id(msg_id: int) -> Optional[str]:
    msg = db.get_message(msg_id)
    if not msg:
        return None
    return msg["gmail_thread_id"] or None


def _check_lock(msg_id: int, actor: str) -> Optional[str]:
    """Имя владельца если лок занят ДРУГИМ. None если свободно/мой."""
    st = operator_lock.state(msg_id)
    if st is None:
        clear_thread_busy(_msg_thread_id(msg_id), "operator")
        return None
    owner, _ = st
    return None if owner == actor else owner


def _acquire_lock(msg_id: int, actor: str) -> None:
    operator_lock.remember(msg_id, actor)
    tid = _msg_thread_id(msg_id)
    mark_thread_busy(tid, "operator", actor)
    if tid:
        _THREAD_OP_MSG[tid] = msg_id


def _release_lock(msg_id: int) -> None:
    operator_lock.forget(msg_id)
    thread_id = _msg_thread_id(msg_id)
    clear_thread_busy(thread_id, "operator")
    if thread_id:
        _THREAD_OP_MSG.pop(thread_id, None)
    if thread_id:
        try:
            import scheduler as _sched
            _sched.drain_deferred_thread(thread_id)
        except Exception:
            logger.exception("drain_deferred_thread fail")


def _lock_remaining_min(msg_id: int) -> int:
    return operator_lock.remaining_min(msg_id)


# ============================================================
# No-op заглушки (бот больше не рисует/синхронизирует карточки).
# Оставлены чтобы не трогать десятки call-site'ов в scheduler/api_ma.
# ============================================================

def refresh_pipeline_for_active_chats() -> None:
    """No-op: интерактивного pipeline в боте больше нет (вся работа в MA)."""
    return None


def broadcast_after_external_action(msg_id: int) -> None:
    """No-op: карточек для синхронизации в боте больше нет."""
    return None


def broadcast_thread_state(gmail_thread_id: str) -> None:
    """No-op: мини-карточек треда в боте больше нет."""
    return None


# ============================================================
# ============================================================
# Обработка устаревших callback-кнопок (до переписки 2026-06-23)
# ============================================================

def send_pending_summary(chat_id: int, edit_msg_id: Optional[int] = None) -> int:
    """Отправить/обновить сводку висячих задач. edit_msg_id → edit in-place."""
    threads = [t for t in db.pipeline_threads() if t["has_pending_draft"]]
    ts = datetime.now().strftime("%H:%M:%S")
    if not threads:
        text = f"✅ <b>Нет висячих задач</b>\n<i>Обновлено: {ts}</i>"
        kb: list = []
    else:
        text = f"📋 <b>Висячих задач: {len(threads)}</b>\n<i>Обновлено: {ts}</i>"
        kb = []
        for t in threads[:15]:
            mid = _safe(t, "id")
            thread_id = _safe(t, "gmail_thread_id") or ""
            buyer = (_safe(t, "buyer_display_name") or _safe(t, "buyer_name") or "?")[:18]
            snippet = (_safe(t, "de_client") or "").replace("\n", " ").strip()[:35]
            n = _safe(t, "pending_drafts_count") or 1
            status = _safe(t, "status") or "new"
            icon = {"new": "🆕", "pending": "⏳", "edited": "✏️",
                    "approved": "✅", "deferred": "⏸"}.get(status, "❓")
            count_s = f"({n}) " if n > 1 else ""
            btn_label = f"{icon} {count_s}{buyer} · {snippet}"[:60]
            kb.append([
                {"text": btn_label, "web_app": {"url": _ma_deep_link(f"review_{mid}")}},
                {"text": "🗑", "callback_data": f"close_{thread_id}"},
            ])
    kb.append([{"text": "🔄 Обновить", "callback_data": "tasks"}])
    markup = {"inline_keyboard": kb}
    if edit_msg_id:
        try:
            _http_post_single("editMessageText", {
                "chat_id": chat_id, "message_id": edit_msg_id,
                "text": text, "parse_mode": "HTML",
                "reply_markup": markup,
            })
        except RuntimeError as exc:
            if "message is not modified" in str(exc):
                logger.warning("send_pending_summary: сообщение не изменилось (не должно случаться с timestamp)")
            else:
                logger.error("send_pending_summary edit fail: %s", exc)
        except Exception:
            logger.exception("send_pending_summary edit fail")
    else:
        try:
            _http_post_single("sendMessage", {
                "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                "reply_markup": markup,
            })
        except Exception:
            logger.exception("send_pending_summary send fail")
    return len(threads)


def set_bot_commands() -> None:
    """Регистрация /tasks в меню бота (показывается при вводе /)."""
    try:
        _http_post_single("setMyCommands", {"commands": [
            {"command": "tasks", "description": "Список висячих задач"},
        ]})
    except Exception:
        logger.exception("setMyCommands fail")


def start_callback_poller() -> None:
    """Daemon-поток: обрабатывать callback_query и команды (message).

    - /tasks → send_pending_summary в чат отправителя
    - callback tasks → обновить сводку in-place
    - callback close_THREAD_ID → закрыть тред + обновить сводку
    - прочие callback → «кнопка устарела» (legacy до 2026-06-23)
    """
    import threading
    import time

    def _run() -> None:
        offset = 0
        while True:
            try:
                r = _http_post_single("getUpdates", {
                    "timeout": 25,
                    "offset": offset,
                    "allowed_updates": ["callback_query", "message"],
                })
                for u in (r if isinstance(r, list) else []):
                    offset = u["update_id"] + 1
                    # ── Команда /tasks ──────────────────────────────────────
                    msg = u.get("message")
                    if msg:
                        text = (msg.get("text") or "").strip()
                        chat_id = (msg.get("chat") or {}).get("id")
                        if chat_id and text.lower().startswith("/tasks"):
                            try:
                                send_pending_summary(chat_id)
                            except Exception:
                                logger.exception("send_pending_summary fail")
                        continue
                    # ── Callback-кнопки ─────────────────────────────────────
                    cb = u.get("callback_query")
                    if not cb:
                        continue
                    data = (cb.get("data") or "").strip()
                    cb_msg = cb.get("message") or {}
                    cb_chat = (cb_msg.get("chat") or {}).get("id")
                    cb_mid = cb_msg.get("message_id")
                    try:
                        if data == "tasks":
                            count = send_pending_summary(cb_chat, edit_msg_id=cb_mid)
                            cb_text = f"✅ {count} задач" if count else "✅ Нет задач"
                            _http_post_single("answerCallbackQuery", {
                                "callback_query_id": cb["id"],
                                "text": cb_text,
                            })
                            logger.info("tasks refresh: chat=%s count=%d", cb_chat, count)
                        elif data.startswith("read_"):
                            # Прочитал — пометить + обновить все копии уведомления
                            try:
                                msg_id = int(data[5:])
                            except ValueError:
                                raise RuntimeError(f"bad read_ data: {data!r}")
                            from_user = cb.get("from") or {}
                            reader = from_user.get("first_name") or from_user.get("username") or "?"
                            ts = datetime.now().strftime("%H:%M")
                            db.mark_card_dispatch_read(msg_id, str(cb_chat), f"{reader} · {ts}")
                            reads = [
                                dict(d)["read_by"]
                                for d in db.list_card_dispatches(msg_id)
                                if dict(d).get("read_by")
                            ]
                            new_text = _build_review_text(msg_id, reads)
                            _edit_all_dispatches(msg_id, new_text, _review_kb(msg_id))
                            _http_post_single("answerCallbackQuery", {
                                "callback_query_id": cb["id"],
                                "text": "✅ Отмечено",
                            })
                            logger.info("read: msg=%s reader=%s", msg_id, reader)
                        elif data.startswith("close_"):
                            thread_id = data[6:]
                            db.close_thread(thread_id, closed_by="bot-task-dismiss")
                            count = send_pending_summary(cb_chat, edit_msg_id=cb_mid)
                            _http_post_single("answerCallbackQuery", {
                                "callback_query_id": cb["id"],
                                "text": f"🏁 Тред закрыт · осталось {count}",
                            })
                            logger.info("close thread: %s remaining=%d", thread_id, count)
                        else:
                            _http_post_single("answerCallbackQuery", {
                                "callback_query_id": cb["id"],
                                "text": "Эта кнопка устарела. Используй Mini App — кнопка «📋 MA» снизу чата.",
                                "show_alert": True,
                            })
                    except Exception:
                        logger.exception("callback handler fail data=%r", data)
            except Exception:
                logger.exception("callback_query poll cycle fail")
                time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="cb-poller")
    t.start()
    logger.info("Callback-query poller запущен (daemon)")
