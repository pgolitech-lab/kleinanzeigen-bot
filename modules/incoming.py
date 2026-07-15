# Обработка входящих писем Gmail: парсинг, классификация, авто-ответы, autopilot.
# Выделено из scheduler.py. Публичный API: process_incoming (= _process_incoming).

import json
import logging
import random
import re
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

import config
import database as db
from modules import ad_brief, claude, gmail, parser, telegram_bot

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



def _row_get(row: Any, key: str) -> Any:
    """sqlite3.Row не имеет .get() — обернём чтобы безопасно читать новые колонки
    (на случай старых row объектов до миграции)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _skip_email(account: Any, email: dict[str, Any], reason: str) -> None:
    """DRY: пометить письмо `\\Seen` в Gmail + записать в `processed_messages`.

    Используется во всех skip-точках `_process_incoming` (noreply / junk-subject /
    purchase-side / classifier / max-age / sold / no-ad-ref / dedup). Запись в
    processed_messages нужна orphan-recovery — иначе recovery в цикле снимает
    Seen с тех же junk-писем и реклассифицирует.
    """
    msg_id = (email.get("gmail_message_id") or "").strip()
    if msg_id:
        try:
            db.mark_processed(msg_id, account["id"], reason)
        except Exception:
            logger.exception("mark_processed fail (msg_id=%s, reason=%s)", msg_id, reason)
    try:
        gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
    except Exception as e:
        logger.warning("mark_seen fail для UID=%s: %s", email.get("uid"), e)


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
        _skip_email(account, email, "skipped_dedup")
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
        _skip_email(account, email, "skipped_noreply")
        return

    # Skip системные письма Kleinanzeigen (saved-search alerts, истечение объявления, отзывы)
    if parser.is_junk_subject(subject):
        logger.info(
            "Skip junk system email: account=%s, subject=%r",
            account["gmail_email"], subject[:80],
        )
        _skip_email(account, email, "skipped_junk")
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
        _skip_email(account, email, "skipped_purchase_side")
        return

    # Контент-дедуп: тот же текст в том же треде от того же отправителя за окно —
    # это relay-повтор Kleinanzeigen с новым Message-ID (Message-ID-дедуп его не ловит, Bug 9).
    inbound_thread_dedup = (email.get("gmail_thread_id") or "").strip()
    from_email_dedup = (email.get("from_email") or "").strip()
    if (not force and inbound_thread_dedup and from_email_dedup
            and db.has_recent_identical_incoming(inbound_thread_dedup, from_email_dedup, body)):
        logger.info(
            "Skip: контент-дубликат incoming в треде %s от %s (relay-повтор)",
            inbound_thread_dedup, from_email_dedup,
        )
        _skip_email(account, email, "skipped_dedup")
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
                _skip_email(account, email, "skipped_classifier")
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
                        _skip_email(account, email, "skipped_max_age")
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
        _skip_email(account, email, "skipped_sold")
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
            # Помечаем seen + журналим, иначе письмо перечитывается и заново
            # прогоняется через платный Haiku-classifier на КАЖДОМ поле навсегда.
            _skip_email(account, email, "skipped_filter")
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
        _skip_email(account, email, "skipped_no_ad_ref")
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
        if db.should_reopen_closed_thread(inbound_thread):
            db.reopen_thread(inbound_thread)
            logger.info(
                "Reopened closed thread %s — клиент написал в архивный тред",
                inbound_thread,
            )
        else:
            # Тред закрыт как продажа — не воскрешаем pipeline/автопилот.
            # Письмо всё равно уйдёт оператору через send_for_review ниже.
            logger.info(
                "Sold thread %s получил новое письмо — оставляем закрытым, "
                "уйдёт оператору на ревью, автопилот не воскрешаем",
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
        try:
            telegram_bot.refresh_pipeline_for_active_chats()
        except Exception:
            logger.exception("refresh_pipeline after autopilot-incoming fail")
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

    try:
        # Обновить thread-state на всех ранее отправленных мини-карточках треда
        if thread_id:
            telegram_bot.broadcast_thread_state(thread_id)
    except Exception:
        logger.exception("broadcast_thread_state after incoming fail")

    try:
        telegram_bot.refresh_pipeline_for_active_chats()
    except Exception:
        logger.exception("refresh_pipeline after incoming fail")

    if not force:
        try:
            gmail.mark_seen(account["gmail_email"], account["gmail_app_password"], [email["uid"]])
        except Exception as e:
            logger.warning("Не пометили письмо прочитанным: %s", e)


# Публичный алиас для импорта из scheduler.py
process_incoming = _process_incoming
