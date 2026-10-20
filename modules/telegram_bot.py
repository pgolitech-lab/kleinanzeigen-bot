"""Telegram-бот: исходящие уведомления оператору + маленький callback-поллер (sync, urllib stdlib).

Архитектура (2026-06-23): вся работа оператора — в Mini App. Бот:
  • шлёт уведомления-ссылки в MA (`notify`, `send_for_review`, автопилот, напоминания);
  • держит daemon-поток `start_callback_poller` (long-poll getUpdates, запускается из main.py):
    команда /tasks → сводка висячих задач; кнопки «👁 Прочитал» (read_N), «🗑» (close_THREAD),
    «🔄 Обновить» (tasks); любые старые кнопки до 2026-06-23 → алерт «кнопка устарела».

Lock/thread-busy обёртки оставлены здесь (их зовут outgoing/incoming/api_ma) — это
in-memory concurrency поверх modules/operator_lock, к Telegram отношения не имеют.
Всё сетевое, что может зваться из HTTP-запроса MA, уходит в фон через `run_in_background`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import config
import database as db
from modules import operator_lock

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Фоновый пул: Telegram-уведомления и drain отложенных карточек не держат HTTP-ответ MA.
# Задачи в памяти: при рестарте теряются (drain страхует джоб drain_deferred раз в 5 мин).
_BG = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg-bg")
_BG_QUEUE_WARN = 20
_bg_pending = 0
_bg_pending_lock = threading.Lock()


def run_in_background(fn, *args, **kwargs) -> None:
    """Выполнить fn в фоне; исключение — в лог (а не в никуда)."""
    global _bg_pending
    with _bg_pending_lock:
        _bg_pending += 1
        pending = _bg_pending
    if pending >= _BG_QUEUE_WARN:
        logger.error("фоновая очередь Telegram: %d задач ждут — Telegram/сеть тормозит?", pending)

    def _job() -> None:
        global _bg_pending
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("фоновая задача %s упала", getattr(fn, "__name__", fn))
        finally:
            with _bg_pending_lock:
                _bg_pending -= 1
    _BG.submit(_job)


# Mini App URL для deep-link / web_app кнопок. Pages serves SPA из main:/web-app/.
MA_BASE_URL = "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/"


# Текущий адрес cloudflared quick-tunnel (пишет scripts/update_tunnel_url.sh, ротируется при
# рестарте туннеля). Передаётся в MA параметром api= — без коммита адреса в git.
TUNNEL_URL_FILE = Path(os.environ.get(
    "KAB_TUNNEL_URL_FILE", "/home/pg/.local/state/kleinanzeigen-bot/tunnel_url"))
_TUNNEL_RE = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")


_tunnel_last_problem: list[Optional[str]] = [None]  # чтобы не писать один и тот же WARNING на каждую кнопку


def tunnel_url() -> Optional[str]:
    """Адрес API для MA из TUNNEL_URL_FILE (читается на каждый вызов). None — нет/битый файл."""
    problem = None
    try:
        url = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
        if not _TUNNEL_RE.fullmatch(url):
            problem = f"в {TUNNEL_URL_FILE} не адрес trycloudflare: {url[:100]!r}"
    except OSError as e:
        problem = f"не прочитать {TUNNEL_URL_FILE}: {e}"
    if problem != _tunnel_last_problem[0]:
        if problem:
            logger.warning("tunnel_url: %s — кнопки MA пойдут без api=", problem)
        _tunnel_last_problem[0] = problem
    return None if problem else url


def _ma_deep_link(start_param: str) -> str:
    """URL для web_app кнопки. start_param: 'review_<id>' / 'thread_<id>' / 'dashboard'.
    Все web_app-кнопки бота идут через эту функцию — добавляем актуальный api=<туннель>."""
    url = f"{MA_BASE_URL}?tgWebAppStartParam={start_param}"
    api = tunnel_url()
    if api:
        url += "&api=" + quote(api, safe="")
    return url


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

_LOGGED_METHODS = {"sendMessage", "editMessageText", "deleteMessage"}


def _http_post_single(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Сырой POST к Telegram Bot API. Длинный HTML auto-truncate.

    payload["_kind"] / ["_thread"] (необязательно) — только для строки журнала
    «tg sent: <kind> chat=… thread=…» (по ней QA считает, сколько шлёт бот); в API не уходят.
    """
    token = config.telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан Telegram bot token в настройках")
    kind = payload.get("_kind") or method
    thread = payload.get("_thread")
    if "_kind" in payload or "_thread" in payload:
        payload = {k: v for k, v in payload.items() if k not in ("_kind", "_thread")}
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
    if method in _LOGGED_METHODS:
        logger.info("tg sent: %s chat=%s%s", kind, payload.get("chat_id"),
                    f" thread={thread}" if thread else "")
    return body.get("result", {})


def _http_post(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST + DM-fanout: если operator_dm_ids непуст и chat_id == primary —
    рассылаем sendMessage каждому DM-id. Возвращаем первый result.

    Не дошло НИ одному оператору → RuntimeError (раньше возвращался {} и вызывающий
    считал, что уведомление ушло). Не дошло части — ERROR в лог, результат успешных.
    """
    if method == "sendMessage":
        dm_ids = config.telegram_operator_dm_ids()
        primary = config.telegram_chat_id()
        target = str(payload.get("chat_id", ""))
        if dm_ids and target == str(primary):
            first_result: dict[str, Any] = {}
            failed: list[str] = []
            for dm_id in dm_ids:
                p = dict(payload)
                p["chat_id"] = dm_id
                try:
                    r = _http_post_single(method, p)
                    if not first_result:
                        first_result = r
                except Exception as e:
                    logger.error("DM-fanout: оператору %s не доставлено: %s", dm_id, e)
                    failed.append(str(dm_id))
            if len(failed) == len(dm_ids):
                raise RuntimeError(f"Telegram: уведомление не доставлено ни одному оператору ({len(failed)})")
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


def _review_kb(msg: Any) -> dict:
    """Карточка письма: ответить (MA сразу на черновике) + вся переписка. Подписи ≤ 14 символов."""
    mid = _safe(msg, "id")
    thread_id = _safe(msg, "gmail_thread_id") or ""
    return {"inline_keyboard": [[
        {"text": "✅ Ответить", "web_app": {"url": _ma_deep_link(f"review_{mid}")}},
        {"text": "💬 Переписка", "web_app": {"url": _ma_deep_link(f"thread_{thread_id}" if thread_id else "pipeline")}},
    ]]}


def _oneline(text: Optional[str], limit: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _build_review_text(msg: Any) -> str:
    """Карточка письма покупателя (≤ 4–5 строк, влезает на внешний экран ~306 px):
    кто · что · перевод письма на русский · черновик бота (если уже есть)."""
    thread_id = _safe(msg, "gmail_thread_id") or ""
    is_followup = False
    try:
        # «ответил» — если до этого письма в треде уже были письма покупателя (auto-ack не в счёт)
        is_followup = sum(1 for r in db.thread_history(thread_id)
                          if _safe(r, "direction") == "in" and _safe(r, "de_client")) > 1 if thread_id else False
    except Exception:
        logger.exception("карточка письма: история треда %s не прочитана", thread_id)
    buyer = _html(_oneline(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент", 24))
    head = f"💬 <b>{buyer}</b> · ответил" if is_followup else f"🆕 <b>{buyer}</b> · новое"
    price = _safe(msg, "ad_price")
    ad_line = _html(_oneline(_safe(msg, "ad_title") or "—", 40)) + (f" · {_html(str(price))}" if price else "")
    # По-русски: перевод уже есть к моменту уведомления; немецкий — только если перевода нет
    letter = _oneline(_safe(msg, "ru_client") or _safe(msg, "de_client"), 160)
    lines = [head, ad_line, f"«{_html(letter)}»"]
    if _safe(msg, "is_hot"):
        reason = _oneline(_safe(msg, "hot_reason"), 60)
        lines.insert(0, "🔥 <b>горячий покупатель</b>" + (f": {_html(reason)}" if reason else ""))
    draft = _oneline(_safe(msg, "ru_translation") or _safe(msg, "ru_answer"), 100)
    if draft:
        lines.append(f"<i>Черновик: «{_html(draft)}»</i>")
    return "\n".join(lines)


def quiet_now(now: Optional[datetime] = None) -> bool:
    """Тихие часы по Берлину (tg_quiet_from..tg_quiet_to, по умолчанию 22–08): пуши без звука."""
    import zoneinfo
    try:
        q_from = int(config.get("tg_quiet_from") or 22)
        q_to = int(config.get("tg_quiet_to") or 8)
    except ValueError:
        logger.error("tg_quiet_from/tg_quiet_to не числа — тихие часы 22–08")
        q_from, q_to = 22, 8
    h = (now or datetime.now(zoneinfo.ZoneInfo("Europe/Berlin"))).hour
    return (q_from <= h or h < q_to) if q_from > q_to else (q_from <= h < q_to)


def notify(text: str, start_param: Optional[str] = None,
           label: str = "📋 Открыть в MA", kind: str = "notify", urgent: bool = False) -> dict[str, Any]:
    """Универсальное уведомление операторам: текст + (если задан target) кнопка «Открыть».
    В тихие часы — без звука, кроме urgent (горячий покупатель, продажа, угроза).
    Не доставлено ни одному — RuntimeError (см. _http_post)."""
    payload: dict[str, Any] = {
        "chat_id": config.telegram_chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": (not urgent) and quiet_now(),
        "_kind": kind,
    }
    if start_param:
        payload["reply_markup"] = _open_kb(start_param, label)
    return _http_post("sendMessage", payload)


def notify_admin(text: str, kind: str = "admin") -> bool:
    """Технические сообщения (дайджест ошибок, поломка scout и т.п.) — в telegram_admin_chat_id,
    а не в чат операторов. Пусто → не шлём (WARNING в лог). True — доставлено."""
    chat = (config.get("telegram_admin_chat_id") or "").strip()
    if not chat:
        logger.warning("notify_admin: telegram_admin_chat_id не задан — тех.сообщение не отправлено: %s",
                       _oneline(text, 200))
        return False
    try:
        _http_post_single("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                          "disable_web_page_preview": True, "_kind": kind})
        return True
    except Exception:
        logger.exception("notify_admin: не доставлено в chat=%s", chat)
        return False


def set_menu_button() -> int:
    """Нативная Menu Button у поля ввода → MA, экран «Входящие» (главная задача оператора).
    Ставится при старте и скриптом ops при смене туннеля. Возвращает число чатов, где
    setChatMenuButton прошёл."""
    dm_ids = config.telegram_operator_dm_ids() or [config.telegram_chat_id()]
    btn = {"type": "web_app", "text": "📥 Входящие",
           "web_app": {"url": _ma_deep_link("pipeline")}}
    ok = 0
    for chat in dm_ids:
        try:
            _http_post_single("setChatMenuButton",
                              {"chat_id": int(chat), "menu_button": btn})
            ok += 1
        except Exception:
            logger.exception("set_menu_button fail chat=%s", chat)
    return ok


_REPLY_KB_FLAG = "tg_reply_keyboard_cleared"


def clear_reply_keyboards() -> None:
    """Убрать старую reply-keyboard (Обновить / Назад) в DM каждого оператора — ОДИН раз
    (флаг в settings). Раньше слалось при каждом рестарте: пуш «⚙️» у всех операторов.
    Отправляем служебное сообщение с remove_keyboard и сразу удаляем его."""
    if config.get(_REPLY_KB_FLAG) == "1":
        return
    dm_ids = config.telegram_operator_dm_ids() or [str(config.telegram_chat_id())]
    ok = 0
    for chat in dm_ids:
        try:
            r = _http_post_single("sendMessage", {
                "chat_id": chat,
                "text": "⚙️",
                "reply_markup": {"remove_keyboard": True},
            })
            msg_id = r.get("message_id") if isinstance(r, dict) else None
            if msg_id:
                try:
                    _http_post_single("deleteMessage", {"chat_id": chat, "message_id": msg_id})
                except Exception:
                    logger.warning("clear_reply_keyboards: служебное «⚙️» не удалено chat=%s", chat)
            ok += 1
        except Exception:
            logger.exception("clear_reply_keyboards fail chat=%s", chat)
    if ok == len(dm_ids):
        db.set_setting(_REPLY_KB_FLAG, "1")


def send_for_review(message_id: int) -> Optional[int]:
    """Карточка нового письма каждому оператору (DM-fanout). Возвращает tg message_id первой
    доставленной копии или None (не доставлено никому / нет письма)."""
    msg = db.get_message(message_id)
    if not msg:
        logger.warning("send_for_review: сообщение %s не найдено", message_id)
        return None
    text = _build_review_text(msg)
    kb = _review_kb(msg)
    silent = quiet_now() and not _safe(msg, "is_hot")  # горячего будим и ночью
    thread_id = _safe(msg, "gmail_thread_id") or ""
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
                "disable_notification": silent,
                "_kind": "review", "_thread": thread_id,
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
            # Карточки уже у операторов: падение здесь заставило бы очередь повторить письмо
            # и разослать дубль — только лог
            logger.exception("send_for_review: telegram_message_id не записан msg=%s", message_id)
    return first_mid


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
    "limit": ("🛑", "исчерпан лимит сообщений"),
    "ready_to_buy": ("🎯", "клиент готов покупать, иди закрывай сделку"),
    "wants_contact": ("📞", "просит контакты/реквизиты, передаёт человеку"),
    "threat": ("⚠️", "клиент угрожает, бот предупредил"),
    "manual": (None, None),  # silent
    "stopped_by_sonnet": ("🤖", "Sonnet решил что нужно остановиться"),
}

# Задержка ОДНОГО ответа guard'ом (автопилот продолжает работать, ответ ждёт оператора)
_AUTOPILOT_HOLD_REASONS = {
    "guard_language": ("🈯", "ответ задержан: не на языке клиента"),
    "guard_floor": ("💶", "ответ задержан: цена ниже минимума"),
}


def send_autopilot_stop_notification(msg_id: int, reason: str) -> None:
    """Пинг при остановке автопилота (reason='manual' — silent) или задержке ответа guard'ом."""
    hold = reason in _AUTOPILOT_HOLD_REASONS
    info = _AUTOPILOT_HOLD_REASONS.get(reason) or _AUTOPILOT_STOP_REASONS.get(reason, ("🛑", reason))
    if info[0] is None:
        return
    msg = db.get_message(msg_id)
    if not msg:
        return
    ap = db.get_thread_autopilot(_safe(msg, "gmail_thread_id") or "")
    sent_count = ap["messages_sent"] if ap else 0
    head = ("⏸ <b>АВТОПИЛОТ: ОТВЕТ ЖДЁТ ПРОВЕРКИ</b>" if hold
            else "🛑 <b>АВТОПИЛОТ ОСТАНОВЛЕН</b>")
    text = (f"{head} · #{msg_id}\n"
            f"Причина: {info[0]} {info[1]}\n"
            f"Отправлено: <code>{sent_count}/{config.autopilot_message_cap()}</code>")
    notify(text, f"thread_{_safe(msg, 'gmail_thread_id') or ''}", label="📋 Открыть тред")


def send_autopilot_progress(msg_id: int, count: int) -> None:
    """Короткий пинг на каждый авто-ответ (notify_mode='notify')."""
    msg = db.get_message(msg_id)
    if not msg:
        return
    excerpt = _html((_safe(msg, "de_answer") or "").replace("\n", " ").strip()[:80])
    notify(f"🤖 <code>#{msg_id}</code> · автопилот {count}/{config.autopilot_message_cap()}: <i>{excerpt}</i>",
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
# Оба словаря трогают и потоки uvicorn (MA), и APScheduler (SMTP/incoming) — только под этим локом
_BUSY_LOCK = threading.RLock()


def thread_is_busy(thread_id: Optional[str]) -> bool:
    if not thread_id:
        return False
    with _BUSY_LOCK:
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
        return bool(entry["kinds"])


def mark_thread_busy(thread_id: Optional[str], kind: str, by: str = "?") -> None:
    if not thread_id:
        return
    with _BUSY_LOCK:
        entry = _THREAD_BUSY.setdefault(
            thread_id, {"kinds": set(), "by": by, "since": datetime.utcnow().isoformat()})
        entry["kinds"].add(kind)
        entry["by"] = by


def clear_thread_busy(thread_id: Optional[str], kind: str) -> None:
    if not thread_id:
        return
    with _BUSY_LOCK:
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


def _claim_lock(msg_id: int, actor: str) -> Optional[str]:
    """Атомарно взять лок карточки. None — взят (или уже наш), иначе имя чужого владельца."""
    foreign = operator_lock.try_acquire(msg_id, actor)
    if foreign is not None:
        return foreign
    tid = _msg_thread_id(msg_id)
    mark_thread_busy(tid, "operator", actor)
    if tid:
        with _BUSY_LOCK:
            _THREAD_OP_MSG[tid] = msg_id
    return None


def _release_lock(msg_id: int, actor: Optional[str] = None) -> bool:
    """Снять лок карточки (+ busy треда, + в фоне поднять отложенные письма).
    С actor — только свой: лок другого оператора не трогаем и возвращаем False."""
    if actor is not None:
        if not operator_lock.forget_if_owner(msg_id, actor):
            return False
    else:
        operator_lock.forget(msg_id)
    thread_id = _msg_thread_id(msg_id)
    if thread_id:
        with _BUSY_LOCK:
            # Тред «занят оператором» по ДРУГОЙ карточке (её лок жив) — не снимаем: иначе уход
            # одного оператора со старого письма открывал тред, пока второй ещё печатает
            other = _THREAD_OP_MSG.get(thread_id)
            if other is not None and other != msg_id and operator_lock.state(other) is not None:
                return True
            _THREAD_OP_MSG.pop(thread_id, None)
            clear_thread_busy(thread_id, "operator")
        # drain поднимает отложенные карточки и шлёт send_for_review (Telegram, до 30 с
        # на каждый DM) — в фоне, чтобы не держать ответ MA
        run_in_background(_drain_deferred, thread_id)
    return True


def _drain_deferred(thread_id: str) -> None:
    import scheduler as _sched
    _sched.drain_deferred_thread(thread_id)


# ============================================================
# Итог дня (джоб scheduler, 20:00 по Берлину)
# ============================================================

def evening_summary_text() -> Optional[str]:
    """Итог дня: письма, ответы, продажи (те же числа, что «Сегодня» в Обзоре MA) + кто ждёт сейчас.
    None — день пустой и никто не ждёт (не шлём)."""
    from modules.db_sales import today_berlin
    today = today_berlin()
    t0, t1 = db.berlin_day_bounds_utc(today)  # один источник «сегодня» с Обзором MA
    with db.get_conn() as conn:
        new = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE direction='in' "
                           "AND created_at >= ? AND created_at < ?", (t0, t1)).fetchone()["c"]
        sent = conn.execute("SELECT COUNT(*) AS c FROM messages WHERE status IN ('sent','sent_debug') "
                            "AND COALESCE(sent_at, created_at) >= ? AND COALESCE(sent_at, created_at) < ?",
                            (t0, t1)).fetchone()["c"]
    sales = db.sales_summary(today, today)
    waiting = sum(1 for t in db.pipeline_threads()
                  if t["last_event_kind"] == "in" and not t["autopilot_active"])
    if not (new or sent or sales["count"] or waiting):
        return None
    lines = ["🌙 <b>Итог дня</b>",
             f"Писем: {new} · ответили: {sent}",
             f"Продано: {sales['count']}" + (f" на {int(sales['total_eur'])} €" if sales["count"] else "")]
    lines.append(f"Ждут ответа сейчас: {waiting}" if waiting else "Все ответы отправлены ✅")
    try:
        from modules import inventory
        stock = inventory.summary_text().strip()
        if stock:
            lines += ["", _html(stock)]
    except Exception:
        logger.exception("итог дня: сводка склада не собрана — итог без неё")
    return "\n".join(lines)


def send_evening_summary() -> str:
    """Job: итог дня операторам с кнопкой «Входящие». Пустой день — не шлём."""
    text = evening_summary_text()
    if text is None:
        return "итог дня: пусто — не отправлен"
    notify(text, "pipeline", label="📥 Входящие", kind="evening_summary")
    return "итог дня отправлен"


# ============================================================
# Мониторинг входа CLI в подписку Claude (все LLM-вызовы идут через неё)
# ============================================================

LLM_LOGIN_REMIND_SEC = 6 * 3600  # пока вход не восстановлен — напоминать не чаще раза в 6 ч
LLM_CHECK_FAILS_ALERT = 3  # «не смогли проверить» (таймаут CLI) подряд — тогда тоже алерт
_llm_login_state: dict[str, Any] = {"ok": True, "alerted_at": 0.0, "check_fails": 0, "checked_at": None}


def llm_login_status() -> dict[str, Any]:
    """Последний результат check_llm_login_job (для /health; сам CLI тут не дёргаем — до 30 с)."""
    st = _llm_login_state
    return {"llm_login_ok": st["ok"] if st["checked_at"] else None,
            "llm_checked_at": st["checked_at"]}


def _notify_sent(text: str) -> bool:
    """notify() с признаком «дошло»: fanout глотает ошибки по DM и возвращает {}."""
    try:
        return bool(notify(text))
    except Exception:
        logger.exception("notify не дошёл")
        return False


def check_llm_login_job() -> str:
    """Job (регистрирует scheduler, раз в 10–30 мин): CLI вышел из подписки claude.ai —
    уведомить оператора (с дедупом). Без входа черновики, переводы и автопилот не работают.

    llm.status() возвращает False и при «не залогинен», и при сбое самой проверки
    («Ошибка проверки: …», напр. таймаут CLI) — второе алертим только после
    LLM_CHECK_FAILS_ALERT раз подряд, чтобы один таймаут не будил оператора.
    """
    from modules import llm
    ok, msg = llm.status()
    st = _llm_login_state
    now = time.time()
    checked_at = datetime.utcnow().isoformat(timespec="seconds")
    if ok:
        if not st["ok"] and _notify_sent("🟩 <b>Подписка Claude снова подключена</b> — генерация ответов работает."):
            logger.info("LLM: вход в подписку восстановлен")
        st.update(ok=True, alerted_at=0.0, check_fails=0, checked_at=checked_at)
        return msg
    if msg.startswith("Ошибка проверки"):
        st["check_fails"] += 1
        logger.warning("LLM: проверка входа не удалась (%d подряд): %s", st["check_fails"], msg)
        if st["check_fails"] < LLM_CHECK_FAILS_ALERT:
            return msg
    else:
        st["check_fails"] = 0
    # время последнего результата «нет входа» — или сбоя проверки, повторившегося LLM_CHECK_FAILS_ALERT раз
    st["checked_at"] = checked_at
    if st["ok"] or not st["alerted_at"] or now - st["alerted_at"] >= LLM_LOGIN_REMIND_SEC:
        logger.error("LLM: нет входа в подписку Claude: %s", msg)
        if _notify_sent("🟥 <b>Нет входа в подписку Claude</b> — черновики и переводы не генерируются.\n"
                        f"{_html(msg)}\nНа сервере под пользователем сервиса: <code>claude auth login</code>"):
            st["alerted_at"] = now
    st["ok"] = False
    return msg


# ============================================================
# ============================================================
# Обработка устаревших callback-кнопок (до переписки 2026-06-23)
# ============================================================

def send_pending_summary(chat_id: int, edit_msg_id: Optional[int] = None) -> int:
    """/inbox (/tasks): кто ждёт ответа — кнопка на тред (имя покупателя), до 8 штук.
    Закрытие тредов из бота убрано: только в MA (там есть отмена). edit_msg_id → edit in-place."""
    threads = [t for t in db.pipeline_threads() if t["has_pending_draft"]]
    ts = datetime.now().strftime("%H:%M")
    if not threads:
        text = f"✅ <b>Никто не ждёт ответа</b>\n<i>{ts}</i>"
        kb: list = []
    else:
        text = f"📥 <b>Ждут ответа: {len(threads)}</b>\n<i>{ts}</i>"
        kb = []
        for t in threads[:8]:
            buyer = _oneline(_safe(t, "buyer_display_name") or _safe(t, "buyer_name") or "?", 14)
            snippet = _oneline(_safe(t, "ru_client") or _safe(t, "de_client"), 20)
            kb.append([{"text": f"{buyer} · {snippet}"[:40],
                        "web_app": {"url": _ma_deep_link(f"review_{_safe(t, 'id')}")}}])
        if len(threads) > 8:
            text += f"\nи ещё {len(threads) - 8}"
    kb.append([{"text": "📥 Все входящие", "web_app": {"url": _ma_deep_link("pipeline")}},
               {"text": "🔄 Обновить", "callback_data": "tasks"}])
    markup = {"inline_keyboard": kb}
    if edit_msg_id:
        try:
            _http_post_single("editMessageText", {
                "chat_id": chat_id, "message_id": edit_msg_id,
                "text": text, "parse_mode": "HTML", "reply_markup": markup, "_kind": "inbox",
            })
        except Exception as exc:  # сеть тоже: иначе «🔄 Обновить» крутится без answerCallbackQuery
            if "message is not modified" not in str(exc):
                logger.error("send_pending_summary edit fail: %s", exc)
    else:
        _http_post_single("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": markup, "_kind": "inbox",
        })
    return len(threads)


_HELP_TEXT = (
    "Бот присылает новые письма покупателей, напоминания и события автопилота.\n"
    "«✅ Ответить» — открыть черновик в Mini App, «💬 Переписка» — весь тред.\n"
    "/inbox — кто ждёт ответа.\n"
    "Кнопка «📥 Входящие» внизу — открыть Mini App."
)


def set_bot_commands() -> None:
    """Команды бота в меню «/» (с русскими описаниями)."""
    try:
        _http_post_single("setMyCommands", {"commands": [
            {"command": "inbox", "description": "Кто ждёт ответа"},
            {"command": "help", "description": "Что умеет бот"},
        ]})
    except Exception:
        logger.exception("setMyCommands fail")


def _authorized(user_id: Any) -> bool:
    """Нажимать кнопки и слать команды могут только операторы (telegram_authorized_ids)."""
    return user_id is not None and str(user_id) in config.telegram_authorized_ids()


def _handle_update(u: dict[str, Any]) -> None:
    """Один update из getUpdates. Всё от неавторизованных — молча игнорируем (WARNING в лог):
    иначе любой, кто найдёт бота, получал по /tasks имена покупателей и мог закрывать треды."""
    msg = u.get("message")
    if msg:
        chat_id = (msg.get("chat") or {}).get("id")
        user_id = (msg.get("from") or {}).get("id")
        if not chat_id:
            return
        if not _authorized(user_id):
            logger.warning("tg: сообщение от неавторизованного user=%s chat=%s — игнор", user_id, chat_id)
            return
        text = (msg.get("text") or "").strip().lower()
        if text.startswith(("/inbox", "/tasks", "/start")) or text == "обновить":
            send_pending_summary(chat_id)
        elif text.startswith("/help"):
            _http_post_single("sendMessage", {"chat_id": chat_id, "text": _HELP_TEXT, "_kind": "help"})
        return
    cb = u.get("callback_query")
    if not cb:
        return
    user_id = (cb.get("from") or {}).get("id")
    if not _authorized(user_id):
        logger.warning("tg: кнопка от неавторизованного user=%s data=%r — игнор", user_id, cb.get("data"))
        return
    data = (cb.get("data") or "").strip()
    cb_msg = cb.get("message") or {}
    cb_chat = (cb_msg.get("chat") or {}).get("id")
    if data == "tasks":
        count = send_pending_summary(cb_chat, edit_msg_id=cb_msg.get("message_id"))
        _http_post_single("answerCallbackQuery", {
            "callback_query_id": cb["id"], "text": f"Ждут ответа: {count}" if count else "Никто не ждёт"})
    else:
        # «👁 Прочитал», «🗑» и кнопки до 2026-06-23 — больше не действуют
        _http_post_single("answerCallbackQuery", {
            "callback_query_id": cb["id"],
            "text": "Эта кнопка устарела. Откройте Mini App — кнопка «📥 Входящие» внизу чата.",
            "show_alert": True,
        })


def start_callback_poller() -> None:
    """Daemon-поток: long-poll getUpdates → _handle_update (команды /inbox /tasks /help и
    кнопка «🔄 Обновить»; остальные кнопки — «устарела»). Только от авторизованных."""
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
                    try:
                        _handle_update(u)
                    except Exception:
                        logger.exception("tg: обработка update %s упала", u.get("update_id"))
            except (TimeoutError, urllib.error.URLError) as e:
                # Долгий long-poll getUpdates (timeout=25) периодически упирается
                # в сокет-таймаут/сетевой блип — это НОРМА, не ошибка. Логируем тихо,
                # чтобы не засорять hourly error-monitor ложными алармами.
                logger.debug("callback poll transient network hiccup: %r", e)
                time.sleep(1)
            except RuntimeError as e:
                # 5xx от Telegram (Bad Gateway / Service Unavailable / Gateway
                # Timeout) на long-poll getUpdates — тот же транзиентный upstream-
                # блип, что и сетевой таймаут выше. _http_post_single конвертирует
                # HTTPError в RuntimeError, поэтому распознаём по тексту и логируем
                # тихо, чтобы не засорять hourly error-monitor ложными ERROR.
                msg = str(e)
                transient = any(s in msg for s in (
                    "Bad Gateway", "Service Unavailable", "Gateway Timeout",
                    "HTTP 502", "HTTP 503", "HTTP 504",
                ))
                if transient:
                    logger.debug("callback poll transient upstream 5xx: %r", e)
                    time.sleep(2)
                else:
                    logger.exception("callback_query poll cycle fail")
                    time.sleep(5)
            except Exception:
                logger.exception("callback_query poll cycle fail")
                time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="cb-poller")
    t.start()
    logger.info("Callback-query poller запущен (daemon)")
