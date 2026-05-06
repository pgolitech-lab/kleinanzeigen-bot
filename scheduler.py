# APScheduler: polling Gmail, отправка одобренных ответов, бекап в Drive.
# Все задачи sync, ошибки логируются, но не валят весь процесс.

import hashlib
import json
import logging
import random
import re
import subprocess
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler

import config
import database as db
from modules import ad_brief, backup, claude, gmail, parser, telegram_bot

logger = logging.getLogger(__name__)


# Заранее заготовленные «поводы» для авто-приветствия. Один из них рандомно
# попадает в промпт Haiku — даёт реальную вариативность вместо детерминированного
# текста. Хранить в settings нет смысла: меняется редко, правится в коде.
AUTO_ACK_EXCUSES: list[str] = [
    "сейчас за рулём, не могу подробно отвечать",
    "сейчас на встрече",
    "сейчас на телефоне с другим клиентом",
    "сейчас вне офиса",
    "сейчас занят, освобожусь в течение часа",
    "сейчас в дороге",
]


# Глобальный статус задач: {job_id: {last_run, ok, result, error}}.
# Заполняется через event listener — поэтому работает без обёрток вокруг каждой job.
JOB_STATUS: dict[str, dict[str, Any]] = {}


def _clean_display_name(name: str) -> str:
    """Срезать суффиксы Kleinanzeigen из display name отправителя.

    «Simon über Kleinanzeigen» → «Simon», «Hans via Kleinanzeigen» → «Hans».
    """
    if not name:
        return ""
    cleaned = re.sub(
        r'\s*(?:via|über|ueber|über\s+den\s+Marktplatz)\s+Kleinanzeigen\s*$',
        '', name.strip(), flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _job_listener(event: Any) -> None:
    """Слушатель APScheduler — фиксирует результат каждого выполнения задачи."""
    if event.exception:
        JOB_STATUS[event.job_id] = {
            "last_run": datetime.now().strftime("%H:%M:%S"),
            "ok": False,
            "result": None,
            "error": str(event.exception),
        }
    else:
        JOB_STATUS[event.job_id] = {
            "last_run": datetime.now().strftime("%H:%M:%S"),
            "ok": True,
            "result": str(event.retval) if event.retval is not None else None,
            "error": None,
        }


# ============================================================
# Polling Gmail: входящие → парсинг → Claude → Telegram
# ============================================================

def _row_get(row: Any, key: str) -> Any:
    """sqlite3.Row не имеет .get() — обернём чтобы безопасно читать новые колонки
    (на случай старых row объектов до миграции)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _ack_already_sent_for_thread(thread_id: Optional[str]) -> bool:
    """Был ли уже отправлен (или хотя бы создан в БД) ack для этого треда.

    Защита от двойной отправки при /debug/reprocess или race conditions.
    Учитываем любую ack-row кроме error_send_failed — если попытка падала,
    можно попробовать ещё раз при следующем reprocess.
    """
    if not thread_id:
        return False
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE gmail_thread_id = ? "
            "AND COALESCE(is_auto_ack, 0) = 1 "
            "AND status NOT IN ('error_send_failed', 'error_no_account', "
            "'error_no_original', 'error_no_recipient', 'error_no_debug_email') "
            "LIMIT 1",
            (thread_id,),
        ).fetchone()
    return bool(row)


def _send_auto_ack(
    account: Any,
    in_row: Any,
    body: str,
    buyer_display_name: Optional[str],
) -> Optional[int]:
    """Сгенерить + послать автоответ-приветствие. Возвращает id ack-row или None.

    Уважает send_mode (disabled/redirect/production) — то же поведение что у обычной
    отправки. Любая ошибка (Haiku, SMTP) → log.warning, return None, основной flow
    продолжается без ack.
    """
    mode = config.send_mode()
    if mode == "disabled":
        logger.info(
            "auto-ack skipped (send_mode=disabled), in_msg=%s",
            in_row["id"],
        )
        return None

    thread_id = in_row["gmail_thread_id"] or ""
    if _ack_already_sent_for_thread(thread_id):
        logger.info(
            "auto-ack: уже отправлялся для thread=%s, skip",
            thread_id,
        )
        return None

    # Час в Берлине — для Guten Morgen / Tag / Abend
    try:
        berlin_hour = datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).hour
    except Exception:
        berlin_hour = datetime.utcnow().hour  # fallback
    excuse = random.choice(AUTO_ACK_EXCUSES)

    try:
        ack = claude.generate_auto_ack(
            buyer_display_name=buyer_display_name or "",
            body=body,
            hour_local=berlin_hour,
            excuse_hint=excuse,
        )
    except Exception:
        logger.exception("auto-ack: Haiku упал, пропускаю in_msg=%s", in_row["id"])
        return None

    # RU-перевод текста ack для отображения в веб/тг (Haiku ~$0.0003).
    # Если клиент уже на русском — просто копируем текст.
    ack_lang = ack.get("client_lang") or "de"
    ru_translation = ""
    if ack_lang == "ru":
        ru_translation = ack["ack_text"]
    else:
        try:
            tr = claude.translate_only(
                ack["ack_text"], source_lang=ack_lang, target_lang="ru",
                context=in_row["ad_title"] or "",
            )
            ru_translation = tr.get("translation", "") or ""
        except Exception:
            logger.warning(
                "auto-ack: translate_only fail для in_msg=%s, RU перевод пуст",
                in_row["id"],
            )

    # Вставляем ack-row перед отправкой — чтобы _send_reply мог обновить status.
    ack_id = db.add_message(
        account_id=account["id"],
        direction="out",
        gmail_thread_id=thread_id,
        ad_url=in_row["ad_url"],
        ad_title=in_row["ad_title"],
        ad_id=_row_get(in_row, "ad_id"),
        ad_price=in_row["ad_price"],
        seller_name=in_row["seller_name"],
        buyer_name=in_row["buyer_name"],
        buyer_display_name=buyer_display_name,
        email_subject=_row_get(in_row, "email_subject"),
        client_lang=ack_lang,
        answer_lang=ack_lang,
        de_answer=ack["ack_text"],
        ru_answer=ru_translation,
        ru_translation=ru_translation,
        tokens_in=ack["tokens_in"],
        tokens_out=ack["tokens_out"],
        cost_usd=ack["cost_usd"],
        is_auto_ack=1,
        status="approved",  # _send_reply сам выставит sent / sent_debug
    )
    ack_row = db.get_message(ack_id)
    result = _send_reply(ack_row)
    if result["kind"] == "error":
        logger.warning(
            "auto-ack SMTP failed for in_msg=%s: %s",
            in_row["id"], result.get("message"),
        )
    else:
        logger.info(
            "auto-ack sent for in_msg=%s, mode=%s, lang=%s, cost=$%.5f",
            in_row["id"], mode, ack["client_lang"], ack["cost_usd"],
        )
    return ack_id


def _autopilot_dispatch(msg_id: int, autopilot_row: Any, reply: dict[str, Any]) -> None:
    """Обработать reply в автопилот-режиме.

    Stop checks, авто-отправка / review-card / threat warning, counter increment, notification.
    """
    thread_id = autopilot_row["gmail_thread_id"]
    notify_mode = autopilot_row["notify_mode"]
    current_count = autopilot_row["messages_sent"]

    # Stop checks ПЕРЕД отправкой
    stop_reason: Optional[str] = None
    if reply.get("should_stop"):
        stop_reason = (reply.get("stop_reason") or "stopped_by_sonnet").strip() or "stopped_by_sonnet"
    elif (current_count + 1) > 20:
        stop_reason = "limit"

    if stop_reason == "threat":
        # Отправить предупреждение клиенту + остановить автопилот
        db.update_message(msg_id, status="approved", is_autopilot_reply=1)
        result = _send_reply(db.get_message(msg_id))
        if result.get("kind") in ("sent", "skipped"):
            db.increment_autopilot_messages(thread_id)
        db.stop_thread_autopilot(thread_id, "threat")
        try:
            telegram_bot.send_autopilot_stop_notification(msg_id, "threat")
        except Exception:
            logger.exception("autopilot threat notification fail")
        return

    if stop_reason:
        # Остановить БЕЗ отправки — оператор сам разрулит через обычную review-карточку
        db.stop_thread_autopilot(thread_id, stop_reason)
        try:
            telegram_bot.send_for_review(msg_id)
        except Exception:
            logger.exception("send_for_review (autopilot stop) fail")
        try:
            telegram_bot.send_autopilot_stop_notification(msg_id, stop_reason)
        except Exception:
            logger.exception("autopilot stop notification fail")
        return

    # Нормальная авто-отправка
    db.update_message(msg_id, status="approved", is_autopilot_reply=1)
    result = _send_reply(db.get_message(msg_id))
    if result.get("kind") in ("sent", "skipped"):
        new_count = db.increment_autopilot_messages(thread_id)
        if notify_mode == "notify":
            try:
                telegram_bot.send_autopilot_progress(msg_id, new_count)
            except Exception:
                logger.exception("autopilot progress notification fail")
        logger.info("autopilot reply sent: msg=%s thread=%s count=%d/20",
                    msg_id, thread_id, new_count)
    else:
        # SMTP fail — counter НЕ инкрементим, автопилот остаётся active
        logger.warning("autopilot SMTP fail msg=%s — autopilot остаётся active", msg_id)


def _process_incoming(account: Any, email: dict[str, Any], force: bool = False) -> None:
    """Обработать одно входящее письмо: создать запись, спарсить, сгенерить ответ, послать оператору.

    force=True — режим отладочного реплея: дедуп пропускаем, IMAP `Seen` не ставим
    (чтобы можно было повторить). Используется через /debug/reprocess.
    """
    msg_id = email.get("gmail_message_id") or ""

    # Дедуп по Message-ID (только в обычном режиме)
    if not force and msg_id and db.find_by_gmail_message_id(msg_id):
        gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        return

    raw_body = email.get("body") or ""
    body_html = email.get("body_html") or ""
    # Срезаем шаблонные заголовки Kleinanzeigen — оставляем только текст клиента
    body = parser.clean_email_body(raw_body)
    subject = (email.get("subject") or "").strip()

    # Skip всё что от noreply@... — это всегда системные письма Kleinanzeigen
    # (price changes, ad expiry, account notifications, saved-search и т.п.).
    # Реальные buyer-сообщения приходят с subdomain mail.kleinanzeigen.de
    # (адрес типа 8t0c76kr5n624-...@mail.kleinanzeigen.de — replyto-relay).
    from_email_lower = (email.get("from_email") or "").lower().strip()
    if from_email_lower.startswith("noreply@"):
        logger.info(
            "Skip: noreply sender, account=%s, from=%s, subject=%r",
            account["gmail_email"], from_email_lower, subject[:80],
        )
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception:
            pass
        return

    # Skip системные письма Kleinanzeigen (saved-search alerts, истечение объявления, отзывы)
    if parser.is_junk_subject(subject):
        logger.info(
            "Skip junk system email: account=%s, subject=%r",
            account["gmail_email"], subject[:80],
        )
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception:
            pass
        return

    # Skip переписку где МЫ покупатель (наша покупка, не продажа).
    # Subject `Anfrage zu Ihrer Anzeige` (формальное «Ihrer» = «Вашему», т.к. мы
    # обращаемся к продавцу). Seller-side incoming всегда `Nutzer-Anfrage zu deiner
    # Anzeige` (du-Form, т.к. KZ адресует нас как seller'а). Маркируем тред closed
    # чтобы не попадал в pipeline и больше не предлагал ответ.
    if re.search(r"\bAnfrage zu Ihrer Anzeige\b", subject, re.IGNORECASE):
        logger.info(
            "Skip purchase-side thread (we are buyer): account=%s, subject=%r",
            account["gmail_email"], subject[:80],
        )
        if inbound_thread_id := (email.get("gmail_thread_id") or "").strip():
            db.close_thread(inbound_thread_id, closed_by="auto:purchase-side")
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception:
            pass
        return

    # AI-классификатор (Haiku): финальный gate перед дорогими операциями.
    # Дешёвая модель решает «buyer-inquiry или системная рассылка».
    # Срабатывает на всё что прошло cheap-фильтры (noreply / age / junk-subject blacklist).
    #
    # Bypass: если в этом gmail_thread_id уже есть сохранённый incoming inquiry —
    # это follow-up клиента, не системка. Haiku ошибается на коротких follow-up-ах
    # типа «Danke», «Sorry, Peter natürlich» — маркирует как «automatisch generierte
    # E-Mail» из-за replyto-relay-адреса и теряет реальные сообщения клиента.
    inbound_thread_id = (email.get("gmail_thread_id") or "").strip()
    skip_classifier = False
    if inbound_thread_id:
        with db.get_conn() as conn:
            prior = conn.execute(
                "SELECT 1 FROM messages WHERE gmail_thread_id=? AND direction='in' LIMIT 1",
                (inbound_thread_id,),
            ).fetchone()
        if prior:
            skip_classifier = True
            logger.info(
                "Classifier bypass: thread %s уже имеет inquiry, follow-up принят без проверки",
                inbound_thread_id,
            )

    if not skip_classifier:
        try:
            classify = claude.classify_email_is_inquiry(
                subject=subject,
                from_name=email.get("from_name") or "",
                from_email=email.get("from_email") or "",
                body=body,
            )
            logger.info(
                "Classifier: is_inquiry=%s ($%.5f) reason=%r account=%s subject=%r",
                classify["is_inquiry"], classify["cost_usd"],
                classify["reason"][:80], account["gmail_email"], subject[:60],
            )
            if not classify["is_inquiry"]:
                try:
                    gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
                except Exception:
                    pass
                return
        except Exception:
            logger.exception("classifier упал, пропускаю проверку")
            # Fail-open: если classifier недоступен — продолжаем по cheap-правилам

    # Skip слишком старые письма (по Date-header).
    # Защита от разгребания древнего архива по уже-удалённым/проданным объявлениям.
    max_age = config.inquiry_max_age_days()
    if max_age > 0:
        from email.utils import parsedate_to_datetime
        date_str = (email.get("date") or "").strip()
        if date_str:
            try:
                msg_dt = parsedate_to_datetime(date_str)
                if msg_dt is not None:
                    # Привести к timezone-aware UTC; некоторые отправители шлют наивные даты.
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=__import__("datetime").timezone.utc)
                    age_days = (datetime.now(msg_dt.tzinfo) - msg_dt).days
                    if age_days > max_age:
                        logger.info(
                            "Skip: too old (%d дн., лимит %d), account=%s, subject=%r",
                            age_days, max_age, account["gmail_email"], subject[:60],
                        )
                        try:
                            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
                        except Exception:
                            pass
                        return
            except (TypeError, ValueError) as e:
                logger.debug("parsedate failed: %s", e)

    # Извлечём ID объявления заранее — нужен для проверки sold-флага
    early_ad_id = parser.extract_ad_id(body, body_html)
    if early_ad_id and db.is_ad_sold(early_ad_id):
        # Объявление помечено проданным — не обрабатываем, не зовём Claude.
        # Тихая нотификация в Telegram чтобы оператор знал.
        try:
            telegram_bot._http_post("sendMessage", {
                "chat_id": config.telegram_chat_id(),
                "text": (
                    f"📭 <b>Inquiry на проданное объявление</b>\n"
                    f"От: {email.get('from_name') or email.get('from_email')}\n"
                    f"Объявление #{early_ad_id} помечено как продано — пропустил."
                ),
                "parse_mode": "HTML",
            })
        except Exception:
            logger.exception("Не удалось послать notification про sold")
        # Помечаем seen чтобы не обрабатывать повторно
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception:
            pass
        return

    # Defense in depth: IMAP-фильтр по From должен был отсеять до нас, но если
    # пользователь его выключил — не пускаем не-Kleinanzeigen-почту в Claude/Telegram.
    filter_str = (config.gmail_from_filter() or "").lower().strip()
    if filter_str:
        from_email = (email.get("from_email") or "").lower()
        from_domain_match = filter_str in from_email
        body_has_ad = bool(
            parser.KLEINANZEIGEN_URL_RE.search(body)
            or parser.KLEINANZEIGEN_URL_RE.search(body_html)
        )
        if not (from_domain_match or body_has_ad):
            logger.info(
                "Пропускаем не-Kleinanzeigen письмо: from=%s, subj=%r",
                from_email, subject[:60],
            )
            return

    # Поиск URL: text → html. Если нет — конструируем из ID.
    ad_url = parser.extract_url(body, body_html)
    ad_id = parser.extract_ad_id(body, body_html)
    if not ad_url and ad_id:
        ad_url = parser.url_from_ad_id(ad_id)
        logger.info("URL объявления построен из ID %s: %s", ad_id, ad_url)

    # Defense in depth: настоящий inquiry ВСЕГДА содержит ссылку или ID объявления.
    # Если ни ad_url ни ad_id извлечь не удалось — это системное письмо или мусор,
    # которое прошло subject-blacklist. Скипаем тихо.
    if not ad_id and not ad_url:
        logger.info(
            "Skip: no ad reference in body, account=%s, from=%s, subject=%r",
            account["gmail_email"], email.get("from_email"), subject[:80],
        )
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception:
            pass
        return

    # Сохраняем входящее СРАЗУ, до медленного Playwright-парсинга. Это:
    #  1) гарантирует что письмо не потеряется даже если parse упадёт
    #  2) даёт auto-ack возможность уйти за секунды (Playwright потом, ~10-30с)
    buyer_display = _clean_display_name(email.get("from_name") or "") or None
    # Реальная дата отправки письма (Date-header). При live-fetch ≈ now,
    # при backfill/recovery — позволяет сохранить хронологический порядок в карточке.
    real_created_at: Optional[str] = None
    date_str = (email.get("date") or "").strip()
    if date_str:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            if dt is not None:
                if dt.tzinfo is not None:
                    dt = dt.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None)
                real_created_at = dt.isoformat()
        except Exception:
            real_created_at = None

    add_kwargs: dict[str, Any] = dict(
        gmail_message_id=msg_id,
        gmail_thread_id=email.get("gmail_thread_id") or "",
        ad_url=ad_url,
        ad_id=ad_id,
        # ad_title/ad_price/ad_description/seller_name заполним после parse_ad
        buyer_name=email.get("from_email") or email.get("from_name"),
        buyer_display_name=buyer_display,
        de_client=body,
        email_subject=subject,
        status="new",
    )
    if real_created_at:
        add_kwargs["created_at"] = real_created_at
    inserted_id = db.add_message(
        account_id=account["id"],
        direction="in",
        **add_kwargs,
    )

    # Реактивация: если оператор раньше нажал «🏁 Завершить» — снимаем флаг,
    # тред снова появится в pipeline (клиент написал ещё раз).
    inbound_thread = email.get("gmail_thread_id") or ""
    if inbound_thread and db.is_thread_closed(inbound_thread):
        db.reopen_thread(inbound_thread)
        logger.info(
            "Reopened closed thread %s — клиент написал в архивный тред",
            inbound_thread,
        )
    # Аналогично — снимаем «⏳ Ждать ответа»: клиент ответил, секцию пайплайна
    # должно пересчитать заново (last_event_kind='in' → 🔴).
    if inbound_thread and db.is_thread_waiting(inbound_thread):
        db.unmark_thread_waiting(inbound_thread)
        logger.info(
            "Unmarked waiting thread %s — клиент прислал ответ",
            inbound_thread,
        )

    # ── Auto-ack (накрутка метрики «отвечает в течение X часов») ──
    # Триггер: account.auto_ack_enabled=1 AND это первое incoming в треде
    # AND ack ещё не слался для этого треда (защита от reprocess).
    # Уважает send_mode. Любая ошибка → log + продолжаем без ack.
    # Запускается ДО parse_ad — Playwright медленный, а ack должен уложиться в секунды.
    thread_id = email.get("gmail_thread_id") or ""
    if _row_get(account, "auto_ack_enabled") and thread_id:
        try:
            thread_msgs = db.thread_history(thread_id)
            in_count = sum(1 for r in thread_msgs if r["direction"] == "in")
            has_prior_ack = any(_row_get(r, "is_auto_ack") for r in thread_msgs)
            is_first_inquiry = (in_count == 1) and not has_prior_ack
            if is_first_inquiry:
                in_row = next(
                    (r for r in thread_msgs if r["id"] == inserted_id), None,
                )
                if in_row:
                    _send_auto_ack(
                        account, in_row, body,
                        buyer_display_name=buyer_display,
                    )
        except Exception:
            logger.exception(
                "auto-ack pipeline crash для in_msg=%s, продолжаю основной flow",
                inserted_id,
            )

    # Парсинг страницы Playwright — ПОСЛЕ auto-ack, потому что медленно.
    ad = {"title": "", "price": "", "description": "", "seller_name": ""}
    if ad_url:
        try:
            ad = parser.parse_ad(ad_url)
        except Exception as e:
            logger.warning("Не спарсилась страница %s: %s", ad_url, e)

    # Fallback для title: если парсер ничего не достал — используем тему письма
    # (Kleinanzeigen в Subject обычно пишет «Anfrage: <название объявления>»)
    if not ad.get("title") and subject:
        clean = re.sub(
            r'^(?:Re:\s*|Anfrage(?:\s+zu\s+Ihrer\s+Anzeige)?[:\s-]+)',
            '', subject, flags=re.IGNORECASE,
        ).strip()
        ad["title"] = clean or subject

    # Дописываем распарсенные поля в уже-существующую row
    db.update_message(
        inserted_id,
        ad_title=ad.get("title"),
        ad_price=ad.get("price"),
        ad_description=ad.get("description"),
        seller_name=ad.get("seller_name"),
    )

    # Бриф объявления: достаём из кэша или генерим (если есть ad_id и хоть какие-то данные)
    brief_text = ""
    brief_extra_cost = 0.0
    if ad_id and (ad.get("title") or ad.get("description")):
        cached = db.get_ad_brief(ad_id)
        if cached:
            try:
                key_facts = json.loads(cached["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                key_facts = {}
            brief_text = ad_brief.format_brief_for_claude(cached["brief_md"], key_facts)
        else:
            try:
                bf = ad_brief.generate_brief(
                    ad_title=ad.get("title", ""),
                    ad_url=ad_url or "",
                    ad_price=ad.get("price", ""),
                    ad_description=ad.get("description", ""),
                    seller_name=ad.get("seller_name", ""),
                )
                db.upsert_ad_brief(
                    ad_id=ad_id,
                    ad_title=ad.get("title"),
                    ad_url=ad_url,
                    ad_price=ad.get("price"),
                    brief_md=bf["brief_md"],
                    key_facts_json=json.dumps(bf["key_facts"], ensure_ascii=False),
                )
                brief_text = ad_brief.format_brief_for_claude(bf["brief_md"], bf["key_facts"])
                brief_extra_cost = bf["cost_usd"]
                logger.info(
                    "Brief сгенерён для ad_id=%s (cost=$%.4f)",
                    ad_id, bf["cost_usd"],
                )
            except Exception as e:
                logger.warning("Не удалось сгенерить бриф для ad_id=%s: %s", ad_id, e)

    # История треда из БД (без только что вставленного письма)
    history = [
        h for h in claude.history_for(email.get("gmail_thread_id") or "")
        if h.get("de_client") != body
    ]

    # Релевантные уроки от оператора — топ-5
    lessons_rows = db.find_relevant_lessons(
        ad_id=ad_id, account_id=account["id"], limit=5,
    )
    lessons_payload = [dict(r) for r in lessons_rows]

    # Проверим autopilot для треда — если активен, генерим Sonnet-ответ автоматически.
    # Иначе (manual flow) — Sonnet НЕ дёргаем, дешёво переводим incoming на RU
    # для читабельности и показываем state-0 карточку с кнопкой «🤖 Предложить ответ».
    ap_thread_id = email.get("gmail_thread_id") or ""
    autopilot = db.get_thread_autopilot(ap_thread_id) if ap_thread_id else None
    is_autopilot = bool(autopilot and autopilot["active"])

    if is_autopilot:
        try:
            last_price = None
            for r in reversed(db.thread_history(ap_thread_id)):
                bj = _row_get(r, "deal_brief_json")
                if bj:
                    try:
                        d = json.loads(bj)
                        p = d.get("negotiated_price_eur")
                        if isinstance(p, (int, float)) and p > 0:
                            last_price = float(p)
                            break
                    except Exception:
                        pass
            reply = claude.generate_autopilot_reply(
                de_client_text=body,
                ad_title=ad.get("title", ""),
                ad_price=ad.get("price", ""),
                ad_description=ad.get("description", ""),
                seller_name=ad.get("seller_name", ""),
                history=history, brief_text=brief_text, lessons=lessons_payload,
                floor_eur=autopilot["floor_price_eur"],
                last_our_price_eur=last_price,
            )
        except Exception as e:
            logger.exception("Autopilot Claude упал для msg=%s: %s", inserted_id, e)
            return

        deal_brief_json = json.dumps(reply["deal_brief"], ensure_ascii=False) if reply.get("deal_brief") else None
        db.update_message(
            inserted_id,
            ru_client=reply["ru_client"],
            ru_answer=reply["ru_answer"],
            de_answer=reply["de_answer"],
            ru_translation=reply.get("ru_translation"),
            client_lang=reply.get("client_lang"),
            tokens_in=reply.get("tokens_in"),
            tokens_out=reply.get("tokens_out"),
            cost_usd=(reply.get("cost_usd", 0.0) or 0.0) + brief_extra_cost,
            deal_brief_json=deal_brief_json,
        )
        try:
            _autopilot_dispatch(inserted_id, autopilot, reply)
        except Exception:
            logger.exception("autopilot dispatch упал для msg=%s", inserted_id)
            try:
                telegram_bot.send_for_review(inserted_id)
            except Exception:
                pass
        return

    # ───── Manual flow: дешёвый Haiku-перевод incoming → ru_client (для оператора).
    # Sonnet draft (de_answer / ru_answer / deal_brief) НЕ генерим — оператор сам
    # тапнет «🤖 Предложить ответ» когда захочет (см. handler `propose`).
    try:
        tr = claude.detect_and_translate_to_ru(body)
        db.update_message(
            inserted_id,
            ru_client=tr.get("translation_ru") or "",
            client_lang=tr.get("lang") or "de",
            cost_usd=tr.get("cost_usd", 0.0),
        )
    except Exception:
        logger.exception("incoming RU-translation fail msg=%s", inserted_id)

    # Если тред занят (оператор работает с любой row или идёт SMTP) —
    # откладываем review-карточку. Drain поднимет её при release_lock или
    # после завершения SMTP. Sonnet draft уже сохранён — оператор увидит
    # карточку как только освободится.
    if not force and telegram_bot.thread_is_busy(thread_id):
        db.update_message(inserted_id, status="deferred")
        logger.info(
            "Deferred review-card for msg=%s — thread %s busy",
            inserted_id, thread_id,
        )
        return

    try:
        telegram_bot.send_for_review(inserted_id)
    except Exception as e:
        logger.exception("Не удалось послать оператору msg=%s: %s", inserted_id, e)
        return

    if not force:
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception as e:
            logger.warning("Не пометили письмо прочитанным: %s", e)


def poll_all_accounts() -> str:
    """Job: пройти по всем активным аккаунтам и обработать новые письма.

    Возвращает короткое summary для отображения в /api/status.
    """
    if config.polling_paused():
        return "⏸ Polling на паузе (через /pause)"
    accounts = db.list_accounts(only_active=True)
    from_filter = config.gmail_from_filter() or None
    total_new = 0
    failed_imap = 0
    for acc in accounts:
        try:
            new_emails = gmail.fetch_new(
                acc["gmail_email"], acc["gmail_app_password"],
                from_filter=from_filter,
            )
        except Exception as e:
            failed_imap += 1
            logger.error("IMAP fetch для аккаунта %s упал: %s", acc["id"], e)
            continue
        if new_emails:
            logger.info("Аккаунт %s: %d новых писем", acc["gmail_email"], len(new_emails))
        total_new += len(new_emails)
        for em in new_emails:
            try:
                _process_incoming(acc, em)
            except Exception:
                logger.exception("Ошибка обработки письма")
    return (
        f"Аккаунтов: {len(accounts)}, новых писем: {total_new}"
        + (f", IMAP-ошибок: {failed_imap}" if failed_imap else "")
    )


# ============================================================
# Отправка одобренных оператором ответов
# ============================================================

def generate_draft_for_msg(message_id: int) -> dict[str, Any]:
    """Sonnet-генерация ответа для уже сохранённой row (триггер «🤖 Предложить ответ»).

    Откладывается из `_process_incoming` чтобы оператор сначала видел тред целиком
    и решал вручную нужен ли draft (vs. ⏳ Ждать / 🏁 Завершить / 🚀 Автопилот).
    Делает то же что раньше делалось upfront в _process_incoming для manual flow:
      — собирает history / brief / lessons
      — вызывает claude.generate_reply (RU + DE + ru_translation + deal_brief)
      — генерит cheap Haiku-summary (если есть история)
      — обновляет row, status='pending'
    Возвращает {kind: 'generated' | 'error', message, cost_usd}.
    """
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"msg #{message_id} не найден"}
    if msg["status"] in ("sent", "sent_debug", "skipped", "skipped_sold", "not_sent_disabled"):
        return {"kind": "error",
                "message": f"msg #{message_id} в финальном состоянии ({msg['status']}), draft не нужен"}

    account = db.get_account(msg["account_id"])
    if not account:
        return {"kind": "error", "message": f"msg #{message_id}: аккаунт удалён"}

    thread_id = msg["gmail_thread_id"] or ""
    body = msg["de_client"] or ""
    ad_title = msg["ad_title"] or ""
    ad_price = msg["ad_price"] or ""
    ad_description = msg["ad_description"] or ""
    seller_name = msg["seller_name"] or ""
    ad_id_val = msg["ad_id"] if "ad_id" in msg.keys() else None

    # История (без текущего incoming)
    history = [
        h for h in claude.history_for(thread_id)
        if h.get("de_client") != body
    ]

    # Бриф объявления
    brief_text = ""
    if ad_id_val:
        cached = db.get_ad_brief(ad_id_val)
        if cached:
            try:
                key_facts = json.loads(cached["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                key_facts = {}
            brief_text = ad_brief.format_brief_for_claude(cached["brief_md"], key_facts)

    lessons_rows = db.find_relevant_lessons(
        ad_id=ad_id_val, account_id=account["id"], limit=5,
    )
    lessons_payload = [dict(r) for r in lessons_rows]

    try:
        reply = claude.generate_reply(
            de_client_text=body,
            ad_title=ad_title,
            ad_price=ad_price,
            ad_description=ad_description,
            seller_name=seller_name,
            history=history,
            brief_text=brief_text,
            lessons=lessons_payload,
        )
    except Exception as e:
        logger.exception("generate_draft Sonnet упал для msg=%s: %s", message_id, e)
        return {"kind": "error", "message": f"Sonnet упал: {e}"}

    extra_cost = 0.0
    summary_ru = None
    if history:
        full_history = [
            dict(r) for r in db.thread_history(thread_id)
            if r["id"] != message_id and not _row_get(r, "is_auto_ack")
        ]
        if full_history:
            try:
                sm = claude.summarize_thread(full_history)
                summary_ru = sm["summary_ru"]
                extra_cost += sm["cost_usd"]
            except Exception:
                logger.exception("summarize_thread fail msg=%s", message_id)

    deal_brief_json = (
        json.dumps(reply["deal_brief"], ensure_ascii=False) if reply.get("deal_brief") else None
    )

    prior_cost = msg["cost_usd"] or 0.0
    db.update_message(
        message_id,
        ru_answer=reply["ru_answer"],
        de_answer=reply["de_answer"],
        ru_translation=reply.get("ru_translation"),
        client_lang=reply.get("client_lang") or msg["client_lang"],
        tokens_in=(msg["tokens_in"] or 0) + (reply.get("tokens_in") or 0),
        tokens_out=(msg["tokens_out"] or 0) + (reply.get("tokens_out") or 0),
        cost_usd=prior_cost + (reply.get("cost_usd", 0.0) or 0.0) + extra_cost,
        history_summary_ru=summary_ru,
        deal_brief_json=deal_brief_json,
        status="pending",
    )
    total_cost = (reply.get("cost_usd", 0.0) or 0.0) + extra_cost
    logger.info(
        "Draft сгенерён для msg=%s (cost=$%.4f, status='pending')",
        message_id, total_cost,
    )
    return {"kind": "generated", "cost_usd": total_cost,
            "message": f"✅ Draft готов (стоило ${total_cost:.4f})"}


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


# ============================================================
# Бекап в Google Drive
# ============================================================

# ============================================================
# Напоминалки (Phase 2): клиент молчит N дней → пингнем оператора в TG
# ============================================================

def check_reminders() -> str:
    """Job: найти треды без ответа и предложить оператору пинг.

    Не отправляет пинг сам — только показывает оператору карточку с кнопками.
    Маркирует messages.reminder_state='offered' чтобы не предлагать дважды.
    """
    if not config.reminders_enabled():
        return "Напоминалки выключены в настройках"
    after_days = config.reminder_after_days()
    candidates = db.find_reminder_candidates(after_days=after_days)
    if not candidates:
        return f"Кандидатов на пинг нет (порог {after_days} дн.)"
    sent = 0
    failed = 0
    for row in candidates:
        try:
            telegram_bot.send_reminder_offer(row["id"], days_silent=after_days)
            db.update_message(row["id"], reminder_state="offered")
            sent += 1
        except Exception:
            logger.exception("Не удалось предложить пинг для msg=%s", row["id"])
            failed += 1
    return f"Предложено пингов: {sent}, ошибок: {failed} (порог {after_days} дн.)"


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


def run_backup() -> str:
    """Job: бекап SQLite в Google Drive + ротация старых."""
    try:
        result = backup.backup_and_rotate(keep=14)
        msg = f"OK: {result['backup'].get('name')}, удалено старых: {result['deleted_old']}"
        logger.info("Бекап %s", msg)
        return msg
    except Exception as e:
        logger.error("Бекап упал: %s", e)
        raise  # пусть слушатель event-а зафиксирует error


# ============================================================
# Сборка и запуск
# ============================================================

def build_scheduler() -> BackgroundScheduler:
    """Сконфигурировать APScheduler с тремя задачами."""
    sched = BackgroundScheduler(timezone="Europe/Berlin")

    poll_interval = config.gmail_poll_interval_sec()
    sched.add_job(
        poll_all_accounts,
        trigger="interval",
        seconds=poll_interval,
        id="poll_gmail",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        send_approved_replies,
        trigger="interval",
        seconds=60,
        id="send_replies",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    sched.add_job(
        run_backup,
        trigger="interval",
        hours=config.backup_interval_hours(),
        id="drive_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Напоминалки — раз в час проверяем (само правило по дням внутри),
    # но реальный effect только если включено в настройках.
    sched.add_job(
        check_reminders,
        trigger="interval",
        hours=1,
        id="check_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Daily summary — каждый час пробуем; функция сама пропускает если уже сегодня слала
    # или если вчера не было движений. Запускается в 9 утра локального времени.
    sched.add_job(
        daily_summary,
        trigger="cron",
        hour=9, minute=0,
        id="daily_summary",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Hourly мониторинг логов — alert в Telegram-группу если найдены ERROR/Traceback
    sched.add_job(
        monitor_errors_job,
        trigger="interval",
        hours=1,
        id="monitor_errors",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return sched


_active_sched: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    """Поднять scheduler в фоне. Возвращает экземпляр для shutdown()."""
    global _active_sched
    sched = build_scheduler()
    sched.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    sched.start()
    _active_sched = sched
    logger.info("Scheduler запущен: poll=%ds, send=60s, backup=%dh",
                config.gmail_poll_interval_sec(), config.backup_interval_hours())
    # Поднимаем deferred-rows которые могли остаться от прошлого процесса —
    # in-memory busy-флаги теряются при рестарте, но БД помнит.
    try:
        drained = drain_all_deferred()
        if drained:
            logger.info("Startup: подняли %d deferred-карточек", drained)
    except Exception:
        logger.exception("Startup drain_all_deferred fail")
    return sched


_REPORTED_ERROR_HASHES: set[str] = set()  # in-memory dedup; resets on restart


def monitor_errors_job() -> str:
    """Раз в час: сканировать journalctl за последний час, постить в Telegram digest
    если есть новые ERROR / Traceback / Exception (с дедупликацией по hash).

    Резет дедупа происходит при рестарте сервиса — не страшно: тот же баг
    максимум раз в день alert-нётся снова, и оператор поймёт что он не уходит.
    """
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "kleinanzeigen-bot", "--since", "1 hour ago",
             "--no-pager", "-p", "warning"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.warning("monitor_errors: journalctl failed: %s", e)
        return f"journalctl exception: {e}"
    if proc.returncode != 0:
        return f"journalctl rc={proc.returncode}: {proc.stderr[:200]}"

    skip_substrings = (
        "apscheduler.scheduler: Execution of job",  # «уже бежит» — норма при долгих POP-ах
        "Не пометили письмо прочитанным",  # часто игнорируемая ошибка mark_seen
        "Too many simultaneous connections",  # IMAP rate-limit, recovers автоматом
    )

    interesting: list[str] = []
    for line in proc.stdout.splitlines():
        if not any(token in line for token in (" ERROR ", "Traceback", "raise ", "Exception:")):
            continue
        if any(skip in line for skip in skip_substrings):
            continue
        interesting.append(line)

    if not interesting:
        return "ok, no issues"

    # Группируем по первым ~120 chars после "[<pid>]: " — это и есть «сигнатура» ошибки
    groups: dict[str, tuple[str, int]] = {}
    for line in interesting:
        idx = line.find("]: ")
        sig = line[idx + 3 : idx + 130] if idx >= 0 else line[:130]
        sig = sig.strip()
        h = hashlib.sha1(sig.encode("utf-8", errors="replace")).hexdigest()[:12]
        prev = groups.get(h, (sig, 0))
        groups[h] = (prev[0], prev[1] + 1)

    new_groups = {h: v for h, v in groups.items() if h not in _REPORTED_ERROR_HASHES}
    if not new_groups:
        return f"{len(interesting)} errors, all already reported"

    _REPORTED_ERROR_HASHES.update(new_groups.keys())

    # Telegram digest — только новые группы
    lines_out: list[str] = [
        f"<b>⚠️ Найдены ошибки за последний час</b>",
        f"<i>Всего: {len(interesting)}, новых типов: {len(new_groups)}</i>",
    ]
    for h, (sig, cnt) in list(new_groups.items())[:5]:
        # Telegram parse_mode=HTML — экранируем угловые скобки
        sig_html = (sig.replace("&", "&amp;")
                       .replace("<", "&lt;")
                       .replace(">", "&gt;"))
        lines_out.append(f"\n<code>×{cnt}</code> {sig_html}")
    if len(new_groups) > 5:
        lines_out.append(f"\n…и ещё {len(new_groups) - 5} типов в логах")

    try:
        telegram_bot._http_post("sendMessage", {
            "chat_id": config.telegram_chat_id(),
            "text": "\n".join(lines_out),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    except Exception as e:
        logger.exception("monitor_errors: telegram send failed")
        return f"telegram send failed: {e}"

    return f"posted {len(new_groups)} new error groups"


def _regen_context(msg: Any) -> tuple[list, str, list]:
    """Собрать контекст для регенерации: history (без текущего), brief_text, lessons.
    Используется во всех `regenerate_draft_*` обёртках.
    """
    thread_id = msg["gmail_thread_id"] or ""
    history = claude.history_for(thread_id) if thread_id else []
    history = [h for h in history if h.get("de_client") != msg["de_client"]]

    brief_text = ""
    ad_id_val = msg["ad_id"] if "ad_id" in msg.keys() else None
    if ad_id_val:
        bf = db.get_ad_brief(ad_id_val)
        if bf:
            try:
                kf = json.loads(bf["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                kf = {}
            brief_text = ad_brief.format_brief_for_claude(bf["brief_md"], kf)

    lessons = [dict(r) for r in db.find_relevant_lessons(
        ad_id=ad_id_val, account_id=msg["account_id"], limit=5,
    )]
    return history, brief_text, lessons


def _save_regen_result(message_id: int, msg: Any, result: dict[str, Any]) -> None:
    """Сохранить результат регенерации в БД (накапливая cost/tokens + deal_brief)."""
    prior_in = msg["tokens_in"] or 0
    prior_out = msg["tokens_out"] or 0
    prior_cost = msg["cost_usd"] or 0.0
    fields: dict[str, Any] = dict(
        ru_answer=result["ru_answer"],
        de_answer=result["client_answer"],
        ru_translation=result.get("ru_translation"),
        status="edited",
        tokens_in=prior_in + result["tokens_in"],
        tokens_out=prior_out + result["tokens_out"],
        cost_usd=prior_cost + result["cost_usd"],
    )
    if result.get("deal_brief"):
        fields["deal_brief_json"] = json.dumps(result["deal_brief"], ensure_ascii=False)
    db.update_message(message_id, **fields)


def regenerate_draft(message_id: int, strategy: str) -> dict[str, Any]:
    """Перегенерировать драфт для msg_id под preset-стратегию (fest/harsh/friend/short/regen)."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_strategy(
            msg, strategy=strategy,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft упал msg=%s strategy=%s", message_id, strategy)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"🔄 Драфт перегенерирован ({strategy})", "result": result}


def regenerate_draft_with_price(message_id: int, price_eur: float) -> dict[str, Any]:
    """Перегенерировать драфт под конкретную цену от оператора."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_price(
            msg, price_eur=price_eur,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_price упал msg=%s price=%s", message_id, price_eur)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"💸 Драфт перегенерирован под цену {price_eur}€", "result": result}


def regenerate_draft_with_instruction(message_id: int, instruction: str) -> dict[str, Any]:
    """Перегенерировать драфт по операторской свободной инструкции."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_instruction(
            msg, instruction=instruction,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_instruction упал msg=%s", message_id)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"📝 Драфт перегенерирован по инструкции", "result": result}


def daily_summary() -> Optional[str]:
    """Отправить вчерашнюю сводку в Telegram если были движения.

    Идемпотентна: сохраняет дату последней отправки в settings.last_daily_summary_date.
    Возвращает короткий summary для логов или None если ничего не отправлено.
    """
    today_date = datetime.now().strftime("%Y-%m-%d")
    last_date = config.get("last_daily_summary_date") or ""
    if last_date == today_date:
        return None  # уже отправлено сегодня

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # Берём всё что было создано вчера
    with db.get_conn() as conn:
        stats = conn.execute(
            """
            SELECT
                COUNT(*) AS total_in,
                SUM(CASE WHEN status IN ('sent','sent_debug') THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                COALESCE(SUM(cost_usd), 0) AS total_cost
            FROM messages
            WHERE direction='in' AND substr(created_at, 1, 10) = ?
            """,
            (yesterday,),
        ).fetchone()
        lessons_yday = conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE substr(created_at, 1, 10) = ?",
            (yesterday,),
        ).fetchone()

    total_in = stats["total_in"] or 0
    if total_in == 0 and (lessons_yday["c"] or 0) == 0:
        # Не было движений — молчим. Отметим дату чтобы повторно не пытаться.
        config.set("last_daily_summary_date", today_date)
        return "no activity yesterday"

    sent = stats["sent"] or 0
    pending = stats["pending"] or 0
    skipped = stats["skipped"] or 0
    cost = stats["total_cost"] or 0.0
    lessons_count = lessons_yday["c"] or 0

    text = (
        f"☀️ <b>Сводка за {yesterday}</b>\n"
        f"📥 inquiries: {total_in}\n"
        f"📤 отправлено: {sent}\n"
        f"⏳ ждёт: {pending}\n"
        f"❌ skip: {skipped}\n"
        f"💸 потрачено: ${cost:.4f}\n"
    )
    if lessons_count:
        text += f"🎓 уроков: +{lessons_count} (бот учится)\n"

    try:
        telegram_bot._http_post("sendMessage", {
            "chat_id": config.telegram_chat_id(),
            "text": text,
            "parse_mode": "HTML",
        })
        config.set("last_daily_summary_date", today_date)
        return f"sent: {total_in} in / {sent} out / ${cost:.4f}"
    except Exception as e:
        logger.exception("daily_summary не удалось послать")
        return f"failed: {e}"


def test_reprocess_latest(n: int = 1) -> str:
    """Принудительно переобработать последние N Kleinanzeigen-писем из inbox-а.

    Игнорирует UNSEEN (берёт даже прочитанные), удаляет существующие DB-записи
    с тем же Message-ID (если есть), не помечает в IMAP `Seen`. Полный pipeline:
    parse → claude → telegram. Только для отладки в режимах disabled/redirect.
    """
    accounts = db.list_accounts(only_active=True)
    if not accounts:
        return "Нет активных аккаунтов"
    from_filter = config.gmail_from_filter() or None
    processed = 0
    failed = 0
    for acc in accounts:
        try:
            emails = gmail.fetch_new(
                acc["gmail_email"], acc["gmail_app_password"],
                limit=n, from_filter=from_filter, include_seen=True,
            )
        except Exception as e:
            logger.error("test_reprocess: IMAP %s упал: %s", acc["id"], e)
            failed += 1
            continue
        for em in emails:
            try:
                ext_id = em.get("gmail_message_id") or ""
                if ext_id:
                    existing = db.find_by_gmail_message_id(ext_id)
                    if existing:
                        db.delete_message(existing["id"])
                        logger.info("test_reprocess: удалена старая DB-запись id=%s", existing["id"])
                _process_incoming(acc, em, force=True)
                processed += 1
            except Exception:
                logger.exception("test_reprocess: ошибка письма")
                failed += 1
    return f"Переобработано: {processed}, ошибок: {failed}"


def get_jobs_info() -> list[dict[str, Any]]:
    """Снимок состояния всех задач — для /api/status."""
    if _active_sched is None:
        return []
    out: list[dict[str, Any]] = []
    for job in _active_sched.get_jobs():
        status = JOB_STATUS.get(job.id, {})
        out.append({
            "id": job.id,
            "name": job.name or job.id,
            "next_run": job.next_run_time.strftime("%H:%M:%S") if job.next_run_time else None,
            "last_run": status.get("last_run"),
            "ok": status.get("ok"),
            "result": status.get("result"),
            "error": status.get("error"),
        })
    return out
