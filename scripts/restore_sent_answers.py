#!/usr/bin/env python3
"""Восстановить de_answer строк, у которых уже отправленный ответ затёрт черновиком.

Баг «Предложить» (аудит 2026-10): для строки со status=sent/sent_debug генерировался
новый черновик — de_answer перезаписан, статус стал 'edited', хотя клиенту давно
ушёл другой текст. sent_message_id при этом сохранился — по нему находим письмо в
«Отправленных» Gmail соответствующего аккаунта и берём оттуда реальный текст.

IMAP только на чтение (select readonly, BODY.PEEK). В БД пишет только с --apply,
перед записью — бэкап файла БД (sqlite backup API) рядом с ней.

Usage:
    python3 scripts/restore_sent_answers.py --dry-run                 # авто-поиск кандидатов
    python3 scripts/restore_sent_answers.py --dry-run --ids 2374 2284
    python3 scripts/restore_sent_answers.py --apply --ids 2374 2284 2269 2137
    --db PATH   — другая БД (по умолчанию database.DB_PATH)
"""
from __future__ import annotations

import argparse
import email
import imaplib
import re
import sqlite3
import sys
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database as db  # noqa: E402
from modules import gmail  # noqa: E402

# Кандидаты авто-поиска: ответ точно уходил (есть sent_at и sent_message_id),
# а статус уже не «отправлено».
CANDIDATES_SQL = (
    "SELECT * FROM messages WHERE sent_message_id IS NOT NULL AND sent_message_id != '' "
    "AND sent_at IS NOT NULL AND status NOT IN ('sent', 'sent_debug') ORDER BY id"
)


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sent_mailbox(imap: imaplib.IMAP4) -> str:
    """Имя папки «Отправленные» по флагу \\Sent (у части ящиков оно локализовано)."""
    typ, boxes = imap.list()
    for b in boxes or []:
        line = b.decode(errors="replace") if isinstance(b, bytes) else str(b)
        if "\\Sent" in line:
            return line.split(' "/" ')[-1]
    return '"[Gmail]/Sent Mail"'


def _plain_text(m: Message) -> str:
    for part in m.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _strip_reply_tail(text: str) -> str:
    """Срезать цитату письма покупателя, если почтовый клиент её добавил."""
    text = text.replace("\r\n", "\n")
    cut = re.search(r"(?m)^(Am .{5,120}schrieb.*:|On .{5,120}wrote:|>)", text)
    if cut:
        text = text[:cut.start()]
    return text.strip()


def _fetch_sent(acc: Any, message_ids: list[str]) -> dict[str, dict[str, str]]:
    """{Message-ID: {"text", "to", "date"}} из «Отправленных» аккаунта, только чтение."""
    found: dict[str, dict[str, str]] = {}
    with imaplib.IMAP4_SSL(gmail.IMAP_HOST, gmail.IMAP_PORT, timeout=gmail.IMAP_TIMEOUT) as imap:
        imap.login(acc["gmail_email"], acc["gmail_app_password"])
        typ, _ = imap.select(_sent_mailbox(imap), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"не открылась папка Отправленные у {acc['gmail_email']}")
        for mid in message_ids:
            typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", mid.strip("<>"))
            if typ != "OK":
                raise RuntimeError(f"UID SEARCH не OK для {mid}: {data!r}")
            uids = (data[0] or b"").split() if data else []
            if not uids:
                continue
            typ, data = imap.uid("FETCH", uids[-1].decode(), "(BODY.PEEK[])")
            raw = next((it[1] for it in data or [] if isinstance(it, tuple)), None)
            if typ != "OK" or raw is None:
                raise RuntimeError(f"UID FETCH не OK для {mid}: {typ}")
            m = email.message_from_bytes(raw)
            if (m.get("Message-ID") or "").strip() != mid.strip():
                continue  # поиск по подстроке зацепил чужое письмо
            found[mid] = {"text": _strip_reply_tail(_plain_text(m)),
                          "to": m.get("To") or "", "date": m.get("Date") or ""}
    return found


def _status_for(to_header: str) -> str:
    """Реальному покупателю (relay-адрес Kleinanzeigen) — sent, иначе ушло на debug — sent_debug."""
    return "sent" if "kleinanzeigen.de" in to_header.lower() else "sent_debug"


def _backup(db_path: Path) -> Path:
    dst = db_path.with_name(f"{db_path.stem}-before-restore-{datetime.now():%Y%m%d-%H%M%S}.db")
    src = sqlite3.connect(str(db_path))
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="только показать «было в БД / реально ушло»")
    mode.add_argument("--apply", action="store_true", help="записать в БД (с бэкапом)")
    ap.add_argument("--ids", type=int, nargs="*", help="id строк (по умолчанию — авто-поиск)")
    ap.add_argument("--db", type=Path, default=db.DB_PATH)
    args = ap.parse_args()

    conn = _connect_ro(args.db)
    if args.ids:
        marks = ",".join("?" * len(args.ids))
        rows = conn.execute(f"SELECT * FROM messages WHERE id IN ({marks}) ORDER BY id", args.ids).fetchall()
    else:
        rows = conn.execute(CANDIDATES_SQL).fetchall()
    accounts = {r["id"]: r for r in conn.execute("SELECT * FROM accounts").fetchall()}
    conn.close()
    if not rows:
        print("Кандидатов нет.")
        return 0

    by_acc: dict[int, list[Any]] = {}
    for r in rows:
        if not (r["sent_message_id"] or "").strip():
            print(f"#{r['id']}: нет sent_message_id — пропуск")
            continue
        by_acc.setdefault(r["account_id"], []).append(r)

    plan: list[tuple[Any, str, str]] = []  # (row, текст, статус)
    failed = 0
    for acc_id, acc_rows in by_acc.items():
        acc = accounts.get(acc_id)
        if acc is None:
            print(f"аккаунт {acc_id} не найден — пропуск строк {[r['id'] for r in acc_rows]}")
            failed += len(acc_rows)
            continue
        found = _fetch_sent(acc, [r["sent_message_id"].strip() for r in acc_rows])
        for r in acc_rows:
            got = found.get(r["sent_message_id"].strip())
            print("=" * 78)
            print(f"#{r['id']}  аккаунт {acc_id}  status={r['status']}  sent_at={r['sent_at']}")
            print(f"  sent_message_id: {r['sent_message_id']}")
            print("  --- БЫЛО В БД (de_answer):")
            print("  " + (r["de_answer"] or "").replace("\n", "\n  "))
            if not got:
                print("  --- РЕАЛЬНО УШЛО: письмо в «Отправленных» НЕ НАЙДЕНО — строку не трогаем")
                failed += 1
                continue
            new_status = _status_for(got["to"])
            print(f"  --- РЕАЛЬНО УШЛО ({got['date']}, To: {got['to']}) → status={new_status}:")
            print("  " + got["text"].replace("\n", "\n  "))
            if not got["text"]:
                print("  (пустой text/plain — строку не трогаем)")
                failed += 1
                continue
            same = got["text"].strip() == (r["de_answer"] or "").strip()
            print(f"  --- {'совпадает, правится только статус' if same else 'ОТЛИЧАЕТСЯ'}")
            plan.append((r, got["text"], new_status))

    print("=" * 78)
    print(f"К восстановлению: {len(plan)}, не найдено/пропущено: {failed}")
    if args.dry_run or not plan:
        return 1 if failed else 0

    backup = _backup(args.db)
    print(f"Бэкап БД: {backup}")
    db.DB_PATH = args.db
    for r, text, status in plan:
        # ru_translation тоже мог быть затёрт черновиком — честно помечаем, что
        # зеркало не сверено, а не оставляем перевод чужого текста.
        fields: dict[str, Any] = {"de_answer": text, "status": status}
        if text.strip() != (r["de_answer"] or "").strip():
            fields["ru_translation"] = "(восстановлено из «Отправленных»; перевод не сверен)"
        db.update_message(r["id"], **fields)
        print(f"#{r['id']}: восстановлено, status={status}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
