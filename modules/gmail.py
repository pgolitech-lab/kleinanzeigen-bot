# Gmail IMAP / SMTP. Авторизация через App Password.
# Используется stdlib imaplib + smtplib — никаких внешних зависимостей.

import email
import imaplib
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Optional

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL

# Без timeout-а imaplib висит бесконечно при сетевом разрыве.
# История: ночью 2026-05-07 IMAP завис в socket.recv → polling job не вернулся
# 6 часов, APScheduler пропускал каждый следующий запуск (max_instances=1) →
# письмо от Fahrig дошло, но не было обработано. 30s — компромисс между
# защитой от hang-а и допуском медленных соединений.
IMAP_TIMEOUT = 30


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

def fetch_new(
    gmail_email: str,
    gmail_password: str,
    mailbox: str = "INBOX",
    limit: int = 50,
    from_filter: Optional[str] = None,
    include_seen: bool = False,
) -> list[dict[str, Any]]:
    """Получить непрочитанные (UNSEEN) письма из ящика.

    from_filter: если задано — IMAP-критерий FROM (substring-match по заголовку
    From). Например "kleinanzeigen.de" вернёт только письма от @*.kleinanzeigen.de
    отбрасывая всё остальное на уровне сервера.

    include_seen: если True — игнорировать UNSEEN, брать последние limit
    писем (с учётом from_filter) из всего ящика. Для отладки/реплея.

    Возвращает список dict с полями:
    gmail_message_id, gmail_thread_id, from_email, from_name, to_email,
    subject, body, in_reply_to, references, date, uid.

    Письма НЕ помечаются прочитанными — это делается отдельно через mark_seen()
    после успешной обработки.
    """
    results: list[dict[str, Any]] = []
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox, readonly=True)

        # imaplib принимает несколько аргументов критериев; AND-ятся неявно
        if include_seen:
            criteria = ("FROM", f'"{from_filter}"') if from_filter else ("ALL",)
        elif from_filter:
            criteria = ("UNSEEN", "FROM", f'"{from_filter}"')
        else:
            criteria = ("UNSEEN",)
        typ, data = imap.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []

        # Берём последние limit штук (свежие в конце)
        ids = data[0].split()[-limit:]

        for uid in ids:
            # PEEK не сбрасывает флаг \Seen; одновременно тянем X-GM-THRID
            typ, msg_data = imap.fetch(uid, "(BODY.PEEK[] X-GM-THRID)")
            if typ != "OK" or not msg_data:
                continue

            raw: Optional[bytes] = None
            thrid = ""
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    raw = item[1] if isinstance(item[1], (bytes, bytearray)) else None
                    header = item[0].decode("utf-8", errors="ignore") if isinstance(item[0], bytes) else str(item[0])
                    m = re.search(r"X-GM-THRID\s+(\d+)", header)
                    if m:
                        thrid = m.group(1)
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            from_name, from_email = parseaddr(_decode(msg.get("From")))
            to_addr = parseaddr(_decode(msg.get("To")))[1]
            text_body, html_body = _extract_parts(msg)

            results.append({
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
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
            })

    return results


def mark_seen(
    gmail_email: str,
    gmail_password: str,
    uids: list[str],
    mailbox: str = "INBOX",
) -> None:
    """Пометить письма как прочитанные (флаг \\Seen)."""
    if not uids:
        return
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox)
        for uid in uids:
            imap.store(uid, "+FLAGS", "\\Seen")


def find_orphan_seen_uids(
    gmail_email: str,
    gmail_password: str,
    known_message_ids: set[str],
    from_filter: Optional[str] = None,
    since_days: int = 2,
    mailbox: str = "INBOX",
    limit: int = 200,
) -> list[bytes]:
    """Найти UID-ы SEEN писем за последние N дней которых НЕТ в known_message_ids.

    Защита от race-condition: бот успел `mark_seen`, но не сохранил message-id ни
    в `messages`, ни в `processed_messages` (например IMAP-timeout посередине
    `_process_incoming` или crash). Возвращает список UID-ов чтобы вызывающий
    мог снять Seen → следующий poll снова увидит их как UNSEEN и обработает.

    Дёшево: только заголовок Message-ID per UID (минимум IMAP-трафика).
    """
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    orphans: list[bytes] = []
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox, readonly=True)
        if from_filter:
            criteria = ("SEEN", "FROM", f'"{from_filter}"', "SINCE", since)
        else:
            criteria = ("SEEN", "SINCE", since)
        typ, data = imap.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-limit:]
        if not uids:
            return []
        for uid in uids:
            typ, msg_data = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if typ != "OK" or not msg_data:
                continue
            msg_id = ""
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    raw = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
                    for line in raw.splitlines():
                        if line.lower().startswith(b"message-id:"):
                            msg_id = line.split(b":", 1)[1].strip().decode(errors="ignore")
                            break
                    if msg_id:
                        break
            if msg_id and msg_id not in known_message_ids:
                orphans.append(uid)
    return orphans


def unmark_seen(
    gmail_email: str,
    gmail_password: str,
    uids: list[bytes],
    mailbox: str = "INBOX",
) -> None:
    """Снять флаг \\Seen с UID-ов (orphan-recovery)."""
    if not uids:
        return
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=IMAP_TIMEOUT) as imap:
        imap.login(gmail_email, gmail_password)
        imap.select(mailbox)
        for uid in uids:
            try:
                imap.store(uid, "-FLAGS", "\\Seen")
            except Exception:
                pass  # best-effort; следующий orphan-scan повторит


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
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as smtp:
        smtp.login(gmail_email, gmail_password)
        smtp.send_message(msg)

    return msg_id
