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
from apscheduler.triggers.interval import IntervalTrigger

import config
import database as db
from modules import ad_brief, backup, claude, gmail, parser, scout, telegram_bot

logger = logging.getLogger(__name__)
from modules.incoming import (  # noqa: F401
    AUTO_ACK_EXCUSES, _clean_display_name,
    _row_get, _skip_email, _ack_already_sent_for_thread,
    _send_auto_ack, _autopilot_dispatch, _process_incoming,
    process_incoming, reclean_incoming_bodies, count_polluted_incoming,
)

from modules.drafts import (  # noqa: F401
    generate_draft_for_msg,
    _regen_context, _save_regen_result,
    regenerate_draft, regenerate_draft_with_price, regenerate_draft_with_instruction,
)
from modules.outgoing import (  # noqa: F401
    drain_deferred_thread, drain_all_deferred,
    _send_reply, send_one, send_approved_replies,
    send_followup_ping, send_manual_compose,
)

JOB_STATUS: dict[str, dict[str, Any]] = {}

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

def _poll_one_account(acc: Any, from_filter: Any) -> tuple[int, int, int]:
    """Опросить один аккаунт: fetch + process + orphan-recovery.
    
    Возвращает (new_count, orphans_count, failed_flag).
    Запускается в отдельном потоке из poll_all_accounts.
    """
    new_count = orphans_count = failed = 0
    try:
        new_emails = gmail._imap_retry(
            gmail.fetch_new,
            acc["gmail_email"], acc["gmail_app_password"],
            from_filter=from_filter,
        )
    except Exception as e:
        logger.error("IMAP fetch для аккаунта %s упал: %s", acc["id"], e)
        return 0, 0, 1
    if new_emails:
        logger.info("Аккаунт %s: %d новых писем", acc["gmail_email"], len(new_emails))
    new_count = len(new_emails)
    for em in new_emails:
        try:
            _process_incoming(acc, em)
        except Exception:
            logger.exception("Ошибка обработки письма")
    # Orphan-recovery (best-effort)
    try:
        known = db.known_message_ids_since(since_days=3)
        orphans = gmail._imap_retry(
            gmail.find_orphan_seen_uids,
            acc["gmail_email"], acc["gmail_app_password"],
            known_message_ids=known,
            from_filter=from_filter,
            since_days=2,
        )
        if orphans:
            logger.warning(
                "Orphan-recovery: account=%s найдено %d SEEN-сирот, "
                "снимаю Seen → подхватятся следующим poll-ом",
                acc["gmail_email"], len(orphans),
            )
            gmail.unmark_seen(
                acc["gmail_email"], acc["gmail_app_password"], orphans,
            )
            orphans_count = len(orphans)
    except Exception:
        logger.exception("orphan-recovery упал для аккаунта %s", acc["id"])
    return new_count, orphans_count, failed


def poll_all_accounts() -> str:
    """Job: пройти по всем активным аккаунтам параллельно и обработать новые письма.

    Аккаунты опрашиваются в параллельных потоках — максимальное время цикла
    равно самому медленному аккаунту (не сумме). Каждый поток: fetch_new →
    _process_incoming → orphan-recovery.

    Возвращает короткое summary для отображения в /api/status.
    """
    import concurrent.futures
    if config.polling_paused():
        return "⏸ Polling на паузе (через /pause)"
    accounts = db.list_accounts(only_active=True)
    from_filter = config.gmail_from_filter() or None
    total_new = total_orphans = total_failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(accounts) or 1) as ex:
        futures = {ex.submit(_poll_one_account, acc, from_filter): acc for acc in accounts}
        for fut in concurrent.futures.as_completed(futures):
            try:
                new_c, orphans_c, fail_c = fut.result()
                total_new += new_c
                total_orphans += orphans_c
                total_failed += fail_c
            except Exception:
                logger.exception("poll_one_account future failed")
                total_failed += 1
    return (
        f"Аккаунтов: {len(accounts)}, новых писем: {total_new}"
        + (f", IMAP-ошибок: {total_failed}" if total_failed else "")
        + (f", сирот восстановлено: {total_orphans}" if total_orphans else "")
    )


# ============================================================
# Отправка одобренных оператором ответов
# ============================================================

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


def run_backup() -> str:
    """Job: бекап SQLite в Google Drive + ротация старых."""
    # Ключ сервис-аккаунта не задан → бекап просто выключен. Раньше job падал с
    # ERROR каждый день, и hourly-мониторинг слал алерт в Telegram (2026-07-21).
    if not backup.is_configured():
        logger.info("Бекап пропущен: Google Drive не настроен "
                    "(settings → google_drive_credentials_json)")
        return "backup: not configured, skipped"
    try:
        result = backup.backup_and_rotate(keep=14)
        msg = f"OK: {result['backup'].get('name')}, удалено старых: {result['deleted_old']}"
        logger.info("Бекап %s", msg)
        return msg
    except Exception as e:
        logger.error("Бекап упал: %s", e)
        raise  # пусть слушатель event-а зафиксирует error


_SCOUT_PART_TYPE_RU = {"seat": "сиденье", "bench": "скамейка", "rail": "рельсы", "other": "деталь"}
_SCOUT_COND_RU = {"neu": "новое", "gebraucht": "б/у"}
_SCOUT_FUEL_RU = {"electric": "эл", "diesel": "дизель", "petrol": "бензин", "hybrid": "гибрид"}
_SCOUT_GB_RU = {"automatik": "АКПП", "manuell": "МКПП"}


def _fmt_scout_listing(item: dict) -> str:
    """Одна строка дайджеста: заголовок-ссылка, цена, локация/дата, специфика.

    Исчерпывающая инфа (требование оператора) — не только «цена + ссылка»,
    но и локация, дата публикации и ключевые атрибуты (год/пробег/тип детали/сост.).
    """
    icon = "🚐" if item["kind"] == "car" else "🔧"
    title = telegram_bot._html(item.get("title") or "(без названия)")
    price = item.get("price_raw") or (
        f"{item['price_eur']:.0f} €" if item.get("price_eur") is not None else "цена?")
    url = item.get("url")
    label = f'<a href="{telegram_bot._html(url)}">{title}</a>' if url else title

    bits: list[str] = []
    loc = " ".join(x for x in (item.get("plz"), item.get("city")) if x)
    if loc:
        bits.append(f"📍{loc}")
    if item.get("posted_raw"):
        bits.append(f"🕓{item['posted_raw']}")
    specs: list[str] = []
    if item["kind"] == "car":
        if item.get("year"):
            specs.append(str(item["year"]))
        if item.get("mileage_km"):
            specs.append(f"{int(item['mileage_km']):,}".replace(",", ".") + " км")
        if item.get("fuel"):
            specs.append(_SCOUT_FUEL_RU.get(item["fuel"], item["fuel"]))
        if item.get("gearbox"):
            specs.append(_SCOUT_GB_RU.get(item["gearbox"], item["gearbox"]))
        if item.get("model_family"):
            specs.append(item["model_family"])
    else:
        if item.get("part_type"):
            specs.append(_SCOUT_PART_TYPE_RU.get(item["part_type"], item["part_type"]))
        if item.get("condition"):
            specs.append(_SCOUT_COND_RU.get(item["condition"], item["condition"]))
        if item.get("year"):
            specs.append(str(item["year"]))
    if specs:
        bits.append(" · ".join(specs))
    if item.get("shipping"):
        bits.append("📦")
    meta = ("\n   " + " · ".join(bits)) if bits else ""
    return f"{icon} {label} — {price}{meta}"


def scout_job() -> str:
    """Job: авто-прогон разведки рынка (если включён в настройках)."""
    if not config.scout_auto_enabled():
        return "scout: auto disabled"
    queries = db.list_scout_queries(only_enabled=True)
    if not queries:
        return "scout: нет активных запросов"
    summary = scout.run_scout(page_delay_sec=config.scout_page_delay_sec())
    msg = (f"scout: запросов {summary['ran']}, найдено {summary['total_seen']}, "
           f"новых машин {summary['cars_new']}, новых запчастей {summary['parts_new']}")
    if summary["errors"]:
        msg += f", ошибок {len(summary['errors'])}"
    logger.info(msg)
    if summary["cars_new"] or summary["parts_new"]:
        new_listings = summary.get("new_listings") or []
        # Запчасти (сиденья/скамейки/рельсы) — приоритет оператора, секция идёт
        # первой; машины — тоже важны, второй секцией. Полный список БЕЗ урезания
        # (требование: «исчерпывающая информация» по каждому новому объявлению) —
        # длинный дайджест чанкуется под лимит Telegram, а не обрезается.
        cars = [it for it in new_listings if it["kind"] == "car"]
        parts = [it for it in new_listings if it["kind"] != "car"]
        lines = [
            f"🔎 <b>Рынок обновился</b>\n"
            f"🔧 новых запчастей: {summary['parts_new']}\n"
            f"🚐 новых машин: {summary['cars_new']}"
        ]
        for label_, items in (("🔧 Запчасти", parts), ("🚐 Машины", cars)):
            if not items:
                continue
            lines.append(f"\n<b>{label_}</b>")
            lines.extend(_fmt_scout_listing(it) for it in items)
        chunks = telegram_bot.split_lines_to_chunks(lines)
        telegram_bot.notify_chunks(chunks, "scout", label="🔎 Открыть Рынок")
    return msg


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

    # Разведка рынка — авто-прогон по интервалу (само no-op если выключено в настройках).
    # Якорим старт на 08:59 по Берлину (за минуту до daily_summary в 9:00) — иначе
    # интервал-триггер без явного start_date считает от момента рестарта сервиса,
    # и время прогона "плывёт" на любой удобный процессу час (напр. 5 утра после
    # ночного рестарта).
    berlin_tz = zoneinfo.ZoneInfo("Europe/Berlin")
    scout_anchor = datetime.now(berlin_tz).replace(hour=8, minute=59, second=0, microsecond=0)
    if scout_anchor <= datetime.now(berlin_tz):
        scout_anchor += timedelta(days=1)
    sched.add_job(
        scout_job,
        trigger=IntervalTrigger(
            hours=max(1, config.scout_interval_hours()),
            start_date=scout_anchor,
            timezone=berlin_tz,
        ),
        id="market_scout",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Страховка от застрявших deferred-сообщений (когда operator-lock истёк без release).
    # При нормальной работе не делает ничего (drain_deferred_thread проверяет busy-флаг).
    sched.add_job(
        drain_all_deferred,
        trigger="interval",
        minutes=5,
        id="drain_deferred",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Self-heal: перечистка входящих, где остался служебный web-relay шаблон
    # Kleinanzeigen (см. incoming.reclean_incoming_bodies). При штатной работе
    # находит 0 строк. Раз в 30 минут — редкий кейс, торопиться некуда.
    sched.add_job(
        reclean_incoming_bodies,
        trigger="interval",
        minutes=30,
        id="reclean_bodies",
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
    # Разово чистим backlog писем со служебным relay-шаблоном (письма до фикса
    # парсера или после изменения формата). Дальше это делает периодический job.
    try:
        logger.info("Startup reclean: %s", reclean_incoming_bodies())
    except Exception:
        logger.exception("Startup reclean_incoming_bodies fail")
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

    scout_stats = db.scout_daily_stats(yesterday)
    scout_new = scout_stats["new"]
    scout_removed = scout_stats["removed"]
    scout_new_cars = scout_new.get("car", 0)
    scout_new_parts = scout_new.get("part", 0)
    scout_sold_cars = scout_removed.get("car", 0)
    scout_sold_parts = scout_removed.get("part", 0)
    has_scout_activity = any(
        (scout_new_cars, scout_new_parts, scout_sold_cars, scout_sold_parts))

    total_in = stats["total_in"] or 0
    if total_in == 0 and (lessons_yday["c"] or 0) == 0 and not has_scout_activity:
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
    if has_scout_activity:
        text += (
            f"🔎 <b>Рынок</b>: +{scout_new_cars} машин, +{scout_new_parts} запчастей"
            f" · продано/снято: {scout_sold_cars} машин, {scout_sold_parts} запчастей\n"
        )

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
