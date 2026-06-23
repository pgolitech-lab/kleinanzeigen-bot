"""Telegram-бот: ТОЛЬКО исходящие уведомления оператору (sync, urllib stdlib).

Архитектура (2026-06-23): ставка на сервер. Вся работа оператора — в Mini App /
веб-морде. Бот НЕ принимает ввод — нет long-polling, нет PTB, нет handlers.
Каждое событие → одно сообщение + одна web_app-кнопка «Открыть» в нужный экран MA.

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
    """Уведомление о входящем (новое обращение / ответ клиента) + кнопка → review-экран MA."""
    msg = db.get_message(message_id)
    if not msg:
        logger.warning("send_for_review: сообщение %s не найдено", message_id)
        return None
    thread_id = _safe(msg, "gmail_thread_id") or ""
    is_followup = False
    try:
        is_followup = len(db.thread_history(thread_id)) > 1 if thread_id else False
    except Exception:
        pass
    buyer = _html(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент")
    ad = _html(_safe(msg, "ad_title") or "—")
    price = _safe(msg, "ad_price")
    snippet = _html((_safe(msg, "de_client") or "").replace("\n", " ").strip()[:180])
    head = "💬 <b>Клиент ответил</b>" if is_followup else "🆕 <b>Новое обращение</b>"
    price_s = f" · {_html(str(price))}" if price else ""
    text = (f"{head} · #{message_id}\n"
            f"👤 {buyer} · 🏷 {ad}{price_s}\n"
            f"<i>{snippet}</i>")
    r = notify(text, f"review_{message_id}")
    mid = r.get("message_id") if isinstance(r, dict) else None
    if mid:
        try:
            db.update_message(message_id, telegram_message_id=mid)
        except Exception:
            pass
    return mid


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


def thread_is_busy(thread_id: Optional[str]) -> bool:
    if not thread_id:
        return False
    entry = _THREAD_BUSY.get(thread_id)
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
    mark_thread_busy(_msg_thread_id(msg_id), "operator", actor)


def _release_lock(msg_id: int) -> None:
    operator_lock.forget(msg_id)
    thread_id = _msg_thread_id(msg_id)
    clear_thread_busy(thread_id, "operator")
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
