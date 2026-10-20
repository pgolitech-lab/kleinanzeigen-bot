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
from datetime import datetime, timezone
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


_FLAGS = {"de": "🇩🇪", "en": "🇬🇧", "ru": "🇷🇺", "pl": "🇵🇱", "tr": "🇹🇷", "uk": "🇺🇦", "fr": "🇫🇷",
          "es": "🇪🇸", "it": "🇮🇹", "nl": "🇳🇱", "ar": "🇸🇦", "ro": "🇷🇴"}


def _flag(lang: Optional[str]) -> str:
    return _FLAGS.get((lang or "").lower()[:2], "")


def waiting_threads(threads: Optional[list] = None) -> list:
    """Единый счёт «ждут ответа» — как полоса и бейдж вкладки в Mini App (api_ma.ma_pipeline):
    последнее событие треда — письмо клиента (закреплённые тоже). Сводки и панель считают только так."""
    rows = db.pipeline_threads() if threads is None else threads
    return [t for t in rows if _safe(t, "last_event_kind") == "in"]


def _short_name(msg: Any, limit: int = 10) -> str:
    return _oneline(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент", limit)


def _review_kb(msg: Any) -> dict:
    """Карточка письма: ответить (MA сразу на черновике) · пропустить (одним тапом, с возвратом) ·
    переписка · напомнить/продано (экран треда). Подписи короткие — влезают на внешний экран."""
    mid = _safe(msg, "id")
    thread_id = _safe(msg, "gmail_thread_id") or ""
    thread_link = _ma_deep_link(f"thread_{thread_id}" if thread_id else "pipeline")
    return {"inline_keyboard": [
        [{"text": "✅ Ответить", "web_app": {"url": _ma_deep_link(f"review_{mid}")}},
         {"text": f"⏭ {_short_name(msg)}", "callback_data": f"skip:{mid}"}],
        [{"text": "💰 Продано", "web_app": {"url": thread_link}},
         {"text": "⏰ Напомнить", "web_app": {"url": thread_link}}],
        [{"text": "💬 Переписка", "web_app": {"url": thread_link}}],
    ]}


def _oneline(text: Optional[str], limit: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _gist(text: Optional[str], limit: int = 200) -> str:
    """Суть письма: целые предложения до limit символов (вторая часть вопроса часто про цену);
    первое предложение длиннее лимита — режем по слову."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    out = ""
    for part in re.split(r"(?<=[.!?…])\s+", t):
        cand = (out + " " + part).strip()
        if len(cand) > limit:
            break
        out = cand
    return out or _oneline(t, limit)


_AMOUNT_RE = re.compile(r"(\d[\d.,]*)\s*(?:€|eur\b|euro\b|евро)|(?:€)\s*(\d[\d.,]*)", re.IGNORECASE)


def _offered_price(letter: Optional[str], ad_price: Any) -> Optional[str]:
    """Сумма из письма покупателя («за 300 €»), если это не цена самого объявления."""
    ad_num = re.sub(r"\D", "", str(ad_price or "").split(",")[0].split(".")[0])
    for m in _AMOUNT_RE.finditer(letter or ""):
        raw = (m.group(1) or m.group(2)).rstrip(".,")
        num = re.sub(r"[.,]\d{1,2}$", "", raw)  # копейки/центы отбрасываем
        digits = re.sub(r"\D", "", num)
        if digits and digits != ad_num and 0 < int(digits) < 1_000_000:
            return f"{int(digits)} €"
    return None


def _build_review_text(msg: Any) -> str:
    """Карточка письма покупателя (влезает на внешний экран ~306 px):
    🆕/💬 флаг имя · объявление и цена · суть письма по-русски · автопилот · черновик бота."""
    thread_id = _safe(msg, "gmail_thread_id") or ""
    is_followup = False
    try:
        # «ответил» — если до этого письма в треде уже были письма покупателя (auto-ack не в счёт)
        is_followup = sum(1 for r in db.thread_history(thread_id)
                          if _safe(r, "direction") == "in" and _safe(r, "de_client")) > 1 if thread_id else False
    except Exception:
        logger.exception("карточка письма: история треда %s не прочитана", thread_id)
    buyer = _html(_oneline(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент", 24))
    flag = _flag(_safe(msg, "client_lang"))
    who = f"{flag} <b>{buyer}</b>" if flag else f"<b>{buyer}</b>"
    head = f"💬 {who} · ответил" if is_followup else f"🆕 {who} · новое"
    price = _safe(msg, "ad_price")
    ad_line = "🏷 " + _html(_oneline(_safe(msg, "ad_title") or "—", 40)) + (f" · {_html(str(price))}" if price else "")
    # По-русски: перевод уже есть к моменту уведомления; немецкий — только если перевода нет
    raw_letter = _safe(msg, "ru_client") or _safe(msg, "de_client")
    lines = [head, ad_line, f"«{_html(_gist(raw_letter))}»"]
    offer = _offered_price(f"{_safe(msg, 'de_client') or ''} {_safe(msg, 'ru_client') or ''}", price)
    if offer:
        lines.append(f"💶 предлагает {offer}")
    if _safe(msg, "is_hot"):
        reason = _oneline(_safe(msg, "hot_reason"), 60)
        lines.insert(0, "🔥 <b>горячий покупатель</b>" + (f": {_html(reason)}" if reason else ""))
    try:
        ap = db.get_thread_autopilot(thread_id)
        if ap and ap["active"]:
            lines.append(f"🤖 автопилот {ap['messages_sent']}/{config.autopilot_message_cap()}")
    except Exception:
        logger.exception("карточка письма: автопилот треда %s не прочитан", thread_id)
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
           label: str = "📋 Открыть в MA", kind: str = "notify", urgent: bool = False,
           markup: Optional[dict] = None) -> dict[str, Any]:
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
    if markup:
        payload["reply_markup"] = markup
    elif start_param:
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
            old_card = _card_get(thread_id, str(dm_id)) if thread_id else None
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
                _card_record(thread_id, str(dm_id), tg_mid)
                _delete_old(str(dm_id), old_card)  # одна живая карточка на тред; старая уходит после новой
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


def send_reminder_offer(out_message_id: int, days_silent: float) -> Optional[int]:
    """Уведомление: исходящее висит без ответа N дней — пора напомнить. Кнопка → тред MA."""
    msg = db.get_message(out_message_id)
    if not msg:
        return None
    buyer = _html(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент")
    ad = _html(_safe(msg, "ad_title") or "—")
    silent_for = f"{days_silent:g} дн." if float(days_silent) >= 1 else f"{round(float(days_silent) * 24)} ч"
    text = (f"⏰ <b>Пора напомнить</b> · молчит {silent_for}\n"
            + " ".join(x for x in (_flag(_safe(msg, "client_lang")), buyer, "· 🏷", ad) if x))
    thread_id = _safe(msg, "gmail_thread_id") or ""
    r = notify(text, f"thread_{thread_id}", label="⏰ Напомнить")
    return r.get("message_id") if isinstance(r, dict) else None


# ---- Живая карточка треда: одна на тред и оператора, обновляется на месте ----
# Новое письмо клиента = старая карточка удаляется, новая приходит со звуком (иначе правка не пингует).
# Автопилот правит ту же карточку: прогресс — тихо, остановка, требующая человека, — новой со звуком.

_CARDS_KEY = "tg_live_cards"
_CARDS_TTL_SEC = 3 * 86400
_CARDS_MAX = 300
_CARDS_LOCK = threading.RLock()


def _cards_load() -> dict[str, dict[str, Any]]:
    raw = db.get_setting_fresh(_CARDS_KEY, "")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        cards = json.loads(raw)
    except ValueError:
        logger.warning("живые карточки: %s не JSON — начинаем заново", _CARDS_KEY)
        return {}
    return cards if isinstance(cards, dict) else {}


def _cards_save(cards: dict[str, dict[str, Any]]) -> None:
    now = time.time()
    live = {t: {c: e for c, e in per.items() if now - e.get("at", 0) < _CARDS_TTL_SEC}
            for t, per in cards.items()}
    live = {t: per for t, per in live.items() if per}
    if len(live) > _CARDS_MAX:  # самые свежие
        keep = sorted(live, key=lambda t: max(e.get("at", 0) for e in live[t].values()))[-_CARDS_MAX:]
        live = {t: live[t] for t in keep}
    db.set_setting(_CARDS_KEY, json.dumps(live))


def _card_get(thread_id: str, chat: str) -> Optional[dict[str, Any]]:
    with _CARDS_LOCK:
        return (_cards_load().get(thread_id) or {}).get(str(chat))


def _card_record(thread_id: str, chat: str, mid: int) -> None:
    if not thread_id or not mid:
        return
    with _CARDS_LOCK:
        cards = _cards_load()
        cards.setdefault(thread_id, {})[str(chat)] = {"mid": int(mid), "at": time.time()}
        _cards_save(cards)


def _card_drop(thread_id: str, chat: str) -> None:
    """Убрать прошлую карточку треда (удалить в Telegram — по возможности — и забыть)."""
    if not thread_id:
        return
    ent = _card_get(thread_id, chat)
    if not ent:
        return
    try:
        _http_post_single("deleteMessage", {"chat_id": chat, "message_id": ent["mid"], "_kind": "card_drop"})
    except Exception as e:  # старше 48 ч или уже удалена — не страшно, карточка просто останется в чате
        logger.info("живая карточка: старую не удалить (chat=%s): %s", chat, e)
    with _CARDS_LOCK:
        cards = _cards_load()
        (cards.get(thread_id) or {}).pop(str(chat), None)
        _cards_save(cards)


def _delete_old(chat: str, ent: Optional[dict[str, Any]]) -> None:
    """Удалить прежнюю карточку ПОСЛЕ того, как новая доставлена (иначе при сбое остаёмся без карточки)."""
    if not ent:
        return
    try:
        _http_post_single("deleteMessage", {"chat_id": chat, "message_id": ent["mid"], "_kind": "card_drop"})
    except Exception as e:
        logger.info("живая карточка: старую не удалить (chat=%s): %s", chat, e)


def _operator_chats() -> list[str]:
    return [str(c) for c in (config.telegram_operator_dm_ids() or [config.telegram_chat_id()])]


def _card_put(thread_id: str, text: str, start_param: str, label: str, *, kind: str,
              urgent: bool = False, replace: bool = False) -> Optional[int]:
    """Показать текст в живой карточке треда каждому оператору. Есть карточка и не replace — правим
    на месте (без звука); иначе (нет / не правится / replace) — новое сообщение. Не доставлено
    никому — RuntimeError, как у notify. Возвращает tg message_id первой доставленной."""
    kb = _open_kb(start_param, label)
    first: Optional[int] = None
    failed = 0
    chats = _operator_chats()
    for chat in chats:
        ent = None if replace else _card_get(thread_id, chat)
        if ent:
            try:
                _http_post_single("editMessageText", {
                    "chat_id": chat, "message_id": ent["mid"], "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True, "reply_markup": kb, "_kind": kind, "_thread": thread_id})
                first = first or ent["mid"]
                continue
            except Exception as e:  # RuntimeError Telegram и сетевые URLError/таймаут
                if "not modified" in str(e):
                    first = first or ent["mid"]
                    continue
                logger.info("живая карточка: правка не вышла (%s) — шлём новую", e)
        old = _card_get(thread_id, chat) if replace else None
        try:
            r = _http_post_single("sendMessage", {
                "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": kb, "disable_notification": (not urgent) and quiet_now(),
                "_kind": kind, "_thread": thread_id})
        except Exception as e:
            logger.error("живая карточка: оператору %s не доставлено: %s", chat, e)
            failed += 1
            continue
        mid = r.get("message_id") if isinstance(r, dict) else None
        if mid:
            _card_record(thread_id, chat, mid)
            _delete_old(chat, old)
            first = first or mid
    if failed == len(chats):
        raise RuntimeError(f"Telegram: карточка треда не доставлена ни одному оператору ({failed})")
    return first


# ---- Напоминания пачкой ----

REMINDER_BATCH_BUTTONS = 5  # кнопок «Напомнить · имя»; остальные — через «Входящие»
REMINDER_BATCH_LIST = 8  # строк в сводном сообщении; остальные — «и ещё N»


def reminder_batch_shown_ids(out_message_ids: list[int]) -> list[int]:
    """Какие письма из пачки реально названы в сообщении (читаемые, первые REMINDER_BATCH_LIST).
    Остальные в «…и ещё N» не считаются показанными — их предложим в следующий проход."""
    rows = [m for m in (db.get_message(i) for i in out_message_ids) if m]
    return [m["id"] for m in rows[:REMINDER_BATCH_LIST]]


def send_reminder_batch(out_message_ids: list[int], days_silent: float) -> Optional[int]:
    """Одно сообщение «пора напомнить: N клиентов» вместо N отдельных. Кнопка — «Входящие».
    Пустой список / ни одного читаемого письма — ничего не шлём (None)."""
    rows = [m for m in (db.get_message(i) for i in out_message_ids) if m]
    if not rows:
        return None
    if len(rows) == 1:
        return send_reminder_offer(rows[0]["id"], days_silent)
    silent_for = f"{days_silent:g} дн." if float(days_silent) >= 1 else f"{round(float(days_silent) * 24)} ч"
    lines = [f"⏰ <b>Пора напомнить: {len(rows)}</b> · молчат от {silent_for}"]
    for m in rows[:REMINDER_BATCH_LIST]:
        buyer = _html(_safe(m, "buyer_display_name") or _safe(m, "buyer_name") or "клиент")
        lines.append(f"• {buyer} — {_html(_oneline(_safe(m, 'ad_title') or '—', 40))}")
    if len(rows) > REMINDER_BATCH_LIST:
        lines.append(f"…и ещё {len(rows) - REMINDER_BATCH_LIST}")
    kb = []
    for m in rows[:REMINDER_BATCH_BUTTONS]:  # по одному: тред откроется с готовым напоминанием на предпросмотр
        tid = _safe(m, "gmail_thread_id")
        if tid:
            kb.append([{"text": f"⏰ Напомнить · {_short_name(m, 14)}"[:34],
                        "web_app": {"url": _ma_deep_link(f"thread_{tid}")}}])
    kb.append([{"text": "📥 Входящие", "web_app": {"url": _ma_deep_link("pipeline")}}])
    r = notify("\n".join(lines), kind="reminder_batch", markup={"inline_keyboard": kb})
    return r.get("message_id") if isinstance(r, dict) else None


# ---- Autopilot: одна карточка треда (зовутся из scheduler._autopilot_dispatch / api_ma) ----

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


def _autopilot_card(msg: Any, head: str, status: str) -> str:
    thread_id = _safe(msg, "gmail_thread_id") or ""
    ap = db.get_thread_autopilot(thread_id)
    who = _html(_safe(msg, "buyer_display_name") or _safe(msg, "buyer_name") or "клиент")
    ad = _html(_oneline(_safe(msg, "ad_title") or "—", 50))
    parts = [f"{head} · {who} · {ad}"]
    if ap:
        floor = ap["floor_price_eur"]
        parts.append(f"Отправлено: <code>{ap['messages_sent']}/{config.autopilot_message_cap()}</code>"
                     + (f" · floor <code>{int(floor)}€</code>" if floor else ""))
    parts.append(status)
    return "\n".join(parts)


def send_autopilot_stop_notification(msg_id: int, reason: str) -> None:
    """Остановка автопилота (reason='manual' — молча) или задержка ответа guard'ом — в карточке треда."""
    hold = reason in _AUTOPILOT_HOLD_REASONS
    info = _AUTOPILOT_HOLD_REASONS.get(reason) or _AUTOPILOT_STOP_REASONS.get(reason, ("🛑", reason))
    if info[0] is None:
        return
    msg = db.get_message(msg_id)
    if not msg:
        return
    head = "⏸ <b>Автопилот: ответ ждёт проверки</b>" if hold else "🛑 <b>Автопилот остановлен</b>"
    # Остановка, требующая человека (угроза, готов купить, просит контакты), — новой карточкой со звуком
    urgent = reason in ("threat", "ready_to_buy", "wants_contact")
    thread_id = _safe(msg, "gmail_thread_id") or ""
    _card_put(thread_id, _autopilot_card(msg, head, f"Причина: {info[0]} {info[1]}"), f"thread_{thread_id}",
              "📋 Открыть тред", kind="autopilot_stop", urgent=urgent, replace=urgent)


def send_autopilot_progress(msg_id: int, count: int) -> None:
    """Каждый авто-ответ (notify_mode='notify') — правка той же карточки, без нового пинга."""
    msg = db.get_message(msg_id)
    if not msg:
        return
    excerpt = _html((_safe(msg, "de_answer") or "").replace("\n", " ").strip()[:80])
    thread_id = _safe(msg, "gmail_thread_id") or ""
    _card_put(thread_id, _autopilot_card(msg, "🤖 <b>Автопилот</b>", f"Ответил: <i>{excerpt}</i>"),
              f"thread_{thread_id}", "📋 Открыть тред", kind="autopilot_progress")


def send_autopilot_start_notification(msg_id: int, floor: float, actor: str) -> None:
    """Включение автопилота (notify mode): карточка треда превращается в карточку автопилота."""
    msg = db.get_message(msg_id)
    if not msg:
        return
    thread_id = _safe(msg, "gmail_thread_id") or ""
    _card_put(thread_id, _autopilot_card(msg, "🚀 <b>Автопилот включён</b>", f"Запустил {_html(actor)}"),
              f"thread_{thread_id}", "📋 Открыть тред", kind="autopilot_start")


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

def _first_action(waiting: list) -> Optional[str]:
    """Одна строка «с чего начать»: самый старый ждущий ответа (ждёт дольше всех)."""
    import zoneinfo
    if not waiting:
        return None
    t = min(waiting, key=lambda x: _safe(x, "last_event_at") or "9")
    who = _oneline(_safe(t, "buyer_display_name") or _safe(t, "buyer_name") or "клиент", 20)
    since = ""
    try:
        dt = datetime.fromisoformat(str(_safe(t, "last_event_at")).replace("Z", "")).replace(tzinfo=timezone.utc)
        since = " с " + dt.astimezone(zoneinfo.ZoneInfo("Europe/Berlin")).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        pass
    return f"👉 Начните с: <b>{_html(who)}</b> — {_html(_oneline(_safe(t, 'ad_title') or '—', 30))}, ждёт{since}"


def morning_summary_text() -> Optional[str]:
    """Утренняя сводка: что ждёт сейчас, вчерашние продажи, напоминания, склад и рынок.
    None — нечего сказать (не шлём)."""
    from datetime import timedelta
    from modules.db_sales import today_berlin
    today = today_berlin()
    yday = today - timedelta(days=1)
    sales = db.sales_summary(yday, yday)
    threads = db.pipeline_threads()
    waiting = waiting_threads(threads)
    silent = [t for t in threads if t["last_event_kind"] == "out" and not t["autopilot_active"]]
    lines = ["☀️ <b>Доброе утро</b>"]
    lines.append(f"📥 Ждут ответа: <b>{len(waiting)}</b>" if waiting else "✅ Все ответы отправлены")
    first = _first_action(waiting)
    if first:
        lines.append(first)
    if silent:
        lines.append(f"⏳ Ждём клиентов: {len(silent)}")
    if sales["count"]:
        lines.append(f"💰 Продано вчера: {sales['count']} на {int(sales['total_eur'])} €")
    extra: list[str] = []
    try:
        from modules import inventory
        stock = inventory.summary_text().strip()
        if stock:
            extra.append(_html(stock))
    except Exception:
        logger.exception("утренняя сводка: склад не собран — сводка без него")
        extra.append("📦 склад: нет данных")
    try:
        from modules import market_advisor
        extra += [_html(x) for x in market_advisor.summary_lines()]
    except Exception:
        logger.exception("утренняя сводка: рынок не собран — сводка без него")
        extra.append("📈 рынок: нет данных")
    if extra:
        lines += [""] + extra
    if not (waiting or silent or sales["count"] or extra):
        return None
    return "\n".join(lines)


def send_morning_summary() -> str:
    """Job (08:00 Берлин, как раз после тихих часов): утренняя сводка."""
    text = morning_summary_text()
    if text is None:
        return "утренняя сводка: пусто — не отправлена"
    n = len(waiting_threads())
    # «Разбор» — одна карточка за раз, самый короткий путь к ответам
    notify(text, "triage" if n else "pipeline", label=f"✅ Разобрать {n}" if n else "📥 Входящие",
           kind="morning_summary")
    return "утренняя сводка отправлена"


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
    waiting = len(waiting_threads())
    if not (new or sent or sales["count"] or waiting):
        return None
    lines = ["🌙 <b>Итог дня</b>",
             f"✉️ Писем: {new} · ответили: {sent}",
             f"💰 Продано сегодня: {sales['count']}" + (f" на {int(sales['total_eur'])} €" if sales["count"] else "")]
    lines.append(f"📥 Ждут ответа сейчас: <b>{waiting}</b>" if waiting else "✅ Все ответы отправлены")
    try:
        from modules import inventory
        stock = inventory.summary_text().strip()
        if stock:
            lines += ["", _html(stock)]
    except Exception:
        logger.exception("итог дня: сводка склада не собрана — итог без неё")
        lines += ["", "📦 склад: нет данных"]
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

PANEL_ROWS = 5  # тредов в панели «Входящие»; остальные — в приложении


def _panel(threads: list, undo: "Optional[tuple[int, str]]" = None) -> "tuple[str, dict]":
    """Одно сообщение-панель: заголовок со счётом + по треду строка (имя · суть) и под ней
    «Ответить / Пропустить»; внизу «Все входящие» и «Обновить»."""
    ts = datetime.now().strftime("%H:%M")
    waiting = waiting_threads(threads)
    pinned = sum(1 for t in waiting if _safe(t, "is_pinned"))
    kb: list = []
    if undo:  # «Пропущено · Вернуть» — держится в панели до следующей перерисовки (тост Telegram живёт 2–3 с)
        kb.append([{"text": f"↩ Вернуть · {_oneline(undo[1], 14)}", "callback_data": f"unskip:{undo[0]}:p"}])
    if not waiting:
        text = f"✅ <b>Никто не ждёт ответа</b>\n<i>обновлено {ts}</i>"
    else:
        text = f"📥 <b>Ждут ответа: {len(waiting)}</b>" + (f" (из них закреплено {pinned})" if pinned else "")
        text += f"\n<i>обновлено {ts}</i>"
        waiting = sorted(waiting, key=lambda t: _safe(t, "last_event_at") or "", reverse=True)
        for t in waiting[:PANEL_ROWS]:
            who = " ".join(x for x in (_flag(_safe(t, "client_lang")),
                                       _oneline(_safe(t, "buyer_display_name") or _safe(t, "buyer_name") or "?", 14)) if x)
            snippet = _oneline(_safe(t, "ru_client") or _safe(t, "de_client"), 44)
            text += f"\n\n<b>{_html(who)}</b> · {_html(_oneline(_safe(t, 'ad_title') or '—', 24))}\n«{_html(snippet)}»"
            mid = _safe(t, "id")
            kb.append([{"text": f"✅ Ответить · {_oneline(_safe(t, 'buyer_display_name') or '?', 10)}"[:30],
                        "web_app": {"url": _ma_deep_link(f"review_{mid}")}},
                       {"text": f"⏭ {_oneline(_safe(t, 'buyer_display_name') or _safe(t, 'buyer_name') or '?', 10)}",
                        "callback_data": f"skip:{mid}:p"}])
        if len(waiting) > PANEL_ROWS:
            text += f"\n\n…и ещё {len(waiting) - PANEL_ROWS} — в приложении"
    kb.append([{"text": "📥 Все входящие", "web_app": {"url": _ma_deep_link("pipeline")}},
               {"text": "🔄 Обновить", "callback_data": "tasks"}])
    return text, {"inline_keyboard": kb}


def send_pending_summary(chat_id: int, edit_msg_id: Optional[int] = None,
                         undo: "Optional[tuple[int, str]]" = None) -> int:
    """/inbox (/tasks): панель «Входящие» одним сообщением. edit_msg_id → правка на месте
    (обновить/пропустить не плодят сообщений). Возвращает число ждущих (тот же счёт, что в Mini App)."""
    threads = db.pipeline_threads()
    text, markup = _panel(threads, undo)
    if edit_msg_id:
        try:
            _http_post_single("editMessageText", {
                "chat_id": chat_id, "message_id": edit_msg_id,
                "text": text, "parse_mode": "HTML", "reply_markup": markup, "_kind": "inbox",
            })
        except Exception as exc:  # сеть тоже: иначе «🔄 Обновить» крутится без answerCallbackQuery
            if "message is not modified" not in str(exc):
                raise  # вызывающий скажет оператору, что панель не обновилась
    else:
        _http_post_single("sendMessage", {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": markup, "_kind": "inbox",
        })
    return len(waiting_threads(threads))


# Пропуск из бота: письмо в статусах «ждёт оператора» → skipped, с кнопкой «↩ Вернуть»
_SKIPPABLE = ("new", "pending", "edited", "approved")


def skip_from_bot(msg_id: int, actor: str) -> "tuple[bool, str]":
    """Пропустить письмо кнопкой бота. (ok, человеческий текст). Чужой лок / уже отправлено / в очереди
    отправки — не трогаем и объясняем, а не молча ничего."""
    msg = db.get_message(msg_id)
    if not msg:
        return False, "Письмо не найдено — возможно, его уже убрали."
    status = _safe(msg, "status")
    if status == "send_queued":
        return False, "Письмо уже в очереди на отправку — отмените отправку в приложении."
    if status not in _SKIPPABLE:
        return False, "Это письмо уже обработано — пропускать нечего."
    try:
        ap = db.get_thread_autopilot(_safe(msg, "gmail_thread_id"))
        if ap and ap["active"]:
            return False, "В этом треде работает автопилот — остановите его в приложении."
    except Exception:
        logger.exception("пропуск из бота: автопилот письма %s не прочитан", msg_id)
    busy = _claim_lock(msg_id, actor)
    if busy:
        return False, f"Сейчас с этим письмом работает {busy}."
    try:
        fresh = db.get_message(msg_id)  # статус мог смениться между чтением и локом (очередь отправки в MA)
        if not fresh or _safe(fresh, "status") not in _SKIPPABLE:
            return False, "Письмо только что обработали — пропускать нечего."
        db.update_message(msg_id, status="skipped")
    finally:
        _release_lock(msg_id)
    return True, "Пропущено"


def unskip_from_bot(msg_id: int) -> "tuple[bool, str]":
    msg = db.get_message(msg_id)
    if not msg or _safe(msg, "status") != "skipped":
        return False, "Вернуть нельзя — письмо уже не в «пропущенных»."
    thread_id = _safe(msg, "gmail_thread_id")
    try:
        later = [r for r in db.thread_history(thread_id) if _safe(r, "direction") == "in" and _safe(r, "id", 0) > msg_id]
    except Exception:
        logger.exception("возврат из пропущенных: история треда %s не прочитана", thread_id)
        later = []
    if later:
        return False, "В треде уже есть новое письмо — откройте его в приложении."
    db.update_message(msg_id, status="pending")
    return True, "Вернул в очередь"


_HELP_TEXT = (
    "Бот присылает новые письма покупателей, напоминания и события автопилота.\n"
    "«✅ Ответить» — черновик в Mini App, «⏭ Пропустить» — убрать письмо (можно вернуть), "
    "«💰 Продано» и «⏰ Напомнить» — в треде, «💬 Переписка» — весь диалог.\n"
    "/inbox — панель: кто ждёт ответа.\n"
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
        try:
            count = send_pending_summary(cb_chat, edit_msg_id=cb_msg.get("message_id"))
            answer_text = f"Ждут ответа: {count}" if count else "Никто не ждёт"
        except Exception:
            answer_text = "Не удалось обновить панель — попробуйте ещё раз"
        _http_post_single("answerCallbackQuery", {"callback_query_id": cb["id"], "text": answer_text})
    elif data.startswith(("skip:", "unskip:")):
        _handle_skip_cb(cb, data, cb_chat, cb_msg)
    else:
        # «👁 Прочитал», «🗑» и кнопки до 2026-06-23 — больше не действуют
        _http_post_single("answerCallbackQuery", {
            "callback_query_id": cb["id"],
            "text": "Эта кнопка устарела. Откройте Mini App — кнопка «📥 Входящие» внизу чата.",
            "show_alert": True,
        })


def _handle_skip_cb(cb: dict, data: str, chat_id: Any, cb_msg: dict) -> None:
    """«⏭ Пропустить» / «↩ Вернуть»: правит то же сообщение (панель или карточку), не плодит новых.
    Ошибка — человеческим текстом в всплывающем окне."""
    kind, _, rest = data.partition(":")
    raw, _, where = rest.partition(":")  # where=='p' — нажато в панели «Входящие»
    answer: dict[str, Any] = {"callback_query_id": cb["id"]}
    try:
        msg_id = int(raw)
    except ValueError:
        answer.update(text="Кнопка устарела — откройте «📥 Входящие».", show_alert=True)
        _http_post_single("answerCallbackQuery", answer)
        return
    frm = cb.get("from") or {}
    actor = f"tg:{frm.get('username') or frm.get('id')}"
    try:
        ok, text = skip_from_bot(msg_id, actor) if kind == "skip" else unskip_from_bot(msg_id)
    except Exception:
        logger.exception("tg: %s письма %s упало", kind, msg_id)
        ok, text = False, "Не получилось — попробуйте ещё раз или откройте приложение."
    answer.update(text=text, show_alert=not ok)
    _http_post_single("answerCallbackQuery", answer)
    if not ok:
        return
    mid = cb_msg.get("message_id")
    try:
        if where == "p":
            undo = None
            if kind == "skip":
                skipped = db.get_message(msg_id)
                undo = (msg_id, _short_name(skipped) if skipped else "письмо")
            send_pending_summary(chat_id, edit_msg_id=mid, undo=undo)  # панель без пропущенного + «Вернуть»
        elif kind == "skip":
            _http_post_single("editMessageText", {
                "chat_id": chat_id, "message_id": mid, "parse_mode": "HTML", "_kind": "skip",
                "text": f"⏭ <i>Пропущено</i>\n{_html(_oneline(cb_msg.get('text'), 120))}",
                "reply_markup": {"inline_keyboard": [[
                    {"text": "↩ Вернуть", "callback_data": f"unskip:{msg_id}"},
                    {"text": "📥 Входящие", "web_app": {"url": _ma_deep_link("pipeline")}}]]}})
        else:
            msg = db.get_message(msg_id)
            if msg:
                _http_post_single("editMessageText", {
                    "chat_id": chat_id, "message_id": mid, "parse_mode": "HTML", "_kind": "skip",
                    "text": _build_review_text(msg), "reply_markup": _review_kb(msg),
                    "disable_web_page_preview": True})
    except Exception:
        logger.exception("tg: правка сообщения после %s письма %s не вышла", kind, msg_id)


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
