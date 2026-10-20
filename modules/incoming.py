# Обработка входящих писем Gmail: парсинг, классификация, авто-ответы, autopilot.
# Выделено из scheduler.py. Публичный API: process_incoming (= _process_incoming).

import json
import logging
import random
import re
import zoneinfo
from datetime import datetime
from typing import Any, Optional

import config
import database as db
from modules import ad_brief, claude, guardrails, parser, telegram_bot
from modules.outgoing import _row_get, _send_reply

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


def _skip_email(account: Any, email: dict[str, Any], reason: str) -> None:
    """DRY: записать skip-нутое письмо в `processed_messages`.

    Используется во всех skip-точках `_process_incoming` (noreply / junk-subject /
    purchase-side / classifier / max-age / sold / no-ad-ref / dedup). Запись в
    processed_messages нужна orphan-recovery — иначе recovery в цикле снимает
    Seen с тех же junk-писем и реклассифицирует. Флаг `\\Seen` ставит вызывающий
    (`mail_queue`) одной пачкой после обработки — здесь IMAP не трогаем.
    """
    msg_id = (email.get("gmail_message_id") or "").strip()
    if msg_id:
        try:
            db.mark_processed(msg_id, account["id"], reason)
        except Exception:
            logger.exception("mark_processed fail (msg_id=%s, reason=%s)", msg_id, reason)


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
    # opt/llm отдаёт RU-зеркало сразу (ack_text_ru) — отдельный перевод не нужен;
    # фолбэк на translate_only, если ключа нет.
    ru_translation = (ack.get("ack_text_ru") or "").strip()
    if ru_translation:
        pass
    elif ack_lang == "ru":
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


def _deliver_review(message_id: int) -> None:
    """Карточка оператору с проверкой доставки.

    `telegram_bot.send_for_review` глотает ошибки fanout и возвращает None, если
    не дошла ни одна копия, — превращаем это в исключение, чтобы очередь
    оставила письмо UNSEEN и повторила, а не пометила «обработано» молча."""
    if not telegram_bot.send_for_review(message_id):
        raise RuntimeError(f"карточка msg={message_id} не доставлена ни одному оператору")


# Языки на кириллице: для них guardrails.outgoing_language_ok проверяем как для 'ru'
# (сама функция знает только ru — иначе украинский ответ украинцу = «провал»).
_CYRILLIC_LANGS = {"ru", "uk", "bg", "sr", "mk", "be"}

# Цена в тексте клиенту: «80 €», «80,- €», «1.200 EUR», «€ 80», «80 Euro», а также
# без валюты: «80,-», «für 80», «VB 80», «Preis 80», «letzter Preis: 80».
_NUM = r"(\d{1,3}(?:[.\s]\d{3})+|\d+)"
_PRICE_RE = re.compile(
    r"(?:€|EUR)\s*" + _NUM
    + r"|" + _NUM + r"(?:[,.](?:\d{1,2}|-))?\s*(?:€|EUR\b|Euro\b)"
    + r"|" + _NUM + r",-"
    + r"|\b(?:für|fuer|for|VB|Preis|price|Festpreis)\b\s*:?\s*" + _NUM + r"\b(?![.,]?\d|\s*(?:km|cm|mm|kg|Jahre|Stück|%))",
    re.IGNORECASE,
)


def _prices_in_text(text: str) -> list[float]:
    out: list[float] = []
    for m in _PRICE_RE.finditer(text or ""):
        raw = next((g for g in m.groups() if g), "")
        digits = re.sub(r"[.\s]", "", raw)
        if digits.isdigit():
            out.append(float(digits))
    return out


def _thread_client_lang(thread_id: str, exclude_id: int) -> Optional[str]:
    """Язык клиента по прежним входящим треда (самый частый) — независимо от
    того, что заявила модель в текущем ответе."""
    counts: dict[str, int] = {}
    for r in db.thread_history(thread_id) or []:
        if r["id"] == exclude_id or r["direction"] != "in":
            continue
        lang = (_row_get(r, "client_lang") or "").strip().lower()[:2]
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _autopilot_guard_reason(reply: dict[str, Any], autopilot_row: Any,
                            msg_id: Optional[int] = None) -> Optional[str]:
    """Детерминированные проверки авто-ответа ДО отправки («код владеет числами»).

    Возвращает причину остановки или None, если ответ можно слать:
      guard_language — текст не на языке клиента (напр. ушёл бы русский черновик)
                       или модель заявила язык, отличный от языка треда;
      guard_floor    — цена в брифе ИЛИ любая цена в тексте клиенту ниже floor.
    """
    text = reply.get("de_answer") or ""
    model_lang = (reply.get("client_lang") or "").strip().lower()[:2]
    thread_lang = _thread_client_lang(autopilot_row["gmail_thread_id"], msg_id or -1)
    if thread_lang and model_lang and thread_lang != model_lang:
        return "guard_language"
    lang = thread_lang or model_lang
    if not lang:
        return "guard_language"  # язык клиента неизвестен — вслепую не шлём
    # После слияния opt/llm — общий guard guardrails.outgoing_language_ok_llm
    # (эвристика по стоп-словам/буквам + Haiku при неуверенности, fail-closed).
    llm_guard = getattr(guardrails, "outgoing_language_ok_llm", None)
    if llm_guard is not None:
        try:
            ok = llm_guard(text, lang)
        except Exception:
            logger.exception("autopilot: language guard упал — ответ задерживаем")
            ok = False
        if not ok:
            return "guard_language"
    else:
        # Фолбэк до слияния: алфавит + независимая детекция Haiku (как lang-check в MA).
        if not guardrails.outgoing_language_ok(text, "ru" if lang in _CYRILLIC_LANGS else lang):
            return "guard_language"
        try:
            detected = (claude.detect_text_lang(text).get("lang") or "").strip().lower()[:2]
        except Exception:
            logger.exception("autopilot: detect_text_lang упал — ответ задерживаем")
            return "guard_language"
        if detected not in ("", "un") and detected != lang:
            return "guard_language"
    floor = autopilot_row["floor_price_eur"]
    if floor:
        price = (reply.get("deal_brief") or {}).get("negotiated_price_eur")
        if isinstance(price, (int, float)) and guardrails.floor_violation(price, floor):
            return "guard_floor"
        # Модель может написать в тексте одну цену, а в брифе другую — проверяем текст.
        if any(guardrails.floor_violation(p, floor) for p in _prices_in_text(text)):
            return "guard_floor"
    return None


# Почему guard задержал конкретный ответ — коротко для оператора.
_GUARD_HELD_TEXT = {
    "guard_language": "ответ не на языке клиента",
    "guard_floor": "цена ниже минимума",
    "guard_operator_draft": "в треде ждёт решения оператора прошлый черновик",
}


def _autopilot_held_notification(msg_id: int, thread_id: str, guard: str, floor: Any) -> None:
    """Уведомление: этот ответ автопилота задержан guard'ом, автопилот на треде включён."""
    why = _GUARD_HELD_TEXT.get(guard, guard)
    if guard == "guard_floor" and floor:
        why += f" ({floor:g} €)" if isinstance(floor, (int, float)) else f" ({floor} €)"
    text = (f"⏸ <b>Автопилот: ответ задержан</b> · #{msg_id}\n"
            f"Причина: {telegram_bot._html(why)}\n"
            f"Ответ не отправлен — проверьте карточку. Автопилот на треде остаётся включённым.")
    try:
        telegram_bot.notify(text, f"thread_{thread_id}", label="📋 Открыть тред")
    except Exception:
        logger.exception("autopilot held notification fail msg=%s", msg_id)


_SENT_STATUSES = db.SENT_STATUSES


def _thread_has_open_draft(thread_id: str, exclude_id: int, since: Optional[str]) -> bool:
    """Есть ли в треде прошлый ответ автопилота, задержанный guard'ом и ещё не
    разобранный оператором: входящий со status='new' и готовым de_answer после
    старта автопилота (ручной поток de_answer при приёме не генерит, а черновик,
    с которого оператор включил автопилот, старше started_at)."""
    for r in db.thread_history(thread_id) or []:
        if (r["id"] != exclude_id and r["direction"] == "in" and r["status"] == "new"
                and (r["de_answer"] or "").strip()
                and (not since or (r["created_at"] or "") >= since)):
            return True
    return False


def _autopilot_to_review(msg_id: int, thread_id: str, stop_reason: Optional[str]) -> None:
    """Отдать черновик автопилота оператору на ревью; с stop_reason — ещё и
    выключить автопилот треда и прислать уведомление об остановке."""
    if stop_reason:
        db.stop_thread_autopilot(thread_id, stop_reason)
    card_err: Optional[Exception] = None
    try:
        _deliver_review(msg_id)
    except Exception as e:
        logger.exception("send_for_review (autopilot) fail msg=%s", msg_id)
        card_err = e
    if stop_reason:
        try:
            telegram_bot.send_autopilot_stop_notification(msg_id, stop_reason)
        except Exception:
            logger.exception("autopilot stop notification fail msg=%s", msg_id)
    if card_err:
        # Очередь повторит письмо → ветка «довести до карточки» в _process_incoming.
        raise card_err


def _autopilot_dispatch(msg_id: int, autopilot_row: Any, reply: dict[str, Any]) -> None:
    """Обработать reply в автопилот-режиме.

    Автопилот, включённый на треде, отвечает клиенту сам (решение владельца).
    Порядок: stop-причины от модели и лимит (`autopilot_message_cap`) → guard'ы
    языка и floor (`guardrails`) → авто-отправка + counter + уведомление.
    Guard задерживает только этот конкретный ответ: карточка оператору +
    уведомление, автопилот на треде остаётся включённым.
    """
    thread_id = autopilot_row["gmail_thread_id"]
    notify_mode = autopilot_row["notify_mode"]
    current_count = autopilot_row["messages_sent"] or 0
    cap = config.autopilot_message_cap()

    stop_reason: Optional[str] = None
    if reply.get("should_stop"):
        stop_reason = (reply.get("stop_reason") or "stopped_by_sonnet").strip() or "stopped_by_sonnet"
    elif (current_count + 1) > cap:
        stop_reason = "limit"

    # Остановка без отправки — оператор разрулит через обычную review-карточку.
    # threat — исключение: отправляем предупреждение клиенту (если пройдёт guard).
    if stop_reason and stop_reason != "threat":
        _autopilot_to_review(msg_id, thread_id, stop_reason)
        return

    # Оператор работает с тредом или в треде висит незакрытый черновик (прошлый
    # задержанный ответ) — не отвечаем поверх него: клиент получил бы два ответа.
    guard: Optional[str] = None
    if telegram_bot.thread_is_busy(thread_id) or _thread_has_open_draft(
            thread_id, msg_id, _row_get(autopilot_row, "started_at")):
        guard = "guard_operator_draft"
    else:
        guard = _autopilot_guard_reason(reply, autopilot_row, msg_id)
        if stop_reason == "threat" and guard == "guard_floor":
            guard = None  # в предупреждении об угрозе цены нет — floor неприменим
    if guard:
        logger.warning("autopilot guard %s: msg=%s thread=%s — ответ задержан, карточка оператору",
                       guard, msg_id, thread_id)
        if stop_reason == "threat":
            # Предупреждение не ушло — стоп по stop-причине; уведомление об остановке
            # шлёт _autopilot_to_review, «автопилот включён» тут было бы неправдой.
            _autopilot_to_review(msg_id, thread_id, "threat")
            return
        _autopilot_to_review(msg_id, thread_id, None)
        _autopilot_held_notification(msg_id, thread_id, guard, autopilot_row["floor_price_eur"])
        return

    db.update_message(msg_id, status="approved", is_autopilot_reply=1)
    result = _send_reply(db.get_message(msg_id))
    sent_ok = result.get("kind") in ("sent", "skipped")

    if stop_reason == "threat":
        if not sent_ok:
            # Предупреждение не ушло — черновик оператору, а не только «стоп».
            logger.warning("autopilot threat: SMTP fail msg=%s — карточка оператору: %s",
                           msg_id, result.get("message"))
            _autopilot_to_review(msg_id, thread_id, "threat")
            return
        db.increment_autopilot_messages(thread_id)
        db.stop_thread_autopilot(thread_id, "threat")
        try:
            telegram_bot.send_autopilot_stop_notification(msg_id, "threat")
        except Exception:
            logger.exception("autopilot threat notification fail")
        return

    if sent_ok:
        new_count = db.increment_autopilot_messages(thread_id)
        if notify_mode == "notify":
            try:
                telegram_bot.send_autopilot_progress(msg_id, new_count)
            except Exception:
                logger.exception("autopilot progress notification fail")
        logger.info("autopilot reply sent: msg=%s thread=%s count=%d/%d",
                    msg_id, thread_id, new_count, cap)
    else:
        # SMTP fail — counter НЕ инкрементим, автопилот остаётся active.
        # Клиент ответа не получил — карточку оператору, чтобы не повисло молча.
        logger.warning("autopilot SMTP fail msg=%s — autopilot остаётся active, карточка оператору: %s",
                       msg_id, result.get("message"))
        _autopilot_to_review(msg_id, thread_id, None)


# Маркеры служебного web-relay футера Kleinanzeigen — по ним дёшево (SQL LIKE)
# находим письма, где в de_client остался служебный шаблон (регресс парсера или
# письма, сохранённые до фикса parser._strip_relay_template 2026-07-31).
_RELAY_MARKERS_SQL = (
    "de_client LIKE '%Beantworte diese Nachricht%' "
    "OR de_client LIKE '%Dein Team von Kleinanzeigen%' "
    "OR de_client LIKE '%Schütze dich vor Betrug%' "
    "OR de_client LIKE '%Zum Schutz unserer Nutzer%'"
)


def count_polluted_incoming() -> int:
    """Сколько входящих писем всё ещё содержат служебный шаблон Kleinanzeigen.

    Дёшево (один SQL COUNT по LIKE-маркерам футера). 0 при штатной работе.
    Используется в health-проверке приложения (`web/api_ma.py:/health`).
    """
    try:
        with db.get_conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM messages "
                f"WHERE direction='in' AND de_client IS NOT NULL AND de_client != '' "
                f"AND ({_RELAY_MARKERS_SQL})"
            ).fetchone()
        return int(row["c"] or 0)
    except Exception:
        logger.exception("count_polluted_incoming failed")
        return 0


def reclean_incoming_bodies(scan_limit: int = 500, translate_cap: int = 25) -> str:
    """Self-heal: перечистить входящие письма, где остался web-relay шаблон.

    Находит письма по служебным маркерам футера, прогоняет `clean_email_body`
    (с точным именем отправителя) и, если текст реально меняется, обновляет
    de_client + пере-переводит ru_client (best-effort Haiku, с потолком за прогон).

    Идемпотентна: после очистки следующий прогон не находит ничего (0 строк).
    Если у письма маркеры есть, но очистка НЕ помогла — это `stuck` (возможный
    регресс `parser._strip_relay_template`), логируем ERROR → ловит
    monitor_errors_job и алертит оператора.

    Возвращает короткий summary для JOB_STATUS / логов.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, de_client, buyer_display_name, client_lang FROM messages "
            f"WHERE direction='in' AND de_client IS NOT NULL AND de_client != '' "
            f"AND ({_RELAY_MARKERS_SQL}) ORDER BY id DESC LIMIT ?",
            (scan_limit,),
        ).fetchall()

    fixed = stuck = translated = 0
    for r in rows:
        cleaned = parser.clean_email_body(
            r["de_client"], sender_name=r["buyer_display_name"]
        )
        if cleaned == r["de_client"] or not cleaned.strip():
            stuck += 1
            continue
        kw: dict[str, Any] = {"de_client": cleaned}
        # Пере-перевод для операторского превью (ru_client), с потолком за прогон.
        if translated < translate_cap:
            try:
                tr = claude.detect_and_translate_to_ru(cleaned)
                ru = tr.get("translation_ru") or ""
                if ru:
                    kw["ru_client"] = ru
                    kw["client_lang"] = tr.get("lang") or r["client_lang"] or "de"
                translated += 1
            except Exception:
                logger.warning("reclean: перевод не удался для msg=%s", r["id"])
        db.update_message(r["id"], **kw)
        fixed += 1
        logger.info("reclean: msg=%s очищен от relay-шаблона Kleinanzeigen", r["id"])

    if stuck:
        # ERROR — чтобы monitor_errors_job поднял алерт: маркеры есть, а парсер
        # не справляется (формат письма изменился → нужно обновить парсер).
        logger.error(
            "reclean: %d писем с маркерами KZ-шаблона НЕ очистились — "
            "возможен регресс parser._strip_relay_template", stuck,
        )
    if fixed or stuck:
        logger.info(
            "reclean: fixed=%d, stuck=%d, translated=%d, scanned=%d",
            fixed, stuck, translated, len(rows),
        )
    return f"reclean: fixed={fixed}, stuck={stuck}, translated={translated}, scanned={len(rows)}"


def _already_processed(gmail_message_id: str) -> bool:
    """Есть ли письмо в журнале processed_messages (skip-нутые и т. п.)."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE gmail_message_id = ? LIMIT 1",
            (gmail_message_id,),
        ).fetchone()
    return bool(row)


def _known_thread_ad(thread_id: str, exclude_id: Optional[int] = None) -> Optional[dict[str, str]]:
    """Данные объявления из прошлых писем этого треда (если их уже парсил Playwright).

    Признак настоящего парсинга — непустое описание (fallback из темы письма
    даёт только title). None — тред новый или парсинг раньше не удался.
    """
    if not thread_id:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT ad_title, ad_price, ad_description, seller_name FROM messages "
            "WHERE gmail_thread_id = ? AND direction = 'in' AND id != ? "
            "AND COALESCE(ad_title, '') != '' AND COALESCE(ad_description, '') != '' "
            "ORDER BY id DESC LIMIT 1",
            (thread_id, exclude_id or -1),
        ).fetchone()
    if not row:
        return None
    return {
        "title": row["ad_title"] or "",
        "price": row["ad_price"] or "",
        "description": row["ad_description"] or "",
        "seller_name": row["seller_name"] or "",
    }


# Пометка в ru_client, если перевод входящего не удался (вместо пустоты и выдуманного языка).
TRANSLATION_FAILED_RU = "⚠️ перевод не удался — см. оригинал"

# Письмо старше стольких дней — «запоздало»: без автопилота и auto-ack.
LATE_AFTER_DAYS = 1.0


_MESSAGES_COLUMNS: set[str] = set()


def _messages_has_column(name: str) -> bool:
    """Есть ли колонка в messages (миграцию добавляет модуль db; до неё — не пишем).
    Кэшируем только положительный ответ, чтобы колонка подхватилась без рестарта."""
    if name in _MESSAGES_COLUMNS:
        return True
    try:
        with db.get_conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    except Exception:
        logger.exception("PRAGMA table_info(messages) упал")
        return False
    _MESSAGES_COLUMNS.update(cols)
    return name in cols


def _email_age_days(email: dict[str, Any]) -> Optional[float]:
    """Возраст письма в днях по Date-header (None — даты нет или не разобралась)."""
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    date_str = (email.get("date") or "").strip()
    if not date_str:
        return None
    try:
        msg_dt = parsedate_to_datetime(date_str)
    except (TypeError, ValueError, IndexError):
        logger.debug("parsedate failed: %r", date_str)
        return None
    if msg_dt is None:
        return None
    if msg_dt.tzinfo is None:
        # Некоторые отправители шлют наивные даты — считаем UTC.
        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - msg_dt).total_seconds() / 86400


def _notify_too_old(account: Any, email: dict[str, Any], age_days: float, max_age: int) -> None:
    h = telegram_bot._html
    text = (f"📭 <b>Старое письмо не обработано</b> ({age_days:.0f} дн., лимит {max_age})\n"
            f"Ящик: {h(account['gmail_email'])}\n"
            f"От: {h(email.get('from_name') or email.get('from_email') or '?')}\n"
            f"Тема: {h((email.get('subject') or '')[:120])}\n"
            f"Если нужно — ответьте в Kleinanzeigen вручную.")
    try:
        telegram_bot.notify(text)
    except Exception:
        logger.exception("Уведомление о старом письме не ушло")


def _notify_late(message_id: int, thread_id: str, age_days: float, autopilot_on: bool) -> None:
    text = (f"⏰ <b>Запоздалое письмо</b> · #{message_id} — пришло {age_days:.1f} дн. назад.\n"
            + ("Автопилот треда на него не отвечал" if autopilot_on else "Auto-ack на него не слался")
            + " — ответьте вручную.")
    try:
        telegram_bot.notify(text, f"thread_{thread_id}", label="📋 Открыть тред")
    except Exception:
        logger.exception("Уведомление о запоздалом письме не ушло msg=%s", message_id)


def _history_for_prompt(thread_id: str, exclude_id: int) -> list[dict[str, Any]]:
    """История треда для модели без текущего письма (по id строки, не по тексту).

    Источник — claude.history_for(thread_id, exclude_id=...) из opt/llm (до слияния —
    фолбэк на сборку здесь). Наш ответ оставляем, только если он реально ушёл
    клиенту: задержанный guard'ом или неотправленный черновик клиент не видел —
    модель не должна на него ссылаться. Пустой thread_id → без истории."""
    if not thread_id:
        return []
    try:
        rows = claude.history_for(thread_id, exclude_id=exclude_id)
    except TypeError:
        # До слияния opt/llm: history_for(thread_id) без exclude_id и без id/status.
        rows = [
            {"id": r["id"], "direction": r["direction"], "de_client": r["de_client"],
             "de_answer": r["de_answer"], "status": r["status"]}
            for r in db.thread_history(thread_id) if r["id"] != exclude_id
        ]
    out = []
    for h in rows:
        h = dict(h)
        if h.get("status") not in _SENT_STATUSES:
            h["de_answer"] = None
        out.append(h)
    return out


def _process_incoming(account: Any, email: dict[str, Any], force: bool = False) -> None:
    """Обработать одно входящее письмо: создать запись, спарсить, сгенерить ответ, послать оператору.

    force=True — режим отладочного реплея: дедуп пропускаем. Используется через
    /debug/reprocess.

    IMAP не трогает: флаг `\\Seen` ставит вызывающий (`modules/mail_queue.py`)
    после нормального возврата. Исключение наружу = письмо остаётся UNSEEN и
    будет повторено (с потолком попыток в очереди).
    """
    msg_id = email.get("gmail_message_id") or ""

    # Дедуп по Message-ID (только в обычном режиме)
    existing = db.find_by_gmail_message_id(msg_id) if (not force and msg_id) else None
    if existing:
        if (existing["direction"] == "in" and existing["status"] == "new"
                and not _row_get(existing, "telegram_message_id")):
            # Строка сохранена, но карточка оператору не дошла (исключение /
            # рестарт посреди обработки / Telegram недоступен). Признак доставки —
            # telegram_message_id: send_for_review пишет его только при успехе.
            # Просто status='new' НЕ признак: так висит и доставленная карточка,
            # которую оператор ещё не открыл (иначе — дубли карточек на каждом
            # повторном попадании письма). Ошибка пробрасывается → очередь повторит.
            if telegram_bot.thread_is_busy(existing["gmail_thread_id"]):
                db.update_message(existing["id"], status="deferred")
                logger.info("Письмо %s (msg=%s): тред занят — карточка отложена",
                            msg_id, existing["id"])
                return
            logger.warning("Письмо %s уже в БД (msg=%s), но не дошло до оператора — "
                           "довожу до карточки", msg_id, existing["id"])
            _deliver_review(existing["id"])
            return
        _skip_email(account, email, "skipped_dedup")
        return
    # Уже видели и сознательно skip-нули (junk / classifier / …), но \Seen тогда
    # не встал (сбой IMAP) — не гоняем повторно через платный классификатор.
    if not force and msg_id and _already_processed(msg_id):
        logger.info("Skip: письмо %s уже в processed_messages", msg_id)
        return

    raw_body = email.get("body") or ""
    body_html = email.get("body_html") or ""
    # Display-имя отправителя (для точного среза «Antwort von <имя>» в relay-шаблоне)
    buyer_display = _clean_display_name(email.get("from_name") or "") or None
    # Срезаем шаблонные заголовки/футеры Kleinanzeigen (в т.ч. web-relay
    # уведомления, где текст клиента завёрнут в служебный шаблон) — оставляем
    # только текст клиента.
    body = parser.clean_email_body(raw_body, sender_name=buyer_display)
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

    # Возраст письма по Date-header. Старше inquiry_max_age_days (по умолчанию 7) —
    # не обрабатываем, но оператору уведомление (раньше — тихий выброс). Старше
    # LATE_AFTER_DAYS — «запоздало»: карточка оператору, автопилот и auto-ack молчат
    # (отвечать через сутки «сейчас за рулём» — хуже, чем не отвечать).
    max_age = config.inquiry_max_age_days()
    age_days = _email_age_days(email)
    if max_age > 0 and age_days is not None and age_days > max_age:
        logger.info(
            "Skip: too old (%.1f дн., лимит %d), account=%s, subject=%r",
            age_days, max_age, account["gmail_email"], subject[:60],
        )
        _notify_too_old(account, email, age_days, max_age)
        _skip_email(account, email, "skipped_max_age")
        return
    late = age_days is not None and age_days > LATE_AFTER_DAYS

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

    # AI-классификатор (Haiku): финальный gate перед дорогими операциями.
    # Дешёвая модель решает «buyer-inquiry или системная рассылка».
    # Срабатывает только на то, что прошло ВСЕ бесплатные фильтры выше
    # (noreply / junk-subject / max-age / from / ссылка на объявление).
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
        if prior and not parser.is_system_message_body(body):
            skip_classifier = True
            logger.info(
                "Classifier bypass: thread %s уже имеет inquiry, follow-up принят без проверки",
                inbound_thread_id,
            )
        elif prior:
            logger.info(
                "Classifier NOT bypassed для thread %s — тело похоже на системное "
                "письмо, прогоняем Haiku несмотря на follow-up",
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

    # Проданное объявление — после классификатора: проверка бесплатная, но шлёт
    # уведомление оператору, а на системную рассылку его слать незачем.
    if ad_id and db.is_ad_sold(ad_id):
        # Объявление помечено проданным — не обрабатываем, не зовём Claude.
        # Тихая нотификация в Telegram чтобы оператор знал.
        try:
            telegram_bot._http_post("sendMessage", {
                "chat_id": config.telegram_chat_id(),
                "text": (
                    f"📭 <b>Inquiry на проданное объявление</b>\n"
                    f"От: {email.get('from_name') or email.get('from_email')}\n"
                    f"Объявление #{ad_id} помечено как продано — пропустил."
                ),
                "parse_mode": "HTML",
            })
        except Exception:
            logger.exception("Не удалось послать notification про sold")
        _skip_email(account, email, "skipped_sold")
        return

    # Сохраняем входящее СРАЗУ, до медленного Playwright-парсинга. Это:
    #  1) гарантирует что письмо не потеряется даже если parse упадёт
    #  2) даёт auto-ack возможность уйти за секунды (Playwright потом, ~10-30с)
    # buyer_display вычислен выше (нужен и для среза relay-шаблона).
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
    conv_id = (email.get("ka_conversation_id") or "").strip()
    if conv_id and _messages_has_column("ka_conversation_id"):
        add_kwargs["ka_conversation_id"] = conv_id
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
    if late and _row_get(account, "auto_ack_enabled"):
        logger.info("auto-ack пропущен: письмо запоздало (%.1f дн.) msg=%s", age_days, inserted_id)
    if _row_get(account, "auto_ack_enabled") and thread_id and not late:
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

    # Парсинг страницы Playwright — ПОСЛЕ auto-ack, потому что медленно (10–30 с,
    # целый Chromium). Для ответа в уже известном треде объявление берём из БД.
    ad = {"title": "", "price": "", "description": "", "seller_name": ""}
    known_ad = _known_thread_ad(thread_id, exclude_id=inserted_id)
    if known_ad:
        ad = known_ad
        logger.info("Объявление для thread=%s взято из БД, без Playwright", thread_id)
    elif ad_url:
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

    history = _history_for_prompt(email.get("gmail_thread_id") or "", inserted_id)

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
    if is_autopilot and late:
        # Запоздалое письмо — автопилот не отвечает, обычная карточка оператору.
        logger.info("autopilot пропущен: письмо запоздало (%.1f дн.) msg=%s", age_days, inserted_id)
        is_autopilot = False

    if is_autopilot:
        try:
            last_price = None
            for r in reversed(db.thread_history(ap_thread_id)):
                if r["status"] not in _SENT_STATUSES:
                    continue  # цена задержанного/неотправленного ответа — не «наша последняя»
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
            # Письмо клиента не должно потеряться: без ответа модели отдаём его
            # оператору обычной карточкой (автопилот остаётся активным).
            logger.exception("Autopilot Claude упал для msg=%s: %s", inserted_id, e)
            # Сбой доставки карточки пробрасываем — очередь повторит письмо.
            _deliver_review(inserted_id)
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
            # Карточка оператору; не дошла — пробрасываем, очередь повторит письмо.
            _deliver_review(inserted_id)
        return

    # ───── Manual flow: дешёвый Haiku-перевод incoming → ru_client (для оператора).
    # Sonnet draft (de_answer / ru_answer / deal_brief) НЕ генерим — оператор сам
    # тапнет «🤖 Предложить ответ» когда захочет (см. handler `propose`).
    try:
        tr = claude.detect_and_translate_to_ru(body)
        fields: dict[str, Any] = {"ru_client": tr.get("translation_ru") or TRANSLATION_FAILED_RU,
                                  "cost_usd": tr.get("cost_usd", 0.0)}
        if tr.get("lang"):
            fields["client_lang"] = tr["lang"]  # язык не выдумываем: нет — остаётся пустым
        db.update_message(inserted_id, **fields)
    except Exception:
        logger.exception("incoming RU-translation fail msg=%s", inserted_id)
        # Пометка в карточке вместо пустого перевода; client_lang не подставляем.
        db.update_message(inserted_id, ru_client=TRANSLATION_FAILED_RU)

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
        _deliver_review(inserted_id)
    except Exception as e:
        # Пробрасываем: очередь оставит письмо UNSEEN и повторит, а повтор
        # (status='new' без telegram_message_id) пойдёт веткой «довести до карточки» выше.
        logger.exception("Не удалось послать оператору msg=%s: %s", inserted_id, e)
        raise
    if late:
        _notify_late(inserted_id, thread_id, age_days, bool(autopilot and autopilot["active"]))


# Публичный алиас для импорта из scheduler.py
process_incoming = _process_incoming
