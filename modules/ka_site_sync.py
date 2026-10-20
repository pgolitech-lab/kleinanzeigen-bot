# Импорт НАШИХ реплик с сайта Kleinanzeigen в БД (ответы владельца из приложения KZ).
#
# Поток sync_unanswered():
#   1. треды, где последним говорил покупатель (pipeline_threads.last_event_kind='in')
#      и событие не старше max_age_days — свежие первыми, не больше limit;
#   2. у треда нет ka_conversation_id (письма до 6437599) → дочитываем заголовок
#      X-Conversation-ID из Gmail по gmail_message_id (IMAP read-only, PEEK) и сохраняем;
#   3. по аккаунтам ПОСЛЕДОВАТЕЛЬНО: одна браузерная сессия (modules.ka_site.KaSession),
#      переписка → наши реплики → пропуск совпадающих с нашими почтовыми ответами →
#      db.add_external_reply (идемпотентно по external_id).
#
# Защита аккаунта: один процесс за раз (fcntl-лок), плановый запуск не чаще
# ka_site_min_interval_min на аккаунт, паузы между переписками, препятствие при входе
# (капча / 2FA / неверный пароль) → аккаунт в блоке в state-файле + одно уведомление
# оператору; повторных попыток входа нет, пока оператор не войдёт скриптом
# scripts/ka_site_login.py (или не сменит пароль — для bad_credentials).

from __future__ import annotations

import email
import fcntl
import hashlib
import hmac
import imaplib
import json
import logging
import os
import random
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Optional

import database as db
from modules import ka_site
from modules.ka_site import KaBlocked, KaFormatError, KaNotFound, KaSiteError

logger = logging.getLogger(__name__)

STATE_FILE = ka_site.STATE_DIR / "ka_site_state.json"
LOCK_FILE = ka_site.STATE_DIR / "ka_site.lock"

# Блок после капчи/2FA: столько ждём до следующей САМОСТОЯТЕЛЬНОЙ попытки входа.
# bad_credentials/no_credentials — до смены пароля (отпечаток в state).
BLOCK_HOURS = {"captcha": 24, "mfa": 24, "login_failed": 6}
PAUSE_BETWEEN_CONV = (4.0, 9.0)
DUP_WINDOW_MIN = 20  # окно для нечёткого совпадения с нашим почтовым ответом
RECHECK_MIN = 120  # плановая повторная проверка одного неотвеченного треда
GMAIL_MISS_HOURS = 24  # не нашли conversation id в Gmail — не ищем снова столько часов
STALE_HOURS = 6  # нет успешного чтения сайта дольше — ERROR (monitor_errors)
MAIL_SENT = ("sent", "sent_debug")  # наши ответы, ушедшие ПОЧТОЙ (не с сайта)

SETTING_ENABLED = "ka_site_sync_enabled"         # '1' — плановый запуск включён
SETTING_INTERVAL = "ka_site_min_interval_min"    # минимум минут между плановыми заходами
DEFAULT_INTERVAL_MIN = 45


# --- state-файл ------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        # Битый state: сохраняем копию (.corrupt) для разбора и работаем с пустым —
        # блоки при этом снимаются, поэтому ERROR (monitor_errors пришлёт алерт).
        bad = STATE_FILE.with_suffix(".corrupt")
        try:
            os.replace(STATE_FILE, bad)
        except OSError:
            bad = STATE_FILE
        logger.error("ka_site: state-файл не читается (копия: %s) — блоки аккаунтов сброшены",
                     bad, exc_info=True)
        return {}


def _ensure_dir() -> None:
    ka_site.STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(ka_site.STATE_DIR, 0o700)


def _save_state(state: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = STATE_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_CREAT не меняет права уже существующего файла
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def _acc_state(state: dict[str, Any], account_id: int) -> dict[str, Any]:
    return state.setdefault("accounts", {}).setdefault(str(int(account_id)), {})


def _fp_key() -> bytes:
    """Локальный секрет для отпечатка пароля (без него хэш перебирался бы по state)."""
    kf = ka_site.STATE_DIR / "ka_site.key"
    try:
        return kf.read_bytes()
    except FileNotFoundError:
        _ensure_dir()
        key = os.urandom(32)
        fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key


def _pw_fingerprint(email_: str, password: str) -> str:
    return hmac.new(_fp_key(), f"{email_}\0{password}".encode(), hashlib.sha256).hexdigest()[:16]


def _now() -> datetime:
    return datetime.utcnow()


def block_status(account_id: int, creds: tuple[str, str],
                 state: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Причина блока аккаунта или None. Блок по паролю снимается сменой пароля."""
    st = _acc_state(state if state is not None else _load_state(), account_id)
    reason = st.get("block_reason")
    if not reason:
        return None
    if reason in ("bad_credentials", "no_credentials"):
        if st.get("pw_fp") != _pw_fingerprint(*creds):
            return None  # креды поменяли — можно пробовать
        return reason
    until = st.get("blocked_until")
    if until and _now().isoformat() >= until:
        return None
    return reason


def _set_block(state: dict[str, Any], account_id: int, kind: str,
               creds: tuple[str, str]) -> bool:
    """Записать блок; True — уведомить оператора (новый блок или прошли сутки с прошлого)."""
    st = _acc_state(state, account_id)
    was = st.get("block_reason")
    last_note = st.get("notified_at") or ""
    remind = last_note < (_now() - timedelta(hours=24)).isoformat()
    st["block_reason"] = kind
    st["blocked_at"] = _now().isoformat(timespec="seconds")
    st["pw_fp"] = _pw_fingerprint(*creds)
    hours = BLOCK_HOURS.get(kind)
    st["blocked_until"] = ((_now() + timedelta(hours=hours)).isoformat(timespec="seconds")
                           if hours else None)
    if was != kind or remind:
        st["notified_at"] = _now().isoformat(timespec="seconds")
        return True
    return False


_BLOCK_KEYS = ("block_reason", "blocked_at", "blocked_until", "pw_fp", "notified_at")


def _forget_block(state: dict[str, Any], account_id: int) -> None:
    st = _acc_state(state, account_id)
    for k in _BLOCK_KEYS:
        st.pop(k, None)


def clear_block(account_id: int) -> None:
    """Снять блок (после успешного ручного входа)."""
    state = _load_state()
    _forget_block(state, account_id)
    _save_state(state)


_BLOCK_TEXT = {
    "captcha": "сайт показал капчу",
    "mfa": "сайт просит код подтверждения (2FA)",
    "bad_credentials": "неверный логин или пароль",
    "no_credentials": "сессия истекла, а логин/пароль Kleinanzeigen не заданы",
    "login_failed": "вход не удался",
}


def _notify_block(account: Any, kind: str) -> None:
    from modules import telegram_bot
    text = (f"⚠️ Сайт Kleinanzeigen, аккаунт <b>{_esc(account['name'])}</b>: "
            f"{_BLOCK_TEXT.get(kind, kind)}.\n"
            "Чтение ответов с сайта для этого аккаунта остановлено (повторных входов не будет).\n"
            "Что сделать: ввести логин/пароль в «Аккаунты» или войти вручную — "
            f"<code>python3 scripts/ka_site_login.py --account {int(account['id'])}</code>.")
    try:
        telegram_bot.notify(text)
    except Exception:
        # Уведомление не ушло — блок всё равно стоит; ERROR поймает monitor_errors.
        logger.error("ka_site: не удалось уведомить оператора о блоке аккаунта %s (%s)",
                     account["id"], kind, exc_info=True)


def _esc(s: Any) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


@contextmanager
def _single_run() -> Iterator[bool]:
    """Неблокирующий межпроцессный лок: True — мы единственные; False — уже кто-то идёт."""
    _ensure_dir()
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --- креды / сессия ----------------------------------------------------------------------

def _creds(account: Any) -> tuple[str, str]:
    keys = account.keys() if hasattr(account, "keys") else []
    em = (account["kleinanzeigen_email"] if "kleinanzeigen_email" in keys else None) or ""
    pw = (account["kleinanzeigen_password"] if "kleinanzeigen_password" in keys else None) or ""
    return em.strip(), pw


def account_ready(account: Any) -> bool:
    """Есть чем зайти: сохранённая сессия или логин+пароль."""
    em, pw = _creds(account)
    return ka_site.session_path(account["id"]).exists() or bool(em and pw)


SessionFactory = Callable[..., Any]


def _default_session(account: Any, **kw: Any) -> ka_site.KaSession:
    em, pw = _creds(account)
    return ka_site.KaSession(account["id"], email=em, password=pw, **kw)


# --- Gmail: X-Conversation-ID по Message-ID -------------------------------------------------

_ALL_MAIL_RE = re.compile(rb'\(([^)]*)\) "[^"]*" "?([^"]+)"?$')


def _all_mail_box(imap: imaplib.IMAP4) -> str:
    """Папка «Вся почта» (special-use \\All) — имя локализовано, ищем по флагу."""
    typ, boxes = imap.list()
    for line in boxes or []:
        if not isinstance(line, bytes):
            continue
        m = _ALL_MAIL_RE.search(line)
        if m and b"\\All" in m.group(1):
            return m.group(2).decode()
    logger.warning("ka_site: в Gmail не нашли папку \\All — ищем письма только во INBOX")
    return "INBOX"


def conversation_ids_from_gmail(gmail_email: str, gmail_password: str,
                                message_ids: list[str]) -> dict[str, str]:
    """{Message-ID: X-Conversation-ID} для писем-уведомлений; read-only, без \\Seen."""
    from modules.gmail import IMAP_HOST, IMAP_PORT, IMAP_TIMEOUT
    found: dict[str, str] = {}
    wanted = [m for m in message_ids if m and re.fullmatch(r"<[^<>\"\s]+>", m.strip())]
    if not wanted:
        return found
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        box = _all_mail_box(imap)
        typ, _ = imap.select(f'"{box}"', readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"select {box!r}: {typ}")
        for mid in wanted:
            typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid.strip()}"')
            if typ != "OK":
                raise imaplib.IMAP4.error(f"SEARCH Message-ID: {typ}")
            uids = (data[0] or b"").split()
            if not uids:
                continue
            typ, fd = imap.uid("FETCH", uids[-1],
                               "(BODY.PEEK[HEADER.FIELDS (X-CONVERSATION-ID)])")
            if typ != "OK":
                raise imaplib.IMAP4.error(f"FETCH: {typ}")
            for part in fd or []:
                if isinstance(part, tuple):
                    conv = (email.message_from_bytes(part[1]).get("X-Conversation-ID") or "").strip()
                    if ka_site.is_valid_conversation_id(conv):
                        found[mid] = conv
    return found


# --- выбор тредов -------------------------------------------------------------------------

def _row(r: Any, key: str) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def unanswered_threads(max_age_days: float = 21, account_id: Optional[int] = None) -> list[Any]:
    """Треды, где последним писал покупатель, свежие первыми."""
    since = (_now() - timedelta(days=max_age_days)).isoformat()
    active = {a["id"] for a in db.list_accounts(only_active=True)}
    rows = [r for r in db.pipeline_threads()
            if r["last_event_kind"] == "in" and (r["last_event_at"] or "") >= since
            and r["account_id"] in active
            and (account_id is None or r["account_id"] == account_id)]
    rows.sort(key=lambda r: r["last_event_at"] or "", reverse=True)
    return rows


def _thread_conversation_id(tid: str) -> tuple[Optional[str], list[Any]]:
    """(ka_conversation_id треда | None, входящие строки треда свежими первыми)."""
    hist = list(db.thread_history(tid))
    ins = [h for h in reversed(hist) if h["direction"] == "in"]
    for h in reversed(hist):
        conv = _row(h, "ka_conversation_id")
        if conv:
            return conv, ins
    return None, ins


# --- дедуп с нашими почтовыми ответами --------------------------------------------------

def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def is_our_mail_reply(tid: str, text: str, sent_at: str) -> bool:
    """Реплика с сайта = наш ответ, ушедший ПОЧТОЙ (бот/автоответ)?

    Сравниваем только с sent/sent_debug: уже импортированные sent_external отсекает
    external_id, иначе повтор владельца («Ja», «Ok») считался бы дублем.
    Точное совпадение без окна (сайт и почта расходятся по времени) ИЛИ нечёткое:
    ответ в ±DUP_WINDOW_MIN, один текст — начало другого (Kleinanzeigen может срезать
    подпись/цитату при пересылке)."""
    t = datetime.fromisoformat(sent_at)
    lo = (t - timedelta(minutes=DUP_WINDOW_MIN)).isoformat()
    hi = (t + timedelta(minutes=DUP_WINDOW_MIN)).isoformat()
    site = _norm(text)
    for h in db.thread_history(tid):
        if h["status"] not in MAIL_SENT or not h["de_answer"]:
            continue
        ours = _norm(h["de_answer"])
        if ours == site:
            return True
        if not h["sent_at"] or not (lo <= h["sent_at"] <= hi):
            continue
        short, long_ = sorted((site, ours), key=len)
        if len(short) >= 20 and long_.startswith(short[: max(20, len(short) - 5)]):
            return True
    return False


def _known_external_ids(tid: str) -> set[str]:
    return {h["external_id"] for h in db.thread_history(tid)
            if _row(h, "source") == "site" and _row(h, "external_id")}


# --- публичное API ------------------------------------------------------------------------

def fetch_conversation(account_id: int, conversation_id: str,
                       session_factory: SessionFactory = _default_session) -> list[dict[str, Any]]:
    """Переписка с сайта: [{sender, ours, text, sent_at, external_id}] по времени.

    Одноразовая сессия (для CLI/по требованию). Уважает блок аккаунта и лок."""
    account = db.get_account(account_id)
    if account is None:
        raise ValueError(f"нет аккаунта {account_id}")
    with _single_run() as mine:
        if not mine:
            raise KaSiteError("сайт уже читает другой процесс — повторите позже")
        state = _load_state()
        reason = block_status(account_id, _creds(account), state)
        if reason:
            raise KaBlocked(reason, "аккаунт заблокирован для сайта (см. scripts/ka_site_login.py)")
        try:
            with session_factory(account) as s:
                data = s.get_conversation(conversation_id)
                s.save_state()
            _forget_block(state, account_id)  # вошли — следующий блок снова уведомит
            _save_state(state)
        except KaBlocked as e:
            if _set_block(state, account_id, e.kind, _creds(account)):
                _notify_block(account, e.kind)
            _save_state(state)
            raise
        return ka_site.parse_conversation(data, conversation_id)


def _translate_ru(text: str, client_lang: Optional[str], report: dict[str, Any]) -> Optional[str]:
    lang = (client_lang or "de").lower()
    if lang == "ru":
        return text
    from modules import claude
    try:
        # source_lang не передаём: владелец пишет по-немецки и при client_lang=en/pl
        return claude.translate_only(text, target_lang="ru")["translation"]
    except Exception:
        # Перевод — только для показа оператору; реплика важнее. Если не удались ВСЕ
        # переводы запуска — ERROR в конце sync_unanswered.
        logger.warning("ka_site: перевод реплики на RU не удался — сохраняем без него",
                       exc_info=True)
        report["translate_failed"] += 1
        return None


def sync_unanswered(limit: int = 15, dry_run: bool = False, account_id: Optional[int] = None,
                    max_age_days: float = 21, force: bool = False, translate: bool = True,
                    session_factory: SessionFactory = _default_session,
                    sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Импорт наших реплик с сайта для неотвеченных тредов. Возвращает отчёт.

    force — игнорировать интервалы (аккаунта и повторной проверки треда) — по требованию
    оператора; блоки (капча/2FA/пароль) force НЕ снимает. dry_run — ничего не пишет в БД
    (и не сохраняет найденные conversation id), но на сайт ходит."""
    report: dict[str, Any] = {
        "dry_run": dry_run, "candidates": 0, "threads": 0, "fetched_ok": 0, "imported": 0,
        "would_import": [], "known": 0, "already": 0, "no_conv_id": 0, "conv_backfilled": 0,
        "not_found": 0, "translate_failed": 0, "errors": [], "accounts": {},
    }
    with _single_run() as mine:
        if not mine:
            report["skipped"] = "busy"
            logger.warning("ka_site: пропуск — сайт уже читает другой процесс (лок %s)", LOCK_FILE)
            return report
        state = _load_state()
        threads = unanswered_threads(max_age_days, account_id)
        report["candidates"] = len(threads)
        by_acc: dict[int, list[tuple[Any, str]]] = {}
        need_gmail: dict[int, list[tuple[Any, list[Any]]]] = {}
        misses = state.setdefault("gmail_miss", {})
        miss_cutoff = (_now() - timedelta(hours=GMAIL_MISS_HOURS)).isoformat()
        for t in threads:
            conv, ins = _thread_conversation_id(t["gmail_thread_id"])
            if conv:
                by_acc.setdefault(t["account_id"], []).append((t, conv))
            elif not force and (misses.get(t["gmail_thread_id"]) or "") >= miss_cutoff:
                report["no_conv_id"] += 1  # недавно уже искали в Gmail — не нашли
            else:
                need_gmail.setdefault(t["account_id"], []).append((t, ins))

        _backfill_from_gmail(need_gmail, by_acc, misses, report, dry_run)
        for tid in [k for k, v in misses.items() if (v or "") < miss_cutoff]:
            misses.pop(tid, None)

        budget = max(0, int(limit))
        interval = _interval_min()
        for acc_id, items in sorted(by_acc.items()):
            if budget <= 0:
                break
            acc = db.get_account(acc_id)
            ar: dict[str, Any] = report["accounts"].setdefault(acc_id, {"conversations": 0})
            if acc is None or not account_ready(acc):
                ar["skipped"] = "no_credentials"
                continue
            name = acc["name"]
            reason = block_status(acc_id, _creds(acc), state)
            if reason:
                ar["skipped"] = f"blocked:{reason}"
                continue
            st = _acc_state(state, acc_id)
            last = st.get("last_run_at")
            if not force and last and \
                    _now() < datetime.fromisoformat(last) + timedelta(minutes=interval):
                ar["skipped"] = "interval"
                continue
            checked = st.setdefault("checked", {})
            if not force:
                # Один и тот же неотвеченный тред — не чаще RECHECK_MIN, если в нём
                # не было нового события (новое письмо покупателя → проверяем сразу).
                items = [x for x in items if _due(checked.get(x[0]["gmail_thread_id"]),
                                                  x[0]["last_event_at"])]
            if not items:
                ar["skipped"] = "nothing_due"
                continue
            items.sort(key=lambda x: x[0]["last_event_at"] or "", reverse=True)
            items = items[:budget]
            st["last_run_at"] = _now().isoformat(timespec="seconds")
            _save_state(state)  # даже при падении следующий заход не раньше интервала
            try:
                with session_factory(acc) as s:
                    try:
                        before = report["fetched_ok"]
                        budget = _read_account(s, items, checked, ar, report, budget,
                                               dry_run, translate, sleep, name)
                        if report["fetched_ok"] > before:
                            _forget_block(state, acc_id)  # вошли — следующий блок снова уведомит
                    finally:
                        # Обновлённые куки сохраняем и после ошибки (кроме блока —
                        # там сессия бесполезна, но и не вредна).
                        try:
                            s.save_state()
                        except Exception:
                            logger.warning("ka_site: не сохранили сессию аккаунта «%s»", name,
                                           exc_info=True)
            except KaBlocked as e:
                ar["skipped"] = f"blocked:{e.kind}"
                logger.error("ka_site: аккаунт «%s» — вход на сайт остановлен: %s", name, e)
                if _set_block(state, acc_id, e.kind, _creds(acc)):
                    _notify_block(acc, e.kind)
            except KaSiteError as e:
                logger.error("ka_site: аккаунт «%s»: %s", name, e)
                report["errors"].append(f"site#{acc_id}: {e}")
            except Exception as e:  # Playwright/сеть: один аккаунт не валит остальные
                logger.exception("ka_site: аккаунт «%s» — сбой браузера/сети", name)
                report["errors"].append(f"site#{acc_id}: {type(e).__name__}: {e}")
            _prune_checked(checked)
            _save_state(state)

        if report["fetched_ok"] and not dry_run:
            state["last_ok_at"] = _now().isoformat(timespec="seconds")
        state.setdefault("first_run_at", _now().isoformat(timespec="seconds"))
        _save_state(state)
        report["last_ok_at"] = state.get("last_ok_at")
        report["first_run_at"] = state.get("first_run_at")

    if report["no_conv_id"]:
        logger.warning("ka_site: у %d неотвеченных тредов нет conversation id (нет в Gmail)",
                       report["no_conv_id"])
    if report["not_found"] and report["not_found"] * 2 > max(1, report["threads"]):
        logger.warning("ka_site: %d из %d переписок сайт не нашёл (404)",
                       report["not_found"], report["threads"])
    attempted = report["imported"] + report["already"]
    if translate and attempted and report["translate_failed"] >= report["imported"] > 0:
        logger.error("ka_site: ни одна импортированная реплика не переведена на RU (%d шт.)",
                     report["imported"])
    logger.info("ka_site: %s", {k: v for k, v in report.items() if k != "would_import"})
    return report


def _backfill_from_gmail(need_gmail: dict[int, list[tuple[Any, list[Any]]]],
                         by_acc: dict[int, list[tuple[Any, str]]], misses: dict[str, str],
                         report: dict[str, Any], dry_run: bool) -> None:
    """X-Conversation-ID старых тредов из Gmail (одна IMAP-сессия на аккаунт)."""
    for acc_id, items in need_gmail.items():
        acc = db.get_account(acc_id)
        if acc is None or not acc["gmail_email"] or not acc["gmail_app_password"]:
            report["no_conv_id"] += len(items)
            continue
        mids = [_row(i, "gmail_message_id") for _, ins in items for i in ins[:3]]
        try:
            found = conversation_ids_from_gmail(acc["gmail_email"], acc["gmail_app_password"],
                                                [m for m in mids if m])
        except Exception as e:  # один сломанный ящик не останавливает импорт остальных
            logger.error("ka_site: Gmail-бэкфилл conversation id, аккаунт «%s»: %s",
                         acc["name"], e, exc_info=not isinstance(e, (imaplib.IMAP4.error, OSError)))
            report["errors"].append(f"gmail#{acc_id}: {e}")
            report["no_conv_id"] += len(items)
            continue
        for t, ins in items:
            hit = next(((i, found[i["gmail_message_id"]]) for i in ins[:3]
                        if _row(i, "gmail_message_id") in found), None)
            if not hit:
                report["no_conv_id"] += 1
                if not dry_run:
                    misses[t["gmail_thread_id"]] = _now().isoformat(timespec="seconds")
                continue
            row, conv = hit
            if not dry_run:
                db.update_message(row["id"], ka_conversation_id=conv)
            report["conv_backfilled"] += 1
            by_acc.setdefault(acc_id, []).append((t, conv))


def _read_account(s: Any, items: list[tuple[Any, str]], checked: dict[str, Any],
                  ar: dict[str, Any], report: dict[str, Any], budget: int, dry_run: bool,
                  translate: bool, sleep: Callable[[float], None], name: str) -> int:
    """Переписки одного аккаунта в открытой сессии. Возвращает остаток budget."""
    for n, (t, conv) in enumerate(items):
        if n:
            sleep(random.uniform(*PAUSE_BETWEEN_CONV))
        budget -= 1
        ar["conversations"] += 1
        report["threads"] += 1
        mark = {"at": _now().isoformat(timespec="seconds"), "event": t["last_event_at"]}
        try:
            data = s.get_conversation(conv)
            msgs = ka_site.parse_conversation(data, conv)
        except KaNotFound:
            report["not_found"] += 1
            if not dry_run:
                checked[t["gmail_thread_id"]] = mark  # не занимать бюджет каждый запуск
            continue
        except KaFormatError as e:
            # Формат сменился — дальше по этому аккаунту не идём.
            logger.error("ka_site: аккаунт «%s», формат ответа сайта не узнан (%s): %s",
                         name, conv, e)
            report["errors"].append(f"format:{conv}: {e}")
            break
        report["fetched_ok"] += 1
        _import_thread(t, conv, msgs, report, dry_run, translate)
        if not dry_run:
            checked[t["gmail_thread_id"]] = mark
    return budget


def _due(entry: Optional[dict[str, Any]], last_event_at: Optional[str]) -> bool:
    if not entry:
        return True
    if entry.get("event") != last_event_at:
        return True
    try:
        return _now() >= datetime.fromisoformat(entry["at"]) + timedelta(minutes=RECHECK_MIN)
    except (KeyError, TypeError, ValueError):
        return True


def _prune_checked(checked: dict[str, Any]) -> None:
    cutoff = (_now() - timedelta(days=30)).isoformat()
    for tid in [k for k, v in checked.items() if not isinstance(v, dict) or (v.get("at") or "") < cutoff]:
        checked.pop(tid, None)


def _import_thread(t: Any, conv: str, msgs: list[dict[str, Any]], report: dict[str, Any],
                   dry_run: bool, translate: bool) -> None:
    tid = t["gmail_thread_id"]
    have = _known_external_ids(tid)
    for m in msgs:
        if not m["ours"]:
            continue
        if m["external_id"] in have:
            report["already"] += 1
            continue
        if is_our_mail_reply(tid, m["text"], m["sent_at"]):
            report["known"] += 1
            continue
        if dry_run:
            report["would_import"].append({
                "thread": tid, "account_id": t["account_id"], "sent_at": m["sent_at"],
                "external_id": m["external_id"], "text": m["text"][:120],
            })
            continue
        text_ru = _translate_ru(m["text"], _row(t, "client_lang"), report) if translate else None
        _, created = db.add_external_reply(
            account_id=t["account_id"], gmail_thread_id=tid, external_id=m["external_id"],
            text=m["text"], sent_at=m["sent_at"], ka_conversation_id=conv, text_ru=text_ru,
        )
        if created:
            report["imported"] += 1
        else:
            report["already"] += 1


def _interval_min() -> float:
    raw = db.get_setting(SETTING_INTERVAL)
    try:
        return max(15.0, float(raw)) if raw not in (None, "") else float(DEFAULT_INTERVAL_MIN)
    except ValueError:
        logger.error("ka_site: настройка %s=%r не число — беру %s мин",
                     SETTING_INTERVAL, raw, DEFAULT_INTERVAL_MIN)
        return float(DEFAULT_INTERVAL_MIN)


def run_scheduled() -> Optional[dict[str, Any]]:
    """Точка входа для APScheduler-джоба (регистрирует mail). Выключено по умолчанию.

    Сигнал здоровья: есть неотвеченные треды, а успешного чтения сайта не было
    STALE_HOURS (по любой причине — блок, нет кредов, лок, формат, сеть) → ERROR,
    его ловит monitor_errors."""
    if (db.get_setting(SETTING_ENABLED) or "0").strip() != "1":
        return None
    rep = sync_unanswered()
    ref = rep.get("last_ok_at") or rep.get("first_run_at")
    if rep.get("candidates") and ref and \
            _now() - datetime.fromisoformat(ref) > timedelta(hours=STALE_HOURS):
        logger.error("ka_site: нет успешного чтения сайта больше %d ч (последнее: %s), "
                     "неотвеченных тредов: %d; аккаунты: %s", STALE_HOURS,
                     rep.get("last_ok_at") or "никогда", rep["candidates"], rep.get("accounts"))
    return rep
