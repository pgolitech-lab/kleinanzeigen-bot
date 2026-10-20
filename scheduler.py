# APScheduler: polling Gmail, отправка одобренных ответов, бекап в Drive.
# Все задачи sync, ошибки логируются, но не валят весь процесс.

import concurrent.futures
import hashlib
import logging
import re
import threading
import time
import zoneinfo
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
import database as db
from modules import gmail, mail_queue, scout, telegram_bot

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
    send_manual_compose, reset_stuck_sending,
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

# Общий пул потоков для IMAP-опроса (по потоку на аккаунт), живёт весь процесс —
# не создаём новый на каждый 15-секундный проход.
_IMAP_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="imap-poll")
# Бюджет времени прохода: дольше не ждём (IMAP_TIMEOUT=30 с + 1 ретрай). Аккаунт,
# чей прошлый опрос ещё висит, в следующем проходе пропускается (`_POLLING`).
POLL_BUDGET_SEC = 40
ORPHAN_SCAN_MINUTES = 20

_POLLING: set[int] = set()
_POLLING_LOCK = threading.Lock()
# last_ok_at — последний проход, где реально опрошен хотя бы один аккаунт.
# accounts[id] — {last_ok_at, fails_in_row, skipped_busy} для поиска «тихо сломанного» ящика.
POLL_STATE: dict[str, Any] = {"last_ok_at": None, "last_ok_ts": None, "last_pass_sec": None,
                              "last_errors": 0, "accounts": {}}
# Порог подряд идущих неудач опроса аккаунта (≈5 мин при 15 с), после которого ERROR
# (его подберёт monitor_errors_job и пришлёт в Telegram). Повторяется раз в ACCOUNT_FAIL_REPEAT.
ACCOUNT_FAIL_ALERT = 20
ACCOUNT_FAIL_REPEAT = 240
# Обработка одного письма дольше этого — ERROR «очередь стоит».
QUEUE_STUCK_SEC = 300
_stuck_reported_uid: Optional[str] = None


def _acc_state(acc_id: int) -> dict[str, Any]:
    return POLL_STATE["accounts"].setdefault(
        acc_id, {"last_ok_at": None, "fails_in_row": 0, "skipped_busy": 0})


def _poll_one_account(acc: Any, from_filter: Any) -> tuple[str, int]:
    """Забрать новые письма одного аккаунта в очередь обработки (без обработки).

    Возвращает (исход 'ok'|'fail'|'busy', поставлено_в_очередь). Запускается в пуле _IMAP_POOL.
    """
    acc_id = acc["id"]
    with _POLLING_LOCK:
        if acc_id in _POLLING:
            logger.info("IMAP-опрос аккаунта %s ещё идёт с прошлого прохода — пропуск", acc_id)
            return "busy", 0
        _POLLING.add(acc_id)
    try:
        new_emails = gmail._imap_retry(
            gmail.fetch_new,
            acc["gmail_email"], acc["gmail_app_password"],
            from_filter=from_filter,
            skip_uids=mail_queue.inflight_uids(acc_id),
        )
    except Exception as e:
        logger.warning("IMAP fetch для аккаунта %s упал: %s", acc_id, e)
        return "fail", 0
    finally:
        with _POLLING_LOCK:
            _POLLING.discard(acc_id)
    added = mail_queue.enqueue(acc, new_emails)
    if added:
        logger.info("Аккаунт %s: %d новых писем → очередь", acc["gmail_email"], added)
    return "ok", added


def _account_outcome(acc: Any, outcome: str) -> None:
    st = _acc_state(acc["id"])
    if outcome == "ok":
        st["last_ok_at"] = datetime.utcnow().isoformat()
        st["fails_in_row"] = 0
        return
    if outcome == "busy":
        st["skipped_busy"] += 1
    st["fails_in_row"] += 1
    n = st["fails_in_row"]
    if n == ACCOUNT_FAIL_ALERT or (n > ACCOUNT_FAIL_ALERT and n % ACCOUNT_FAIL_REPEAT == 0):
        logger.error("IMAP: аккаунт %s (%s) не опрашивается %d проходов подряд, последний успех %s",
                     acc["id"], acc["gmail_email"], n, st["last_ok_at"] or "никогда")


def _check_queue_stuck() -> None:
    global _stuck_reported_uid
    q = mail_queue.status()
    if q["busy_sec"] > QUEUE_STUCK_SEC and q["current_uid"] != _stuck_reported_uid:
        _stuck_reported_uid = q["current_uid"]
        logger.error("Очередь писем стоит: uid=%s обрабатывается %.0f с, в очереди %d",
                     q["current_uid"], q["busy_sec"], q["queue_len"])


def poll_all_accounts() -> str:
    """Job: опросить все активные аккаунты параллельно и поставить новые письма в очередь.

    Только IMAP (UID SEARCH + пакетный FETCH) — обработку (LLM, Playwright, SMTP)
    делает поток `mail_queue`, поэтому проход укладывается в секунды и не
    задерживает опрос остальных аккаунтов. Orphan-recovery — отдельный джоб.

    Возвращает короткое summary для отображения в /api/status.
    """
    # Поток очереди поднимаем и на паузе: уже забранные письма должны дообработаться.
    mail_queue.start()
    if config.polling_paused():
        return "⏸ Polling на паузе (через /pause)"
    mail_queue.release_due_retries()
    _check_queue_stuck()
    started = time.monotonic()
    accounts = db.list_accounts(only_active=True)
    from_filter = config.gmail_from_filter() or None
    futures = {_IMAP_POOL.submit(_poll_one_account, acc, from_filter): acc for acc in accounts}
    done, not_done = concurrent.futures.wait(futures, timeout=POLL_BUDGET_SEC)
    total_new = 0
    counts = {"ok": 0, "fail": 0, "busy": 0}
    for fut in done:
        try:
            outcome, new_c = fut.result()
        except Exception:
            logger.exception("poll_one_account future failed")
            outcome, new_c = "fail", 0
        counts[outcome] += 1
        total_new += new_c
        _account_outcome(futures[fut], outcome)
    for fut in not_done:
        counts["busy"] += 1
        _account_outcome(futures[fut], "busy")
    if not_done:
        logger.warning("IMAP-опрос: %d аккаунтов не уложились в %d с — дочитают в фоне",
                       len(not_done), POLL_BUDGET_SEC)
    POLL_STATE["last_pass_sec"] = round(time.monotonic() - started, 2)
    POLL_STATE["last_errors"] = counts["fail"] + counts["busy"]
    if counts["ok"]:
        POLL_STATE["last_ok_at"] = datetime.utcnow().isoformat()
        POLL_STATE["last_ok_ts"] = time.time()
    q = mail_queue.status()
    return (
        f"Аккаунтов: {len(accounts)}, новых писем: {total_new}, в очереди: {q['queue_len']}"
        + (f", IMAP-ошибок: {counts['fail']}" if counts["fail"] else "")
        + (f", не опрошено (висит прошлый опрос): {counts['busy']}" if counts["busy"] else "")
    )


def last_poll_ok_at() -> float | None:
    """Unix ts последнего прохода poll_all_accounts, где хотя бы один аккаунт
    опрошен без ошибки (None — с запуска ещё ни разу). Для /health → poll_age_sec."""
    return POLL_STATE.get("last_ok_ts")


def poll_status() -> dict[str, Any]:
    """Состояние почтового конвейера для /health: последний удачный опрос + очередь."""
    return {**POLL_STATE, "queue": mail_queue.status()}


def orphan_recovery_job() -> str:
    """Job: найти SEEN-письма, которые бот не сохранил и не журналил, и снять с них \\Seen.

    Раньше шёл в каждом 15-секундном проходе (×5 аккаунтов) — отдельные IMAP-сессии
    и половина таймаутов. Теперь раз в ORPHAN_SCAN_MINUTES + при старте.
    Набор известных Message-ID читается из БД после IMAP FETCH каждого ящика.
    """
    if config.polling_paused():
        return "⏸ Polling на паузе"
    accounts = db.list_accounts(only_active=True)
    from_filter = config.gmail_from_filter() or None
    # Известные Message-ID читаем ПОСЛЕ IMAP FETCH (callable): очередь ставит \Seen
    # только после коммита строки в БД, значит всё, что в IMAP уже SEEN, к моменту
    # чтения видно в БД. Набор, прочитанный до цикла, давал гонку: письмо,
    # обработанное за эти секунды, считалось сиротой и шло на повтор.
    total = failed = 0
    for acc in accounts:
        try:
            orphans = gmail._imap_retry(
                gmail.find_orphan_seen_uids,
                acc["gmail_email"], acc["gmail_app_password"],
                known_message_ids=lambda: db.known_message_ids_since(since_days=7),
                from_filter=from_filter,
                since_days=2,
            )
            if orphans:
                logger.warning(
                    "Orphan-recovery: account=%s найдено %d SEEN-сирот, "
                    "снимаю Seen → подхватятся следующим poll-ом",
                    acc["gmail_email"], len(orphans),
                )
                gmail._imap_retry(
                    gmail.unmark_seen,
                    acc["gmail_email"], acc["gmail_app_password"], orphans,
                )
                total += len(orphans)
        except Exception as e:
            failed += 1
            logger.warning("orphan-recovery для аккаунта %s не удался: %s", acc["id"], e)
    return f"orphan-recovery: аккаунтов {len(accounts)}, сирот {total}" + (
        f", ошибок {failed}" if failed else "")


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




def _fmt_scout_listing(item: dict) -> str:
    icon = "🚐" if item["kind"] == "car" else "🔧"
    title = telegram_bot._html(item.get("title") or "(без названия)")
    price = item.get("price_raw") or (
        f"{item['price_eur']:.0f} €" if item.get("price_eur") is not None else "цена?")
    url = item.get("url")
    label = f'<a href="{telegram_bot._html(url)}">{title}</a>' if url else title
    return f"{icon} {label} — {price}"


def _alert_scout_empty(summary: dict, n_queries: int) -> None:
    """0 объявлений по ВСЕМ запросам — почти наверняка поломка (сменилась вёрстка /
    блокировка), а не пустой рынок. С 01.09.2026 scout так месяц молчал незамеченным.
    Алерт не чаще раза в сутки."""
    today = datetime.now().strftime("%Y-%m-%d")
    if config.get("scout_empty_alert_date") == today:
        return
    config.set("scout_empty_alert_date", today)
    errors = summary.get("errors") or []
    text = (
        f"⚠️ <b>Scout: 0 объявлений по всем {n_queries} запросам</b>\n"
        f"Похоже, Kleinanzeigen сменил вёрстку или блокирует запросы — "
        f"рынок не обновляется. Нужна проверка modules/scout.py."
    )
    if errors:
        text += "\n\nОшибки:\n" + "\n".join(
            telegram_bot._html(e[:200]) for e in errors[:3])
    try:
        telegram_bot.notify(text)
    except Exception:
        logger.exception("scout: empty-alert notify fail")


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
    if summary["total_seen"] == 0:
        _alert_scout_empty(summary, len(queries))
    if summary["cars_new"] or summary["parts_new"]:
        new_listings = summary.get("new_listings") or []
        # Отдельные секции и отдельный кап на машины и запчасти (сиденья/скамейки/рельсы) —
        # иначе при наплыве новых машин запчасти просто не попадают в общий топ-N карточек.
        cars = [it for it in new_listings if it["kind"] == "car"]
        parts = [it for it in new_listings if it["kind"] != "car"]
        text = (
            f"🔎 <b>Рынок обновился</b>\n"
            f"🚐 новых машин: {summary['cars_new']}\n"
            f"🔧 новых запчастей: {summary['parts_new']}\n"
        )
        for label_, items in (("🚐 Машины", cars), ("🔧 Запчасти", parts)):
            if not items:
                continue
            shown = items[:SCOUT_NOTIFY_MAX_PER_KIND]
            extra = len(items) - len(shown)
            text += f"\n<b>{label_}</b>\n" + "\n".join(_fmt_scout_listing(it) for it in shown)
            if extra > 0:
                text += f"\n… и ещё {extra}"
        telegram_bot.notify(text, "scout", label="🔎 Открыть Рынок")
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

    # Мониторинг ошибок процесса — alert в Telegram если были ERROR/Traceback
    sched.add_job(
        monitor_errors_job,
        trigger="interval",
        minutes=MONITOR_ERRORS_MINUTES,
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

    # Orphan-recovery — раз в ORPHAN_SCAN_MINUTES (+ первый прогон через минуту
    # после старта), а не в каждом 15-секундном проходе опроса.
    sched.add_job(
        orphan_recovery_job,
        trigger="interval",
        minutes=ORPHAN_SCAN_MINUTES,
        next_run_time=datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")) + timedelta(minutes=1),
        id="orphan_recovery",
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

    # Проверка входа в подписку Claude (LLM) — раз в 30 мин, алерт оператору.
    # Функция живёт в ветке opt/api: через getattr, чтобы ветки сливались в любом порядке.
    llm_login_job = getattr(telegram_bot, "check_llm_login_job", None)
    if llm_login_job is not None:
        sched.add_job(
            llm_login_job,
            trigger="interval",
            minutes=30,
            # первая проверка через 2 мин после старта, а не через 30
            next_run_time=datetime.now() + timedelta(minutes=2),
            id="check_llm_login",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.warning("telegram_bot.check_llm_login_job нет — проверка входа в LLM не зарегистрирована")

    return sched


_active_sched: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    """Поднять scheduler в фоне. Возвращает экземпляр для shutdown()."""
    global _active_sched
    install_error_collector()
    # До старта джобов: строки, упавшие посреди SMTP, — в error_send_failed
    # (без переотправки — письмо могло уйти). Иначе висят в 'sending' вечно.
    try:
        reset_stuck_sending()
    except Exception:
        logger.exception("Startup reset_stuck_sending fail")
    mail_queue.start()
    sched = build_scheduler()
    sched.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    sched.start()
    _active_sched = sched
    logger.info("Scheduler запущен: poll=%ds, send=60s (бэкап — kab-backup.timer)",
                config.gmail_poll_interval_sec())
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


# ---- Мониторинг ошибок -------------------------------------------------------
# Раньше job читал `journalctl -p warning`, а Python-логи идут в журнал с
# приоритетом info — фильтр отрезал всё, алерт не сработал ни разу. Теперь
# ERROR/CRITICAL ловит свой logging-handler прямо в процессе (не зависит ни от
# journald, ни от кольцевого log_buffer, который за час вытесняется INFO-шумом).

ERROR_REALERT_SEC = 24 * 3600  # тот же тип ошибки — повторно не чаще раза в сутки
MONITOR_ERRORS_MINUTES = 10
_ERROR_SKIP_SUBSTRINGS = (
    "Execution of job",  # apscheduler «уже бежит»
    "Too many simultaneous connections",  # IMAP rate-limit, recovers автоматом
)
_ERRORS: "deque[tuple[float, str, str]]" = deque(maxlen=1000)  # (ts, сигнатура, пример)
_ERRORS_LOCK = threading.Lock()
_REPORTED_ERROR_HASHES: dict[str, float] = {}  # hash → когда алертили; in-memory


def _error_signature(record: logging.LogRecord) -> tuple[str, str]:
    """(сигнатура для дедупа, текст для алерта). Цифры в сигнатуре схлопнуты —
    «msg=123» и «msg=456» один тип ошибки."""
    try:
        msg = record.getMessage()
    except Exception:
        msg = str(record.msg)
    first = (msg.splitlines() or [""])[0][:200]
    exc = ""
    if record.exc_info and record.exc_info[0] is not None:
        exc = f"{record.exc_info[0].__name__}: {str(record.exc_info[1])[:150]}"
    text = f"{record.name}: {first}" + (f" — {exc}" if exc else "")
    sig = re.sub(r"\d+", "N", f"{record.name}|{first[:120]}|{exc.split(':')[0]}")
    return sig, text


class _ErrorCollector(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            sig, text = _error_signature(record)
            if any(skip in text for skip in _ERROR_SKIP_SUBSTRINGS):
                return
            with _ERRORS_LOCK:
                _ERRORS.append((record.created, sig, text))
        except Exception:
            pass


def install_error_collector() -> None:
    """Повесить сборщик ERROR на root logger (идемпотентно) + логировать
    необработанные исключения потоков (иначе Traceback уходит только в stderr)."""
    root = logging.getLogger()
    if not any(isinstance(h, _ErrorCollector) for h in root.handlers):
        h = _ErrorCollector(level=logging.ERROR)
        root.addHandler(h)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("threading").error(
            "Необработанное исключение в потоке %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def monitor_errors_job() -> str:
    """Каждые MONITOR_ERRORS_MINUTES: новые ERROR/Traceback из процесса → digest в
    Telegram. Дедуп по сигнатуре: тот же тип ошибки — не чаще раза в сутки."""
    with _ERRORS_LOCK:
        items = list(_ERRORS)
        _ERRORS.clear()
    if not items:
        return "ok, no issues"

    groups: dict[str, list] = {}  # hash → [пример, счётчик]
    for _ts, sig, text in items:
        h = hashlib.sha1(sig.encode("utf-8", errors="replace")).hexdigest()[:12]
        g = groups.setdefault(h, [text, 0])
        g[1] += 1

    now = time.time()
    new_groups = {h: v for h, v in groups.items()
                  if now - _REPORTED_ERROR_HASHES.get(h, 0) > ERROR_REALERT_SEC}
    if not new_groups:
        return f"{len(items)} errors, all already reported"

    h_ = telegram_bot._html
    lines_out: list[str] = [
        "<b>⚠️ Ошибки в работе бота</b>",
        f"<i>Всего: {len(items)}, новых типов: {len(new_groups)}</i>",
    ]
    for _h, (text, cnt) in list(new_groups.items())[:5]:
        lines_out.append(f"\n<code>×{cnt}</code> {h_(text[:300])}")
    if len(new_groups) > 5:
        lines_out.append(f"\n…и ещё {len(new_groups) - 5} типов в логах")

    try:
        telegram_bot.notify("\n".join(lines_out))
    except Exception as e:
        # Не помечаем отправленными — попробуем в следующий раз. warning, а не
        # exception: ERROR отсюда снова попал бы в сборщик и зациклил алерт.
        logger.warning("monitor_errors: telegram send failed: %s", e)
        with _ERRORS_LOCK:
            _ERRORS.extend(items)
        return f"telegram send failed: {e}"
    for h in new_groups:
        _REPORTED_ERROR_HASHES[h] = now
    return f"posted {len(new_groups)} new error groups"


def _day_after(day: str) -> str:
    """'YYYY-MM-DD' → следующий день (верхняя граница диапазона по created_at, по индексу)."""
    from datetime import date, timedelta
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


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
            WHERE direction='in' AND created_at >= ? AND created_at < ?
            """,
            (yesterday, _day_after(yesterday)),
        ).fetchone()
        lessons_yday = conn.execute(
            "SELECT COUNT(*) AS c FROM lessons WHERE created_at >= ? AND created_at < ?",
            (yesterday, _day_after(yesterday)),
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
