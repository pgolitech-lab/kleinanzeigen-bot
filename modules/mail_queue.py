# Очередь входящих писем: опрос IMAP отделён от обработки.
#
# Раньше poll-джоб (каждые 15 с) сам гонял _process_incoming — Haiku/Sonnet,
# Playwright (10–30 с), SMTP auto-ack — и проход длился дольше интервала:
# APScheduler пропускал ~37 % запусков, а все аккаунты ждали самое медленное письмо.
#
# Теперь:
#   poll-джоб  → gmail.fetch_new (только UNSEEN, без уже стоящих в очереди UID) → enqueue
#   1 worker   → _process_incoming по одному письму (FIFO) → пакетный UID STORE \Seen
#
# Состояние только в памяти. Письмо помечается \Seen лишь после обработки, поэтому
# при рестарте недообработанное остаётся UNSEEN и будет забрано заново (дедуп по
# Message-ID в _process_incoming не даст обработать дважды уже сохранённое, а
# строку status='new' без карточки — доведёт до оператора).

import logging
import queue
import threading
import time
from typing import Any, Optional

import database as db
from modules import gmail, telegram_bot
from modules.incoming import _process_incoming

logger = logging.getLogger(__name__)

# Паузы перед повтором письма, на котором _process_incoming упал исключением.
# По времени, а не по проходам опроса: короткий простой LLM / сети (минуты) не
# должен «сжигать» письмо. После последней попытки — сдаёмся: журнал
# processed_messages + \Seen + алерт оператору.
RETRY_DELAYS_SEC = (60, 300, 900, 3600)
MAX_ATTEMPTS = len(RETRY_DELAYS_SEC) + 1
# Сколько обработанных UID копить до принудительного сброса \Seen (сбрасываем
# и раньше — как только очередь опустела).
SEEN_FLUSH_BATCH = 20

_Q: "queue.Queue[tuple[dict[str, Any], dict[str, Any]]]" = queue.Queue()
_LOCK = threading.Lock()
# account_id → UID-ы в очереди / в обработке / ждущие \Seen / ждущие повтора.
# Пока UID здесь, fetch_new его не качает, а enqueue не ставит второй раз.
_INFLIGHT: dict[int, set[str]] = {}
_ATTEMPTS: dict[tuple[int, str], int] = {}
# (account_id, uid) → monotonic-время, после которого письмо можно забрать снова.
_RETRY_AT: dict[tuple[int, str], float] = {}
_worker: Optional[threading.Thread] = None
_stats: dict[str, Any] = {
    "processed": 0, "failed": 0, "gave_up": 0, "last_processed_at": None,
    "current_uid": None, "current_started_at": None,
}


def inflight_uids(account_id: int) -> set[str]:
    with _LOCK:
        return set(_INFLIGHT.get(account_id, ()))


def enqueue(account: Any, emails: list[dict[str, Any]]) -> int:
    """Поставить письма аккаунта в очередь (кроме уже стоящих). Возвращает сколько добавлено."""
    acc = dict(account)  # снимок строки — sqlite3.Row не переживает поток/закрытие
    added = 0
    with _LOCK:
        busy = _INFLIGHT.setdefault(acc["id"], set())
        for em in emails:
            uid = str(em.get("uid") or "")
            if not uid or uid in busy:
                continue
            busy.add(uid)
            _Q.put((acc, em))
            added += 1
    return added


def release_due_retries() -> int:
    """Отпустить письма, у которых истекла пауза перед повтором: следующий опрос
    заберёт их снова. Зовётся из poll-джоба. Возвращает число отпущенных."""
    now = time.monotonic()
    with _LOCK:
        due = [k for k, t in _RETRY_AT.items() if t <= now]
        for acc_id, uid in due:
            _RETRY_AT.pop((acc_id, uid), None)
            busy = _INFLIGHT.get(acc_id)
            if busy:
                busy.discard(uid)
    return len(due)


def status() -> dict[str, Any]:
    """Снимок для /health и /api/status. busy_sec — сколько обрабатывается текущее
    письмо (рост без конца = зависла обработка, очередь стоит)."""
    with _LOCK:
        inflight = sum(len(v) for v in _INFLIGHT.values())
        retrying = len(_RETRY_AT)
    started = _stats["current_started_at"]
    return {
        "queue_len": _Q.qsize(),
        "inflight": inflight,
        "retrying": retrying,
        "worker_alive": bool(_worker and _worker.is_alive()),
        "busy_sec": round(time.monotonic() - started, 1) if started else 0.0,
        **{k: v for k, v in _stats.items() if k != "current_started_at"},
    }


def _release(account_id: int, uids: list[str]) -> None:
    with _LOCK:
        busy = _INFLIGHT.get(account_id)
        if busy:
            busy.difference_update(uids)


def _flush_seen(pending: dict[int, tuple[dict[str, Any], list[str]]]) -> None:
    """Одной UID STORE-командой на аккаунт пометить обработанные письма \\Seen."""
    for acc_id, (acc, uids) in list(pending.items()):
        try:
            gmail._imap_retry(gmail.mark_seen, acc["gmail_email"], acc["gmail_app_password"], uids)
        except Exception as e:
            # Не страшно: письмо останется UNSEEN, следующий опрос заберёт его
            # снова, а дедуп по Message-ID в _process_incoming сразу его пропустит.
            logger.warning("mark_seen fail account=%s uids=%s: %s", acc_id, uids, e)
        finally:
            _release(acc_id, uids)
    pending.clear()


def _alert_gave_up(acc: dict[str, Any], em: dict[str, Any], why: str) -> None:
    """Оператору в Telegram: письмо не обработано и больше не будет повторяться."""
    h = telegram_bot._html
    text = (
        f"⚠️ <b>Письмо не обработано</b> ({h(why)})\n"
        f"Ящик: {h(acc.get('gmail_email') or '')}\n"
        f"От: {h(em.get('from_name') or em.get('from_email') or '?')}\n"
        f"Тема: {h((em.get('subject') or '')[:120])}\n"
        f"Помечено прочитанным — проверьте его в Gmail вручную."
    )
    try:
        telegram_bot.notify(text)
    except Exception:
        logger.exception("Алерт о необработанном письме не ушёл")


def _give_up(acc: dict[str, Any], em: dict[str, Any], why: str) -> None:
    _stats["gave_up"] += 1
    msg_id = (em.get("gmail_message_id") or "").strip()
    if msg_id:
        try:
            db.mark_processed(msg_id, acc["id"], "error_processing")
        except Exception:
            logger.exception("mark_processed(error_processing) fail uid=%s", em.get("uid"))
    _alert_gave_up(acc, em, why)


def _handle(acc: dict[str, Any], em: dict[str, Any]) -> bool:
    """Обработать одно письмо. True — можно ставить \\Seen; False — оставить UNSEEN на повтор."""
    uid = str(em.get("uid") or "")
    key = (acc["id"], uid)
    if em.get("_broken"):
        logger.error("Письмо uid=%s account=%s не разобралось (битый MIME) — пропускаем", uid, acc["id"])
        _give_up(acc, em, "не разобрался MIME")
        return True
    _stats["current_uid"] = uid
    _stats["current_started_at"] = time.monotonic()
    try:
        _process_incoming(acc, em)
        _ATTEMPTS.pop(key, None)
        _stats["processed"] += 1
        return True
    except Exception:
        attempts = _ATTEMPTS.get(key, 0) + 1
        _stats["failed"] += 1
        if attempts < MAX_ATTEMPTS:
            _ATTEMPTS[key] = attempts
            delay = RETRY_DELAYS_SEC[attempts - 1]
            with _LOCK:
                _RETRY_AT[key] = time.monotonic() + delay
            logger.exception("Ошибка обработки письма uid=%s account=%s (попытка %d/%d), повтор через %d с",
                             uid, acc["id"], attempts, MAX_ATTEMPTS, delay)
            return False
        _ATTEMPTS.pop(key, None)
        logger.exception("Письмо uid=%s account=%s упало %d раз — сдаёмся, помечаем прочитанным",
                         uid, acc["id"], attempts)
        _give_up(acc, em, f"{attempts} неудачных попыток")
        return True
    finally:
        _stats["last_processed_at"] = time.time()
        _stats["current_uid"] = None
        _stats["current_started_at"] = None


def _run() -> None:
    pending: dict[int, tuple[dict[str, Any], list[str]]] = {}
    n_pending = 0
    try:
        while True:
            try:
                acc, em = _Q.get(timeout=5)
            except queue.Empty:
                if pending:
                    _flush_seen(pending)
                    n_pending = 0
                continue
            uid = str(em.get("uid") or "")
            try:
                if _handle(acc, em):
                    pending.setdefault(acc["id"], (acc, []))[1].append(uid)
                    n_pending += 1
                # False: UID остаётся в _INFLIGHT до release_due_retries()
                if pending and (_Q.empty() or n_pending >= SEEN_FLUSH_BATCH):
                    _flush_seen(pending)
                    n_pending = 0
            except Exception:
                # Поток очереди не должен умирать ни при каких ошибках.
                logger.exception("mail_queue worker: неожиданная ошибка uid=%s", uid)
                _release(acc["id"], [uid])
            finally:
                _Q.task_done()
    finally:
        # Сюда попадаем только при BaseException (MemoryError, SystemExit…).
        logger.error("mail_queue worker завершился — будет перезапущен следующим опросом")


def start() -> None:
    """Запустить worker-поток (идемпотентно). Если прежний поток умер — его
    UID-ы в _INFLIGHT больше никто не освободит: сбрасываем их, письма заберутся
    заново (повторную обработку отсечёт дедуп по Message-ID)."""
    global _worker
    if _worker and _worker.is_alive():
        return
    if _worker is not None:
        logger.error("mail_queue: worker был мёртв — перезапуск, сброс in-flight UID")
        with _LOCK:
            _INFLIGHT.clear()
            _RETRY_AT.clear()
            # Очередь тоже сбрасываем: письма в ней заново придут опросом.
            while True:
                try:
                    _Q.get_nowait()
                    _Q.task_done()
                except queue.Empty:
                    break
    _worker = threading.Thread(target=_run, name="mail-queue", daemon=True)
    _worker.start()
    logger.info("mail_queue: worker запущен")
