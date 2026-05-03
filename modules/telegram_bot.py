# Telegram-бот: уведомления оператору о новых сообщениях с inline-кнопками.
# Архитектура:
#   • Отправка уведомлений (sync) — через HTTP API напрямую (urllib stdlib).
#     Вызывается из scheduler-а в любом потоке без asyncio.
#   • Приём callback-ов и редактирование (async) — python-telegram-bot Application
#     с long polling. Запускается отдельным процессом или в отдельном потоке.

import asyncio
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
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
from modules import ad_brief, claude, parser

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


# ============================================================
# HTTP API (sync) — для отправки уведомлений из scheduler-а
# ============================================================

def _http_post_single(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Сырой POST к Telegram Bot API. Используется внутренне."""
    token = config.telegram_bot_token()
    if not token:
        raise RuntimeError("Не задан Telegram bot token в настройках")
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


def _format_client_history(buyer_email: str) -> str:
    """Полная история переписки с клиентом — все треды по всем объявлениям.

    Используется callback-action `clienthist:N` — присылает оператору в чат
    краткую сводку всех взаимодействий с конкретным buyer-email.
    """
    if not buyer_email:
        return "❓ Нет email клиента"
    threads = db.list_threads_for_client(buyer_email)
    if not threads:
        return f"❓ Нет тредов для <code>{_html(buyer_email)}</code>"

    # Сводка по клиенту
    with db.get_conn() as conn:
        info = conn.execute(
            """
            SELECT
                (SELECT buyer_display_name FROM messages
                 WHERE buyer_name = ? AND buyer_display_name IS NOT NULL
                 ORDER BY id DESC LIMIT 1) AS name,
                COUNT(*) AS total,
                MIN(created_at) AS first,
                MAX(created_at) AS last
            FROM messages WHERE buyer_name = ?
            """,
            (buyer_email, buyer_email),
        ).fetchone()

    name = (info["name"] if info else "") or buyer_email.split("@")[0]
    lines: list[str] = []
    lines.append(f"<b>📋 История клиента: {_html(name)}</b>")
    lines.append(f"<code>{_html(buyer_email[:80])}</code>")
    lines.append(
        f"💬 {info['total']} сообщ. в {len(threads)} тред(ах) · "
        f"🕒 {info['first'][:10]} → {info['last'][:10]}"
    )
    lines.append("")

    for i, t in enumerate(threads, 1):
        msgs = list(db.thread_history(t["thread_id"]))
        ad_title = (t["ad_title"] or "— без объявления —")[:60]
        lines.append(
            f"<b>━ Тред {i}/{len(threads)}: 📦 {_html(ad_title)} [{t['last_status']}] ━</b>"
        )
        # Если есть Claude-summary в последнем сообщении треда — показываем
        last_with_summary = next(
            (m for m in reversed(msgs) if _safe_get(m, "history_summary_ru")),
            None,
        )
        if last_with_summary:
            sm = _safe_get(last_with_summary, "history_summary_ru")
            if sm:
                lines.append(f"<i>🤖 {_html(sm)}</i>")
        # Последние 4 turn-а компактно (RU only с fallback на оригинал)
        recent = msgs[-4:]
        if len(msgs) > len(recent):
            lines.append(f"<i>(показано последних {len(recent)} из {len(msgs)})</i>")
        for m in recent:
            ts = (m["created_at"] or "")[:16].replace("T", " ")
            if m["de_client"]:
                client_text = m["ru_client"] or m["de_client"] or ""
                lines.append(f"● {ts}: {_html(_truncate(client_text, 180))}")
            if m["de_answer"] and m["status"] in ("sent", "sent_debug", "edited", "approved"):
                is_ack = _safe_get(m, "is_auto_ack")
                label = "🤖 Auto-ack" if is_ack else "Мы"
                our_text = m["ru_answer"] or m["de_answer"] or ""
                lines.append(f"○ {label} [{m['status']}]: {_html(_truncate(our_text, 180))}")
        lines.append("")

    return "\n".join(lines)


def _format_thread_history(thread_id: str, current_msg: sqlite3.Row, limit: int = 3) -> str:
    """Собрать блок «История» — Claude-summary + последние N turn-ов на русском.

    current_msg — строка БД для msg-под-обработкой; из неё берём кэшированный
    history_summary_ru. quotes показываем на русском (ru_client / ru_answer),
    с fallback на оригинал если перевода нет. Лимит длины — Telegram 4096 chars.
    """
    if not thread_id:
        return ""
    rows = db.thread_history(thread_id)
    prior = [r for r in rows if r["id"] != current_msg["id"]]
    if not prior:
        return ""

    lines: list[str] = []
    total = len(prior)
    lines.append(f"<b>━━━ История ({total}) ━━━</b>")

    # Claude-summary (если был сгенерирован при поступлении)
    summary = _safe_get(current_msg, "history_summary_ru")
    if summary:
        lines.append(f"<i>🤖 {_html(summary)}</i>")
        lines.append("")

    if total > limit:
        lines.append(f"<i>(последние {limit} реплики)</i>")

    for r in prior[-limit:]:
        ts = (r["created_at"] or "")[:16].replace("T", " ")
        # Клиент: ru_client → fallback на de_client
        if r["de_client"]:
            buyer = _safe_get(r, "buyer_display_name") or r["buyer_name"] or "?"
            buyer_short = (buyer.split("@")[0] if "@" in buyer else buyer)[:30]
            client_text = r["ru_client"] or r["de_client"] or ""
            lines.append(
                f"● <b>{ts} {_html(buyer_short)}:</b> {_html(_truncate(client_text, 250))}"
            )
        # Наш ответ — только если был отправлен/редактирован
        if r["de_answer"] and r["status"] in ("sent", "sent_debug", "edited", "approved"):
            is_ack = _safe_get(r, "is_auto_ack")
            label = "🤖 Auto-ack" if is_ack else "Мы"
            our_text = r["ru_answer"] or r["de_answer"] or ""
            lines.append(
                f"○ <b>{label} [{r['status']}]:</b> {_html(_truncate(our_text, 250))}"
            )
    lines.append("")
    return "\n".join(lines)


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
    """Warning-блок если клиент засветился ещё где-то.

    Два уровня:
      1. ТОЧНОЕ совпадение по buyer_display_name (он же по имени в других тредах)
      2. ПОДОЗРЕНИЕ по стилю — Haiku-детект из similar_buyers_json (другие имена,
         но style suspiciously похож)

    Оба уровня в одном жёлто-красном блоке. Пусто если ничего не нашли.
    """
    display = _safe_get(msg, "buyer_display_name")
    related: list = []
    if display and display.strip():
        related = db.find_related_inquiries(
            display_name=display,
            exclude_thread_id=msg["gmail_thread_id"] or None,
            limit=8,
        )

    # Style-similarity matches
    sim_matches: list = []
    sim_raw = _safe_get(msg, "similar_buyers_json")
    if sim_raw:
        try:
            sim_matches = json.loads(sim_raw) or []
        except Exception:
            sim_matches = []

    if not related and not sim_matches:
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

    if sim_matches:
        if related:
            lines.append("")  # разделитель внутри блока
        lines.append(
            f"⚠️ <b>ПОХОЖИЙ СТИЛЬ ОБЩЕНИЯ</b> "
            f"({len(sim_matches)} подозрител{'ьное' if len(sim_matches) == 1 else 'ьных'})"
        )
        lines.append("<i>Возможно тот же клиент под другим именем / с другого аккаунта</i>")
        for m in sim_matches[:5]:
            cid = m.get("candidate_msg_id")
            score = m.get("suspicion_score", 0)
            reason = (m.get("reason") or "")[:80]
            # Дополнительно подгрузим имя кандидата
            cmsg = db.get_message(cid) if cid else None
            cname = ""
            if cmsg:
                cname = (cmsg["buyer_display_name"] or cmsg["buyer_name"] or "?").split("@")[0][:20]
            lines.append(
                f"  • <code>#{cid}</code> «{_html(cname)}» · подозрение {score}/10 · "
                f"<i>{_html(reason)}</i>"
            )

    lines.append("🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨")
    return "\n".join(lines)


def _format_review_text(msg: sqlite3.Row) -> str:
    """Собрать форматированное сообщение для оператора (HTML)."""
    # Шапка: режим + msg_id (для отладки/поиска карточки) + status
    header = f"{_mode_badge()} · <code>#{msg['id']}</code>"
    status = msg["status"] or ""
    if status and status not in ("new", "pending"):
        header += f" · <code>{_html(status)}</code>"
    lines: list[str] = [header]
    # Если для треда активен автопилот — большой жёлтый блок наверху
    ap = db.get_thread_autopilot(msg["gmail_thread_id"] or "")
    if ap and ap["active"]:
        lines.append(
            "🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨\n"
            f"🤖 <b>АВТОПИЛОТ АКТИВЕН ({ap['messages_sent']}/20)</b>\n"
            f"floor: <code>{ap['floor_price_eur']}€</code> · "
            f"режим: {'🔔 notify' if ap['notify_mode'] == 'notify' else '🤫 silent'}\n"
            f"<i>запущен {_html(ap['started_by'] or '?')} в {(ap['started_at'] or '')[11:19]}</i>\n"
            "🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨"
        )
    # Warning если этот клиент уже пишет в нескольких тредах
    related_warn = _format_related_warning(msg)
    if related_warn:
        lines.append(related_warn)
    title = msg["ad_title"] or _safe_get(msg, "email_subject") or ""
    if title:
        lines.append(f"<b>📦 {_html(title)}</b>")
    if msg["ad_price"]:
        lines.append(f"💰 {_html(msg['ad_price'])}")

    # Предупреждение если объявление снято с публикации или зарезервировано
    ad_state = parser.detect_ad_state(msg["ad_title"])
    if ad_state == "deleted":
        lines.append("⚠️ <b>ОБЪЯВЛЕНИЕ УДАЛЕНО</b> — товар не продаётся, но клиент пишет. Учти при ответе.")
    elif ad_state == "reserved":
        lines.append("🔒 <b>ОБЪЯВЛЕНИЕ ЗАРЕЗЕРВИРОВАНО</b> — уже договорились с другим, но клиент пишет.")

    # Наш аккаунт: имя продавца с объявы + email Gmail-аккаунта (для контекста кто кому отвечает)
    if msg["seller_name"] or msg["account_id"]:
        acc = db.get_account(msg["account_id"]) if msg["account_id"] else None
        gmail_email = acc["gmail_email"] if acc else ""
        seller_label = msg["seller_name"] or (acc["name"] if acc else "")
        if seller_label and gmail_email:
            lines.append(f"🏪 Наш: {_html(seller_label)} ({_html(gmail_email)})")
        elif seller_label:
            lines.append(f"🏪 Наш: {_html(seller_label)}")
        elif gmail_email:
            lines.append(f"🏪 Наш: {_html(gmail_email)}")
    # Имя покупателя — приоритет display name из email, fallback на email
    buyer_disp = _safe_get(msg, "buyer_display_name")
    buyer_email = msg["buyer_name"] or ""
    if buyer_disp:
        lines.append(f"👤 Клиент: {_html(buyer_disp)}")
    elif buyer_email:
        lines.append(f"👤 Клиент: {_html(buyer_email)}")
    ad_url = msg["ad_url"]
    ad_id = _safe_get(msg, "ad_id")
    if ad_url:
        link_text = f"Объявление #{ad_id}" if ad_id else "Объявление"
        lines.append(f'<a href="{_html(ad_url)}">{link_text}</a>')
    elif ad_id:
        lines.append(f"🆔 ID объявления: <code>{_html(ad_id)}</code>")
    lines.append("")

    # Язык клиента и язык ответа могут отличаться (если оператор сказал «переведи на X»)
    client_code = _safe_get(msg, "client_lang") or "de"
    answer_code = _safe_get(msg, "answer_lang") or client_code
    c_name, c_flag = claude.lang_display(client_code)
    a_name, a_flag = claude.lang_display(answer_code)

    # Бриф объявления (если есть) — между шапкой и сообщением клиента
    ad_id_val = _safe_get(msg, "ad_id")
    if ad_id_val:
        try:
            brief_row = db.get_ad_brief(ad_id_val)
        except Exception:
            brief_row = None
        if brief_row:
            try:
                key_facts = json.loads(brief_row["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                key_facts = {}
            brief_compact = ad_brief.format_brief_for_telegram(brief_row["brief_md"], key_facts)
            if brief_compact:
                lines.append("<b>📋 Бриф:</b>")
                lines.append(_html(brief_compact))
                lines.append("")

    # Лог операторских инструкций (💸 цена / 📝 своя инструкция)
    extra = _safe_get(msg, "extra_notes")
    if extra and extra.strip():
        lines.append("<b>📌 Дополнительные инструкции:</b>")
        for ln in extra.strip().split("\n"):
            ln = ln.strip()
            if ln:
                lines.append(f"• {_html(ln)}")
        lines.append("")

    # Прошлая переписка треда (если есть)
    history_block = _format_thread_history(msg["gmail_thread_id"] or "", msg)
    if history_block:
        lines.append(history_block)

    # ── Статус-aware шапка для секций ответа ──
    # Для sent/sent_debug — "✅ ОТПРАВЛЕНО (RU/DE) в HH:MM:SS"
    # Для edited — "✏️ Черновик отредактирован"
    # Для skipped — "❌ Пропущено"
    # Для error_* / not_sent_disabled — "❌/🔒 [status]"
    # Для new/pending/approved — "✍️ Черновик / 📤 К отправке" (как раньше)
    status = msg["status"] or "new"
    sent_at_str = ""
    if msg["sent_at"]:
        # ISO «2026-05-03T15:37:41.234567» → «15:37:41»
        sent_at_str = f" в {msg['sent_at'][11:19]}"

    if status == "sent":
        ans_ru_label = f"<b>✅ ОТПРАВЛЕНО (🇷🇺 RU){sent_at_str}:</b>"
        ans_xx_label = f"<b>✅ ОТПРАВЛЕНО ({a_flag} {_html(a_name)}){sent_at_str}:</b>"
        ans_use_blockquote = True
    elif status == "sent_debug":
        ans_ru_label = f"<b>🟡 ОТПРАВЛЕНО (debug, 🇷🇺 RU){sent_at_str}:</b>"
        ans_xx_label = f"<b>🟡 ОТПРАВЛЕНО (debug, {a_flag} {_html(a_name)}){sent_at_str}:</b>"
        ans_use_blockquote = True
    elif status == "edited":
        ans_ru_label = "<b>✏️ Черновик ответа (🇷🇺 RU) — отредактирован:</b>"
        ans_xx_label = f"<b>✏️ К отправке ({a_flag} {_html(a_name)}) — отредактирован:</b>"
        ans_use_blockquote = False
    elif status in ("skipped", "skipped_sold"):
        ans_ru_label = f"<b>❌ Пропущено (🇷🇺 RU) [{status}]:</b>"
        ans_xx_label = f"<b>❌ Пропущено ({a_flag} {_html(a_name)}) [{status}]:</b>"
        ans_use_blockquote = False
    elif status == "not_sent_disabled":
        ans_ru_label = "<b>🔒 Не отправлено (режим disabled, 🇷🇺 RU):</b>"
        ans_xx_label = f"<b>🔒 Не отправлено (режим disabled, {a_flag} {_html(a_name)}):</b>"
        ans_use_blockquote = False
    elif status.startswith("error_"):
        ans_ru_label = f"<b>❌ Ошибка [{status}] (🇷🇺 RU):</b>"
        ans_xx_label = f"<b>❌ Ошибка [{status}] ({a_flag} {_html(a_name)}):</b>"
        ans_use_blockquote = False
    else:  # new / pending / approved
        ans_ru_label = "<b>✍️ Черновик ответа (🇷🇺 RU):</b>"
        ans_xx_label = f"<b>📤 К отправке ({a_flag} {_html(a_name)}):</b>"
        ans_use_blockquote = False

    # ── Входящее сообщение клиента — всегда в blockquote (визуальный левый бордер) ──
    if msg["de_client"]:
        header_lbl = "Новое сообщение клиента" if history_block else "Клиент пишет"
        lines.append(f"<b>📥 {header_lbl} ({c_flag} {_html(c_name)}):</b>")
        lines.append(f"<blockquote>{_html(msg['de_client'])}</blockquote>")
        lines.append("")
    if msg["ru_client"] and client_code != "ru":
        lines.append("<b>🇷🇺 Перевод:</b>")
        lines.append(f"<blockquote>{_html(msg['ru_client'])}</blockquote>")
        lines.append("")

    # ── Наш ответ — blockquote только когда уже отправлено (визуально завершённый) ──
    def _fmt_body(text: str) -> str:
        return f"<blockquote>{_html(text)}</blockquote>" if ans_use_blockquote else _html(text)

    if msg["ru_answer"]:
        lines.append(ans_ru_label)
        lines.append(_fmt_body(msg["ru_answer"]))
        lines.append("")
    if msg["de_answer"]:
        lines.append(ans_xx_label)
        lines.append(_fmt_body(msg["de_answer"]))
        # Точный перевод того что уйдёт клиенту — для верификации оператором
        ru_trans = _safe_get(msg, "ru_translation")
        if ru_trans and ru_trans.strip() and (_safe_get(msg, "client_lang") or "") != "ru":
            lines.append("")
            lines.append("<b>🇷🇺 Перевод (что значит):</b>")
            lines.append(_fmt_body(ru_trans))

    # Стоимость генерации
    cost = _safe_get(msg, "cost_usd") or 0
    if cost:
        tin = _safe_get(msg, "tokens_in") or 0
        tout = _safe_get(msg, "tokens_out") or 0
        lines.append("")
        lines.append(f"<i>💸 ${cost:.4f} ({tin} in / {tout} out tokens)</i>")

    return "\n".join(lines)


def _review_keyboard(message_id: int) -> dict[str, Any]:
    """Inline-клавиатура: 7 рядов × 2 столбца, ✅ Отправить full-width в центре.

    Финальный layout (см. spec docs/superpowers/specs/2026-05-03-tg-rework-design.md):
        ✏️ Правка RU       │ ✏️ Правка DE
        💎 Без торга        │ 💸 Своя цена
        📝 Своя инструкция │ ❌ Пропустить
        ✅ ОТПРАВИТЬ (full-width)
        👊 Жёстче           │ ☺️ Мягче
        ✂️ Короче           │ 🔁 Переформулировать
        💰 Товар продан    │ 📋 История клиента
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Правка RU", "callback_data": f"editru:{message_id}"},
                {"text": "✏️ Правка DE", "callback_data": f"editde:{message_id}"},
            ],
            [
                {"text": "💎 Без торга", "callback_data": f"q:fest:{message_id}"},
                {"text": "💸 Своя цена", "callback_data": f"price:{message_id}"},
            ],
            [
                {"text": "📝 Своя инструкция", "callback_data": f"instr:{message_id}"},
                {"text": "❌ Пропустить", "callback_data": f"skip:{message_id}"},
            ],
            [
                {"text": "✅  О Т П Р А В И Т Ь  ✅", "callback_data": f"send:{message_id}"},
            ],
            [
                {"text": "👊 Жёстче", "callback_data": f"t:harsh:{message_id}"},
                {"text": "☺️ Мягче", "callback_data": f"t:friend:{message_id}"},
            ],
            [
                {"text": "✂️ Короче", "callback_data": f"t:short:{message_id}"},
                {"text": "🔁 Переформулировать", "callback_data": f"t:regen:{message_id}"},
            ],
            [
                {"text": "💰 Товар продан", "callback_data": f"sold:{message_id}"},
                {"text": "📋 История клиента", "callback_data": f"clienthist:{message_id}"},
            ],
            [
                {"text": "🚀 Полный автопилот", "callback_data": f"apstart:{message_id}"},
            ],
            [
                {"text": "↩ Назад к pipeline", "callback_data": f"back:{message_id}"},
            ],
        ]
    }


def _autopilot_active_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура карточки активного автопилота: только Stop + Назад."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Остановить автопилот", callback_data=f"apstop:{message_id}")],
        [InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{message_id}")],
    ])


def _autopilot_mode_choice_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """После ввода floor — выбор режима уведомлений."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Notify (start/inbound/stop)", callback_data=f"apconfirm:notify:{message_id}")],
        [InlineKeyboardButton("🤫 Silent (только при стопе)", callback_data=f"apconfirm:silent:{message_id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"inputcancel:{message_id}")],
    ])


def _review_keyboard_obj(message_id: int) -> InlineKeyboardMarkup:
    """Тот же набор что _review_keyboard, но как InlineKeyboardMarkup для PTB-async."""
    rows = _review_keyboard(message_id)["inline_keyboard"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
        for row in rows
    ])


def _input_cancel_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Одна кнопка «Отменить» — для режима ожидания текста (правка/цена/инструкция)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отменить", callback_data=f"inputcancel:{message_id}")
    ]])


def _confirmation_keyboard(original_callback: str, message_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения: ▶️ Продолжить (выполняет оригинальный callback) | ❌ Отменить."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  П Р О Д О Л Ж И Т Ь", callback_data=f"confirm:{original_callback}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel:{message_id}")],
    ])


# ============================================================
# Confirmation explanations
# ============================================================

# Действия, которые требуют подтверждения перед выполнением.
# Ключи — либо `action` (для 2-частных callback типа send/skip),
# либо `action:sub` (для 3-частных типа q:fest, t:harsh).
NEEDS_CONFIRM: set[str] = {
    "send", "skip", "sold", "clienthist",
    "price", "instr",  # эти открывают input-режим — но всё равно через confirm
    "q:fest",
    "t:harsh", "t:friend", "t:short", "t:regen",
}

# Объяснения для confirm-карточки. Ключ совпадает с NEEDS_CONFIRM.
ACTION_EXPLANATIONS: dict[str, str] = {
    "send": "<b>✅ Отправить</b> — отправит черновик клиенту через SMTP. Финальное действие, отменить нельзя.",
    "skip": "<b>❌ Пропустить</b> — пометит сообщение <code>skipped</code>, клиент <b>НЕ</b> получит ответ.",
    "sold": "<b>💰 Товар продан</b> — пометит объявление как ПРОДАНО. Будущие inquiries по нему авто-skip.",
    "clienthist": "<b>📋 История клиента</b> — покажет полную переписку с этим клиентом по всем тредам.",
    "price": "<b>💸 Своя цена</b> — введёшь конкретную цену числом, Claude перепишет драфт под неё. ~$0.005",
    "instr": "<b>📝 Своя инструкция</b> — введёшь свободный промпт, Claude перепишет по нему. ~$0.005-0.01",
    "q:fest": "<b>💎 Без торга</b> — Claude перепишет драфт как вежливый твёрдый отказ от торга. ~$0.005",
    "t:harsh": "<b>👊 Жёстче</b> — Claude перепишет тон жёстче (твёрдо, без сюсюканий). ~$0.005",
    "t:friend": "<b>☺️ Мягче</b> — Claude перепишет тон мягче (теплее, человечнее). ~$0.005",
    "t:short": "<b>✂️ Короче</b> — Claude сократит ответ в 2 раза. ~$0.005",
    "t:regen": "<b>🔁 Переформулировать</b> — Claude напишет тот же смысл другими словами. ~$0.005",
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

# In-memory лок треда: msg_id → (actor_str, acquired_datetime).
# 5 мин таймаут. Lock acquire-ится на любое actionable-нажатие; release при
# завершении действия. Если оператор начал input-режим и забыл — lock авто-освободится.
_THREAD_LOCKS: dict[int, tuple[str, datetime]] = {}
LOCK_TIMEOUT_SEC = 300  # 5 минут


def _check_lock(msg_id: int, actor: str) -> Optional[str]:
    """Возвращает имя текущего владельца если лок занят ДРУГИМ. None если свободно/мой."""
    e = _THREAD_LOCKS.get(msg_id)
    if not e:
        return None
    owner, acquired = e
    if owner == actor:
        return None
    age = (datetime.utcnow() - acquired).total_seconds()
    if age < LOCK_TIMEOUT_SEC:
        return owner
    # Auto-expire
    del _THREAD_LOCKS[msg_id]
    return None


def _acquire_lock(msg_id: int, actor: str) -> None:
    _THREAD_LOCKS[msg_id] = (actor, datetime.utcnow())


def _release_lock(msg_id: int) -> None:
    _THREAD_LOCKS.pop(msg_id, None)


def _lock_remaining_min(msg_id: int) -> int:
    """Минут до auto-release. 0 если нет лока или истёк."""
    e = _THREAD_LOCKS.get(msg_id)
    if not e:
        return 0
    age = (datetime.utcnow() - e[1]).total_seconds()
    remaining = LOCK_TIMEOUT_SEC - age
    return max(0, int(remaining // 60) + 1)


async def _broadcast_card(context: Any, msg_id: int, text: str, reply_markup: Any = None) -> None:
    """Edit ВСЕ копии карточки msg_id (по таблице card_dispatches).

    Используется для broadcast-обновлений в DM-mode (а в group-mode — просто
    одна row в card_dispatches, та же логика).
    """
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


async def _safe_edit(query, text: str, reply_markup=None) -> None:
    """Edit текущей карточки. Молча игнорим 'Message is not modified' (если контент тот же)."""
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


async def _bot_edit(context, chat_id: int, tg_msg_id: int, text: str, reply_markup=None) -> None:
    """Edit карточки по chat_id+message_id (когда нет query — например после _on_text)."""
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=tg_msg_id,
            text=text, parse_mode="HTML",
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
        lines.append(f"🕒 Последнее наше сообщение: {sent_at[:19]}")
    if msg["de_answer"]:
        excerpt = msg["de_answer"][:300]
        if len(msg["de_answer"]) > 300:
            excerpt += "…"
        lines.append("")
        lines.append("📤 <i>Что мы писали:</i>")
        lines.append(_html(excerpt))
    return "\n".join(lines)


def _reminder_keyboard(message_id: int) -> dict[str, Any]:
    """Кнопки на карточке-предложении пинга: ping/skip + snooze."""
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
        "inline_keyboard": [[
            {"text": "📋 Открыть карточку треда", "callback_data": f"pipe:{msg_id}"},
        ]]
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

    text = _format_review_text(msg)
    # Если автопилот активен для треда — урезанная клавиатура (только Stop + Back)
    ap = db.get_thread_autopilot(msg["gmail_thread_id"] or "")
    if ap and ap["active"]:
        kb_obj = _autopilot_active_keyboard(message_id)
        # Конвертируем в dict для _http_post (sync API)
        kb = {"inline_keyboard": [
            [{"text": btn.text, "callback_data": btn.callback_data} for btn in row]
            for row in kb_obj.inline_keyboard
        ]}
    else:
        kb = _review_keyboard(message_id)
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
        update_fields: dict[str, Any] = {"telegram_message_id": first_tg_msg_id}
        if msg["status"] == "new":
            update_fields["status"] = "pending"
        db.update_message(message_id, **update_fields)
    return first_tg_msg_id


# ============================================================
# Polling (async) — обработка кнопок и режима редактирования
# ============================================================

# In-memory tracker для batch-удаления при следующем /pipeline.
# Ключ: chat_id (int), значение: set message_id-ов которые нужно удалить.
# Заполняется при каждом отправлении бот-сообщения и при получении операторского текста.
# Очищается полностью при /pipeline вызове.
_CHAT_TRACKED_MSGS: dict[int, set[int]] = {}


def _track_msg(chat_id: Any, msg_id: Optional[int]) -> None:
    """Запомнить message_id чтобы удалить при следующем /pipeline вызове."""
    if not chat_id or not msg_id:
        return
    try:
        _CHAT_TRACKED_MSGS.setdefault(int(chat_id), set()).add(int(msg_id))
    except (TypeError, ValueError):
        pass


async def _delete_all_tracked(context: Any, chat_id: Any) -> int:
    """Удалить все запомненные сообщения для chat_id. Возвращает кол-во удалённых."""
    msgs = _CHAT_TRACKED_MSGS.pop(int(chat_id), set())
    deleted = 0
    for mid in sorted(msgs, reverse=True):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            pass
    return deleted


async def _delete_range(context: Any, chat_id: Any, max_msg_id: int, scan: int = 50) -> int:
    """Sweep: попытаться удалить msg_id в диапазоне [max_id-scan .. max_id] параллельно.

    Используется `deleteMessages` (batch до 100 за вызов, Bot API 7.4+) если доступно,
    иначе fallback на параллельные delete_message через asyncio.gather.

    scan=50 — лимит чтобы не зацепить «запустить бота» system-сообщение в начале чата.
    """
    ids = list(range(max(1, max_msg_id - scan + 1), max_msg_id + 1))
    if not ids:
        return 0
    # Попытка batch-API
    try:
        await context.bot.delete_messages(chat_id=chat_id, message_ids=ids)
        return len(ids)
    except Exception:
        pass  # fallback к параллельным single-deletes
    results = await asyncio.gather(
        *[
            context.bot.delete_message(chat_id=chat_id, message_id=mid)
            for mid in ids
        ],
        return_exceptions=True,
    )
    return sum(1 for r in results if not isinstance(r, Exception))


# Состояние «оператор сейчас в режиме ввода»:
# (chat_id, user_id) -> {"action": "edit_ru"|"edit_de"|"price"|"instruction",
#                        "msg_id": int (БД),
#                        "tg_msg_id": int (карточка),
#                        "stub_msg_id": Optional[int] (force_reply сообщение, для cleanup)}
# Per-user, чтобы в группе сообщения одного оператора не перехватили input-сессию другого.
_PENDING_INPUTS: dict[tuple[int, int], dict[str, Any]] = {}


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


def _msg_link(message_id: int, tg_msg_id: Optional[int]) -> str:
    """Кликабельный <code>#N</code> с deep-link на оригинальную карточку (если есть)."""
    label = f"#{message_id}"
    if not tg_msg_id:
        return f"<code>{label}</code>"
    internal = _supergroup_internal_id(config.telegram_chat_id())
    if not internal:
        return f"<code>{label}</code>"  # для DM deep-link не работает
    url = f"https://t.me/c/{internal}/{tg_msg_id}"
    return f'<a href="{url}"><code>{label}</code></a>'


def _format_pipeline_messages() -> list[tuple[str, Optional[InlineKeyboardMarkup]]]:
    """Список (text, kb) пар для пайплайна. 1 элемент = 1 Telegram-сообщение.

    Структура:
      [0] header — общая шапка с подсчётами
      [1..N] карточка треда — разделитель + 3 строки брифа + 1 кнопка со временем

    Каждый тред = отдельное сообщение. Кнопка `pipe:<msg_id>` ведёт на thread-detail.
    """
    rows = db.pipeline_threads()
    waiting_us = [r for r in rows if not _safe_get(r, "has_any_sent")]
    waiting_client = [r for r in rows if _safe_get(r, "has_any_sent")]

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
    for section_rows, marker in ((waiting_us, "🔴"), (waiting_client, "🟢")):
        for r in section_rows:
            n += 1
            out.append(_pipeline_thread_card(r, n, marker))

    return out


def _pipeline_thread_card(r: sqlite3.Row, n: int, marker: str) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка одного треда: разделитель + 3 строки брифа в тексте + 1 inline-кнопка со временем.

    Каждая строка ≤ 65 символов. Кнопка `pipe:<msg_id>` ведёт на thread-detail.
    """
    has_real = bool(_safe_get(r, "has_real_reply"))
    has_any_sent = bool(_safe_get(r, "has_any_sent"))
    has_draft = bool(_safe_get(r, "has_pending_draft"))
    has_ack_only = has_any_sent and not has_real

    # Автопилот active → перебивает обычный status
    ap = db.get_thread_autopilot(r["gmail_thread_id"] or "")
    is_ap_active = bool(ap and ap["active"])

    if is_ap_active:
        status_txt = f"🤖 автопилот {ap['messages_sent']}/20"
    elif has_real:
        status_txt = "ответили"
    elif has_ack_only and has_draft:
        status_txt = "ack+draft"
    elif has_ack_only:
        status_txt = "ack"
    elif has_draft:
        status_txt = "draft"
    else:
        status_txt = "новый"

    draft_marker = "📝" if has_draft and not is_ap_active else ""
    buyer_raw = _safe_get(r, "buyer_display_name") or r["buyer_name"] or "?"
    buyer = (buyer_raw.split("@")[0] if "@" in buyer_raw else buyer_raw)[:25]
    ad = _short_ad_label(r)
    price = _short_price(r["ad_price"])

    ts_iso = (
        _safe_get(r, "sent_at") if r["status"] in ("sent", "sent_debug")
        else r["created_at"]
    ) or r["created_at"]
    hhmm = (ts_iso or "")[11:16]
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
        InlineKeyboardButton(button_label, callback_data=f"pipe:{r['id']}")
    ]])
    return text, kb


def _pipeline_button_lines(r: sqlite3.Row, n: int, marker: str) -> tuple[str, str, str]:
    """Три строки для тройки stacked-кнопок одного треда.

    Returns:
        (line1, line2, line3):
            line1 = «N marker[📝] · CLIENT · STATUS»     — шапка
            line2 = «TITLE · PRICE · AGE»                 — деталь товара
            line3 = «💎 condition · мин N€»  ИЛИ          — sales-контекст из брифа,
                    «← ‘последняя реплика клиента…’»     ИЛИ превью последнего сообщения

    Каждая ≤ 64 символа (Telegram cap).

    STATUS: ответили / ack+draft / ack / draft / новый
    """
    BUDGET = 64

    ts_iso = (
        _safe_get(r, "sent_at") if r["status"] in ("sent", "sent_debug")
        else r["created_at"]
    ) or r["created_at"]
    age = _humanize_age(ts_iso)

    has_real = bool(_safe_get(r, "has_real_reply"))
    has_any_sent = bool(_safe_get(r, "has_any_sent"))
    has_draft = bool(_safe_get(r, "has_pending_draft"))
    has_ack_only = has_any_sent and not has_real

    if has_real:
        status_txt = "ответили"
    elif has_ack_only and has_draft:
        status_txt = "ack+draft"
    elif has_ack_only:
        status_txt = "ack"
    elif has_draft:
        status_txt = "draft"
    else:
        status_txt = "новый"

    draft_marker = "📝" if has_draft else ""
    buyer_raw = _safe_get(r, "buyer_display_name") or r["buyer_name"] or "?"
    buyer = (buyer_raw.split("@")[0] if "@" in buyer_raw else buyer_raw)[:18]
    ad_full = _short_ad_label(r)
    price = _short_price(r["ad_price"])

    # line1
    line1 = f"{n} {marker}{draft_marker} · {buyer} · {status_txt}"
    if len(line1) > BUDGET:
        line1 = line1[:BUDGET - 1] + "…"

    # line2: TITLE · PRICE · AGE
    fixed_parts = []
    if price:
        fixed_parts.append(price)
    fixed_parts.append(age)
    fixed_tail = " · ".join(fixed_parts)
    title_budget = BUDGET - len(fixed_tail) - len(" · ")
    if title_budget <= 3:
        line2 = fixed_tail
    else:
        title = ad_full
        if len(title) > title_budget:
            title = title[:title_budget - 1].rstrip() + "…"
        line2 = f"{title} · {fixed_tail}"

    # line3: бриф или превью клиента
    line3 = _pipeline_line3_context(r)
    if len(line3) > BUDGET:
        line3 = line3[:BUDGET - 1].rstrip() + "…"

    return line1, line2, line3


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
    """Полная карточка треда: вся переписка (на всех языках) + meta."""
    thread_id = msg["gmail_thread_id"] or ""
    rows = list(db.thread_history(thread_id)) if thread_id else [msg]

    # Header
    title = msg["ad_title"] or _safe_get(msg, "email_subject") or "—"
    buyer_disp = _safe_get(msg, "buyer_display_name") or msg["buyer_name"] or "?"
    last_ts = max((r["created_at"] for r in rows), default=msg["created_at"])
    age = _humanize_age(last_ts)
    lines: list[str] = [
        f"<b>📋 Тред #{msg['id']}</b>",
        f"<b>📦 {_html(title)}</b>",
    ]
    if msg["ad_price"]:
        lines.append(f"💰 {_html(msg['ad_price'])}")
    lines.append(f"👤 Клиент: <b>{_html(buyer_disp)}</b>")
    if msg["buyer_name"] and msg["buyer_name"] != buyer_disp:
        lines.append(f"<code>{_html(msg['buyer_name'])}</code>")
    if msg["ad_url"]:
        ad_id_val = _safe_get(msg, "ad_id")
        link_text = f"Объявление #{ad_id_val}" if ad_id_val else "Объявление"
        lines.append(f'<a href="{_html(msg["ad_url"])}">{link_text}</a>')
    lines.append(f"⏱ Последнее событие: {age} назад ({last_ts[:16].replace('T', ' ')})")
    # Warning если клиент пишет в нескольких тредах
    related_warn = _format_related_warning(msg)
    if related_warn:
        lines.append("")
        lines.append(related_warn)
    lines.append("")
    lines.append("─────────────────")

    # Все сообщения хронологически: входящее + наш ответ (если есть).
    # Auto-ack rows (is_auto_ack=1) ИСКЛЮЧАЕМ из chat-вью — это накрутка метрики,
    # не часть реального диалога. Покажем компактным footer-ом под incoming.
    auto_ack_count = 0
    for r in rows:
        if _safe_get(r, "is_auto_ack"):
            auto_ack_count += 1
            continue  # пропускаем full-bubble, посчитаем для footer
        ts = (r["created_at"] or "")[11:16]
        # Входящее
        if r["de_client"]:
            cl = _safe_get(r, "client_lang") or "?"
            cflag, _ = claude.lang_display(cl), None
            cflag_str = cflag[1] if isinstance(cflag, tuple) else "🌐"
            sender = (_safe_get(r, "buyer_display_name") or r["buyer_name"] or "?").split("@")[0][:25]
            lines.append(f"<b>📥 {_html(sender)} · {ts} · {cflag_str}:</b>")
            lines.append(f"<blockquote>{_html(r['de_client'])}</blockquote>")
            if r["ru_client"] and (_safe_get(r, "client_lang") or "") != "ru":
                lines.append(f"<i>🇷🇺 {_html(r['ru_client'])}</i>")
            lines.append("")
        # Исходящее в той же row (наш ответ — может быть draft или sent)
        if r["de_answer"] and r["status"] in ("sent", "sent_debug", "edited", "approved", "pending", "new"):
            ping_marker = " 🔔" if _safe_get(r, "is_reminder") else ""
            status_marker = ""
            if r["status"] == "sent":
                status_marker = " ✅"
            elif r["status"] == "sent_debug":
                status_marker = " 🟡"
            elif r["status"] in ("pending", "new", "approved"):
                status_marker = " ⏳ ДРАФТ (ждёт отправки)"
            elif r["status"] == "edited":
                status_marker = " ✏️ ДРАФТ (отредактирован, ждёт отправки)"
            al = _safe_get(r, "answer_lang") or _safe_get(r, "client_lang") or "de"
            aflag = claude.lang_display(al)[1]
            lines.append(f"<b>📤 Мы · {ts}{ping_marker}{status_marker} · {aflag}:</b>")
            lines.append(f"<blockquote>{_html(r['de_answer'])}</blockquote>")
            if r["ru_answer"]:
                lines.append(f"<i>🇷🇺 {_html(r['ru_answer'])}</i>")
            lines.append("")

    if auto_ack_count:
        lines.append(
            f"<i>🤖 Скрыт {auto_ack_count} авто-ack для метрики (не реальная реплика)</i>"
        )
    lines.append("─────────────────")
    return "\n".join(lines)


def _thread_detail_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Клавиатура thread-detail карточки: Compose + ещё опции + Назад."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Написать клиенту сообщение", callback_data=f"compose:{msg_id}")],
        [InlineKeyboardButton("📋 Открыть карточку ревью", callback_data=f"opencard:{msg_id}")],
        [InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{msg_id}")],
    ])


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
        except Exception:
            logger.exception("Не удалось послать pipeline-сообщение")

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
        deleted_swept = await _delete_range(context, chat_id, trigger_msg_id, scan=50)
    logger.info(
        "/pipeline: удалено %d tracked + %d sweep в chat %s (pipeline kept: %d)",
        deleted_tracked, deleted_swept, chat_id, len(new_pipeline_ids),
    )


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


async def _enter_input_mode(
    query, context, update,
    *, action: str, msg, prompt_label: str,
    draft_text: Optional[str] = None,
    placeholder: str = "",
) -> None:
    """Войти в input-режим: правка RU/DE / своя цена / своя инструкция.

    A (нажавший) видит prompt + pre-блок + Cancel.
    Other operators (если DM-fanout) видят «🔒 @A работает» без кнопок.
    """
    chat_id = query.message.chat_id
    user_id = update.effective_user.id if update.effective_user else 0
    tg_msg_id = query.message.message_id
    actor = _actor_name(update)

    # Карточка для A — только prompt + Cancel (БЕЗ pre-блока, он будет отдельным сообщением)
    addendum_lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{prompt_label}",
        f"От: <b>{_html(actor)}</b>",
    ]
    if draft_text:
        addendum_lines.append("<i>↓ Текущий драфт пришлю отдельным сообщением — long-press для копирования.</i>")

    new_text_actor = _format_review_text(msg) + "\n".join(addendum_lines)
    await _safe_edit(query, new_text_actor, reply_markup=_input_cancel_keyboard(msg["id"]))

    # Broadcast LOCKED-state к остальным dispatches (не A): жёлтый-на-красном
    locked_text = _format_review_text(msg) + (
        "\n\n🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨\n"
        f"⚠️ <b>КАРТОЧКА В РАБОТЕ У {_html(actor).upper()}</b> ⚠️\n"
        f"<i>Input-режим · до 5 мин · кнопки скрыты для тебя</i>\n"
        "🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨"
    )
    for d in db.list_card_dispatches(msg["id"]):
        if str(d["chat_id"]) == str(chat_id) and d["tg_msg_id"] == tg_msg_id:
            continue  # skip A's copy — она уже обновлена выше
        try:
            await context.bot.edit_message_text(
                chat_id=d["chat_id"],
                message_id=d["tg_msg_id"],
                text=locked_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logger.warning("lock-broadcast edit fail: chat=%s err=%s", d["chat_id"], e)
        except Exception:
            logger.exception("lock-broadcast: ошибка edit")

    # Stub-сообщение с pre-блоком (для draft_text) или коротким prompt (для price/instr).
    # Pre-блок в отдельном сообщении легче long-press-копировать (не нужно скроллить
    # через всю карточку).
    if draft_text:
        stub_text = (
            f"<pre>{_html(draft_text)}</pre>\n\n"
            "↑ Long-press → Скопировать → отправь свою версию реплаем"
        )
    else:
        stub_text = "↑ Введи значение и отправь реплаем"

    stub_msg_id: Optional[int] = None
    try:
        stub = await context.bot.send_message(
            chat_id=chat_id,
            text=stub_text,
            parse_mode="HTML",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=(placeholder or "Твой ввод..."),
            ),
            reply_to_message_id=tg_msg_id,
        )
        if stub:
            stub_msg_id = stub.message_id
            _track_msg(chat_id, stub_msg_id)
    except Exception:
        logger.exception("Не удалось послать stub-сообщение")

    _PENDING_INPUTS[(chat_id, user_id)] = {
        "action": action,
        "msg_id": msg["id"],
        "tg_msg_id": tg_msg_id,
        "stub_msg_id": stub_msg_id,
    }


async def _exit_input_mode(query, context, msg_id: int, *, restore_card: bool = True) -> None:
    """Выйти из input-режима: удалить stub force_reply сообщение, восстановить карточку."""
    chat_id = query.message.chat_id
    user_id = query.from_user.id if query.from_user else 0
    pending = _PENDING_INPUTS.pop((chat_id, user_id), None)
    if pending and pending.get("stub_msg_id"):
        try:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=pending["stub_msg_id"],
            )
        except Exception:
            pass
    for k in list(_PENDING_INPUTS.keys()):
        if _PENDING_INPUTS[k]["msg_id"] == msg_id:
            _PENDING_INPUTS.pop(k, None)
    # Освобождаем лок треда — input завершён/отменён
    _release_lock(msg_id)
    if restore_card:
        msg = db.get_message(msg_id)
        if msg:
            await _broadcast_card(
                context, msg_id, _format_review_text(msg),
                reply_markup=_review_keyboard_obj(msg_id),
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
        confirm_text = _format_review_text(msg) + _confirmation_addendum(action_key)
        await _safe_edit(
            query, confirm_text,
            reply_markup=_confirmation_keyboard(payload, msg_id),
        )
        # Broadcast «soft lock» к остальным dispatches — ярко
        locked_text = _format_review_text(msg) + (
            "\n\n🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨\n"
            f"⚠️ <b>{_html(actor).upper()} ПОДТВЕРЖДАЕТ ДЕЙСТВИЕ</b> ⚠️\n"
            f"<i>Подожди завершения или отмены</i>\n"
            "🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨🟥🟨"
        )
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
                    reply_markup=None,
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
        text = _format_review_text(msg)
        kb = _review_keyboard_obj(msg_id)
        await _safe_edit(query, text, reply_markup=kb)
        await _broadcast_card(context, msg_id, text, reply_markup=kb)
        return

    # ── Cancel из input-режима (правка/цена/инструкция) → закрыть, восстановить ──
    if action == "inputcancel":
        await _exit_input_mode(query, context, msg_id, restore_card=True)
        return

    # ── noop: сепараторная кнопка в pipeline — ничего не делает ──
    if action == "noop":
        return  # query.answer() уже сработал в начале

    # ── back: назад к pipeline (то же что тап «🔄 Обновить») ──
    # Send-first → delete-after pattern, чтобы chat не пустел.
    if action == "back":
        chat_id = query.message.chat_id
        clicked_msg_id = query.message.message_id

        # 1) Шлём свежий pipeline (chat не пустеет)
        messages = _format_pipeline_messages()
        persistent_kb = _persistent_menu_keyboard()
        new_ids: set[int] = set()
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
            except Exception:
                logger.exception("back→pipeline send failed")

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

    # ── Autopilot start: ввод floor ──
    if action == "apstart":
        # Проверим не активен ли уже
        ap = db.get_thread_autopilot(msg["gmail_thread_id"] or "")
        if ap and ap["active"]:
            await query.answer(
                f"🤖 Уже активен ({ap['messages_sent']}/20). Используй 🛑 чтоб остановить.",
                show_alert=True,
            )
            return
        # Default floor — из ad_briefs.key_facts.min_acceptable_eur
        ad_id_val = _safe_get(msg, "ad_id")
        default_floor = ""
        if ad_id_val:
            try:
                bf = db.get_ad_brief(ad_id_val)
                if bf and bf["key_facts_json"]:
                    kf = json.loads(bf["key_facts_json"] or "{}")
                    mp = kf.get("min_acceptable_eur")
                    if isinstance(mp, (int, float)) and mp > 0:
                        default_floor = str(int(mp))
            except Exception:
                pass
        prompt = (
            "🚀 <b>Включаем автопилот</b>\n"
            "<i>Бот будет авто-отвечать клиенту до закрытия сделки или 20-го сообщения.</i>\n\n"
            f"💰 <b>Floor цены (€)</b> — ниже не уступит."
        )
        if default_floor:
            prompt += f" Default: <code>{default_floor}</code>"
        await _enter_input_mode(
            query, context, update,
            action="autopilot_floor", msg=msg,
            prompt_label=prompt,
            draft_text=None,
            placeholder=default_floor or "1500",
        )
        return

    # ── Autopilot confirm (после ввода floor + выбора режима): apconfirm:silent:N / apconfirm:notify:N ──
    if action == "apconfirm":
        notify_mode = sub  # "silent" или "notify"
        if notify_mode not in ("silent", "notify"):
            await query.message.reply_text(f"Битый режим: {sub}")
            return
        # Floor должен быть в state — но мы храним в _PENDING_INPUTS["payload"]
        chat_id_now = query.message.chat_id
        user_id_now = update.effective_user.id if update.effective_user else 0
        pending = _PENDING_INPUTS.get((chat_id_now, user_id_now))
        floor_val = None
        if pending and pending.get("action") == "autopilot_pending_mode":
            floor_val = pending.get("floor_eur")
        if floor_val is None:
            await query.message.reply_text("⚠️ Состояние потеряно — начни заново через 🚀")
            return
        # Активируем
        thread_id = msg["gmail_thread_id"] or ""
        db.start_thread_autopilot(
            thread_id, floor_price_eur=float(floor_val),
            notify_mode=notify_mode, started_by=actor,
        )
        # Очищаем state
        _PENDING_INPUTS.pop((chat_id_now, user_id_now), None)
        # Удалим stub-сообщение если есть
        stub = pending.get("stub_msg_id") if pending else None
        if stub:
            try:
                await context.bot.delete_message(chat_id=chat_id_now, message_id=stub)
            except Exception:
                pass
        # Обновляем карточку (broadcast) — теперь с autopilot-active клавиатурой
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id, _format_review_text(updated),
            reply_markup=_autopilot_active_keyboard(msg_id),
        )
        # Notify-старт если режим notify
        if notify_mode == "notify":
            try:
                send_autopilot_start_notification(msg_id, float(floor_val), actor)
            except Exception:
                logger.exception("autopilot start notification fail")
        return

    # ── Autopilot stop (manual) ──
    if action == "apstop":
        thread_id = msg["gmail_thread_id"] or ""
        db.stop_thread_autopilot(thread_id, "manual")
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id,
            _format_review_text(updated) + f"\n\n<i>🛑 {_html(actor)} остановил автопилот вручную</i>",
            reply_markup=_review_keyboard_obj(msg_id),
        )
        return

    # ── Lock-check: actions меняющие state треда требуют монопольного доступа ──
    # Если другой оператор уже работает с тредом — popup, return.
    LOCKABLE_ACTIONS = {"send", "skip", "sold", "editru", "editde", "price", "instr",
                        "q", "t"}
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
        clicked_msg_id = query.message.message_id

        text = _format_thread_detail(msg)
        if len(text) > 4000:
            text = text[:3990] + "\n\n<i>...(обрезано — открой web-морду /threads/<id>)</i>"

        sent = await context.bot.send_message(
            chat_id=chat_id, text=text,
            reply_markup=_thread_detail_keyboard(msg_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        new_detail_id = sent.message_id if sent else None
        if new_detail_id:
            _track_msg(chat_id, new_detail_id)

        # Удаляем pipeline-сообщения. Они в диапазоне [clicked-5 .. new_detail-1]:
        # пара сообщений перед clicked (header) + все карточки тредов после clicked
        # (которые были в pipeline). detail и всё что новее — не трогаем.
        if new_detail_id:
            ids_to_delete = list(range(
                max(1, clicked_msg_id - 5),
                new_detail_id,  # exclusive — не удаляем новый detail
            ))
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=ids_to_delete)
            except Exception:
                # fallback на параллельные single-deletes
                await asyncio.gather(*[
                    context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    for mid in ids_to_delete
                ], return_exceptions=True)
        # Также удаляем in-memory tracked для чистоты
        if chat_id in _CHAT_TRACKED_MSGS:
            _CHAT_TRACKED_MSGS[chat_id] = {
                mid for mid in _CHAT_TRACKED_MSGS[chat_id]
                if mid >= (new_detail_id or clicked_msg_id)
            }
        return

    # ── opencard: переслать карточку ревью (с удалением предыдущей detail-карточки) ──
    if action == "opencard":
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
            )
        except Exception:
            pass
        new_tg = send_for_review(msg_id)
        if not new_tg:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"Не удалось переслать #{msg_id}",
            )
        return

    # ── compose: писать клиенту произвольное сообщение (operator-initiated) ──
    if action == "compose":
        await _enter_input_mode(
            query, context, update,
            action="compose", msg=msg,
            prompt_label=(
                "✉️ <b>Напиши сообщение клиенту</b> (текст на русском — переведу на язык клиента; "
                "или с директивой <code>на немецком: ...</code>)"
            ),
            draft_text=None,
            placeholder="Напиши клиенту что-нибудь...",
        )
        return

    # ── Edit RU / Edit DE — БЕЗ confirmation, сразу в input-режим ──
    if action == "editru":
        await _enter_input_mode(
            query, context, update,
            action="edit_ru", msg=msg,
            prompt_label=f"✏️ <b>Жду правку RU</b>",
            draft_text=msg["ru_answer"] or "",
            placeholder="Твоя правка на русском...",
        )
        return
    if action == "editde":
        client_lang = _safe_get(msg, "client_lang") or "de"
        lang_name, lang_flag = claude.lang_display(client_lang)
        await _enter_input_mode(
            query, context, update,
            action="edit_de", msg=msg,
            prompt_label=f"✏️ <b>Жду правку {lang_flag} {lang_name}</b> (без перевода — сохраню как есть, обратно в RU переведу для зеркала)",
            draft_text=msg["de_answer"] or "",
            placeholder=f"Правка на языке клиента ({lang_name})...",
        )
        return

    # ── Своя цена / Своя инструкция — после confirm → input-режим ──
    if action == "price":
        await _enter_input_mode(
            query, context, update,
            action="price", msg=msg,
            prompt_label="💸 <b>Введи цену цифрой (€)</b>, например: <code>2300</code>",
            draft_text=None,
            placeholder="2300",
        )
        return
    if action == "instr":
        await _enter_input_mode(
            query, context, update,
            action="instruction", msg=msg,
            prompt_label="📝 <b>Введи инструкцию для Claude</b> (например: «ответь жёстче, упомяни самовывоз»)",
            draft_text=None,
            placeholder="ответь жёстче, упомяни самовывоз...",
        )
        return

    # ── Send / Skip / Sold / Clienthist / Quick / Tweak — выполнить ──
    if action == "send":
        db.update_message(msg_id, status="approved")
        await _broadcast_card(context, msg_id, _format_review_text(msg) + f"\n\n⏳ <i>{_html(actor)} → отправляю…</i>", reply_markup=None)
        import scheduler
        result = await asyncio.to_thread(scheduler.send_one, msg_id)
        if result["kind"] == "sent":
            mode_tag = " (debug)" if result.get("mode") == "redirect" else ""
            footer = (
                "\n\n🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢\n"
                f"✅ <b>{_html(actor).upper()} ОТПРАВИЛ КЛИЕНТУ{mode_tag.upper()}</b> ✅\n"
                "🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢"
            )
        elif result["kind"] == "skipped":
            footer = (
                "\n\n🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧\n"
                f"🔒 <b>{_html(actor).upper()}: РЕЖИМ DISABLED</b>\n"
                "<i>Сообщение не отправлено</i>\n"
                "🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧🟨🟧"
            )
        else:
            footer = (
                "\n\n🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥\n"
                f"❌ <b>{_html(actor).upper()}: ОШИБКА</b>\n"
                f"<i>{_html(result.get('message') or 'unknown')}</i>\n"
                "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥"
            )
        await _broadcast_card(
            context, msg_id,
            _format_review_text(db.get_message(msg_id)) + footer,
            reply_markup=None,
        )
        _release_lock(msg_id)
        return

    if action == "skip":
        db.update_message(msg_id, status="skipped")
        await _broadcast_card(
            context, msg_id,
            _format_review_text(db.get_message(msg_id)) + f"\n\n<i>👤 {_html(actor)} пропустил</i>",
            reply_markup=None,
        )
        _release_lock(msg_id)
        return

    if action == "sold":
        ad_id = _safe_get(msg, "ad_id")
        if not ad_id:
            await query.message.reply_text("⚠️ У сообщения нет ad_id — нечем помечать.")
            _release_lock(msg_id)
            return
        db.mark_ad_sold(ad_id, sold=True)
        db.update_message(msg_id, status="skipped_sold")
        await _broadcast_card(
            context, msg_id,
            _format_review_text(db.get_message(msg_id)) + (
                "\n\n🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢\n"
                f"💰 <b>{_html(actor).upper()} ПОМЕТИЛ #{ad_id} ПРОДАНО</b> 💰\n"
                "<i>Будущие inquiries по этому объявлению — auto-skip</i>\n"
                "🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢🟩🟢"
            ),
            reply_markup=None,
        )
        _release_lock(msg_id)
        return

    if action == "clienthist":
        buyer_email = msg["buyer_name"] or ""
        history_text = _format_client_history(buyer_email)
        if len(history_text) > 4000:
            history_text = history_text[:3990] + "\n\n<i>...(обрезано)</i>"
        # Восстановим карточку в нормальный вид (она была в confirm-state)
        await _safe_edit(
            query, _format_review_text(msg),
            reply_markup=_review_keyboard_obj(msg_id),
        )
        await query.message.reply_text(
            history_text, parse_mode="HTML", disable_web_page_preview=True,
        )
        return

    # Quick (q:fest) или Tweak (t:harsh/friend/short/regen) — preset-регенерация
    if action == "q" or action == "t":
        if msg["status"] in ("sent", "sent_debug", "skipped", "skipped_sold"):
            await _broadcast_card(
                context, msg_id,
                _format_review_text(msg) +
                f"\n\n⚠️ <i>Сообщение уже {msg['status']}. Регенерация не имеет смысла.</i>",
                reply_markup=None,
            )
            _release_lock(msg_id)
            return
        kind_label = "перегенерирую" if action == "q" else "переписываю"
        await _broadcast_card(
            context, msg_id,
            _format_review_text(msg) + f"\n\n⏳ <i>{_html(actor)} → {kind_label} «{sub}»...</i>",
            reply_markup=None,
        )
        import scheduler
        result = await asyncio.to_thread(scheduler.regenerate_draft, msg_id, sub)
        if result["kind"] == "regenerated":
            updated = db.get_message(msg_id)
            await _broadcast_card(context, msg_id, _format_review_text(updated), reply_markup=_review_keyboard_obj(msg_id))
        else:
            await _broadcast_card(
                context, msg_id,
                _format_review_text(msg) + f"\n\n{_html(result['message'])}",
                reply_markup=_review_keyboard_obj(msg_id),
            )
        _release_lock(msg_id)
        return

    # ── Reminder-карточки (без изменений) ──
    if action == "remind":
        await _safe_edit(query, (query.message.text_html or "") + f"\n\n⏳ <i>{_html(actor)} → генерирую follow-up...</i>", reply_markup=None)
        import scheduler
        result = await asyncio.to_thread(scheduler.send_followup_ping, msg_id)
        await _safe_edit(
            query,
            (query.message.text_html or "") + f"\n\n<b>👤 {_html(actor)}:</b>\n{_html(result['message'])}",
            reply_markup=None,
        )
        return

    if action == "remindskip":
        db.update_message(msg_id, reminder_state="skipped")
        await _safe_edit(
            query,
            (query.message.text_html or "") + f"\n\n❌ <i>{_html(actor)} забил на пинг.</i>",
            reply_markup=None,
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
            (query.message.text_html or "") + f"\n\n⏰ <i>{_html(actor)} отложил на {days} дн. — напомню {snooze_until[:10]}</i>",
            reply_markup=None,
        )
        return

    await query.message.reply_text(f"Неизвестное действие: {action}")


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

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    pending = _PENDING_INPUTS.pop((chat_id, user_id), None)
    if pending is None:
        return  # не в input-сессии — игнор (обычная болтовня в группе)
    action = pending["action"]
    msg_id = pending["msg_id"]
    tg_msg_id = pending["tg_msg_id"]
    stub_msg_id = pending.get("stub_msg_id")
    actor = _actor_name(update)

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пустой текст, отменено.")
        return

    # Чистим: операторское сообщение + force_reply stub (карточка in-place переживёт)
    try:
        await update.message.delete()
    except Exception:
        pass
    if stub_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=stub_msg_id)
        except Exception:
            pass

    msg_row = db.get_message(msg_id)
    if not msg_row:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ msg={msg_id} не найден в БД")
        return

    # ============================================================
    # edit_ru — оператор написал по-русски, Claude переводит на client_lang
    # ============================================================
    if action == "edit_ru":
        default_lang = _safe_get(msg_row, "client_lang") or "de"
        prior_ru = _safe_get(msg_row, "ru_answer")
        prior_de = _safe_get(msg_row, "de_answer")

        override_lang, ru_text = claude.detect_lang_override(text)
        target_lang = override_lang or default_lang
        lang_name, lang_flag = claude.lang_display(target_lang)
        progress = (
            f"🎯 Распознал директиву: перевожу на {lang_flag} {lang_name} (вместо авто-{default_lang})..."
            if override_lang else f"⏳ Перевожу на {lang_flag} {lang_name}..."
        )
        await _bot_edit(context, chat_id, tg_msg_id,
                        _format_review_text(msg_row) + f"\n\n<i>{progress}</i>")
        try:
            result = claude.translate_only(ru_text, target_lang=target_lang)
        except Exception as e:
            _PENDING_INPUTS[(chat_id, user_id)] = pending  # возвращаем state — пусть пробует ещё
            await _bot_edit(context, chat_id, tg_msg_id,
                            _format_review_text(msg_row) + f"\n\n❌ <i>Ошибка перевода: {e}. Пришли текст ещё раз.</i>",
                            reply_markup=_input_cancel_keyboard(msg_id))
            return
        new_de = result["translation"]
        prior_cost = _safe_get(msg_row, "cost_usd") or 0.0
        prior_in = _safe_get(msg_row, "tokens_in") or 0
        prior_out = _safe_get(msg_row, "tokens_out") or 0
        db.update_message(
            msg_id,
            ru_answer=ru_text, de_answer=new_de, status="edited",
            answer_lang=target_lang,
            tokens_in=prior_in + result["tokens_in"],
            tokens_out=prior_out + result["tokens_out"],
            cost_usd=prior_cost + result["cost_usd"],
        )
        if prior_ru and ru_text and prior_ru.strip() != ru_text.strip():
            try:
                db.add_lesson(
                    message_id=msg_id, account_id=msg_row["account_id"],
                    ad_id=_safe_get(msg_row, "ad_id"),
                    client_lang=_safe_get(msg_row, "client_lang"),
                    client_situation_ru=_safe_get(msg_row, "ru_client"),
                    bad_draft_ru=prior_ru, bad_draft_de=prior_de,
                    good_answer_ru=ru_text, good_answer_de=new_de,
                )
                logger.info("Урок сохранён (edit_ru) по msg=%s", msg_id)
            except Exception:
                logger.exception("Не удалось сохранить урок")
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id,
            _format_review_text(updated) + f"\n\n<i>✏️ переписал {_html(actor)} (RU)</i>",
            reply_markup=_review_keyboard_obj(msg_id),
        )
        _release_lock(msg_id)
        return

    # ============================================================
    # edit_de — оператор написал на языке клиента; back-translate в RU для зеркала
    # ============================================================
    if action == "edit_de":
        client_lang = _safe_get(msg_row, "client_lang") or "de"
        prior_ru = _safe_get(msg_row, "ru_answer")
        prior_de = _safe_get(msg_row, "de_answer")
        lang_name, lang_flag = claude.lang_display(client_lang)

        new_de = text  # как ввёл оператор — отправится клиенту в этом виде
        new_ru = text  # default если client_lang=ru или back-translate упадёт
        extra_cost = 0.0
        extra_in = 0
        extra_out = 0
        if client_lang != "ru":
            await _bot_edit(context, chat_id, tg_msg_id,
                            _format_review_text(msg_row) + f"\n\n<i>⏳ Back-translate {lang_flag} → 🇷🇺 для зеркала RU...</i>")
            try:
                bt = claude.translate_only(text, source_lang=client_lang, target_lang="ru")
                new_ru = bt["translation"]
                extra_cost = bt["cost_usd"]
                extra_in = bt["tokens_in"]
                extra_out = bt["tokens_out"]
            except Exception as e:
                logger.warning("back-translate упал: %s, оставлю ru_answer = de_answer", e)
                # не валим всё — просто RU = DE текст (хуже но без потери)

        prior_cost = _safe_get(msg_row, "cost_usd") or 0.0
        prior_in = _safe_get(msg_row, "tokens_in") or 0
        prior_out = _safe_get(msg_row, "tokens_out") or 0
        db.update_message(
            msg_id,
            ru_answer=new_ru, de_answer=new_de, status="edited",
            answer_lang=client_lang,
            tokens_in=prior_in + extra_in,
            tokens_out=prior_out + extra_out,
            cost_usd=prior_cost + extra_cost,
        )
        if prior_ru and new_ru and prior_ru.strip() != new_ru.strip():
            try:
                db.add_lesson(
                    message_id=msg_id, account_id=msg_row["account_id"],
                    ad_id=_safe_get(msg_row, "ad_id"),
                    client_lang=client_lang,
                    client_situation_ru=_safe_get(msg_row, "ru_client"),
                    bad_draft_ru=prior_ru, bad_draft_de=prior_de,
                    good_answer_ru=new_ru, good_answer_de=new_de,
                )
                logger.info("Урок сохранён (edit_de) по msg=%s", msg_id)
            except Exception:
                logger.exception("Не удалось сохранить урок")
        updated = db.get_message(msg_id)
        await _broadcast_card(
            context, msg_id,
            _format_review_text(updated) + f"\n\n<i>✏️ переписал {_html(actor)} ({lang_flag} {lang_name}, без перевода)</i>",
            reply_markup=_review_keyboard_obj(msg_id),
        )
        _release_lock(msg_id)
        return

    # ============================================================
    # price — парсим число, регенерим под него
    # ============================================================
    if action == "price":
        # Принимаем «2300», «2300€», «2.300», «2,500» и подобное
        clean = text.replace(" ", "").replace(" ", "")
        m = re.search(r"(\d{1,6})(?:[.,](\d+))?", clean)
        if not m:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не нашёл цены в «{text[:40]}». Сессия отменена. Попробуй ещё раз через кнопку.",
            )
            await _broadcast_card(context, msg_id, _format_review_text(msg_row),
                                  reply_markup=_review_keyboard_obj(msg_id))
            _release_lock(msg_id)
            return
        whole = m.group(1)
        frac = m.group(2) or ""
        try:
            price = float(f"{whole}.{frac}") if frac else float(whole)
        except ValueError:
            price = 0
        if not (0 < price < 1_000_000):
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Странная цена {price}€, отменяю.")
            await _broadcast_card(context, msg_id, _format_review_text(msg_row),
                                  reply_markup=_review_keyboard_obj(msg_id))
            _release_lock(msg_id)
            return

        await _broadcast_card(context, msg_id,
                              _format_review_text(msg_row) + f"\n\n<i>⏳ {_html(actor)} → перегенерирую под цену {price}€...</i>",
                              reply_markup=None)
        import scheduler
        result = await asyncio.to_thread(scheduler.regenerate_draft_with_price, msg_id, price)
        if result["kind"] == "regenerated":
            price_str = f"{int(price)}" if float(price).is_integer() else f"{price:.2f}"
            db.append_extra_note(msg_id, f"💸 {actor}: цена {price_str}€")
            updated = db.get_message(msg_id)
            await _broadcast_card(context, msg_id, _format_review_text(updated),
                                  reply_markup=_review_keyboard_obj(msg_id))
        else:
            await _broadcast_card(context, msg_id,
                                  _format_review_text(msg_row) + f"\n\n{_html(result['message'])}",
                                  reply_markup=_review_keyboard_obj(msg_id))
        _release_lock(msg_id)
        return

    # ============================================================
    # instruction — операторская свободная инструкция → регенерация
    # ============================================================
    if action == "instruction":
        await _broadcast_card(context, msg_id,
                              _format_review_text(msg_row) + f"\n\n<i>⏳ {_html(actor)} → перегенерирую по инструкции...</i>",
                              reply_markup=None)
        import scheduler
        result = await asyncio.to_thread(scheduler.regenerate_draft_with_instruction, msg_id, text)
        if result["kind"] == "regenerated":
            note_text = text[:120] + ("…" if len(text) > 120 else "")
            db.append_extra_note(msg_id, f"📝 {actor}: «{note_text}»")
            updated = db.get_message(msg_id)
            await _broadcast_card(context, msg_id, _format_review_text(updated),
                                  reply_markup=_review_keyboard_obj(msg_id))
        else:
            await _broadcast_card(context, msg_id,
                                  _format_review_text(msg_row) + f"\n\n{_html(result['message'])}",
                                  reply_markup=_review_keyboard_obj(msg_id))
        _release_lock(msg_id)
        return

    # ============================================================
    # autopilot_floor — ввод floor цены, далее показываем выбор режима
    # ============================================================
    if action == "autopilot_floor":
        m = re.search(r"(\d{1,7})", text.replace(" ", ""))
        if not m:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не нашёл число в «{text[:40]}». Попробуй заново через 🚀.",
            )
            await _broadcast_card(
                context, msg_id, _format_review_text(msg_row),
                reply_markup=_review_keyboard_obj(msg_id),
            )
            return
        floor = float(m.group(1))
        if not (1 <= floor < 1_000_000):
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Странный floor {floor}€.")
            return
        # Сохраняем floor в state, переключаемся в режим выбора notify_mode
        _PENDING_INPUTS[(chat_id, user_id)] = {
            "action": "autopilot_pending_mode",
            "msg_id": msg_id,
            "tg_msg_id": tg_msg_id,
            "stub_msg_id": stub_msg_id,
            "floor_eur": floor,
        }
        # Edit карточку: показать выбор режима
        await _bot_edit(
            context, chat_id, tg_msg_id,
            _format_review_text(msg_row) + (
                f"\n\n🚀 <b>floor = {int(floor)}€</b> — выбери режим уведомлений:\n"
                f"• 🔔 Notify — пинг при старте/incoming/стопе\n"
                f"• 🤫 Silent — пинг только при стопе"
            ),
            reply_markup=_autopilot_mode_choice_keyboard(msg_id),
        )
        return

    # ============================================================
    # compose — оператор пишет клиенту с нуля; bot переводит и шлёт как reply на last incoming
    # ============================================================
    if action == "compose":
        await _bot_edit(context, chat_id, tg_msg_id,
                        f"<i>⏳ {_html(actor)} → перевожу и отправляю клиенту…</i>")
        import scheduler
        result = await asyncio.to_thread(scheduler.send_manual_compose, msg_id, text)
        # result['kind']: sent / skipped / error
        kind = result.get("kind")
        if kind == "sent":
            footer = f"\n\n✅ <i>Отправлено клиенту ({_html(actor)})</i>"
        elif kind == "skipped":
            footer = f"\n\n🔒 <i>{_html(actor)}: режим disabled — не отправлено</i>"
        else:
            footer = f"\n\n❌ <i>Ошибка: {_html(result.get('message') or 'unknown')}</i>"
        # Заново показываем full thread detail (чтобы было видно новое сообщение)
        new_msg = db.get_message(msg_id)
        await _bot_edit(
            context, chat_id, tg_msg_id,
            _format_thread_detail(new_msg) + footer,
            reply_markup=_thread_detail_keyboard(msg_id),
        )
        return

    logger.warning("Неизвестный action в _PENDING_INPUTS: %s", action)


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
            )
        except Exception:
            logger.exception("Telegram polling упал")

    t = threading.Thread(target=_runner, daemon=True, name="tg-polling")
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_polling()
