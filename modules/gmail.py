# Gmail IMAP / SMTP. Авторизация через App Password.
# Используется stdlib imaplib + smtplib — никаких внешних зависимостей.

import email
import imaplib
import logging as _log
import re
import smtplib
import ssl
import time as _time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Callable, Optional, TypeVar

_T = TypeVar("_T")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL
SMTP_TIMEOUT = 30

# Без timeout-а imaplib висит бесконечно при сетевом разрыве.
# История: ночью 2026-05-07 IMAP завис в socket.recv → polling job не вернулся
# 6 часов, APScheduler пропускал каждый следующий запуск (max_instances=1) →
# письмо от Fahrig дошло, но не было обработано. 30s — компромисс между
# защитой от hang-а и допуском медленных соединений.
IMAP_TIMEOUT = 30
IMAP_RETRY_DELAY = 3  # секунд между попытками при TimeoutError

# Транзиентные серверные ошибки Gmail приходят как базовый imaplib.IMAP4.error
# (НЕ abort) с характерным текстом. Напр. «Lookup failed …» при login — бэкенд
# Gmail временно не смог найти аккаунт (наблюдалось разово на orphan-recovery
# 2026-10-01). Их ретраим. Auth-ошибки («Invalid credentials»,
# «Application-specific password required») тоже IMAP4.error — но НЕ транзиент,
# должны падать сразу без ретрая, иначе замаскируют реальную проблему конфига.
_TRANSIENT_IMAP_ERROR_RE = re.compile(
    r"lookup failed|system error|temporar|unavailable|try again|server busy",
    re.I,
)


def _is_transient_imap_error(exc: BaseException) -> bool:
    """True если base IMAP4.error — транзиентная серверная ошибка Gmail, не auth."""
    return bool(_TRANSIENT_IMAP_ERROR_RE.search(str(exc)))


def _imap_retry(fn: Callable[..., _T], *args: Any, retries: int = 1, **kwargs: Any) -> _T:
    """Вызвать fn, повторив до `retries` раз при транзиентной IMAP-ошибке.

    Ловим TimeoutError/OSError (сетевой таймаут, разрыв сокета) и
    imaplib.IMAP4.abort (сервер уронил соединение посреди команды — «socket
    error: EOF»). abort НЕ является подклассом OSError, поэтому его нужно
    перечислять явно — иначе EOF-разрыв Gmail пролетал мимо retry и всплывал
    как ложный ERROR (напр. orphan-recovery). Базовый IMAP4.error ретраим
    только при транзиентном тексте Gmail (`_is_transient_imap_error`) — auth-
    ошибки падают сразу. Каждый повтор fn открывает новое IMAP4_SSL-соединение,
    так что реконнект после abort корректен.
    """
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except (TimeoutError, OSError, imaplib.IMAP4.error) as exc:
            # abort — подкласс error, ловится здесь же; базовый error ретраим
            # только если текст указывает на транзиент (а не auth-отказ).
            transient = (
                isinstance(exc, (TimeoutError, OSError, imaplib.IMAP4.abort))
                or _is_transient_imap_error(exc)
            )
            if attempt >= retries or not transient:
                raise
            _log.warning("IMAP %s таймаут (попытка %d/%d), повтор через %ds: %s",
                         fn.__name__, attempt + 1, retries + 1, IMAP_RETRY_DELAY, exc)
            _time.sleep(IMAP_RETRY_DELAY)


# --- Вспомогательные функции ---

def _decode(value: Optional[str]) -> str:
    """Декодирует MIME-encoded заголовок (=?utf-8?...?=) в обычную строку.

    Также схлопывает RFC 5322 header folding (CRLF+SP внутри длинных заголовков) —
    иначе SMTP-отправка падает на «Header values may not contain linefeed or
    carriage return characters» если позже использовать значение в `msg["Subject"]`.
    """
    if not value:
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except Exception:
        decoded = value
    # Снимаем folding и любые случайные CR/LF — нормализуем в одинарный пробел
    return decoded.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()


def _strip_html(html: str) -> str:
    """Грубое преобразование HTML в текст (для писем где нет text/plain части)."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode_payload(part: email.message.Message) -> str:
    """Декодировать тело part-а с учётом charset. Возвращает строку."""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_parts(msg: email.message.Message) -> tuple[str, str]:
    """Извлечь text/plain и raw text/html отдельно. Возвращает (text, html_raw).

    text — для передачи в Claude и в БД.
    html_raw — для парсинга URL/метаданных (там всё богаче, кнопки/линки/title).
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if ctype == "text/plain":
                text_parts.append(_decode_payload(part))
            elif ctype == "text/html":
                html_parts.append(_decode_payload(part))
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html_parts.append(_decode_payload(msg))
        else:
            text_parts.append(_decode_payload(msg))

    text_combined = "\n".join(text_parts).strip()
    html_combined = "\n".join(html_parts).strip()
    if not text_combined and html_combined:
        # Если text/plain нет — рендерим из HTML
        text_combined = _strip_html(html_combined)
    return text_combined, html_combined


def _extract_text(msg: email.message.Message) -> str:
    """Совместимость со старым кодом: только text-часть."""
    text, _ = _extract_parts(msg)
    return text


# --- IMAP: получение писем ---
#
# ВСЕ команды — UID-варианты (`imap.uid("SEARCH"|"FETCH"|"STORE")`). Номер
# последовательности (seq-num) живёт только в рамках одной сессии и сдвигается,
# когда в ящик падает новое письмо: раньше `mark_seen` в НОВОМ соединении ставил
# \Seen по seq-num из fetch_new и мог пометить прочитанным чужое письмо. UID
# стабилен между сессиями (пока не сменился UIDVALIDITY — у Gmail INBOX не меняется).

# Сколько UID склеивать в один FETCH/STORE. Gmail держит длинные команды, но
# очень длинная строка UID-ов упирается в лимит строки команды — режем пачками.
_UID_CHUNK = 100


def _uid_set(uids: list[str]) -> str:
    """Список UID → IMAP message set «1,5,9»."""
    return ",".join(uids)


def _chunks(items: list[str], size: int = _UID_CHUNK) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _norm_uid(uid: Any) -> str:
    return uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)


def _uid_search(imap: imaplib.IMAP4, *criteria: str) -> list[str]:
    """UID SEARCH → список UID-строк по возрастанию (свежие в конце)."""
    typ, data = imap.uid("SEARCH", None, *criteria)
    if typ != "OK":
        # Не «писем нет», а сбой — пусть всплывёт как ошибка опроса.
        raise imaplib.IMAP4.error(f"UID SEARCH не OK: {data!r}")
    if not data or not data[0]:
        return []
    return [_norm_uid(u) for u in data[0].split()]


_UID_RE = re.compile(rb"\bUID\s+(\d+)", re.I)
_THRID_RE = re.compile(rb"X-GM-THRID\s+(\d+)", re.I)


def _uid_fetch(imap: imaplib.IMAP4, uids: list[str], what: str) -> dict[str, tuple[bytes, bytes]]:
    """Пакетный UID FETCH. Возвращает {uid: (заголовок ответа, литерал-данные)}.

    imaplib отдаёт список, где каждое письмо — tuple (b'7 (UID 123 X-GM-THRID 9
    BODY[] {2048}', b'<raw>'), а между ними — b')'. UID ищем в заголовке tuple
    (в ответе на UID FETCH сервер обязан его вернуть).
    """
    out: dict[str, tuple[bytes, bytes]] = {}
    for chunk in _chunks(uids):
        typ, data = imap.uid("FETCH", _uid_set(chunk), what)
        if typ != "OK":
            # Не «писем нет», а сбой — как в _uid_search/_uid_store: всплывает
            # ошибкой опроса (fails_in_row), а не тихим пустым результатом.
            raise imaplib.IMAP4.error(f"UID FETCH {_uid_set(chunk)} не OK: {data!r}")
        if not data:
            _log.warning("IMAP UID FETCH %s: пустой ответ", _uid_set(chunk))
            continue
        # UID обычно в заголовке tuple, но сервер вправе прислать его ПОСЛЕ
        # литерала — отдельным элементом вида b' UID 5)'. Учитываем оба случая.
        orphan: Optional[tuple[bytes, bytes]] = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                head = item[0] if isinstance(item[0], (bytes, bytearray)) else str(item[0]).encode()
                body = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
                m = _UID_RE.search(head)
                if m:
                    out[m.group(1).decode()] = (bytes(head), bytes(body))
                    orphan = None
                else:
                    orphan = (bytes(head), bytes(body))
            elif orphan and isinstance(item, (bytes, bytearray)):
                m = _UID_RE.search(item)
                if m:
                    out[m.group(1).decode()] = (orphan[0] + b" " + bytes(item), orphan[1])
                orphan = None
    missing = [u for u in uids if u not in out]
    if missing:
        _log.warning("IMAP UID FETCH: сервер не вернул UID %s", missing[:20])
    return out


def _uid_store(imap: imaplib.IMAP4, uids: list[str], op: str) -> None:
    """Пакетный UID STORE флага \\Seen (op = '+FLAGS' / '-FLAGS')."""
    for chunk in _chunks(uids):
        typ, data = imap.uid("STORE", _uid_set(chunk), op, "(\\Seen)")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"UID STORE {op} не OK: {data!r}")


def _search_criteria(from_filter: Optional[str], *base: str) -> tuple[str, ...]:
    if from_filter:
        return (*base, "FROM", f'"{from_filter}"')
    return base or ("ALL",)


def _parse_message(uid: str, head: bytes, raw: bytes) -> dict[str, Any]:
    """Сырые байты письма → dict, который ест incoming._process_incoming."""
    m = _THRID_RE.search(head)
    thrid = m.group(1).decode() if m else ""
    msg = email.message_from_bytes(raw)
    from_name, from_email = parseaddr(_decode(msg.get("From")))
    to_addr = parseaddr(_decode(msg.get("To")))[1]
    text_body, html_body = _extract_parts(msg)
    return {
        "gmail_message_id": (msg.get("Message-ID") or "").strip(),
        "gmail_thread_id": thrid,
        "from_email": from_email,
        "from_name": from_name,
        "to_email": to_addr,
        "subject": _decode(msg.get("Subject")),
        "body": text_body,
        "body_html": html_body,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": (msg.get("References") or "").strip(),
        "date": _decode(msg.get("Date")),
        # id переписки на сайте Kleinanzeigen (вид «49sml:2cxlxdm:2pxg5bw9w») —
        # одинаков для всех писем одного диалога; по нему kab-site сопоставит тред
        # с перепиской на сайте (ответы владельца из приложения в почту не попадают).
        "ka_conversation_id": (msg.get("X-Conversation-ID") or "").strip(),
        "uid": uid,
    }


def fetch_new(
    gmail_email: str,
    gmail_password: str,
    mailbox: str = "INBOX",
    limit: int = 50,
    from_filter: Optional[str] = None,
    include_seen: bool = False,
    skip_uids: Optional[set[str]] = None,
) -> list[dict[str, Any]]:
    """Получить непрочитанные (UNSEEN) письма из ящика.

    from_filter: если задано — IMAP-критерий FROM (substring-match по заголовку
    From). Например "kleinanzeigen.de" вернёт только письма от @*.kleinanzeigen.de
    отбрасывая всё остальное на уровне сервера.

    include_seen: если True — игнорировать UNSEEN, брать последние limit
    писем (с учётом from_filter) из всего ящика. Для отладки/реплея.

    skip_uids: UID-ы, которые уже стоят в очереди обработки (ещё не помечены
    \\Seen) — тело для них не качаем повторно.

    Одна IMAP-сессия: UID SEARCH + один пакетный UID FETCH тел.

    Возвращает список dict с полями:
    gmail_message_id, gmail_thread_id, from_email, from_name, to_email,
    subject, body, in_reply_to, references, date, uid (UID, строка).

    Письма НЕ помечаются прочитанными — это делается отдельно через mark_seen()
    после успешной обработки.
    """
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox, readonly=True)

        base = () if include_seen else ("UNSEEN",)
        uids = _uid_search(imap, *_search_criteria(from_filter, *base))
        if skip_uids:
            uids = [u for u in uids if u not in skip_uids]
        # Берём последние limit штук (свежие в конце)
        uids = uids[-limit:]
        if not uids:
            return []

        # PEEK не сбрасывает флаг \Seen; одновременно тянем X-GM-THRID
        fetched = _uid_fetch(imap, uids, "(UID X-GM-THRID BODY.PEEK[])")

    results: list[dict[str, Any]] = []
    for uid in uids:  # сохраняем порядок SEARCH (старые → новые)
        got = fetched.get(uid)
        if not got or not got[1]:
            continue
        try:
            results.append(_parse_message(uid, *got))
        except Exception:
            # Одно битое письмо не должно блокировать опрос всего аккаунта:
            # отдаём заглушку, очередь залогирует, алертнёт и пометит \Seen.
            _log.exception("Не разобрали письмо UID=%s (%s)", uid, gmail_email)
            results.append({"uid": uid, "gmail_message_id": "", "_broken": True})
    return results


def mark_seen(
    gmail_email: str,
    gmail_password: str,
    uids: list[str],
    mailbox: str = "INBOX",
) -> None:
    """Пометить письма как прочитанные (флаг \\Seen) — по UID, одной командой."""
    uids = [_norm_uid(u) for u in uids if u]
    if not uids:
        return
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox)
        _uid_store(imap, uids, "+FLAGS")


def find_orphan_seen_uids(
    gmail_email: str,
    gmail_password: str,
    known_message_ids: "set[str] | Callable[[], set[str]]",
    from_filter: Optional[str] = None,
    since_days: int = 2,
    mailbox: str = "INBOX",
    limit: int = 200,
) -> list[str]:
    """Найти UID-ы SEEN писем за последние N дней которых НЕТ в known_message_ids.

    Защита от race-condition: бот успел `mark_seen`, но не сохранил message-id ни
    в `messages`, ни в `processed_messages` (например IMAP-timeout посередине
    `_process_incoming` или crash). Возвращает список UID-ов чтобы вызывающий
    мог снять Seen → следующий poll снова увидит их как UNSEEN и обработает.

    Дёшево: один пакетный UID FETCH только заголовка Message-ID.
    known_message_ids может быть callable — тогда вызывается ПОСЛЕ FETCH (без гонки
    с очередью, которая успела обработать и пометить письмо за время поиска).
    """
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox, readonly=True)
        uids = _uid_search(imap, *_search_criteria(from_filter, "SEEN", "SINCE", since))[-limit:]
        if not uids:
            return []
        fetched = _uid_fetch(imap, uids, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")

    known = known_message_ids() if callable(known_message_ids) else known_message_ids
    orphans: list[str] = []
    for uid in uids:
        got = fetched.get(uid)
        if not got:
            continue
        msg_id = ""
        for line in got[1].splitlines():
            if line.lower().startswith(b"message-id:"):
                msg_id = line.split(b":", 1)[1].strip().decode(errors="ignore")
                break
        if msg_id and msg_id not in known:
            orphans.append(uid)
    return orphans


def unmark_seen(
    gmail_email: str,
    gmail_password: str,
    uids: list[str],
    mailbox: str = "INBOX",
) -> None:
    """Снять флаг \\Seen с UID-ов (orphan-recovery). Ошибка — наверх: вызывающий
    логирует, следующий orphan-scan повторит."""
    uids = [_norm_uid(u) for u in uids if u]
    if not uids:
        return
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox)
        _uid_store(imap, uids, "-FLAGS")


def test_connection(gmail_email: str, gmail_password: str) -> tuple[bool, str]:
    """Проверка IMAP подключения. Возвращает (успех, сообщение)."""
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
            imap.login(gmail_email, gmail_password)
            imap.select("INBOX", readonly=True)
        return True, "IMAP подключение OK"
    except Exception as e:
        return False, f"IMAP ошибка: {e}"


# --- SMTP: отправка писем ---

def send_reply(
    gmail_email: str,
    gmail_password: str,
    from_name: str,
    to_email: str,
    subject: str,
    body: str,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> str:
    """Отправить письмо через Gmail SMTP. Возвращает Message-ID отправленного.

    Для сохранения треда передавай in_reply_to (Message-ID письма-родителя)
    и references (существующая цепочка из письма-родителя).
    """
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{gmail_email}>" if from_name else gmail_email
    msg["To"] = to_email
    msg["Subject"] = subject

    # Свой Message-ID, чтобы вернуть его и сохранить в БД
    msg_id = make_msgid(domain=gmail_email.split("@")[-1] or "gmail.com")
    msg["Message-ID"] = msg_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to.strip()

    # References = существующая цепочка + Message-ID родителя
    refs_parts: list[str] = []
    if references:
        refs_parts.append(references.strip())
    if in_reply_to and in_reply_to.strip() not in (references or ""):
        refs_parts.append(in_reply_to.strip())
    if refs_parts:
        msg["References"] = " ".join(refs_parts)

    msg.set_content(body)

    context = ssl.create_default_context()
    # timeout: без него smtplib висит бесконечно при сетевом разрыве и держит
    # вызывающий поток (job / HTTP-запрос оператора).
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=SMTP_TIMEOUT) as smtp:
        smtp.login(gmail_email, gmail_password)
        smtp.send_message(msg)

    return msg_id
