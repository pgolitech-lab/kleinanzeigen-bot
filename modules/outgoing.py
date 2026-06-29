# Отправка исходящих сообщений: SMTP, followup, ручной compose, drain-deferred.
# Выделено из scheduler.py. Публичный API:
#   drain_deferred_thread, drain_all_deferred
#   _send_reply, send_one, send_approved_replies
#   send_followup_ping, send_manual_compose

import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

import config
import database as db
from modules import claude, gmail, telegram_bot

logger = logging.getLogger(__name__)

def _row_get(row: Any, key: str) -> Any:
    """Безопасный getter для sqlite3.Row — None если колонки нет."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None

def drain_deferred_thread(thread_id: str) -> int:
    """Поднять отложенные review-карточки треда.

    Зовётся из telegram_bot._release_lock и после завершения SMTP в _send_reply.
    Если тред всё ещё `thread_is_busy` (другой kind не очищен) — пропускаем.
    Возвращает кол-во поднятых карточек.
    """
    if not thread_id:
        return 0
    if telegram_bot.thread_is_busy(thread_id):
        logger.debug("drain_deferred: thread %s ещё busy, skip", thread_id)
        return 0
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE gmail_thread_id=? AND direction='in' "
            "AND status='deferred' ORDER BY created_at ASC, id ASC",
            (thread_id,),
        ).fetchall()
    raised = 0
    for r in rows:
        try:
            db.update_message(r["id"], status="pending")
            telegram_bot.send_for_review(r["id"])
            raised += 1
        except Exception:
            logger.exception("drain_deferred: send_for_review fail msg=%s", r["id"])
    if raised:
        logger.info(
            "drain_deferred: подняли %d отложенных карточек в треде %s",
            raised, thread_id,
        )
        try:
            telegram_bot.refresh_pipeline_for_active_chats()
        except Exception:
            logger.exception("refresh_pipeline after drain_deferred fail")
    return raised


def drain_all_deferred() -> int:
    """Поднять ВСЕ отложенные карточки (все треды). Зовётся при старте бота —
    защита от перезапуска во время оператор-lock'а: in-memory busy-флаги
    теряются, но deferred-rows остаются в БД."""
    with db.get_conn() as conn:
        thread_ids = [
            r["gmail_thread_id"] for r in conn.execute(
                "SELECT DISTINCT gmail_thread_id FROM messages "
                "WHERE direction='in' AND status='deferred' AND gmail_thread_id != ''"
            ).fetchall()
        ]
    total = 0
    for tid in thread_ids:
        total += drain_deferred_thread(tid)
    return total


def _send_reply(msg: Any) -> dict[str, Any]:
    """Отправить одобренный черновик через SMTP с сохранением threading.

    Возвращает dict с подробным результатом для отображения оператору:
        {kind: "sent"|"skipped"|"error", mode, to, real_to, subject, message, ...}

    Атомарный claim: транзишн status → 'sending' через UPDATE с WHERE-фильтром по
    разрешённым исходным статусам. Защита от дублирования когда:
      - оператор дважды тапнул «✅ Отправить» (или confirm)
      - параллельно периодический job `send_approved_replies` подхватил тот же row
      - повторный вызов send_one из веб-морды
    Если rowcount=0 — кто-то уже отправил/отправляет → skip.
    """
    SENDABLE = ("approved", "new", "pending", "edited", "error_send_failed")
    with db.get_conn() as _claim_conn:
        cur = _claim_conn.execute(
            "UPDATE messages SET status='sending' "
            "WHERE id = ? AND status IN (" + ",".join("?" * len(SENDABLE)) + ")",
            (msg["id"], *SENDABLE),
        )
        if cur.rowcount == 0:
            fresh = db.get_message(msg["id"])
            cur_status = fresh["status"] if fresh else "?"
            logger.warning(
                "Duplicate-send guard: msg=%s уже в state=%s, skip",
                msg["id"], cur_status,
            )
            return {
                "kind": "skipped", "mode": config.send_mode(),
                "message": (
                    f"⚠️ msg={msg['id']}: уже {cur_status} (другой воркер/тап). "
                    "Повторная отправка заблокирована."
                ),
            }
    # Перечитываем row после claim — теперь status='sending', все поля свежие.
    msg = db.get_message(msg["id"]) or msg

    account = db.get_account(msg["account_id"])
    if not account:
        db.update_message(msg["id"], status="error_no_account")
        return {"kind": "error", "message": f"❌ msg={msg['id']}: аккаунт не найден"}

    # Найти последнее входящее в треде — оттуда берём адрес, тему, Message-ID для In-Reply-To
    original = None
    rows = db.thread_history(msg["gmail_thread_id"]) if msg["gmail_thread_id"] else []
    for r in reversed(rows):
        if r["direction"] == "in":
            original = r
            break
    if not original:
        logger.warning("Нет оригинала в треде для msg=%s", msg["id"])
        db.update_message(msg["id"], status="error_no_original")
        return {"kind": "error", "message": f"❌ msg={msg['id']}: оригинал в треде не найден"}

    to_email = original["buyer_name"] or ""  # email отправителя сохраняли сюда
    if "@" not in to_email:
        logger.warning("Невалидный получатель для msg=%s: %r", msg["id"], to_email)
        db.update_message(msg["id"], status="error_no_recipient")
        return {"kind": "error", "message": f"❌ msg={msg['id']}: невалидный получатель {to_email!r}"}

    # Тема: ответ на исходную (Re: ...)
    # Приоритет: ad_title (parsed Playwright) > email_subject (от Kleinanzeigen) > generic.
    # Auto-ack уходит ДО Playwright — fallback на email_subject критичен.
    subject = "Re: Anfrage"
    if msg["ad_title"]:
        subject = f"Re: {msg['ad_title']}"
    elif msg["email_subject"]:
        es = msg["email_subject"].strip()
        subject = es if es.lower().startswith("re:") else f"Re: {es}"
    # Defensively: SMTP запрещает CR/LF в значении заголовка (RFC 5322).
    # Subject от MIME-folding мог сохраниться с \r\n (старые rows до fix _decode).
    subject = subject.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()

    # Предохранитель: проверяем режим отправки
    mode = config.send_mode()
    body = msg["de_answer"] or ""

    if mode == "disabled":
        db.update_message(msg["id"], status="not_sent_disabled")
        logger.warning(
            "[SEND DISABLED] msg=%s НЕ отправлен. Должен был уйти на %s, тема %r",
            msg["id"], to_email, subject,
        )
        return {
            "kind": "skipped", "mode": "disabled",
            "real_to": to_email, "subject": subject,
            "message": (
                f"🔒 НЕ отправлено (режим disabled).\n"
                f"Должно было уйти на: {to_email}\n"
                f"Тема: {subject}"
            ),
        }

    actual_to = to_email
    actual_subject = subject
    actual_body = body
    actual_in_reply_to = original["gmail_message_id"]

    if mode == "redirect":
        debug_to = config.debug_email()
        if not debug_to or "@" not in debug_to:
            db.update_message(msg["id"], status="error_no_debug_email")
            logger.error("[SEND REDIRECT] debug_email не задан, msg=%s не отправлен", msg["id"])
            return {
                "kind": "error", "mode": "redirect",
                "message": f"❌ msg={msg['id']}: режим redirect, но debug_email не задан в /settings",
            }
        actual_to = debug_to
        actual_subject = f"[DEBUG → {to_email}] {subject}"
        actual_body = (
            f"⚠️ DEBUG-режим. В production это письмо ушло бы на: {to_email}\n"
            f"Аккаунт: {account['name']} ({account['gmail_email']})\n"
            f"msg_id={msg['id']}, thread={msg['gmail_thread_id'] or '-'}\n"
            f"{'─' * 50}\n\n"
            f"{body}"
        )
        # В debug-режиме НЕ сохраняем тред (In-Reply-To), чтобы письма
        # не попадали в твою исходную переписку.
        actual_in_reply_to = None

    # Помечаем тред «sending» — параллельные incoming в этот же тред будут
    # отложены (status='deferred') и поднимутся после release.
    send_thread_id = msg["gmail_thread_id"] or ""
    telegram_bot.mark_thread_busy(send_thread_id, "sending", by="smtp")
    try:
        try:
            sent_message_id = gmail.send_reply(
                gmail_email=account["gmail_email"],
                gmail_password=account["gmail_app_password"],
                from_name=account["name"] or "",
                to_email=actual_to,
                subject=actual_subject,
                body=actual_body,
                in_reply_to=actual_in_reply_to,
                references=None,
            )
        except Exception as e:
            logger.exception("SMTP отправка упала для msg=%s: %s", msg["id"], e)
            db.update_message(msg["id"], status="error_send_failed")
            return {
                "kind": "error", "mode": mode,
                "message": f"❌ msg={msg['id']}: SMTP упал — {e}",
            }
    finally:
        telegram_bot.clear_thread_busy(send_thread_id, "sending")
        # После завершения отправки — поднять отложенные карточки треда.
        try:
            drain_deferred_thread(send_thread_id)
        except Exception:
            logger.exception("drain_deferred_thread fail после SMTP")

    sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    db.update_message(
        msg["id"],
        status="sent" if mode == "production" else "sent_debug",
        # Сохраняем outgoing Message-ID в отдельной колонке, НЕ перезаписываем gmail_message_id
        # (там лежит ID buyer's incoming — нужен для дедупа и In-Reply-To).
        sent_message_id=sent_message_id,
        sent_at=datetime.utcnow().isoformat(),
    )
    logger.info(
        "[SEND %s] msg=%s → %s%s",
        mode.upper(), msg["id"], actual_to,
        f" (вместо {to_email})" if mode == "redirect" else "",
    )
    if mode == "production":
        message = (
            f"✅ Отправлено покупателю\n"
            f"📬 Адрес: {actual_to}\n"
            f"📝 Тема: {actual_subject}\n"
            f"🕒 {sent_at}\n"
            f"🆔 SMTP Message-ID: {sent_message_id}"
        )
    else:  # redirect
        message = (
            f"✅ Отправлено на debug-адрес: {actual_to}\n"
            f"📨 В production ушло бы на: {to_email}\n"
            f"📝 Тема: {actual_subject}\n"
            f"🕒 {sent_at}"
        )
    try:
        # Thread-state обновление: все мини-карточки треда покажут «мы ответили at HH:MM»
        if send_thread_id:
            telegram_bot.broadcast_thread_state(send_thread_id)
    except Exception:
        logger.exception("broadcast_thread_state after send_reply fail")
    try:
        telegram_bot.refresh_pipeline_for_active_chats()
    except Exception:
        logger.exception("refresh_pipeline after send_reply fail")
    return {
        "kind": "sent", "mode": mode,
        "to": actual_to, "real_to": to_email,
        "subject": actual_subject,
        "sent_at": sent_at,
        "smtp_message_id": sent_message_id,
        "message": message,
    }


def send_one(message_id: int) -> dict[str, Any]:
    """Публичная обёртка: отправить ровно одно сообщение по id, вернуть отчёт.
    Используется из веб-морды и Telegram-handler-а для немедленной отправки.
    """
    row = db.get_message(message_id)
    if not row:
        return {"kind": "error", "message": f"❌ msg={message_id}: не найдено в БД"}
    return _send_reply(row)


def send_approved_replies() -> str:
    """Job: отправить все ответы со status='approved'."""
    queue = db.list_messages(status="approved", limit=100)
    for msg in queue:
        try:
            _send_reply(msg)
        except Exception:
            logger.exception("send_approved_replies: ошибка для msg=%s", msg["id"])
    return f"Режим {config.send_mode()}, в очереди было {len(queue)}"



def send_followup_ping(out_message_id: int) -> dict[str, Any]:
    """Сгенерить и отправить follow-up клиенту по конкретному «висящему» исходящему.

    out_message_id — id исходящего сообщения, на которое клиент не ответил.
    Возвращает result dict (как send_one): kind, message и т.п.
    """
    src = db.get_message(out_message_id)
    if not src:
        return {"kind": "error", "message": f"❌ msg={out_message_id} не найдено"}
    # Висящим исходящим может быть либо direction='out' (отдельная исходящая —
    # например предыдущий ping), либо direction='in' но status='sent'/'sent_debug'
    # (гибридный формат: incoming-вопрос + outgoing-ответ в одной row).
    if not (src["direction"] == "out" or src["status"] in ("sent", "sent_debug")):
        return {"kind": "error",
                "message": f"❌ msg={out_message_id}: не висящий исходящий (direction={src['direction']}, status={src['status']})"}

    account = db.get_account(src["account_id"])
    if not account:
        return {"kind": "error", "message": f"❌ msg={out_message_id}: аккаунт удалён"}

    thread_id = src["gmail_thread_id"] or ""
    history = claude.history_for(thread_id) if thread_id else []
    client_lang = src["client_lang"] or src["answer_lang"] or "de"

    # Бриф (если есть) — для контекста
    brief_text = ""
    ad_id_val = src["ad_id"] if "ad_id" in src.keys() else None
    if ad_id_val:
        bf = db.get_ad_brief(ad_id_val)
        if bf:
            try:
                kf = json.loads(bf["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                kf = {}
            brief_text = ad_brief.format_brief_for_claude(bf["brief_md"], kf)

    # Уроки от оператора (только good_answer_ru — для подражания стилю)
    lessons = [dict(r) for r in db.find_relevant_lessons(
        ad_id=ad_id_val, account_id=src["account_id"], limit=3,
    )]

    days_silent = config.reminder_after_days()
    try:
        ping = claude.generate_followup_ping(
            history=history, client_lang=client_lang,
            days_silent=days_silent, brief_text=brief_text, lessons=lessons,
        )
    except Exception as e:
        logger.exception("Claude follow-up ping упал для out_msg=%s", out_message_id)
        return {"kind": "error", "message": f"❌ Claude упал: {e}"}

    # Найдём оригинал входящего треда, чтобы взять адрес покупателя и In-Reply-To
    rows = db.thread_history(thread_id) if thread_id else []
    original_in = None
    for r in reversed(rows):
        if r["direction"] == "in":
            original_in = r
            break
    if not original_in:
        return {"kind": "error", "message": f"❌ msg={out_message_id}: нет оригинала в треде"}

    # Создаём новую исходящую запись со статусом approved + is_reminder=1,
    # затем тут же отправляем через _send_reply.
    new_id = db.add_message(
        account_id=src["account_id"],
        direction="out",
        gmail_thread_id=thread_id,
        ad_url=src["ad_url"], ad_title=src["ad_title"], ad_price=src["ad_price"],
        ad_id=ad_id_val,
        seller_name=src["seller_name"], buyer_name=src["buyer_name"],
        ru_answer=ping["ru_text"], de_answer=ping["client_text"],
        client_lang=client_lang, answer_lang=client_lang,
        tokens_in=ping["tokens_in"], tokens_out=ping["tokens_out"], cost_usd=ping["cost_usd"],
        is_reminder=1,
        status="approved",
    )
    new_row = db.get_message(new_id)
    result = _send_reply(new_row)
    # Помечаем оригинальное «висящее» исходящее как обработанное
    db.update_message(out_message_id, reminder_state="approved")
    return result


def send_manual_compose(source_msg_id: int, operator_text: str) -> dict[str, Any]:
    """Operator-initiated сообщение клиенту (compose-режим из thread-detail карточки).

    source_msg_id: id любой row в нужном треде (берём оттуда thread_id, аккаунт).
    operator_text: что написал оператор. Если на русском — переводим на client_lang.
                   Поддерживается директива «на немецком: ...» / «in english: ...».

    Шлётся как SMTP reply на ПОСЛЕДНЕЕ incoming-сообщение клиента в треде
    (для Gmail-thread continuity). Создаётся row с direction='out', status sent/sent_debug.
    """
    src = db.get_message(source_msg_id)
    if not src:
        return {"kind": "error", "message": f"msg #{source_msg_id} не найден"}

    thread_id = src["gmail_thread_id"] or ""
    if not thread_id:
        return {"kind": "error", "message": "У сообщения нет thread_id"}

    rows = db.thread_history(thread_id)
    last_in = next((r for r in reversed(rows) if r["direction"] == "in"), None)
    if not last_in:
        return {"kind": "error", "message": "В треде нет incoming-сообщений (нечем reply-ить)"}

    account = db.get_account(src["account_id"])
    if not account:
        return {"kind": "error", "message": "Аккаунт удалён"}

    # Распознаём директиву языка
    override_lang, ru_text = claude.detect_lang_override(operator_text)
    client_lang = (
        last_in["client_lang"] or src["client_lang"] or "de"
    )
    target_lang = override_lang or client_lang

    # Если target == ru — без перевода
    if target_lang == "ru":
        translated = ru_text
        cost = 0.0
        in_t = out_t = 0
    else:
        try:
            ad_ctx = (last_in["ad_title"] if last_in else None) or src["ad_title"] or ""
            r = claude.translate_only(ru_text, target_lang=target_lang, context=ad_ctx)
            translated = r["translation"]
            cost = r["cost_usd"]
            in_t = r["tokens_in"]
            out_t = r["tokens_out"]
        except Exception as e:
            logger.exception("send_manual_compose: translate упал")
            return {"kind": "error", "message": f"перевод упал: {e}"}

    # Создаём out-row с status='approved' → передаём в _send_reply
    new_id = db.add_message(
        account_id=src["account_id"],
        direction="out",
        gmail_thread_id=thread_id,
        ad_url=src["ad_url"],
        ad_title=src["ad_title"],
        ad_id=_row_get(src, "ad_id"),
        ad_price=src["ad_price"],
        seller_name=src["seller_name"],
        buyer_name=last_in["buyer_name"],
        buyer_display_name=_row_get(last_in, "buyer_display_name"),
        email_subject=_row_get(last_in, "email_subject"),
        ru_answer=ru_text,
        de_answer=translated,
        client_lang=client_lang,
        answer_lang=target_lang,
        tokens_in=in_t,
        tokens_out=out_t,
        cost_usd=cost,
        status="approved",
    )
    new_row = db.get_message(new_id)
    result = _send_reply(new_row)
    logger.info("Manual compose for thread=%s, target_lang=%s, kind=%s",
                thread_id, target_lang, result.get("kind"))
    return result


