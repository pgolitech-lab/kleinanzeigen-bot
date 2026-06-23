# Telegram-бот: уведомления оператору о новых сообщениях с inline-кнопками.
# Архитектура:
#   • Отправка уведомлений (sync) — через HTTP API напрямую (urllib stdlib).
#     Вызывается из scheduler-а в любом потоке без asyncio.
#   • Приём callback-ов и редактирование (async) — python-telegram-bot Application
#     с long polling. Запускается отдельным процессом или в отдельном потоке.

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database as db
from modules import ad_brief, claude, operator_lock, parser

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

# Mini App URL для deep-link buttons. Pages serves SPA из main:/web-app/.
MA_BASE_URL = "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/"


def _ma_deep_link(start_param: str) -> str:
    """Сформировать URL для web_app кнопки. start_param формата 'review_<msg_id>' / 'thread_<id>'."""
    return f"{MA_BASE_URL}?tgWebAppStartParam={start_param}"


def _safe(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        return default


def _minicard_text(msg) -> str:
    """Thread-state мини-карточка: показывает АКТУАЛЬНОЕ состояние треда.

    Обновляется после каждого incoming/outgoing event через broadcast_thread_state.
    """
    thread_id = _safe(msg, "gmail_thread_id") or ""
    history = db.thread_history(thread_id) if thread_id else [msg]
    if not history:
        history = [msg]

    # Latest in-row для buyer/ad/deal_brief/status
    latest_in = None
    for r in reversed(history):
        if _safe(r, "direction") == "in":
            latest_in = r
            break
    if latest_in is None:
        latest_in = msg

    buyer = _safe(latest_in, "buyer_display_name") or "?"
    ad_title = _safe(latest_in, "ad_title") or "(без названия)"
    ad_price = _safe(latest_in, "ad_price") or "?"
    status = _safe(latest_in, "status") or ""

    # Latest event chronologically (in или out)
    latest_event = None  # (kind, text, ts)
    for r in history:
        d = _safe(r, "direction")
        if d == "in" and _safe(r, "de_client"):
            ts = (_safe(r, "created_at") or "")[:16]
            latest_event = ("in", _safe(r, "de_client"), ts)
        elif d == "out" and _safe(r, "de_answer") and _safe(r, "status") in ("sent", "sent_debug"):
            ts = (_safe(r, "sent_at") or _safe(r, "created_at") or "")[:16]
            latest_event = ("out", _safe(r, "de_answer"), ts)

    # deal_brief из latest in-row
    deal_brief = None
    raw = _safe(latest_in, "deal_brief_json")
    if raw:
        try:
            d = json.loads(raw)
            summary = (d.get("summary_ru") or "").strip()
            price = d.get("negotiated_price_eur")
            assess = (d.get("client_assessment") or "").strip()
            parts = []
            if isinstance(price, (int, float)) and price > 0:
                parts.append(f"💰 {int(price)}€")
            if assess:
                parts.append(f"🏷 {assess}")
            if summary:
                deal_brief = summary + (" · " + " · ".join(parts) if parts else "")
            elif parts:
                deal_brief = " · ".join(parts)
        except Exception:
            pass

    # Sold-цена явная — приоритет
    sold_price = _safe(latest_in, "sold_price_eur")
    if sold_price is not None:
        status = "skipped_sold"

    status_emoji = {
        "pending": "📨", "new": "📨", "edited": "✏️", "approved": "👌",
        "sent": "✅", "sent_debug": "🟨", "skipped": "❌", "skipped_sold": "💰",
        "deferred": "⏸",
    }.get(status or "", "📨")

    lines = [f"{status_emoji} <b>{_html(buyer)}</b>"]
    lines.append(f"🏷 {_html(ad_title)} · {_html(ad_price)}")

    if latest_event:
        kind, body, ts = latest_event
        snippet = (body or "")[:160].replace("\n", " ").strip()
        if len(body or "") > 160:
            snippet += "…"
        arrow = "👤" if kind == "in" else "📤"
        hhmm = ""
        if ts:
            try:
                # iso 'YYYY-MM-DDTHH:MM' → 'DD.MM HH:MM'
                from datetime import datetime as _dt
                d_ = _dt.fromisoformat(ts.replace("Z", "")[:16])
                hhmm = d_.strftime("%d.%m %H:%M") + " "
            except Exception:
                pass
        lines.append(f"{arrow} <i>{_html(hhmm)}</i>{_html(snippet)}")

    if deal_brief:
        lines.append(f"💡 {_html(deal_brief)}")

    if sold_price is not None:
        lines.append(f"💰 <b>Продано за {int(sold_price)}€</b>")

    return "\n".join(lines)


def broadcast_thread_state(gmail_thread_id: str) -> None:
    """Обновить ТЕКСТ всех мини-карточек треда (любая msg_id) — thread-state.

    Используется после incoming/outgoing event'а, чтобы все исторические карточки
    показывали актуальное состояние переговоров. Best-effort; ошибки в log.
    """
    if not gmail_thread_id:
        return
    history = db.thread_history(gmail_thread_id)
    if not history:
        return
    # Любая row подойдёт — _minicard_text смотрит полную историю
    msg = history[-1]
    try:
        text = _minicard_text(msg)
    except Exception:
        logger.exception("broadcast_thread_state: _minicard_text failed")
        return
    text = _truncate_html_safe(text)

    dispatches = db.list_thread_dispatches(gmail_thread_id)
    for d in dispatches:
        try:
            _http_post_single("editMessageText", {
                "chat_id": d["chat_id"],
                "message_id": d["tg_msg_id"],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception as e:
            msg_l = str(e).lower()
            if "not modified" in msg_l or "message to edit not found" in msg_l:
                continue
            logger.warning("broadcast_thread_state edit failed chat=%s tgmsg=%s: %s",
                           d["chat_id"], d["tg_msg_id"], e)


def _ma_review_keyboard(msg_id: int) -> dict:
    """Single-button inline keyboard — открывает MA review screen."""
    return {
        "inline_keyboard": [[{
            "text": "📋 Открыть в MA",
            "web_app": {"url": _ma_deep_link(f"review_{msg_id}")},
        }]]
    }


# ============================================================
# HTTP API (sync) — для отправки уведомлений из scheduler-а
# ============================================================

def _http_post_single(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Сырой POST к Telegram Bot API. Используется внутренне.

    Длинный HTML auto-truncate: если text > 4096 chars, обрезаем по безопасной
    HTML-границе. Защищает все sync send/edit пути от Bad Request 'text too long'.
    """
    token = config.telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан Telegram bot token в настройках")
    if method in ("sendMessage", "editMessageText"):
        text = payload.get("text")
        if isinstance(text, str) and len(text) > 4096:
            payload = dict(payload)
            payload["text"] = _truncate_html_safe(text, limit=4090)
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
    """POST к Telegram Bot API + DM-fanout если включён.

    Если `config.telegram_operator_dm_ids()` НЕ пуст и chat_id == primary
    `telegram_chat_id` — рассылаем sendMessage каждому DM-id вместо одного primary.
    Все ID отслеживаются для batch-удаления на /pipeline.
    """
    if method == "sendMessage":
        dm_ids = config.telegram_operator_dm_ids()
        primary = config.telegram_chat_id()
        target = str(payload.get("chat_id", ""))
        if dm_ids and target == str(primary):
            # Fanout — отдаём первый result обратно (для обратной совместимости с
            # вызывающим, который ожидает один result), но шлём всем DM-ам.
            first_result: dict[str, Any] = {}
            for dm_id in dm_ids:
                p = dict(payload)
                p["chat_id"] = dm_id
                try:
                    r = _http_post_single(method, p)
                    if isinstance(r, dict):
                        _track_msg(dm_id, r.get("message_id"))
                    if not first_result:
                        first_result = r
                except Exception:
                    logger.exception("DM-fanout: send to %s failed", dm_id)
            return first_result

    result = _http_post_single(method, payload)
    if method == "sendMessage" and isinstance(result, dict):
        _track_msg(payload.get("chat_id"), result.get("message_id"))
    return result


def _html(s: Optional[str]) -> str:
    """HTML escape для parse_mode=HTML."""
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _truncate(s: Optional[str], max_len: int = 220) -> str:
    """Обрезать длинный текст с многоточием — для компактного отображения в истории."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[:max_len].rstrip() + "…"




def _safe_get(row: sqlite3.Row, key: str) -> Optional[str]:
    """sqlite3.Row не имеет .get() — обернём, чтобы безопасно читать новые колонки."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _ensure_ru_client(msg_row: sqlite3.Row) -> str:
    """Вернуть ru_client. Если пуст — Haiku translate de_client → RU + кэшировать в БД.

    Используется для archived rows из backfill (они без перевода).
    """
    existing = (msg_row["ru_client"] or "").strip()
    if existing:
        return existing
    de = (msg_row["de_client"] or "").strip()
    if not de:
        return ""
    try:
        r = claude.detect_and_translate_to_ru(de)
        ru = (r.get("translation_ru") or "").strip()
        if ru:
            db.update_message(
                msg_row["id"],
                ru_client=ru,
                client_lang=r.get("lang") or msg_row["client_lang"],
            )
            return ru
    except Exception:
        logger.exception("ensure_ru_client: Haiku translate упал msg=%s", msg_row["id"])
    return de[:120]  # fallback


def _last_our_reply_in_thread(thread_id: str) -> Optional[str]:
    """RU-текст нашего последнего исходящего в треде (если есть)."""
    if not thread_id:
        return None
    rows = db.thread_history(thread_id)
    # Ищем последний row с de_answer и status sent/sent_debug/edited (любое исх.)
    for r in reversed(rows):
        if r["de_answer"] and r["status"] in ("sent", "sent_debug", "edited"):
            ru = (r["ru_answer"] or "").strip()
            if ru:
                return ru
            # back-translate если нет RU? Skip — слишком много Haiku-вызовов
            return (r["de_answer"] or "")[:200]
    return None


def _format_related_warning(msg: sqlite3.Row) -> str:
    """Warning-блок если клиент уже писал нам по другим объявлениям.

    Точное совпадение по `buyer_display_name` (один и тот же operator-видимый ник
    в других тредах). Style-similarity (Haiku) убран — давал шум, ел токены.
    Пусто если ничего не нашли.
    """
    display = _safe_get(msg, "buyer_display_name")
    related: list = []
    if display and display.strip():
        related = db.find_related_inquiries(
            display_name=display,
            exclude_thread_id=msg["gmail_thread_id"] or None,
            limit=8,
        )

    if not related:
        return ""

    lines = ["🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨"]

    if related:
        lines.append(
            f"⚠️ <b>«{_html(display)}» УЖЕ ПИШЕТ НАМ</b> "
            f"({len(related)} тред{'а' if 1 < len(related) < 5 else 'ов'} с тем же именем)"
        )
        for r in related:
            title = (r["ad_title"] or "—")[:50]
            price = _short_price(r["ad_price"])
            ts = _safe_get(r, "sent_at") or r["created_at"] or ""
            age = _humanize_age(ts)
            st = r["status"] or ""
            # Какой НАШ аккаунт получил это inquiry — name + email
            acc_label = ""
            try:
                acc = db.get_account(r["account_id"])
                if acc:
                    acc_name = (acc["name"] or "").strip()
                    acc_email = (acc["gmail_email"] or "").strip()
                    if acc_name and acc_email:
                        acc_label = f" · 📧 {_html(acc_name)} ({_html(acc_email)})"
                    elif acc_email:
                        acc_label = f" · 📧 {_html(acc_email)}"
                    elif acc_name:
                        acc_label = f" · 📧 {_html(acc_name)}"
            except Exception:
                pass
            lines.append("")
            lines.append(
                f"  📦 <b>#{r['id']}</b> · {_html(title)} · {price or '—'} · "
                f"⏱{age} · <code>{_html(st)}</code>{acc_label}"
            )
            # Что писал клиент (на RU; для archived rows — translate on-demand)
            client_ru = _ensure_ru_client(r)
            if client_ru:
                excerpt = client_ru.replace("\n", " ").strip()
                if len(excerpt) > 200:
                    excerpt = excerpt[:197] + "…"
                lines.append(f"     ← <i>{_html(excerpt)}</i>")
            # Что мы ответили в этом треде (если ответили)
            our_reply = _last_our_reply_in_thread(r["gmail_thread_id"] or "")
            if our_reply:
                excerpt = our_reply.replace("\n", " ").strip()
                if len(excerpt) > 200:
                    excerpt = excerpt[:197] + "…"
                lines.append(f"     → <i>мы: {_html(excerpt)}</i>")
            else:
                lines.append("     → <i>мы не ответили</i>")

    lines.append("🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨")
    return "\n".join(lines)



def _autopilot_mode_choice_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """После ввода floor — выбор режима уведомлений."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Notify (start/inbound/stop)", callback_data=f"apconfirm:notify:{message_id}")],
        [InlineKeyboardButton("🤫 Silent (только при стопе)", callback_data=f"apconfirm:silent:{message_id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"inputcancel:{message_id}")],
        [InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{message_id}")],
    ])



def _confirmation_keyboard(original_callback: str, message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения: ▶️ Продолжить | ❌ Отменить | ↩ Назад."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  П Р О Д О Л Ж И Т Ь", callback_data=f"confirm:{original_callback}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{message_id}")],
        [InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{message_id}")],
    ])


# ============================================================
# Confirmation explanations
# ============================================================

# Действия, которые требуют подтверждения перед выполнением.
# Ключи — либо `action` (для 2-частных callback типа send/skip),
# либо `action:sub` (для 3-частных типа q:fest, t:harsh).
NEEDS_CONFIRM: set[str] = {
    "closethread", "waitclient",
    "propose", "translate_confirm", "back_to_ru",
}

# Объяснения для confirm-карточки. Ключ совпадает с NEEDS_CONFIRM.
ACTION_EXPLANATIONS: dict[str, str] = {
    "closethread": "<b>🏁 Завершить беседу</b> — уберёт тред из «Состояние переговоров» и из reminder-очереди. Все pending-драфты помечаются <code>skipped</code>. Если клиент напишет снова — тред автоматически вернётся.",
    "waitclient": "<b>⏳ Ждать ответа</b> — мы ничего не отправляем, ждём действия клиента. Pending-драфты помечаются <code>skipped</code>, тред переходит в 🟢 секцию pipeline. Авто-сброс при новом сообщении от клиента.",
    "propose": "<b>🤖 Предложить ответ</b> — Sonnet сгенерирует RU-черновик с учётом всей переписки, объявления и уроков оператора. Стоит ~$0.005-0.01. После — оператор может править и затем тапнуть «✅ Перевести и подтвердить».",
    "translate_confirm": "<b>✅ Перевести и подтвердить</b> — переведу твой RU-черновик на язык клиента. Стоит ~$0.001-0.005. Потом увидишь финальный текст и сможешь тапнуть «✏️ Правка DE» если что-то не так, или «✅ ОТПРАВИТЬ».",
    "back_to_ru": "<b>🇷🇺 Назад к RU</b> — вернуться к RU-черновику для правки. DE-перевод останется как был, но не будет отправлен (потребуется заново «✅ Перевести и подтвердить»).",
}


def _confirmation_addendum(action_key: str) -> str:
    """Текст-приписка к карточке в режиме подтверждения."""
    explanation = ACTION_EXPLANATIONS.get(action_key, f"<i>{action_key}</i>")
    return (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>Подтверждение действия</b>\n\n"
        f"{explanation}"
    )


# ============================================================
# Lock + broadcast (DM-mode)
# ============================================================

# Lock state теперь живёт в modules/operator_lock.py — общий с web/api_ma.
# Wrappers ниже сохраняют orchestration с thread_busy + drain_deferred.

# Per-thread «занят» флаг — синхронизация с входящим polling: пока оператор
# работает с каким-либо row треда (любой active lock) ИЛИ пока идёт SMTP-отправка
# ответа в этом треде, новые incoming для того же thread_id откладываются (status
# 'deferred'). После очистки флага — drain_deferred_thread в scheduler-е поднимает
# отложенные карточки. kinds: {'operator', 'sending'}.
_THREAD_BUSY: dict[str, dict[str, Any]] = {}


def thread_is_busy(thread_id: Optional[str]) -> bool:
    """True если тред в работе у оператора или идёт SMTP-отправка."""
    if not thread_id:
        return False
    entry = _THREAD_BUSY.get(thread_id)
    return bool(entry and entry.get("kinds"))


def mark_thread_busy(thread_id: Optional[str], kind: str, by: str = "?") -> None:
    """Пометить thread занятым. kind ∈ {'operator', 'sending'}."""
    if not thread_id:
        return
    entry = _THREAD_BUSY.setdefault(
        thread_id, {"kinds": set(), "by": by, "since": datetime.utcnow().isoformat()},
    )
    entry["kinds"].add(kind)
    entry["by"] = by


def clear_thread_busy(thread_id: Optional[str], kind: str) -> None:
    """Снять конкретный busy-flag. Если других kind-ов нет — удалить entry."""
    if not thread_id:
        return
    entry = _THREAD_BUSY.get(thread_id)
    if not entry:
        return
    entry["kinds"].discard(kind)
    if not entry["kinds"]:
        _THREAD_BUSY.pop(thread_id, None)


def _msg_thread_id(msg_id: int) -> Optional[str]:
    """Лукап thread_id по msg_id (для bridge'a между msg-id-locks и thread-busy)."""
    msg = db.get_message(msg_id)
    if not msg:
        return None
    return msg["gmail_thread_id"] or None


def _check_lock(msg_id: int, actor: str) -> Optional[str]:
    """Возвращает имя текущего владельца если лок занят ДРУГИМ. None если свободно/мой."""
    st = operator_lock.state(msg_id)
    if st is None:
        # Lock мог только что auto-expire-нуть внутри state() — синхронизируем busy-flag.
        clear_thread_busy(_msg_thread_id(msg_id), "operator")
        return None
    owner, _ = st
    if owner == actor:
        return None
    return owner


def _acquire_lock(msg_id: int, actor: str) -> None:
    operator_lock.remember(msg_id, actor)
    mark_thread_busy(_msg_thread_id(msg_id), "operator", actor)


def _release_lock(msg_id: int) -> None:
    operator_lock.forget(msg_id)
    thread_id = _msg_thread_id(msg_id)
    clear_thread_busy(thread_id, "operator")
    # После освобождения — попробуем поднять отложенные карточки.
    # Импортируем лениво чтоб не было circular-import (telegram_bot ↔ scheduler).
    if thread_id:
        try:
            import scheduler as _sched
            _sched.drain_deferred_thread(thread_id)
        except Exception:
            logger.exception("drain_deferred_thread fail")


def _lock_remaining_min(msg_id: int) -> int:
    """Минут до auto-release. 0 если нет лока или истёк."""
    return operator_lock.remaining_min(msg_id)


async def _broadcast_card(context: Any, msg_id: int, text: str, reply_markup: Any = None) -> None:
    """Edit ВСЕ копии карточки msg_id (по таблице card_dispatches).

    Используется для broadcast-обновлений в DM-mode (а в group-mode — просто
    одна row в card_dispatches, та же логика). Длинный HTML auto-truncate.
    """
    text = _truncate_html_safe(text)
    dispatches = db.list_card_dispatches(msg_id)
    if not dispatches:
        # Fallback на legacy telegram_message_id (для row-ы которая не успела попасть в dispatches)
        msg = db.get_message(msg_id)
        if msg and msg["telegram_message_id"]:
            try:
                await context.bot.edit_message_text(
                    chat_id=config.telegram_chat_id(),
                    message_id=msg["telegram_message_id"],
                    text=text, parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.warning("broadcast_card legacy edit: %s", e)
        return
    for d in dispatches:
        try:
            await context.bot.edit_message_text(
                chat_id=d["chat_id"],
                message_id=d["tg_msg_id"],
                text=text, parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning("broadcast_card edit: chat=%s msg=%s err=%s",
                               d["chat_id"], d["tg_msg_id"], e)
        except Exception:
            logger.exception("broadcast_card: непредвиденная ошибка edit")


def _truncate_html_safe(text: str, limit: int = 4000, suffix: str = "\n\n<i>…(сокращено — открой полную карточку треда)</i>") -> str:
    """Безопасное обрезание HTML до лимита Telegram.

    Режем по последней безопасной границе (`</blockquote>`, `</b>` и т.д.) чтобы
    не оставить незакрытый тег. Если пересекли тег — добавляем suffix-индикатор.
    Telegram-лимит 4096 chars; используем 4000 чтобы оставить headroom для footer-ов
    которые callers иногда добавляют после.
    """
    if len(text) <= limit:
        return text
    target_len = limit - len(suffix)
    if target_len < 100:
        target_len = limit  # suffix слишком длинный — игнорируем
    # Приоритет границ: блочные теги > строки > пробелы
    for boundary in ("</blockquote>", "</pre>", "</code>", "</a>", "</b>", "</i>", "\n\n", "\n", " "):
        cut_at = text.rfind(boundary, 0, target_len)
        if cut_at != -1:
            return text[:cut_at + len(boundary)] + suffix
    return text[:target_len] + suffix


def broadcast_after_external_action(msg_id: int) -> None:
    """Sync обновление всех DM-копий мини-карточек ТРЕДА после внешнего действия.

    Делегирует в broadcast_thread_state — все исторические карточки треда
    показывают одинаковый thread-state. Best-effort.
    """
    try:
        msg = db.get_message(msg_id)
    except Exception:
        logger.exception("broadcast_after_external_action: db.get_message failed")
        return
    if not msg:
        return
    thread_id = msg["gmail_thread_id"] if "gmail_thread_id" in msg.keys() else None
    if thread_id:
        broadcast_thread_state(thread_id)
        return

    # Fallback (legacy без thread_id): обновить только эту карточку
    try:
        text = _truncate_html_safe(_minicard_text(msg))
    except Exception:
        return
    if msg["telegram_message_id"]:
        try:
            _http_post_single("editMessageText", {
                "chat_id": int(config.telegram_chat_id()),
                "message_id": msg["telegram_message_id"],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception:
            pass


def refresh_pipeline_for_active_chats() -> None:
    """Sync re-render всех активных pipeline-сообщений по DM-чатам операторов.

    Использует ordered list из `_CHAT_PIPELINE_MSGS` (заполнен в `_on_pipeline`
    и в callback `back`). Для каждого чата:
      - regenerate `_format_pipeline_messages()` (свежий снимок БД)
      - edit text+reply_markup по индексам в `_http_post_single`
      - count grow → append через sendMessage
      - count shrink → deleteMessage избытка
    Best-effort: все ошибки логируем warning, continue.

    Вызывается из scheduler после incoming/outgoing event и из MA endpoints
    после `broadcast_after_external_action`.
    """
    if not _CHAT_PIPELINE_MSGS:
        return
    try:
        messages = _format_pipeline_messages()
    except Exception:
        logger.exception("refresh_pipeline: _format_pipeline_messages failed")
        return

    new_count = len(messages)
    for chat_id, old_ids in list(_CHAT_PIPELINE_MSGS.items()):
        if not old_ids:
            continue
        try:
            common = min(new_count, len(old_ids))
            # 1) Edit overlapping positions
            for i in range(common):
                text, kb = messages[i]
                text = _truncate_html_safe(text)
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": old_ids[i],
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                # Inline keyboard (web_app buttons на карточках). Index 0 = header
                # без kb; persistent ReplyKb всё равно через editMessageText не меняется.
                if kb is not None:
                    try:
                        payload["reply_markup"] = kb.to_dict() if hasattr(kb, "to_dict") else kb
                    except Exception:
                        pass
                try:
                    _http_post_single("editMessageText", payload)
                except Exception as e:
                    msg = str(e).lower()
                    if "not modified" in msg or "message is not modified" in msg:
                        continue
                    if "message to edit not found" in msg or "message_id_invalid" in msg:
                        logger.info("refresh_pipeline: msg %s gone in chat %s — skip", old_ids[i], chat_id)
                        continue
                    logger.warning("refresh_pipeline edit failed chat=%s msg=%s: %s",
                                   chat_id, old_ids[i], e)

            # 2) Grow: send extras
            new_ordered = list(old_ids[:common])
            for i in range(common, new_count):
                text, kb = messages[i]
                text = _truncate_html_safe(text)
                payload = {
                    "chat_id": chat_id, "text": text,
                    "parse_mode": "HTML", "disable_web_page_preview": True,
                }
                if kb is not None:
                    try:
                        payload["reply_markup"] = kb.to_dict() if hasattr(kb, "to_dict") else kb
                    except Exception:
                        pass
                try:
                    r = _http_post_single("sendMessage", payload)
                    sent_id = (r or {}).get("message_id")
                    if sent_id:
                        new_ordered.append(int(sent_id))
                        _track_msg(chat_id, int(sent_id))
                except Exception as e:
                    logger.warning("refresh_pipeline send extra failed chat=%s: %s", chat_id, e)

            # 3) Shrink: delete excess
            for i in range(new_count, len(old_ids)):
                try:
                    _http_post_single("deleteMessage", {
                        "chat_id": chat_id, "message_id": old_ids[i],
                    })
                except Exception:
                    pass

            _CHAT_PIPELINE_MSGS[chat_id] = new_ordered
        except Exception:
            logger.exception("refresh_pipeline: chat=%s unexpected", chat_id)


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    """Edit текущей карточки. Длинный HTML — auto-truncate. 'not modified' — silent."""
    text = _truncate_html_safe(text)
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit_message_text: %s", e)



def _mode_badge() -> str:
    """Цветная плашка режима отправки в шапке карточки."""
    mode = config.send_mode()
    if mode == "production":
        return "🔴 <b>PROD</b>"
    if mode == "redirect":
        return "🟡 <b>DEBUG</b>"
    return "🔒 <b>OFF</b>"


def _format_reminder_offer(msg: sqlite3.Row, days_silent: int) -> str:
    """Карточка-предложение оператору пингануть молчащего клиента."""
    lines: list[str] = [
        f"<b>⏰ Клиент молчит {days_silent} дн.</b> · <code>#{msg['id']}</code>",
    ]
    title = msg["ad_title"] or _safe_get(msg, "email_subject") or ""
    if title:
        lines.append(f"📦 {_html(title)}")
    contact = msg["seller_name"] or msg["buyer_name"]
    if contact:
        lines.append(f"👤 {_html(contact)}")
    if msg["ad_url"]:
        lines.append(f'<a href="{_html(msg["ad_url"])}">Объявление</a>')
    lines.append("")
    sent_at = msg["sent_at"] or msg["created_at"]
    if sent_at:
        lines.append(f"🕒 Последнее наше сообщение: {_to_berlin(sent_at, '%Y-%m-%d %H:%M:%S')}")
    if msg["de_answer"]:
        excerpt = msg["de_answer"][:300]
        if len(msg["de_answer"]) > 300:
            excerpt += "…"
        lines.append("")
        lines.append("📤 <i>Что мы писали:</i>")
        lines.append(_html(excerpt))
    return "\n".join(lines)


def _reminder_keyboard(message_id: int) -> dict[str, Any]:
    """Кнопки на карточке-предложении пинга: ping/skip + snooze + назад."""
    return {
        "inline_keyboard": [
            [
                {"text": "✉️ Сгенерить ping", "callback_data": f"remind:{message_id}"},
                {"text": "❌ Забить", "callback_data": f"remindskip:{message_id}"},
            ],
            [
                {"text": "⏰ +1 день", "callback_data": f"snooze:1:{message_id}"},
                {"text": "⏰ +3 дня", "callback_data": f"snooze:3:{message_id}"},
                {"text": "⏰ +7 дней", "callback_data": f"snooze:7:{message_id}"},
            ],
            [
                {"text": "↩ Назад к pipeline", "callback_data": f"back:{message_id}"},
            ],
        ]
    }


def send_reminder_offer(out_message_id: int, days_silent: int) -> Optional[int]:
    """Послать оператору карточку-предложение для исходящего которое висит без ответа."""
    msg = db.get_message(out_message_id)
    if not msg:
        return None
    text = _format_reminder_offer(msg, days_silent)
    result = _http_post("sendMessage", {
        "chat_id": config.telegram_chat_id(),
        "text": text,
        "reply_markup": _reminder_keyboard(out_message_id),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return result.get("message_id")


# ============================================================
# Autopilot notifications (вызываются из scheduler._autopilot_dispatch)
# ============================================================

_AUTOPILOT_STOP_REASONS = {
    "limit": ("🛑", "исчерпан лимит 20 сообщений"),
    "ready_to_buy": ("🎯", "клиент готов покупать, иди закрывай сделку"),
    "wants_contact": ("📞", "просит контакты/реквизиты, передаёт человеку"),
    "threat": ("⚠️", "клиент угрожает, бот предупредил"),
    "manual": (None, None),  # silent
    "stopped_by_sonnet": ("🤖", "Sonnet решил что нужно остановиться"),
}


def send_autopilot_stop_notification(msg_id: int, reason: str) -> None:
    """Пинг операторам в DM при остановке автопилота. reason='manual' — silent."""
    info = _AUTOPILOT_STOP_REASONS.get(reason, ("🛑", reason))
    if info[0] is None:
        return  # silent (manual stop)
    msg = db.get_message(msg_id)
    if not msg:
        return
    ap = db.get_thread_autopilot(msg["gmail_thread_id"] or "")
    sent_count = ap["messages_sent"] if ap else 0
    text = (
        "🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨\n"
        f"🛑 <b>АВТОПИЛОТ ОСТАНОВЛЕН · #{msg_id}</b>\n"
        f"Причина: {info[0]} {info[1]}\n"
        f"Сообщений отправлено: <code>{sent_count}/20</code>\n"
        "🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨"
    )
    kb = {
        "inline_keyboard": [
            [{"text": "📋 Открыть карточку треда", "web_app": {"url": _ma_deep_link(f"thread_{msg['gmail_thread_id']}")}}],
            [{"text": "↩ Назад к pipeline", "callback_data": f"back:{msg_id}"}],
        ]
    }
    _http_post("sendMessage", {
        "chat_id": config.telegram_chat_id(),
        "text": text,
        "reply_markup": kb,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def send_autopilot_progress(msg_id: int, count: int) -> None:
    """Короткий пинг при каждом авто-ответе (только если notify_mode='notify')."""
    msg = db.get_message(msg_id)
    if not msg:
        return
    excerpt = (msg["de_answer"] or "").replace("\n", " ").strip()[:80]
    _http_post("sendMessage", {
        "chat_id": config.telegram_chat_id(),
        "text": f"🤖 <code>#{msg_id}</code> · автопилот {count}/20: <i>{_html(excerpt)}</i>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def send_autopilot_start_notification(msg_id: int, floor: float, actor: str) -> None:
    """Пинг при включении автопилота (notify mode)."""
    _http_post("sendMessage", {
        "chat_id": config.telegram_chat_id(),
        "text": (
            f"🚀 <b>АВТОПИЛОТ ВКЛЮЧЁН · #{msg_id}</b>\n"
            f"floor: <code>{int(floor)}€</code> · запустил {_html(actor)}"
        ),
        "parse_mode": "HTML",
    })


def send_for_review(message_id: int) -> Optional[int]:
    """Отправить карточку оператору(ам) в Telegram. Возвращает первый telegram message_id.

    DM-mode (telegram_operator_dm_ids непуст): рассылает каждому DM-id, каждую копию
    регистрирует в card_dispatches для последующего broadcast-обновления.
    Group-mode (DM-ids пусто): один send в primary telegram_chat_id, как раньше.

    Status='pending' выставляется ТОЛЬКО если row была в state 'new' (первичная подача).
    """
    msg = db.get_message(message_id)
    if not msg:
        logger.warning("send_for_review: сообщение %s не найдено в БД", message_id)
        return None

    text = _minicard_text(msg)
    kb = _ma_review_keyboard(message_id)
    dm_ids = config.telegram_operator_dm_ids()
    targets = dm_ids if dm_ids else [config.telegram_chat_id()]

    # Чистим stale dispatches от прошлых send_for_review для этой row
    db.clear_card_dispatches(message_id)

    first_tg_msg_id: Optional[int] = None
    for chat_id in targets:
        try:
            r = _http_post_single("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": kb,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
            tg_msg_id = r.get("message_id") if isinstance(r, dict) else None
            if tg_msg_id:
                _track_msg(chat_id, tg_msg_id)
                db.add_card_dispatch(message_id, chat_id, tg_msg_id)
                if first_tg_msg_id is None:
                    first_tg_msg_id = tg_msg_id
        except Exception:
            logger.exception("send_for_review fanout: send to %s failed", chat_id)

    if first_tg_msg_id:
        # status НЕ меняем при send_for_review — состояние теперь отражает
        # реальный этап flow ('new'=без драфта, 'pending'=есть RU, 'edited'=DE
        # подтверждён). Раньше делали new→pending как маркер «карточка показана
        # оператору», но с lazy-Sonnet flow status='new' значит «нет ещё драфта».
        db.update_message(message_id, telegram_message_id=first_tg_msg_id)
    return first_tg_msg_id


# ============================================================
# Polling (async) — обработка кнопок и режима редактирования
# ============================================================

# In-memory tracker для batch-удаления при следующем /pipeline.
# Ключ: chat_id (int), значение: set message_id-ов которые нужно удалить.
# Заполняется при каждом отправлении бот-сообщения и при получении операторского текста.
# Очищается полностью при /pipeline вызове.
_CHAT_TRACKED_MSGS: dict[int, set[int]] = {}

# Ordered список pipeline-сообщений по chat_id (header=0, cards=1..N) — нужен
# для in-place edit при auto-refresh (см. refresh_pipeline_for_active_chats).
_CHAT_PIPELINE_MSGS: dict[int, list[int]] = {}

# Persist трекинга на диск: переживает рестарт сервиса, чтобы кнопка «Обновить»
# удаляла карточки, отправленные ДО рестарта (в пределах 48ч-лимита Telegram).
_TRACKING_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tracking_state.json"
)
_tracking_lock = threading.Lock()


def _save_tracking() -> None:
    """Атомарно (tmp+os.replace) сохранить трекинг в JSON. Write-through при каждом
    изменении — объём низкий (десятки сообщений/час)."""
    with _tracking_lock:
        try:
            data = {
                "tracked": {str(k): sorted(v) for k, v in _CHAT_TRACKED_MSGS.items()},
                "pipeline": {str(k): list(v) for k, v in _CHAT_PIPELINE_MSGS.items()},
            }
            tmp = _TRACKING_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, _TRACKING_FILE)
        except RuntimeError:
            pass  # set изменился во время итерации (гонка с _track_msg) — следующий save догонит
        except Exception:
            logger.exception("tracking persist failed")


def _load_tracking() -> None:
    """Загрузить трекинг с диска при старте бота. Нет файла / битый → стартуем с пустого."""
    global _CHAT_TRACKED_MSGS, _CHAT_PIPELINE_MSGS
    try:
        with open(_TRACKING_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _CHAT_TRACKED_MSGS = {int(k): set(v) for k, v in data.get("tracked", {}).items()}
        _CHAT_PIPELINE_MSGS = {int(k): list(v) for k, v in data.get("pipeline", {}).items()}
        logger.info("tracking загружен с диска: %d чатов", len(_CHAT_TRACKED_MSGS))
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("tracking load failed — стартуем с пустого")


def _track_msg(chat_id: Any, msg_id: Optional[int]) -> None:
    """Запомнить message_id чтобы удалить при следующем /pipeline вызове."""
    if not chat_id or not msg_id:
        return
    try:
        cid, mid = int(chat_id), int(msg_id)
    except (TypeError, ValueError):
        return
    with _tracking_lock:
        _CHAT_TRACKED_MSGS.setdefault(cid, set()).add(mid)
    _save_tracking()


def _remember_pipeline_msgs(chat_id: Any, msg_ids: list[int]) -> None:
    """Сохранить ordered список pipeline message_ids для последующего in-place refresh."""
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return
    _CHAT_PIPELINE_MSGS[cid] = [int(m) for m in msg_ids if m]
    _save_tracking()



async def _delete_range(context: Any, chat_id: Any, max_msg_id: int, max_batches: int = 12) -> int:
    """Sweep вниз от max_msg_id батчами по 100 через `deleteMessages` (Bot API 7.4+).

    Чистит накопившиеся сообщения даже когда in-memory tracking потерян (после
    рестарта сервиса). Останавливается:
      • на «стене 48ч» — Telegram не даёт ботам удалять сообщения старше 48 часов
        ('message can't be deleted for everyone') → ниже всё неудаляемо;
      • когда диапазон пуст (already-deleted, 'message to delete not found');
      • после max_batches (≈600 id) — защита от лишних API-вызовов.
    Новый pipeline имеет msg_id > max_msg_id (это trigger), поэтому не затрагивается.
    Возвращает верхнюю оценку числа удалённых (not-found внутри батча пропускаются).
    """
    deleted = 0
    cur = int(max_msg_id)
    empty_streak = 0
    for _ in range(max_batches):
        if cur < 1:
            break
        lo = max(1, cur - 99)
        ids = list(range(lo, cur + 1))
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
            deleted += len(ids)
            empty_streak = 0
        except Exception as e:
            msg = str(e).lower()
            if "can't be deleted" in msg or "too old" in msg:
                # Пограничный батч: часть моложе 48ч, часть старше. deleteMessages
                # фейлит ВЕСЬ батч атомарно — поэтому добиваем удаляемые поштучно
                # (молодые удалятся, старше-48ч — нет). Ниже стены всё неудаляемо → стоп.
                results = await asyncio.gather(
                    *[context.bot.delete_message(chat_id=chat_id, message_id=m) for m in ids],
                    return_exceptions=True,
                )
                deleted += sum(1 for r in results if not isinstance(r, Exception))
                break  # стена 48ч
            empty_streak += 1  # 'not found' и пр. — диапазон пуст
            if empty_streak >= 2:
                break
        cur = lo - 1
    return deleted



def _is_authorized(update: Update) -> bool:
    """Проверка что апдейт пришёл от разрешённого chat_id (allowlist)."""
    chat = update.effective_chat
    if not chat:
        return False
    return str(chat.id) in config.telegram_authorized_ids()


def _actor_name(update: Update) -> str:
    """Имя того кто нажал кнопку / прислал сообщение — для трейла действий в группе."""
    user = getattr(update, "effective_user", None)
    if not user:
        return "?"
    if user.username:
        return f"@{user.username}"
    name = user.first_name or ""
    if user.last_name:
        name = (name + " " + user.last_name).strip()
    return name or str(user.id)


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start — простое приветствие + диагностика chat_id."""
    chat = update.effective_chat
    if not chat:
        return
    if not _is_authorized(update):
        await update.message.reply_text(
            f"Нет доступа. Твой chat_id: {chat.id}\n"
            "Если это твой бот — пропиши его в настройках."
        )
        return
    await update.message.reply_text(
        "Привет! Я Kleinanzeigen Bot. Жди уведомлений о новых сообщениях."
    )


async def _on_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    pending = db.list_messages(status="pending", limit=20)
    if not pending:
        await update.message.reply_text("Нет pending-сообщений.")
        return
    lines = [f"<b>📬 Pending: {len(pending)}</b>"]
    for m in pending[:10]:
        title = (m["ad_title"] or m["email_subject"] or "—")[:50]
        buyer = _safe_get(m, "buyer_display_name") or m["buyer_name"] or "?"
        lines.append(f"#{m['id']} · {_html(buyer)} · {_html(title)}")
    if len(pending) > 10:
        lines.append(f"...и ещё {len(pending) - 10}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        today_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM messages "
            "WHERE substr(created_at,1,10) = ?", (today,)
        ).fetchone()
        week_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM messages "
            "WHERE substr(created_at,1,10) >= ?", (week_ago,)
        ).fetchone()
        total_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM messages"
        ).fetchone()
        lessons_row = conn.execute("SELECT COUNT(*) AS n FROM lessons").fetchone()
    text = (
        f"<b>📊 Статистика</b>\n"
        f"Сегодня: {today_row['n']} сообщ. · ${today_row['c']:.4f}\n"
        f"7 дней: {week_row['n']} сообщ. · ${week_row['c']:.4f}\n"
        f"Всего: {total_row['n']} сообщ. · ${total_row['c']:.4f}\n"
        f"🎓 Уроков: {lessons_row['n']}\n"
        f"\nРежим: {_mode_badge()}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def _on_threads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    threads = db.list_threads()
    if not threads:
        await update.message.reply_text("Тредов пока нет.")
        return
    lines = [f"<b>💬 Активные треды: {len(threads)}</b>"]
    for t in threads[:10]:
        title = (t["ad_title"] or "—")[:40]
        buyer = t["buyer_display_name"] or t["buyer_name"] or "?"
        lines.append(
            f"<code>{t['msg_count']}×</code> {_html(buyer)} · "
            f"{_html(title)} · {t['last_status']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _on_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    args = context.args or []
    if args and args[0] in ("production", "redirect", "disabled"):
        config.set("send_mode", args[0])
        await update.message.reply_text(f"✅ Режим переключён на: {args[0]}\n{_mode_badge()}", parse_mode="HTML")
    else:
        cur = config.send_mode()
        await update.message.reply_text(
            f"Текущий режим: <code>{cur}</code> {_mode_badge()}\n"
            f"Чтобы переключить: <code>/mode production</code> | <code>/mode redirect</code> | <code>/mode disabled</code>",
            parse_mode="HTML",
        )


async def _on_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    config.set_polling_paused(True)
    await update.message.reply_text(
        "⏸ Polling Gmail на паузе.\nВходящие НЕ будут обрабатываться. Снять: /resume"
    )


async def _on_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    config.set_polling_paused(False)
    await update.message.reply_text("▶️ Polling возобновлён.")


PIPELINE_BUTTON_LABEL = "🔄 Обновить / ↩ Назад"


def _persistent_menu_keyboard() -> ReplyKeyboardMarkup:
    """Reply-keyboard висящая внизу чата с одной кнопкой Pipeline."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(PIPELINE_BUTTON_LABEL)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Сообщение или команда...",
    )


def _to_berlin(iso_str: Optional[str], fmt: str = "%H:%M") -> str:
    """Конвертировать UTC ISO timestamp в Europe/Berlin локальное время.

    БД хранит всё в UTC (datetime.utcnow / parsedate_to_datetime → UTC). Оператор
    в Берлине, Kleinanzeigen-мессенджер показывает CEST/CET — UI должен совпадать.
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=__import__("datetime").timezone.utc)
        local = dt.astimezone(zoneinfo.ZoneInfo("Europe/Berlin"))
        return local.strftime(fmt)
    except Exception:
        return iso_str[:16].replace("T", " ")


def _humanize_age(iso_str: Optional[str]) -> str:
    """ISO timestamp → компактный возраст: 'Xмин' / 'Xч Yмин' / 'Xдн Yч'."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        delta = datetime.utcnow() - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "сейчас"
        if secs < 60:
            return f"{secs}с"
        if secs < 3600:
            return f"{secs // 60}мин"
        if secs < 86400:
            h = secs // 3600
            m = (secs % 3600) // 60
            return f"{h}ч {m}мин" if m else f"{h}ч"
        d = secs // 86400
        h = (secs % 86400) // 3600
        return f"{d}дн {h}ч" if h else f"{d}дн"
    except Exception:
        return iso_str[:10]


def _detect_config(msg: sqlite3.Row) -> str:
    """Извлечь конфигурацию товара (для сидений: «2+1», «одиночное», «лавка», …).

    Ищем в title + description + brief'е. Для других категорий вернёт «».
    """
    sources: list[str] = []
    ad_id_val = _safe_get(msg, "ad_id")
    if ad_id_val:
        try:
            bf = db.get_ad_brief(ad_id_val)
            if bf and bf["key_facts_json"]:
                kf = json.loads(bf["key_facts_json"] or "{}")
                ts = kf.get("title_short") or ""
                if ts:
                    sources.append(str(ts))
                for spec in (kf.get("key_specs") or []):
                    sources.append(str(spec))
        except Exception:
            pass
    if msg["ad_title"]:
        sources.append(msg["ad_title"])
    if msg["ad_description"]:
        sources.append((msg["ad_description"] or "")[:500])
    text = " ".join(sources).lower()

    # X+Y паттерн (2+1, 3+1, 1+1, ...) — приоритет
    m = re.search(r"\b([1-9])\s*\+\s*([1-9])\b", text)
    if m:
        return f"{m.group(1)}+{m.group(2)}"
    # Специфичные keywords для сидений
    if re.search(r"\beinzelsitz|одиночн|\bsingle seat", text):
        return "одиночное"
    if re.search(r"\bdoppelsitz|двойн|\bdouble seat|2-?er\b", text):
        return "двойное"
    if re.search(r"3-?er\b|dreier|тройка|3[-\s]местн", text):
        return "3-местное"
    if "sitzbank" in text or "лавка" in text:
        return "лавка"
    return ""


def _short_ad_label(msg: sqlite3.Row) -> str:
    """Название товара для pipeline: оригинальный ad_title + конфигурация (2+1 и т.п.).

    Длина ограничена ~100 char — сжато но информативно.
    """
    title = (msg["ad_title"] or "").strip()
    if not title:
        # Fallback на title_short из брифа
        ad_id_val = _safe_get(msg, "ad_id")
        if ad_id_val:
            try:
                bf = db.get_ad_brief(ad_id_val)
                if bf and bf["key_facts_json"]:
                    kf = json.loads(bf["key_facts_json"] or "{}")
                    title = (kf.get("title_short") or "").strip()
            except Exception:
                pass
        if not title:
            return "—"

    config = _detect_config(msg)
    if config and config.lower() not in title.lower():
        title = f"{title} · {config}"

    # Лимит 100 char
    if len(title) > 100:
        title = title[:99].rstrip() + "…"
    return title


def _short_price(price_str: Optional[str]) -> str:
    """Извлечь только число + €: «2.500 €» / «VB 2500€» → «2500€»."""
    if not price_str:
        return ""
    m = re.search(r"(\d[\d.,]*)", price_str)
    if not m:
        return price_str.strip()
    num = m.group(1).replace(".", "").replace(",", "")
    return f"{num}€"


_NEGATION_TOKENS = ("ohne ", "kein", "nicht ", "без ", "не ", "no ", "ne ")


def _has_unnegated_match(text: str, tokens: tuple[str, ...]) -> bool:
    """Найти любой из tokens в text без отрицания перед ним (в окне 25 chars)."""
    for tok in tokens:
        for m in re.finditer(re.escape(tok), text):
            start = max(0, m.start() - 25)
            prefix = text[start:m.start()]
            if any(neg in prefix for neg in _NEGATION_TOKENS):
                continue  # негация — пропускаем match
            return True
    return False


def _ad_condition_marker(msg: sqlite3.Row) -> str:
    """Распознать состояние товара. Учитывает отрицание («ohne Beschädigungen» → не дефект).

    Возвращает: «🆕 новое» / «📦 б/у» / «⚠️ дефект» / «».
    """
    sources = []
    ad_id_val = _safe_get(msg, "ad_id")
    if ad_id_val:
        try:
            bf = db.get_ad_brief(ad_id_val)
            if bf and bf["key_facts_json"]:
                kf = json.loads(bf["key_facts_json"] or "{}")
                cond = (kf.get("condition") or "").strip().lower()
                if cond:
                    sources.append(cond)
        except Exception:
            pass
    if msg["ad_title"]:
        sources.append(msg["ad_title"].lower())
    if msg["ad_description"]:
        sources.append((msg["ad_description"] or "").lower()[:500])
    text = " ".join(sources)

    # Дефект — приоритет, но проверяем отрицание ("ohne defekt", "без повреждений" — НЕ дефект)
    if _has_unnegated_match(text, (
        "defekt", "beschädig", "kaputt", "не работ", "дефект",
        "поврежд", "сломан", "разбит",
    )):
        return "⚠️ дефект"
    # Новое
    if _has_unnegated_match(text, (
        "neuwertig", "wie neu", "ungenutz", "ungebraucht",
        "новое", "новая", "новый", "ovp", "originalverpack",
    )):
        return "🆕 новое"
    # Изолированное «neu» (но не «neuwertig» как слово)
    if re.search(r"\bneu\b", text) and "neuwertig" not in text:
        return "🆕 новое"
    # Б/у
    if _has_unnegated_match(text, (
        "gebraucht", "gepflegt", "second hand", "б/у", "бу ",
        "guter zustand", "sehr guter zustand",
    )):
        return "📦 б/у"
    return ""


def _supergroup_internal_id(chat_id_str: str) -> Optional[str]:
    """-1003941635553 → '3941635553' (для t.me/c/<id>/<msg> deep-link).
    Возвращает None для личных чатов / обычных групп."""
    s = (chat_id_str or "").strip()
    if s.startswith("-100"):
        return s[4:]
    return None



def _format_pipeline_messages() -> list[tuple[str, Optional[InlineKeyboardMarkup]]]:
    """Список (text, kb) пар для пайплайна. 1 элемент = 1 Telegram-сообщение.

    Структура:
      [0] header — общая шапка с подсчётами
      [1..N] карточка треда — разделитель + 3 строки брифа + 1 кнопка со временем

    Каждый тред = отдельное сообщение. Кнопка `pipe:<msg_id>` ведёт на thread-detail.
    """
    rows = db.pipeline_threads()
    # Деление по тому, кто говорил последним по времени:
    # last_event_kind='in' → клиент, ждёт нас. 'out' → мы, ждём клиента.
    waiting_us = [r for r in rows if (_safe_get(r, "last_event_kind") or "in") == "in"]
    waiting_client = [r for r in rows if (_safe_get(r, "last_event_kind") or "in") == "out"]

    out: list[tuple[str, Optional[InlineKeyboardMarkup]]] = []

    header_lines = [
        "<b>📊 Состояние переговоров</b>",
        f"<i>🔴 ждут нас: {len(waiting_us)}  ·  🟢 ждём клиента: {len(waiting_client)}  ·  📝 = есть готовый черновик от бота</i>",
    ]
    if not waiting_us and not waiting_client:
        header_lines.append("")
        header_lines.append("<i>Активных тредов нет — все обработаны / завершены.</i>")
    out.append(("\n".join(header_lines), None))

    n = 0
    # Порядок секций: сначала «🟢 ждём клиента» (менее срочные / старые), затем
    # «🔴 ждут нас» (требуют действия). Так самые свежие события (обычно входящие
    # от клиентов = красные) оказываются ВНИЗУ чата — рядом с полем ввода оператора.
    for section_rows, marker in ((waiting_client, "🟢"), (waiting_us, "🔴")):
        for r in section_rows:
            n += 1
            out.append(_pipeline_thread_card(r, n, marker))

    return out


def _pipeline_thread_card(r: sqlite3.Row, n: int, marker: str) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка одного треда: разделитель + 3 строки брифа в тексте + 1 inline-кнопка со временем.

    Каждая строка ≤ 65 символов. Кнопка `pipe:<msg_id>` ведёт на thread-detail.
    Статус и таймстамп опираются на хронологию событий (last_event_at/_kind),
    а не на «id последнего in-row» — так пайплайн отражает реальный ход переписки.
    """
    has_real = bool(_safe_get(r, "has_real_reply"))
    has_any_sent = bool(_safe_get(r, "has_any_sent"))
    has_draft = bool(_safe_get(r, "has_pending_draft"))
    has_ack_only = has_any_sent and not has_real
    last_kind = _safe_get(r, "last_event_kind") or "in"
    pending_count = int(_safe_get(r, "pending_drafts_count") or 0)

    # Автопилот active → перебивает обычный status
    ap = db.get_thread_autopilot(r["gmail_thread_id"] or "")
    is_ap_active = bool(ap and ap["active"])

    # Статус по хронологии последнего события + наличию pending драфтов
    if is_ap_active:
        status_txt = f"🤖 автопилот {ap['messages_sent']}/20"
    elif last_kind == "out":
        # Последним говорили мы → ждём клиента
        status_txt = "ответили · ждём клиента" if has_real else "ack · ждём клиента"
        if has_draft:
            status_txt += f" (+{pending_count} draft)"
    else:
        # Последним был клиент → нужна наша реакция
        if has_draft and pending_count > 1:
            status_txt = f"draft x{pending_count} · ждёт нас"
        elif has_draft:
            status_txt = "draft · ждёт нас"
        elif has_ack_only:
            status_txt = "ack · ждёт нас"
        elif has_real:
            status_txt = "ответили · клиент написал"
        else:
            status_txt = "новый · ждёт нас"

    draft_marker = "📝" if has_draft and not is_ap_active else ""
    buyer_raw = _safe_get(r, "buyer_display_name") or r["buyer_name"] or "?"
    buyer = (buyer_raw.split("@")[0] if "@" in buyer_raw else buyer_raw)[:25]
    ad = _short_ad_label(r)
    price = _short_price(r["ad_price"])

    # Таймстамп = реальное последнее событие (in.created_at или out.sent_at)
    ts_iso = _safe_get(r, "last_event_at") or _safe_get(r, "sent_at") or r["created_at"]
    hhmm = _to_berlin(ts_iso, "%H:%M")
    age = _humanize_age(ts_iso)

    # 3 текстовых строки брифа
    line1 = f"<b>{n} {marker}{draft_marker} · {_html(buyer)} · {status_txt}</b>"
    cond_marker = _ad_condition_marker(r)
    line2_parts = [_html(ad)]
    if cond_marker:
        line2_parts.append(cond_marker)  # эмодзи безопасны
    if price:
        line2_parts.append(_html(price))
    line2 = " · ".join(line2_parts)
    line3_raw = _pipeline_line3_context(r)
    line3 = _html(line3_raw) if line3_raw and line3_raw != "—" else ""

    text_parts = [line1, line2]
    if line3:
        text_parts.append(line3)
    text = "\n".join(text_parts)

    button_label = f"⏱ {hhmm} · {age} назад"
    if len(button_label) > 64:
        button_label = button_label[:63] + "…"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(button_label, web_app=WebAppInfo(url=_ma_deep_link(f"thread_{r['gmail_thread_id']}")))
    ]])
    return text, kb



def _pipeline_line3_context(r: sqlite3.Row) -> str:
    """Sales-контекст для 3-й строки карточки.

    Приоритет:
        1. deal_brief_json (динамический бриф сделки от Sonnet) — если есть.
           Возвращает многострочный текст: «🤝 резюме», «📋 expected», «🏷 assessment».
        2. ad_briefs.key_facts (статичный бриф объявления) — fallback.
        3. Превью последнего сообщения клиента — последний fallback.
    """
    # 1. Динамический deal_brief
    deal_raw = _safe_get(r, "deal_brief_json")
    if deal_raw:
        try:
            d = json.loads(deal_raw)
            lines: list[str] = []
            summary = (d.get("summary_ru") or "").strip()
            if summary:
                lines.append(f"🤝 {summary}")
            expected = (d.get("expected_next") or "").strip()
            price = d.get("negotiated_price_eur")
            assess = (d.get("client_assessment") or "").strip()
            tail_parts = []
            if isinstance(price, (int, float)) and price > 0:
                tail_parts.append(f"💰 {int(price)}€")
            if expected:
                tail_parts.append(f"⏳ {expected}")
            if assess:
                tail_parts.append(f"🏷 {assess}")
            if tail_parts:
                lines.append(" · ".join(tail_parts))
            if lines:
                return "\n".join(lines)
        except Exception:
            pass

    # 2. Статичный ad_brief
    ad_id_val = _safe_get(r, "ad_id")
    if ad_id_val:
        try:
            bf = db.get_ad_brief(ad_id_val)
            if bf and bf["key_facts_json"]:
                kf = json.loads(bf["key_facts_json"] or "{}")
                parts: list[str] = []
                cond = (kf.get("condition") or "").strip()
                if cond:
                    parts.append(f"💎 {cond}")
                min_p = kf.get("min_acceptable_eur")
                if isinstance(min_p, (int, float)) and min_p > 0:
                    parts.append(f"мин {int(min_p)}€")
                if parts:
                    return " · ".join(parts)
        except Exception:
            pass

    # 3. Превью последнего incoming-сообщения клиента
    text = (r["ru_client"] or r["de_client"] or "").strip().replace("\n", " ")
    if text:
        if len(text) > 55:
            text = text[:54].rstrip() + "…"
        return f"← {text}"
    return "—"


def _format_thread_detail(msg: sqlite3.Row) -> str:
    """Полная карточка треда: лог реальных событий переписки + meta.

    Логика рендера:
    - Используем `db.thread_events()` — flat-список с реальными таймстампами
      (incoming = email Date, outgoing = sent_at). Каждое событие — одно сообщение.
    - Показываем ТОЛЬКО реально произошедшее: incoming клиента и наши отправленные
      ответы (status sent/sent_debug). Pending/new/edited черновики не показываем —
      они не часть истории, оператор увидит их в review-карточке.
    - Auto-ack показываем как обычное наше сообщение — клиент его получил и видит,
      это часть его восприятия диалога (поэтому скрывать нельзя — теряется смысл
      ответа клиента «Danke» в ответ на auto-ack).
    """
    thread_id = msg["gmail_thread_id"] or ""
    events = db.thread_events(thread_id) if thread_id else []
    # Фильтр: только incoming + реально отправленное. Драфты (pending/new/edited/approved)
    # — не часть истории, они в review-карточке.
    SENT_OUT = {"sent", "sent_debug"}
    visible_events = [
        e for e in events
        if e["kind"] == "in" or e["status"] in SENT_OUT
    ]

    # Header
    title = msg["ad_title"] or _safe_get(msg, "email_subject") or "—"
    buyer_disp = _safe_get(msg, "buyer_display_name") or msg["buyer_name"] or "?"
    last_ts = (
        max((e["ts"] for e in visible_events if e["ts"]), default=msg["created_at"])
        or msg["created_at"]
    )
    age = _humanize_age(last_ts)

    lines: list[str] = [
        f"<b>📋 Тред #{msg['id']}</b>",
        f"<b>📦 {_html(title)}</b>",
    ]
    if msg["ad_price"]:
        lines.append(f"💰 {_html(msg['ad_price'])}")
    lines.append(f"👤 Клиент: <b>{_html(buyer_disp)}</b>")

    # Наш аккаунт — кому именно пишет клиент (важно для multi-account работы)
    acc = db.get_account(msg["account_id"]) if msg["account_id"] else None
    if acc:
        seller_label = msg["seller_name"] or acc["name"] or ""
        gmail_email = acc["gmail_email"] or ""
        if seller_label and gmail_email:
            lines.append(f"🏪 Наш: {_html(seller_label)} (<code>{_html(gmail_email)}</code>)")
        elif gmail_email:
            lines.append(f"🏪 Наш: <code>{_html(gmail_email)}</code>")

    if msg["ad_url"]:
        ad_id_val = _safe_get(msg, "ad_id")
        link_text = f"Объявление #{ad_id_val}" if ad_id_val else "Объявление"
        lines.append(f'<a href="{_html(msg["ad_url"])}">{link_text}</a>')
    lines.append(f"⏱ Последнее событие: {age} назад ({_to_berlin(last_ts, '%Y-%m-%d %H:%M')})")

    related_warn = _format_related_warning(msg)
    if related_warn:
        lines.append("")
        lines.append(related_warn)
    lines.append("")
    lines.append("─────────────────")

    # Лента событий — таймстампы в Europe/Berlin (UTC хранится в БД, оператор в CEST)
    for e in visible_events:
        r = e["row"]
        ts = _to_berlin(e["ts"], "%H:%M")
        if e["kind"] == "in":
            cl = _safe_get(r, "client_lang") or "?"
            cflag = claude.lang_display(cl)[1]
            sender = (_safe_get(r, "buyer_display_name") or r["buyer_name"] or "?").split("@")[0][:25]
            lines.append(f"<b>📥 {_html(sender)} · {ts} · {cflag}:</b>")
            lines.append(f"<blockquote>{_html(e['text'])}</blockquote>")
            if e["ru_text"] and (cl != "ru"):
                lines.append(f"<i>🇷🇺 {_html(e['ru_text'])}</i>")
            lines.append("")
        else:
            ack_marker = " 🤖 ack" if e["is_auto_ack"] else ""
            ping_marker = " 🔔" if _safe_get(r, "is_reminder") else ""
            status_marker = " ✅" if e["status"] == "sent" else " 🟡"  # sent_debug
            al = _safe_get(r, "answer_lang") or _safe_get(r, "client_lang") or "de"
            aflag = claude.lang_display(al)[1]
            lines.append(f"<b>📤 Мы{ack_marker} · {ts}{ping_marker}{status_marker} · {aflag}:</b>")
            lines.append(f"<blockquote>{_html(e['text'])}</blockquote>")
            if e["ru_text"]:
                lines.append(f"<i>🇷🇺 {_html(e['ru_text'])}</i>")
            lines.append("")

    lines.append("─────────────────")
    return "\n".join(lines)


def _thread_detail_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура thread-detail карточки: Compose + быстрые действия + Назад."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Написать клиенту сообщение", callback_data=f"compose:{msg_id}")],
        [InlineKeyboardButton("📋 Открыть карточку ревью", callback_data=f"opencard:{msg_id}")],
        [
            InlineKeyboardButton("⏳ Ждать ответа", callback_data=f"waitclient:{msg_id}"),
            InlineKeyboardButton("🏁 Завершить", callback_data=f"closethread:{msg_id}"),
        ],
        [InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{msg_id}")],
    ])


def _split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """Разрезать длинный HTML-текст для Telegram (лимит 4096) на несколько чанков.

    Делим по двойным `\\n\\n` ТОЛЬКО на границах ВНЕ открытых HTML-блоков.
    Иначе можем разорвать `<blockquote>foo\\n\\nbar</blockquote>` пополам и
    Telegram упадёт с «can't find end tag corresponding to start tag».

    Стратегия:
      1. Идём по тексту, считаем баланс `<blockquote>` / `</blockquote>`.
      2. Кандидаты для cut — позиции `\\n\\n` где баланс == 0 (вне блока).
      3. Берём последний валидный cut ≤ limit. Повторяем для остатка.
      4. Fallback: hard-cut если ни одной границы не нашлось.
    """
    if len(text) <= limit:
        return [text]

    OPEN_TAGS = ("<blockquote>", "<pre>", "<code>")
    CLOSE_TAGS = ("</blockquote>", "</pre>", "</code>")

    def _safe_cut_positions(s: str, hard_limit: int) -> int:
        """Найти лучшую safe-позицию <= hard_limit для разреза."""
        # Считаем баланс открытых тегов до каждого `\n\n`
        depth = 0
        last_safe = -1
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            # Дешёвый чек: открывающий тег
            if ch == "<":
                matched = False
                for ot in OPEN_TAGS:
                    if s.startswith(ot, i):
                        depth += 1
                        i += len(ot)
                        matched = True
                        break
                if matched:
                    continue
                for ct in CLOSE_TAGS:
                    if s.startswith(ct, i):
                        depth -= 1
                        i += len(ct)
                        matched = True
                        break
                if matched:
                    continue
            # `\n\n` — кандидат если depth == 0
            if depth == 0 and ch == "\n" and i + 1 < n and s[i + 1] == "\n":
                if i + 2 <= hard_limit:
                    last_safe = i + 2  # позиция ПОСЛЕ \n\n
                else:
                    break
            i += 1
        return last_safe

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = _safe_cut_positions(rest, limit)
        if cut <= 0:
            # Fallback: ищем любой `\n\n` <= limit (даже внутри тегов — лучше рисковать
            # один раз, чем бесконечно). Если нет — hard-cut.
            cut = rest.rfind("\n\n", 0, limit)
            if cut == -1:
                cut = limit
            else:
                cut += 2
        chunks.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def _locked_keyboard(msg_id: int, actor: str) -> dict[str, Any]:
    """Soft-lock клавиатура: показывает «🔒 В работе у X» вместо реальных кнопок.

    Тап → noop-callback → popup-toast. Используется для broadcast не-актору в
    DM-fanout — op2 видит «затенённое» состояние пока op1 что-то делает.
    Внизу — обязательное «↩ Назад к pipeline» (правило: на любом экране бота).
    """
    short_actor = actor[:30] if actor else "оператор"
    return {
        "inline_keyboard": [
            [{"text": f"🔒 В работе у {short_actor}",
              "callback_data": f"locked_noop:{msg_id}"}],
            [{"text": "↩ Назад к pipeline", "callback_data": f"back:{msg_id}"}],
        ]
    }


def _back_only_keyboard(msg_id: int) -> dict[str, Any]:
    """Минимальная клавиатура «↩ Назад к pipeline» — для финальных состояний карточки
    (после send/skip/sold), чтобы оператор всегда мог вернуться к списку тредов."""
    return {
        "inline_keyboard": [[
            {"text": "↩ Назад к pipeline", "callback_data": f"back:{msg_id}"},
        ]],
    }


async def _on_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /pipeline — удалить ВСЁ ранее отправленное в чат, показать свежий статус."""
    if not _is_authorized(update):
        return

    chat_id = update.effective_chat.id

    # Запомним свежий триггер (сообщение пользователя с /pipeline или «📊 Pipeline»)
    if update.message:
        _track_msg(chat_id, update.message.message_id)

    trigger_msg_id = (update.message.message_id if update.message else 0) or 0

    # СНАЧАЛА шлём свежий pipeline (чтобы chat никогда не казался пустым → нет
    # «Запустить бота» CTA-плейсхолдера от Telegram). ПОТОМ удаляем старое.
    messages = _format_pipeline_messages()
    persistent_kb = _persistent_menu_keyboard()
    pipeline_ids: list[int] = []
    for i, (text, kb) in enumerate(messages):
        kwargs: dict[str, Any] = {
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if kb:
            kwargs["reply_markup"] = kb
        elif i == 0:
            kwargs["reply_markup"] = persistent_kb
        try:
            msg = await context.bot.send_message(**kwargs)
            if msg:
                _track_msg(chat_id, msg.message_id)
                pipeline_ids.append(msg.message_id)
        except Exception:
            logger.exception("Не удалось послать pipeline-сообщение")

    _remember_pipeline_msgs(chat_id, pipeline_ids)

    # Теперь удаляем старое: tracked-минус-новый-pipeline + sweep назад от trigger
    # (новый pipeline имеет msg_id > trigger_msg_id, в sweep не попадёт)
    new_pipeline_ids = set()
    if chat_id in _CHAT_TRACKED_MSGS:
        # отделяем только что отправленные pipeline-сообщения чтобы не удалить
        # (они только что добавлены в _CHAT_TRACKED_MSGS, имеют > trigger_msg_id)
        for mid in list(_CHAT_TRACKED_MSGS[chat_id]):
            if trigger_msg_id and mid > trigger_msg_id:
                new_pipeline_ids.add(mid)
        # удаляем всё кроме нового pipeline
        old_msgs = _CHAT_TRACKED_MSGS[chat_id] - new_pipeline_ids
        _CHAT_TRACKED_MSGS[chat_id] = new_pipeline_ids  # сохраняем актуальный pipeline для следующего удаления
        deleted_tracked = 0
        for mid in sorted(old_msgs, reverse=True):
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                deleted_tracked += 1
            except Exception:
                pass
    else:
        deleted_tracked = 0

    deleted_swept = 0
    if trigger_msg_id:
        deleted_swept = await _delete_range(context, chat_id, trigger_msg_id)
    logger.info(
        "/pipeline: удалено %d tracked + %d sweep в chat %s (pipeline kept: %d)",
        deleted_tracked, deleted_swept, chat_id, len(new_pipeline_ids),
    )
    _save_tracking()  # зафиксировать pruned-набор на диск


async def _on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /menu — установить/восстановить persistent reply-кнопку 📊 Pipeline."""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "📊 Меню активно — кнопка <code>🔄 Обновить</code> закреплена внизу чата.\n"
        "Тапни её чтобы очистить чат и показать свежий pipeline.",
        parse_mode="HTML",
        reply_markup=_persistent_menu_keyboard(),
    )


async def _on_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /card N — переслать карточку ревью для msg #N (для jump-to-card с мобилы)."""
    if not _is_authorized(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /card <id> — например /card 67")
        return
    try:
        mid = int(args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text(f"Битый id: {args[0]!r}")
        return
    msg = db.get_message(mid)
    if not msg:
        await update.message.reply_text(f"msg #{mid} не найден")
        return
    new_tg = send_for_review(mid)
    if new_tg:
        await update.message.reply_text(f"↗ Карточка #{mid} переслана.")


async def _on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "<b>Команды:</b>\n"
        "/pipeline — 📊 состояние переговоров (что ждёт нас, что — клиента)\n"
        "/menu — закрепить «📊 Pipeline» внизу чата (если случайно убрал)\n"
        "/card &lt;id&gt; — переслать карточку ревью (для jump-to)\n"
        "/pending — список pending-сообщений\n"
        "/stats — статистика и расходы API\n"
        "/threads — активные диалоги\n"
        "/mode [production|redirect|disabled] — режим отправки\n"
        "/pause — поставить polling на паузу\n"
        "/resume — возобновить polling\n"
        "/start — приветствие, диагностика chat_id\n"
        "/help — этот список",
        parse_mode="HTML",
    )




async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия inline-кнопок ✅/✏️/❌."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not _is_authorized(update):
        await query.message.reply_text("Нет доступа.")
        return

    raw = query.data or ""

    # confirm:<original> → bypass-флаг, выполнить как оригинал
    bypass_confirm = raw.startswith("confirm:")
    payload = raw[len("confirm:"):] if bypass_confirm else raw

    try:
        parts = payload.split(":")
        if len(parts) == 2:
            action, msg_id_str = parts
            sub = ""
        elif len(parts) == 3:
            action, sub, msg_id_str = parts
        else:
            raise ValueError("bad parts")
        msg_id = int(msg_id_str)
    except (ValueError, AttributeError):
        await query.message.reply_text(f"Битый callback: {raw!r}")
        return

    msg = db.get_message(msg_id)
    if not msg:
        await query.message.reply_text("Сообщение не найдено в БД.")
        return

    actor = _actor_name(update)
    action_key = f"{action}:{sub}" if sub else action

    # ── Confirmation gate ──
    # A видит confirm-preview с [▶️ Продолжить] / [❌ Отменить].
    # Остальные операторы видят «🔒 @A в подтверждении» без кнопок (мягкий лок).
    if not bypass_confirm and action_key in NEEDS_CONFIRM:
        confirm_text = _minicard_text(msg) + _confirmation_addendum(action_key)
        await _safe_edit(
            query, confirm_text,
            reply_markup=_confirmation_keyboard(payload, msg_id),
        )
        # Broadcast «soft lock» к остальным dispatches — ярко
        locked_text = _minicard_text(msg) + (
            "\n\n🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨\n"
            f"⚠️ <b>{_html(actor).upper()} ПОДТВЕРЖДАЕТ ДЕЙСТВИЕ</b> ⚠️\n"
            f"<i>Подожди завершения или отмены</i>\n"
            "🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨"
        )
        locked_kb = _locked_keyboard(msg_id, actor)
        for d in db.list_card_dispatches(msg_id):
            if str(d["chat_id"]) == str(query.message.chat_id) and d["tg_msg_id"] == query.message.message_id:
                continue
            try:
                await context.bot.edit_message_text(
                    chat_id=d["chat_id"],
                    message_id=d["tg_msg_id"],
                    text=locked_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=locked_kb,
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    logger.warning("confirm-lock-broadcast: %s", e)
            except Exception:
                logger.exception("confirm-lock-broadcast")
        return

    # ── Cancel из confirm-карточки → восстановить нормальный вид ──
    # Прямой edit query.message (всегда работает) + broadcast best-effort к остальным
    if action == "cancel":
        text = _minicard_text(msg)
        kb = _ma_review_keyboard(msg_id)
        await _safe_edit(query, text, reply_markup=kb)
        await _broadcast_card(context, msg_id, text, reply_markup=kb)
        return

    # ── Cancel из input-режима (правка/цена/инструкция) → закрыть, восстановить ──
    if action == "inputcancel":
        _release_lock(msg_id)
        msg = db.get_message(msg_id)
        if msg:
            await _broadcast_card(
                context, msg_id, _minicard_text(msg),
                reply_markup=_ma_review_keyboard(msg_id),
            )
        return

    # ── noop: сепараторная кнопка в pipeline — ничего не делает ──
    if action == "noop":
        return  # query.answer() уже сработал в начале

    # ── locked_noop: тап по «🔒 В работе у X» — popup-toast, ничего не меняем ──
    if action == "locked_noop":
        await query.answer(
            "🔒 Карточка в работе у другого оператора. Жди освобождения.",
            show_alert=False,
        )
        return

    # ── back: назад к pipeline (то же что тап «🔄 Обновить») ──
    # Send-first → delete-after pattern, чтобы chat не пустел.
    if action == "back":
        chat_id = query.message.chat_id
        clicked_msg_id = query.message.message_id

        # 1) Шлём свежий pipeline (chat не пустеет)
        messages = _format_pipeline_messages()
        persistent_kb = _persistent_menu_keyboard()
        new_ids: set[int] = set()
        ordered_ids: list[int] = []
        for i, (text, kb) in enumerate(messages):
            kwargs: dict[str, Any] = {
                "chat_id": chat_id, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }
            if kb:
                kwargs["reply_markup"] = kb
            elif i == 0:
                kwargs["reply_markup"] = persistent_kb
            try:
                m = await context.bot.send_message(**kwargs)
                if m:
                    _track_msg(chat_id, m.message_id)
                    new_ids.add(m.message_id)
                    ordered_ids.append(m.message_id)
            except Exception:
                logger.exception("back→pipeline send failed")
        _remember_pipeline_msgs(chat_id, ordered_ids)

        # 2) Удаляем старое: tracked минус new_ids + sweep назад от clicked
        if chat_id in _CHAT_TRACKED_MSGS:
            old = _CHAT_TRACKED_MSGS[chat_id] - new_ids
            _CHAT_TRACKED_MSGS[chat_id] = new_ids
            for mid in sorted(old, reverse=True):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
        await _delete_range(context, chat_id, clicked_msg_id, scan=50)
        return

    # ── Lock-check: actions меняющие state треда требуют монопольного доступа ──
    # Если другой оператор уже работает с тредом — popup, return.
    LOCKABLE_ACTIONS = {"closethread", "waitclient",
                        "propose", "translate_confirm", "back_to_ru"}
    if action in LOCKABLE_ACTIONS:
        owner = _check_lock(msg_id, actor)
        if owner:
            remaining = _lock_remaining_min(msg_id)
            await query.answer(
                f"🔒 Тред в работе у {owner}, ~{remaining} мин до автосброса.",
                show_alert=True,
            )
            return
        _acquire_lock(msg_id, actor)

    # ── Pipeline-навигация: тап на кнопку треда → thread-detail на чистом ниже ──
    # Шлём detail-карточку, потом удаляем СТАРЫЕ tracked (pipeline-карточки) кроме
    # только что отправленной. БЕЗ агрессивного sweep (пара десятков) — sweep есть
    # только в Обновить/Назад. Здесь — узкое удаление tracked-only.
    if action == "pipe":
        chat_id = query.message.chat_id

        # Тред может быть длиннее 4096 chars — рубим на чанки. Клавиатура
        # на последнем сообщении.
        text = _format_thread_detail(msg)
        chunks = _split_for_telegram(text, limit=4000)
        sent_ids: list[int] = []
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            sent = await context.bot.send_message(
                chat_id=chat_id, text=chunk,
                reply_markup=_thread_detail_keyboard(msg_id) if is_last else None,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            if sent and sent.message_id:
                _track_msg(chat_id, sent.message_id)
                sent_ids.append(sent.message_id)

        # Удаляем все tracked-сообщения СТАРШЕ первого нового чанка,
        # КРОМЕ наших только что отправленных. Это убивает весь pipeline
        # (header + карточки тредов) и любые residual-чанки от прошлых открытий.
        if sent_ids:
            first_new = min(sent_ids)
            sent_set = set(sent_ids)
            if chat_id in _CHAT_TRACKED_MSGS:
                to_delete = sorted(
                    {mid for mid in _CHAT_TRACKED_MSGS[chat_id]
                     if mid < first_new and mid not in sent_set},
                    reverse=True,
                )
                try:
                    await context.bot.delete_messages(chat_id=chat_id, message_ids=to_delete)
                except Exception:
                    await asyncio.gather(*[
                        context.bot.delete_message(chat_id=chat_id, message_id=mid)
                        for mid in to_delete
                    ], return_exceptions=True)
                _CHAT_TRACKED_MSGS[chat_id] = sent_set
        return

    if action == "waitclient":
        thread_id = msg["gmail_thread_id"] or ""
        if not thread_id:
            await query.message.reply_text("⚠️ У сообщения нет gmail_thread_id.")
            _release_lock(msg_id)
            return
        # Все pending in-rows этого треда → skipped (не висят как «есть драфт»).
        with db.get_conn() as _conn:
            _conn.execute(
                "UPDATE messages SET status='skipped' "
                "WHERE gmail_thread_id = ? AND direction='in' "
                "AND status IN ('pending', 'new', 'edited', 'approved')",
                (thread_id,),
            )
        db.mark_thread_waiting(thread_id, marked_by=actor)
        await _broadcast_card(
            context, msg_id,
            _minicard_text(db.get_message(msg_id)) + (
                "\n\n🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢\n"
                f"⏳ <b>{_html(actor).upper()}: ЖДЁМ ОТВЕТА КЛИЕНТА</b>\n"
                "<i>Тред в 🟢 секции pipeline. Авто-сброс при новом сообщении от клиента.</i>\n"
                "🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢"
            ),
            reply_markup=_back_only_keyboard(msg_id),
        )
        _release_lock(msg_id)
        return

    if action == "closethread":
        thread_id = msg["gmail_thread_id"] or ""
        if not thread_id:
            await query.message.reply_text("⚠️ У сообщения нет gmail_thread_id — нечего закрывать.")
            _release_lock(msg_id)
            return
        # Все pending in-rows этого треда → skipped (чтоб не висели как «есть драфт»).
        with db.get_conn() as _conn:
            _conn.execute(
                "UPDATE messages SET status='skipped' "
                "WHERE gmail_thread_id = ? AND direction='in' "
                "AND status IN ('pending', 'new', 'edited', 'approved')",
                (thread_id,),
            )
        db.close_thread(thread_id, closed_by=actor)
        await _broadcast_card(
            context, msg_id,
            _minicard_text(db.get_message(msg_id)) + (
                "\n\n🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢\n"
                f"🏁 <b>{_html(actor).upper()} ЗАВЕРШИЛ БЕСЕДУ</b>\n"
                "<i>Тред убран из «Состояние переговоров». Если клиент напишет — вернётся автоматически.</i>\n"
                "🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢"
            ),
            reply_markup=_back_only_keyboard(msg_id),
        )
        _release_lock(msg_id)
        return

    # ── Reminder-карточки ──
    # Финальные состояния (после действия) ВСЕГДА с _back_only_keyboard —
    # правило: на любом экране бота должна быть кнопка возврата к pipeline.
    if action == "remind":
        await _safe_edit(query, (query.message.text_html or "") + f"\n\n⏳ <i>{_html(actor)} → генерирую follow-up...</i>", reply_markup=None)
        import scheduler
        result = await asyncio.to_thread(scheduler.send_followup_ping, msg_id)
        await _safe_edit(
            query,
            (query.message.text_html or "") + f"\n\n<b>👤 {_html(actor)}:</b>\n{_html(result['message'])}",
            reply_markup=_back_only_keyboard(msg_id),
        )
        return

    if action == "remindskip":
        db.update_message(msg_id, reminder_state="skipped")
        await _safe_edit(
            query,
            (query.message.text_html or "") + f"\n\n❌ <i>{_html(actor)} забил на пинг.</i>",
            reply_markup=_back_only_keyboard(msg_id),
        )
        return

    if action == "snooze":
        from datetime import datetime, timedelta
        try:
            days = int(sub)
        except ValueError:
            await query.message.reply_text(f"Битый snooze: {sub}")
            return
        snooze_until = (datetime.utcnow() + timedelta(days=days)).isoformat()
        db.update_message(msg_id, reminder_snooze_until=snooze_until, reminder_state=None)
        await _safe_edit(
            query,
            (query.message.text_html or "") + f"\n\n⏰ <i>{_html(actor)} отложил на {days} дн. — напомню {_to_berlin(snooze_until, '%Y-%m-%d')}</i>",
            reply_markup=_back_only_keyboard(msg_id),
        )
        return

    if action == "propose":
        # Триггер Sonnet draft. До этого момента row была status='new' без de/ru_answer.
        await _broadcast_card(
            context, msg_id,
            _minicard_text(msg) + f"\n\n⏳ <i>{_html(actor)} → генерирую черновик ответа Sonnet…</i>",
            reply_markup=None,
        )
        import scheduler as _sched
        result = await asyncio.to_thread(_sched.generate_draft_for_msg, msg_id)
        updated = db.get_message(msg_id) or msg
        if result["kind"] == "generated":
            footer = f"\n\n<i>🤖 Черновик готов (стоило ${result['cost_usd']:.4f}). Проверь и нажми «✅ Перевести и подтвердить».</i>"
        else:
            footer = f"\n\n❌ <i>Не удалось: {_html(result.get('message') or 'unknown')}</i>"
        await _broadcast_card(
            context, msg_id,
            _minicard_text(updated) + footer,
            reply_markup=_ma_review_keyboard(msg_id),
        )
        _release_lock(msg_id)
        return

    if action == "translate_confirm":
        # RU черновик → DE-перевод. Status pending → edited.
        if not msg["ru_answer"]:
            await query.answer("Сначала сгенерируй RU-черновик.", show_alert=True)
            _release_lock(msg_id)
            return
        target_lang = msg["client_lang"] or "de"
        ad_ctx = msg["ad_title"] or _safe_get(msg, "email_subject") or ""
        await _broadcast_card(
            context, msg_id,
            _minicard_text(msg) + f"\n\n⏳ <i>{_html(actor)} → перевожу RU → {target_lang}…</i>",
            reply_markup=None,
        )
        try:
            tr = await asyncio.to_thread(
                claude.translate_only,
                msg["ru_answer"], target_lang=target_lang, context=ad_ctx,
            )
        except Exception as e:
            logger.exception("translate_confirm fail msg=%s", msg_id)
            await _broadcast_card(
                context, msg_id,
                _minicard_text(msg) + f"\n\n❌ <i>Перевод упал: {_html(str(e))}</i>",
                reply_markup=_ma_review_keyboard(msg_id),
            )
            _release_lock(msg_id)
            return
        new_de = tr["translation"]
        prior_cost = msg["cost_usd"] or 0.0
        prior_in = msg["tokens_in"] or 0
        prior_out = msg["tokens_out"] or 0
        db.update_message(
            msg_id,
            de_answer=new_de,
            ru_translation=msg["ru_answer"],  # back-translation = исходный RU
            answer_lang=target_lang,
            status="edited",
            tokens_in=prior_in + tr.get("tokens_in", 0),
            tokens_out=prior_out + tr.get("tokens_out", 0),
            cost_usd=prior_cost + tr.get("cost_usd", 0.0),
        )
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id,
            _minicard_text(updated) + (
                f"\n\n<i>🌐 Перевёл (стоило ${tr.get('cost_usd', 0.0):.4f}). "
                "Проверь DE и нажми «✅ ОТПРАВИТЬ».</i>"
            ),
            reply_markup=_ma_review_keyboard(msg_id),
        )
        _release_lock(msg_id)
        return

    if action == "back_to_ru":
        # Откатить статус из 'edited' обратно в 'pending' — оператор хочет
        # переделать RU. DE остаётся как есть до следующего translate_confirm.
        db.update_message(msg_id, status="pending")
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id,
            _minicard_text(updated) + f"\n\n<i>↩ {_html(actor)} вернулся к редактированию RU. Тапни «✅ Перевести и подтвердить» когда будешь готов.</i>",
            reply_markup=_ma_review_keyboard(msg_id),
        )
        _release_lock(msg_id)
        return

    # Default: unknown action OR action that was moved to MA. Inform user gently.
    await query.answer(text="Открой в Mini App для этого действия", show_alert=False)


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст вне команд: триггер кнопки «📊 Pipeline» или диспатч по action."""
    if not _is_authorized(update):
        return
    text_raw = (update.message.text or "").strip()

    # Триггер persistent reply-кнопки «📊 Pipeline»
    if text_raw == PIPELINE_BUTTON_LABEL:
        try:
            await update.message.delete()
        except Exception:
            pass
        await _on_pipeline(update, context)
        return

    # Non-pipeline text: ignore (input-mode sessions removed)


# ============================================================
# Сборка и запуск
# ============================================================

def build_application() -> Application:
    """Собрать Application с handler-ами."""
    token = config.telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан Telegram bot token в настройках")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(CommandHandler("pending", _on_pending))
    app.add_handler(CommandHandler("stats", _on_stats))
    app.add_handler(CommandHandler("threads", _on_threads))
    app.add_handler(CommandHandler("pipeline", _on_pipeline))
    app.add_handler(CommandHandler("menu", _on_menu))
    app.add_handler(CommandHandler("card", _on_card))
    app.add_handler(CommandHandler("mode", _on_mode))
    app.add_handler(CommandHandler("pause", _on_pause))
    app.add_handler(CommandHandler("resume", _on_resume))
    app.add_handler(CommandHandler("help", _on_help))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

    _load_tracking()  # восстановить трекинг отправленного после рестарта
    return app


def run_polling() -> None:
    """Блокирующий запуск polling. Используется при `python -m telegram_bot`."""
    app = build_application()
    logger.info("Telegram bot polling запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_polling_in_thread() -> Optional[Any]:
    """Запустить Telegram polling в фоновом потоке (свой event loop).

    Если token не задан — возвращает None и пишет warning. Иначе возвращает
    daemon-Thread (умрёт вместе с основным процессом).
    Используется из main.py при старте сервиса — не блокирует scheduler/uvicorn.
    """
    import threading

    if not config.telegram_bot_token():
        logger.warning("Telegram polling не запущен: bot token не задан в настройках")
        return None

    def _runner() -> None:
        try:
            app = build_application()
            logger.info("Telegram polling: старт в потоке")
            # stop_signals=None — в не-main thread сигнал-хэндлеры PTB не работают
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=None,
                close_loop=False,
                # bootstrap_retries=-1 — бесконечно ретраить bootstrap (get_me)
                # при старте, пока сеть/DNS не поднимутся. Без этого DNS-гонка
                # на ребуте роняла поток polling навсегда (Failed run 0 of 0).
                bootstrap_retries=-1,
            )
        except Exception:
            logger.exception("Telegram polling упал")

    t = threading.Thread(target=_runner, daemon=True, name="tg-polling")
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_polling()
