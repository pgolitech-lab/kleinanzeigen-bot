# Модуль работы с SQLite. Содержит схему таблиц и базовые CRUD функции.
# Используется stdlib sqlite3 (синхронный) — этого достаточно для масштабов проекта.

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

# Путь к файлу БД рядом с проектом
DB_PATH = Path(__file__).parent / "kleinanzeigen.db"


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Контекст-менеджер соединения с БД. Возвращает строки как dict-like (Row)."""
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Идемпотентный ADD COLUMN для лёгкой миграции схемы."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """Создаёт таблицы при первом запуске. Идемпотентна."""
    with get_conn() as conn:
        # Аккаунты Kleinanzeigen + связанный Gmail
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                gmail_email TEXT NOT NULL,
                gmail_app_password TEXT NOT NULL,
                kleinanzeigen_email TEXT,
                kleinanzeigen_password TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        # Переписка по объявлениям
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                direction TEXT NOT NULL,                 -- 'in' или 'out'
                ad_url TEXT,
                ad_title TEXT,
                ad_price TEXT,
                ad_description TEXT,
                seller_name TEXT,
                buyer_name TEXT,
                de_client TEXT,                          -- оригинал письма клиента (DE)
                ru_client TEXT,                          -- перевод письма клиента (RU)
                ru_answer TEXT,                          -- черновик ответа на русском
                de_answer TEXT,                          -- черновик ответа на немецком
                status TEXT NOT NULL DEFAULT 'new',      -- new/pending/approved/sent/skipped/edited
                gmail_message_id TEXT,
                gmail_thread_id TEXT,
                telegram_message_id INTEGER,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            )
        """)

        # Глобальные настройки (ключ-значение)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # Кэш брифов объявлений: один бриф на ad_id, переиспользуется во всех тредах
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_briefs (
                ad_id TEXT PRIMARY KEY,
                ad_title TEXT,
                ad_url TEXT,
                ad_price TEXT,
                brief_md TEXT NOT NULL,
                key_facts_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Уроки от оператора: пары (плохой_черновик_от_бота, исправленный_оператором)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,
                account_id INTEGER,
                ad_id TEXT,
                client_lang TEXT,
                client_situation_ru TEXT,
                bad_draft_ru TEXT,
                bad_draft_de TEXT,
                good_answer_ru TEXT,
                good_answer_de TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_ad ON lessons(ad_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_account ON lessons(account_id)")

        # Индексы для частых запросов
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(gmail_thread_id)")

        # Миграции существующих БД
        _add_column_if_missing(conn, "messages", "email_subject", "TEXT")
        _add_column_if_missing(conn, "messages", "ad_id", "TEXT")
        _add_column_if_missing(conn, "messages", "client_lang", "TEXT")
        _add_column_if_missing(conn, "messages", "answer_lang", "TEXT")
        _add_column_if_missing(conn, "messages", "tokens_in", "INTEGER")
        _add_column_if_missing(conn, "messages", "tokens_out", "INTEGER")
        _add_column_if_missing(conn, "messages", "cost_usd", "REAL")
        # Напоминалки: state на исходящем сообщении ('none'/'offered'/'approved'/'skipped'),
        # is_reminder=1 у исходящего, которое САМО является follow-up-пингом.
        _add_column_if_missing(conn, "messages", "reminder_state", "TEXT")
        _add_column_if_missing(conn, "messages", "is_reminder", "INTEGER")
        # Outgoing Message-ID (то что вернул SMTP). Раньше перезаписывали gmail_message_id —
        # ломало дедуп incoming-писем. Теперь храним отдельно.
        _add_column_if_missing(conn, "messages", "sent_message_id", "TEXT")
        # Display name покупателя из From-header email-а (Kleinanzeigen передаёт реальное имя).
        # buyer_name остаётся email-ом (нужен как To: для отправки).
        _add_column_if_missing(conn, "messages", "buyer_display_name", "TEXT")
        # Краткое резюме истории треда ДО этого сообщения, на русском, 1-2 предложения.
        # Генерируется Claude в _process_incoming, кэшируется. Используется в Telegram-карточке.
        _add_column_if_missing(conn, "messages", "history_summary_ru", "TEXT")
        # Snooze для напоминалок: дата ISO до которой не показывать карточку пинга.
        _add_column_if_missing(conn, "messages", "reminder_snooze_until", "TEXT")
        # ad_briefs.sold_at — пометка что товар продан, новые inquiries по этому ad_id скипнутся.
        _add_column_if_missing(conn, "ad_briefs", "sold_at", "TEXT")
        # Auto-ack: per-account toggle (накрутка метрики «отвечает в течение X часов»).
        _add_column_if_missing(conn, "accounts", "auto_ack_enabled", "INTEGER NOT NULL DEFAULT 0")
        # is_auto_ack=1 у row которая является авто-приветствием (чтобы отличать от
        # настоящего ответа оператора в reminder-логике и в отображении).
        _add_column_if_missing(conn, "messages", "is_auto_ack", "INTEGER")
        # extra_notes — лог операторских инструкций (💸 цена / 📝 свободная инструкция),
        # рендерится в карточке Telegram как раздел «Дополнительные инструкции».
        # Каждая запись на отдельной строке: «HH:MM <emoji> @actor: <text>».
        _add_column_if_missing(conn, "messages", "extra_notes", "TEXT")
        # is_autopilot_reply=1 у row которая была сгенерена и отправлена в авто-пилот режиме.
        _add_column_if_missing(conn, "messages", "is_autopilot_reply", "INTEGER")
        # Цена за которую товар реально продан (заполняется при тапе «Продано» в MA).
        # NULL = не продано или цена не зафиксирована.
        _add_column_if_missing(conn, "messages", "sold_price_eur", "REAL")
        # thread_autopilot — состояние авто-пилота per-thread.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_autopilot (
                gmail_thread_id TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                floor_price_eur REAL NOT NULL,
                notify_mode TEXT NOT NULL,
                messages_sent INTEGER NOT NULL DEFAULT 0,
                started_by TEXT,
                started_at TEXT NOT NULL,
                stopped_at TEXT,
                stop_reason TEXT
            )
        """)
        # similar_buyers_json — список JSON {msg_id, display_name, suspicion_score, reason}
        # для подозрительно похожих по стилю incoming-сообщений от других клиентов
        # (для детекта «один и тот же человек под разными именами/в разные аккаунты»).
        _add_column_if_missing(conn, "messages", "similar_buyers_json", "TEXT")
        # ru_translation — точный обратный перевод de_answer на русский (генерится
        # Sonnet вместе с reply). Отличается от ru_answer тем что ru_answer — это
        # «черновик/инструкция RU», а ru_translation — буквальный перевод того что
        # реально уйдёт клиенту. Оператор видит оба для верификации.
        _add_column_if_missing(conn, "messages", "ru_translation", "TEXT")
        # deal_brief_json — динамический бриф сделки (генерится Sonnet вместе с reply):
        # {summary_ru, expected_next, negotiated_price_eur, client_assessment}.
        # Кэшируется на in-row, обновляется при каждой Sonnet-генерации (новый incoming + регенерации).
        _add_column_if_missing(conn, "messages", "deal_brief_json", "TEXT")

        # card_dispatches — fanout-копии карточек в DM операторов.
        # При DM-mode (telegram_operator_dm_ids задан) каждая карточка ревью
        # рассылается каждому оператору; здесь храним (msg_id, chat_id, tg_msg_id)
        # для broadcast-обновлений когда любой оператор кликает действие.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS card_dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                tg_msg_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_card_dispatches_msg ON card_dispatches(message_id)"
        )

        # closed_threads — треды завершённые оператором кнопкой «🏁 Завершить беседу».
        # Не отображаются в pipeline/reminder. Если в закрытом треде приходит новое
        # incoming — флаг автоматически снимается (тред реактивируется).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS closed_threads (
                gmail_thread_id TEXT PRIMARY KEY,
                closed_at TEXT NOT NULL,
                closed_by TEXT
            )
        """)

        # wait_threads — оператор пометил «⏳ Ждать ответа клиента»: pending-драфты
        # пропущены, тред переходит в 🟢 секцию pipeline (ждём клиента) — потому что
        # incoming клиента это не вопрос-нам, а просто инфа («друг занят»). Авто-сброс
        # при новом incoming. В pipeline-events добавляется virtual out-event на
        # marked_at — так last_event_kind становится 'out'.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wait_threads (
                gmail_thread_id TEXT PRIMARY KEY,
                marked_at TEXT NOT NULL,
                marked_by TEXT
            )
        """)

        # processed_messages — журнал ВСЕХ обработанных писем (включая skip-нутые).
        # Используется orphan-recovery для отличия «никогда не видели» от «видели и
        # сознательно skip-нули». Без этой таблицы recovery в бесконечном цикле
        # реобрабатывает junk-письма (snoozes Haiku-classifier на $0.001 каждое).
        # Reason: 'inquiry' | 'skipped_dedup' | 'skipped_noreply' | 'skipped_junk' |
        #         'skipped_purchase_side' | 'skipped_classifier' | 'skipped_max_age' |
        #         'skipped_sold' | 'skipped_no_ad_ref'.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                gmail_message_id TEXT PRIMARY KEY,
                account_id INTEGER,
                processed_at TEXT NOT NULL,
                reason TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_messages(processed_at)"
        )


# --- ACCOUNTS ---

def list_accounts(only_active: bool = False) -> list[sqlite3.Row]:
    """Возвращает список аккаунтов."""
    sql = "SELECT * FROM accounts"
    if only_active:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def get_account(account_id: int) -> Optional[sqlite3.Row]:
    """Получить аккаунт по id."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()


def add_account(
    name: str,
    gmail_email: str,
    gmail_app_password: str,
    kleinanzeigen_email: Optional[str] = None,
    kleinanzeigen_password: Optional[str] = None,
) -> int:
    """Создать аккаунт. Возвращает id новой записи."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO accounts (name, gmail_email, gmail_app_password,
                                  kleinanzeigen_email, kleinanzeigen_password,
                                  is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (name, gmail_email, gmail_app_password,
             kleinanzeigen_email, kleinanzeigen_password,
             datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def update_account(account_id: int, **fields: Any) -> None:
    """Обновить произвольные поля аккаунта."""
    if not fields:
        return
    allowed = {"name", "gmail_email", "gmail_app_password",
               "kleinanzeigen_email", "kleinanzeigen_password",
               "is_active", "auto_ack_enabled"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE accounts SET {cols} WHERE id = ?",
                     (*fields.values(), account_id))


def delete_account(account_id: int) -> None:
    """Удалить аккаунт (вместе с его сообщениями через CASCADE)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


# --- MESSAGES ---

def add_message(account_id: int, direction: str, **fields: Any) -> int:
    """Создать запись сообщения. direction = 'in' (входящее) или 'out' (исходящее).

    `created_at` берётся из fields если задан явно (используется для входящих с
    реальной email Date), иначе текущее UTC.
    """
    fields = {
        "created_at": datetime.utcnow().isoformat(),
        **fields,
        "account_id": account_id,
        "direction": direction,
        "status": fields.get("status", "new"),
    }
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO messages ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid


def update_message(message_id: int, **fields: Any) -> None:
    """Обновить поля сообщения."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE messages SET {cols} WHERE id = ?",
                     (*fields.values(), message_id))


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    """Получить сообщение по id."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def list_messages(
    account_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Список сообщений с фильтрами."""
    sql = "SELECT * FROM messages WHERE 1=1"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def delete_message(message_id: int) -> None:
    """Удалить сообщение по id."""
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))


def get_thread_autopilot(thread_id: Optional[str]) -> Optional[sqlite3.Row]:
    """Получить state автопилота для треда (или None)."""
    if not thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM thread_autopilot WHERE gmail_thread_id = ?",
            (thread_id,),
        ).fetchone()


def start_thread_autopilot(
    thread_id: str, floor_price_eur: float, notify_mode: str,
    started_by: Optional[str] = None,
) -> None:
    """Включить (или переактивировать) автопилот для треда."""
    if not thread_id:
        return
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        # UPSERT: если уже была запись (например stopped) — переактивируем
        conn.execute(
            """
            INSERT INTO thread_autopilot
                (gmail_thread_id, active, floor_price_eur, notify_mode, messages_sent,
                 started_by, started_at, stopped_at, stop_reason)
            VALUES (?, 1, ?, ?, 0, ?, ?, NULL, NULL)
            ON CONFLICT(gmail_thread_id) DO UPDATE SET
                active = 1,
                floor_price_eur = excluded.floor_price_eur,
                notify_mode = excluded.notify_mode,
                messages_sent = 0,
                started_by = excluded.started_by,
                started_at = excluded.started_at,
                stopped_at = NULL,
                stop_reason = NULL
            """,
            (thread_id, floor_price_eur, notify_mode, started_by, now),
        )


def increment_autopilot_messages(thread_id: str) -> int:
    """Увеличить счётчик отправленных автопилотом. Возвращает новый count."""
    if not thread_id:
        return 0
    with get_conn() as conn:
        conn.execute(
            "UPDATE thread_autopilot SET messages_sent = messages_sent + 1 WHERE gmail_thread_id = ?",
            (thread_id,),
        )
        row = conn.execute(
            "SELECT messages_sent FROM thread_autopilot WHERE gmail_thread_id = ?",
            (thread_id,),
        ).fetchone()
    return row["messages_sent"] if row else 0


def stop_thread_autopilot(thread_id: str, reason: str) -> None:
    """Выключить автопилот с указанной причиной."""
    if not thread_id:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE thread_autopilot SET active = 0, stopped_at = ?, stop_reason = ? "
            "WHERE gmail_thread_id = ?",
            (datetime.utcnow().isoformat(), reason, thread_id),
        )


def add_card_dispatch(message_id: int, chat_id: str, tg_msg_id: int) -> None:
    """Записать fanout-копию карточки (msg_id × chat_id → tg_msg_id)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO card_dispatches (message_id, chat_id, tg_msg_id, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (message_id, str(chat_id), tg_msg_id, datetime.utcnow().isoformat()),
        )


def clear_card_dispatches(message_id: int) -> None:
    """Удалить ВСЕ dispatches для message_id (перед новым fanout-ом, чтоб не было stale)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM card_dispatches WHERE message_id = ?", (message_id,))


def list_card_dispatches(message_id: int) -> list[sqlite3.Row]:
    """Все fanout-копии карточки message_id (для broadcast-обновления)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM card_dispatches WHERE message_id = ? ORDER BY id",
            (message_id,),
        ).fetchall()


# --- CLOSED THREADS ---

def close_thread(gmail_thread_id: str, closed_by: Optional[str] = None) -> None:
    """Пометить тред завершённым — не показывать в pipeline/reminder.

    UPSERT: повторный вызов обновит closed_at и closed_by. Реактивация —
    через `reopen_thread` или автоматически при новом incoming в этот thread_id.
    """
    if not gmail_thread_id:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO closed_threads (gmail_thread_id, closed_at, closed_by) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(gmail_thread_id) DO UPDATE SET "
            "closed_at = excluded.closed_at, closed_by = excluded.closed_by",
            (gmail_thread_id, datetime.utcnow().isoformat(), closed_by),
        )


def reopen_thread(gmail_thread_id: str) -> None:
    """Снять флаг закрытия (например при новом incoming в treadе)."""
    if not gmail_thread_id:
        return
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM closed_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        )


def is_thread_closed(gmail_thread_id: str) -> bool:
    """Проверить closed-флаг."""
    if not gmail_thread_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM closed_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()
    return bool(row)


def mark_thread_waiting(gmail_thread_id: str, marked_by: Optional[str] = None) -> None:
    """Пометить тред «ждём клиента» — переводит pipeline в 🟢 секцию.

    UPSERT: повторный вызов обновит marked_at. Авто-сброс при новом incoming.
    """
    if not gmail_thread_id:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wait_threads (gmail_thread_id, marked_at, marked_by) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(gmail_thread_id) DO UPDATE SET "
            "marked_at = excluded.marked_at, marked_by = excluded.marked_by",
            (gmail_thread_id, datetime.utcnow().isoformat(), marked_by),
        )


def unmark_thread_waiting(gmail_thread_id: str) -> None:
    """Снять wait-флаг (новое incoming или ручная отмена)."""
    if not gmail_thread_id:
        return
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM wait_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        )


def is_thread_waiting(gmail_thread_id: str) -> bool:
    """Проверить wait-флаг."""
    if not gmail_thread_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM wait_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()
    return bool(row)


# --- PROCESSED MESSAGES (orphan-recovery support) ---

def mark_processed(
    gmail_message_id: str,
    account_id: Optional[int],
    reason: str,
) -> None:
    """Запомнить что письмо обработано (или сознательно skip-нуто).

    Используется orphan-recovery: чтобы отличить «не видели» от «видели и решили
    не сохранять» (junk/noreply/sold/etc). Без этого recovery в цикле тащит из
    Gmail те же junk-письма и тратит Haiku-classifier API.
    """
    if not gmail_message_id:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO processed_messages (gmail_message_id, account_id, processed_at, reason) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(gmail_message_id) DO UPDATE SET "
            "processed_at = excluded.processed_at, reason = excluded.reason",
            (gmail_message_id, account_id, datetime.utcnow().isoformat(), reason),
        )


def known_message_ids_since(since_days: int = 3) -> set[str]:
    """Все Message-ID-ы которые бот за последние N дней так или иначе видел.

    Объединяет:
      - `messages.gmail_message_id` за созданные за period (все обработанные inquiry)
      - `processed_messages.gmail_message_id` за period (skip-нутые / повторы)
    Используется в orphan-recovery scan: всё что НЕ в этом set-е и помечено в
    Gmail как Seen — потенциальный сирота.
    """
    cutoff_iso = (datetime.utcnow() - timedelta(days=since_days)).isoformat()
    out: set[str] = set()
    with get_conn() as conn:
        for r in conn.execute(
            "SELECT gmail_message_id FROM messages "
            "WHERE created_at > ? AND gmail_message_id IS NOT NULL AND gmail_message_id != ''",
            (cutoff_iso,),
        ).fetchall():
            out.add(r["gmail_message_id"])
        for r in conn.execute(
            "SELECT gmail_message_id FROM processed_messages WHERE processed_at > ?",
            (cutoff_iso,),
        ).fetchall():
            out.add(r["gmail_message_id"])
    return out


def append_extra_note(message_id: int, note: str) -> None:
    """Дописать строку в messages.extra_notes (с timestamp в начале).

    Используется при `💸 Своя цена` / `📝 Своя инструкция` чтобы лог операторских
    директив был виден в карточке ревью.
    """
    note = (note or "").strip()
    if not note:
        return
    timestamp = datetime.utcnow().strftime("%H:%M")
    new_line = f"{timestamp} {note}"
    with get_conn() as conn:
        row = conn.execute(
            "SELECT extra_notes FROM messages WHERE id = ?", (message_id,),
        ).fetchone()
        if not row:
            return
        existing = (row["extra_notes"] or "").strip()
        combined = (existing + "\n" + new_line) if existing else new_line
        conn.execute(
            "UPDATE messages SET extra_notes = ? WHERE id = ?",
            (combined, message_id),
        )


def find_by_gmail_message_id(gmail_message_id: str) -> Optional[sqlite3.Row]:
    """Найти сообщение по Gmail message-id (для дедупликации при polling)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE gmail_message_id = ?",
            (gmail_message_id,),
        ).fetchone()


def find_reminder_candidates(after_days: float) -> list[sqlite3.Row]:
    """Найти треды, в которых последнее по ВРЕМЕНИ событие — наш отправленный ответ.

    Раскладываем сообщения на события:
      - incoming: row с de_client ⇒ event at created_at
      - outgoing: row с de_answer + status sent/sent_debug ⇒ event at sent_at
    Берём последнее событие в каждом треде по `at_time`. Если оно outgoing и старше
    after_days — тред нуждается в пинге (если ещё не предлагали и не auto-ack).

    Эта схема корректно работает после recovery/backfill (ID может быть высоким,
    а реальное время — старым) и на dual-purpose row'ах (в одной строке incoming
    + наш sent-ответ имеют разные таймстампы и должны учитываться как 2 события).
    """
    cutoff = (datetime.utcnow() - timedelta(days=float(after_days))).isoformat()
    now_iso = datetime.utcnow().isoformat()
    sql = """
        WITH events AS (
            -- incoming-события: строки с письмом клиента
            SELECT id, gmail_thread_id, created_at AS at_time, 'in' AS kind
              FROM messages
             WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
               AND de_client IS NOT NULL
            UNION ALL
            -- outgoing-события: строки с реально отправленным ответом
            SELECT id, gmail_thread_id, sent_at AS at_time, 'out' AS kind
              FROM messages
             WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
               AND de_answer IS NOT NULL
               AND status IN ('sent', 'sent_debug')
               AND sent_at IS NOT NULL
        ),
        last_event AS (
            SELECT e1.* FROM events e1
             WHERE e1.at_time = (
                SELECT MAX(e2.at_time) FROM events e2
                 WHERE e2.gmail_thread_id = e1.gmail_thread_id
             )
        )
        SELECT m.* FROM messages m
        JOIN last_event le ON m.id = le.id
        WHERE le.kind = 'out'
          AND le.at_time < ?
          AND (m.reminder_state IS NULL OR m.reminder_state = 'none')
          AND COALESCE(m.is_reminder, 0) = 0
          AND COALESCE(m.is_auto_ack, 0) = 0
          AND (m.reminder_snooze_until IS NULL OR m.reminder_snooze_until < ?)
          -- Закрытые оператором треды не пингуем
          AND NOT EXISTS (
              SELECT 1 FROM closed_threads ct
               WHERE ct.gmail_thread_id = m.gmail_thread_id
          )
        ORDER BY le.at_time ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (cutoff, now_iso)).fetchall()


def thread_history(gmail_thread_id: str) -> list[sqlite3.Row]:
    """Все сообщения треда в хронологическом порядке (для контекста Claude и UI).

    Сортировка по `created_at` (реальная дата письма из Date-header), id — tiebreaker.
    Это важно для recovery/backfill: подобранные задним числом письма получают
    высокий id, но их created_at может быть старее уже сохранённых ответов.
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE gmail_thread_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (gmail_thread_id,),
        ).fetchall()


def thread_events(gmail_thread_id: str) -> list[dict[str, Any]]:
    """Развернуть row'ы треда в плоский список событий с реальными таймстампами.

    Одна row 'in' с заполненным de_answer = ДВА события: клиент написал (created_at),
    мы ответили (sent_at). Это даёт правильный порядок когда клиент отправил
    follow-up между нашим draft-ом и его реальной отправкой.

    Каждое событие: {ts, kind, row, text, ru_text, status, is_auto_ack}.
    """
    rows = thread_history(gmail_thread_id)
    events: list[dict[str, Any]] = []
    SENT_LIKE = {"sent", "sent_debug", "edited", "approved", "pending", "new"}

    for r in rows:
        # Входящее
        if r["de_client"]:
            events.append({
                "ts": r["created_at"] or "",
                "kind": "in",
                "row": r,
                "text": r["de_client"],
                "ru_text": r["ru_client"],
                "status": r["status"],
                "is_auto_ack": False,
            })
        # Исходящее (для direction=out — это сама row; для direction=in — наш ответ
        # на этот клиентский inquiry, ушёл позже клиентского письма).
        # Показываем только если уже отправлено / готово (исключаем новые draft'ы).
        de_ans = r["de_answer"]
        status = r["status"] or ""
        if de_ans and status in SENT_LIKE:
            ts = r["sent_at"] or r["created_at"] or ""
            try:
                is_ack = bool(r["is_auto_ack"])
            except (IndexError, KeyError):
                is_ack = False
            ru_text = r["ru_answer"]
            try:
                if not ru_text:
                    ru_text = r["ru_translation"]
            except (IndexError, KeyError):
                pass
            events.append({
                "ts": ts,
                "kind": "out",
                "row": r,
                "text": de_ans,
                "ru_text": ru_text,
                "status": status,
                "is_auto_ack": is_ack,
            })

    events.sort(key=lambda e: (e["ts"] or "", e["row"]["id"]))
    return events


def list_clients() -> list[sqlite3.Row]:
    """Список уникальных покупателей с агрегатами: сколько объявлений, сообщений, треды,
    последняя активность, последний статус, стоимость API.
    """
    sql = """
        SELECT
            m.buyer_name AS email,
            (SELECT buyer_display_name FROM messages s
             WHERE s.buyer_name = m.buyer_name
               AND s.buyer_display_name IS NOT NULL
             ORDER BY s.id DESC LIMIT 1) AS display_name,
            COUNT(DISTINCT m.ad_id) AS ad_count,
            COUNT(DISTINCT m.gmail_thread_id) AS thread_count,
            COUNT(*) AS msg_count,
            MAX(m.created_at) AS last_at,
            (SELECT status FROM messages s
             WHERE s.buyer_name = m.buyer_name
             ORDER BY s.id DESC LIMIT 1) AS last_status,
            COALESCE(SUM(m.cost_usd), 0) AS total_cost
        FROM messages m
        WHERE m.buyer_name IS NOT NULL AND m.buyer_name != ''
          AND m.direction = 'in'
        GROUP BY m.buyer_name
        ORDER BY last_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def find_related_inquiries(
    display_name: Optional[str],
    exclude_thread_id: Optional[str] = None,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Найти ДРУГИЕ треды от того же клиента (по buyer_display_name).

    Если у нескольких inquiries (даже на разные объявления / в разные аккаунты)
    одинаковое display_name — почти всегда тот же человек.

    Возвращает по одной row на gmail_thread_id (последний incoming в треде).
    Сортировка — DESC по последнему событию.
    """
    if not display_name or not display_name.strip():
        return []
    sql = """
    WITH last_in AS (
        SELECT m.* FROM messages m
        WHERE m.direction = 'in'
          AND m.buyer_display_name = ?
          AND m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
          AND m.id = (
              SELECT MAX(m2.id) FROM messages m2
              WHERE m2.gmail_thread_id = m.gmail_thread_id
                AND m2.direction = 'in'
          )
    )
    SELECT * FROM last_in
    WHERE 1=1
    """
    params: list[Any] = [display_name.strip()]
    if exclude_thread_id:
        sql += " AND gmail_thread_id != ?"
        params.append(exclude_thread_id)
    sql += " ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_threads_for_client(buyer_email: str) -> list[sqlite3.Row]:
    """Все треды конкретного покупателя (по buyer_name = email)."""
    if not buyer_email:
        return []
    sql = """
        SELECT
            m.gmail_thread_id AS thread_id,
            COUNT(*) AS msg_count,
            MAX(m.created_at) AS last_at,
            (SELECT status FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
             ORDER BY s.id DESC LIMIT 1) AS last_status,
            (SELECT ad_title FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_title IS NOT NULL AND ad_title != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_title,
            (SELECT ad_id FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_id IS NOT NULL AND ad_id != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_id,
            (SELECT ad_price FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_price IS NOT NULL AND ad_price != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_price
        FROM messages m
        WHERE m.buyer_name = ?
          AND m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
        GROUP BY m.gmail_thread_id
        ORDER BY last_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (buyer_email,)).fetchall()


def pipeline_threads() -> list[sqlite3.Row]:
    """Все треды в активной фазе для команды /pipeline.

    Возвращает по одной row на тред — последний incoming + aggregate-колонки.
    Используется ХРОНОЛОГИЯ событий (created_at для in, sent_at для out), а не
    MAX(id) — так корректно отражается ход переписки после recovery/backfill.

    Колонки:
      - has_any_sent: есть хоть одно отправленное (sent/sent_debug) в треде
      - has_real_reply: есть отправленное НЕ auto-ack (in-row.sent или out-row не-ack)
      - has_pending_draft: в треде есть in-row с status pending/new/edited/approved
      - last_event_at: время последнего события (in.created_at или out.sent_at)
      - last_event_kind: 'in' или 'out' — кто говорил последним по времени
      - pending_drafts_count: сколько in-row ждут ревью оператора

    Сортировка по last_event_at ASC — самые старые наверху (дольше висят, выше срочность).
    """
    sql = """
    WITH events AS (
        SELECT gmail_thread_id, created_at AS at_time, 'in' AS kind FROM messages
         WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
           AND de_client IS NOT NULL
        UNION ALL
        SELECT gmail_thread_id, sent_at AS at_time, 'out' AS kind FROM messages
         WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
           AND de_answer IS NOT NULL
           AND status IN ('sent', 'sent_debug')
           AND sent_at IS NOT NULL
        UNION ALL
        -- Виртуальное «out»-событие от оператора: «ждём клиента» → ставит секцию 🟢
        SELECT gmail_thread_id, marked_at AS at_time, 'out' AS kind
          FROM wait_threads
         WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
    ),
    last_event AS (
        SELECT e1.gmail_thread_id, e1.at_time AS last_event_at, e1.kind AS last_event_kind
          FROM events e1
         WHERE e1.at_time = (
            SELECT MAX(e2.at_time) FROM events e2
             WHERE e2.gmail_thread_id = e1.gmail_thread_id
         )
        GROUP BY e1.gmail_thread_id
    ),
    counts AS (
        SELECT gmail_thread_id,
               SUM(CASE WHEN status IN ('sent', 'sent_debug') THEN 1 ELSE 0 END) AS any_sent_count,
               -- Реальный sent: либо in-row.status=sent (наш Sonnet-ответ),
               -- либо outgoing-row не-auto-ack (manual compose/ping).
               SUM(CASE WHEN status IN ('sent', 'sent_debug')
                         AND (direction = 'in' OR COALESCE(is_auto_ack, 0) = 0)
                        THEN 1 ELSE 0 END) AS real_sent_count,
               SUM(CASE WHEN direction = 'in'
                         AND status IN ('pending', 'new', 'edited', 'approved')
                        THEN 1 ELSE 0 END) AS pending_drafts_count
        FROM messages
        WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
        GROUP BY gmail_thread_id
    ),
    last_in AS (
        SELECT m.* FROM messages m
        WHERE m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
          AND m.direction = 'in'
          AND m.created_at = (
              SELECT MAX(m2.created_at) FROM messages m2
              WHERE m2.gmail_thread_id = m.gmail_thread_id
                AND m2.direction = 'in'
          )
        GROUP BY m.gmail_thread_id  -- в случае tie по created_at оставит одну
    )
    SELECT li.*,
           le.last_event_at,
           le.last_event_kind,
           COALESCE(c.any_sent_count, 0)         AS any_sent_count,
           COALESCE(c.real_sent_count, 0)        AS real_sent_count,
           COALESCE(c.pending_drafts_count, 0)   AS pending_drafts_count,
           CASE WHEN COALESCE(c.any_sent_count, 0) > 0 THEN 1 ELSE 0 END   AS has_any_sent,
           CASE WHEN COALESCE(c.real_sent_count, 0) > 0 THEN 1 ELSE 0 END  AS has_real_reply,
           CASE WHEN COALESCE(c.pending_drafts_count, 0) > 0 THEN 1 ELSE 0 END AS has_pending_draft
    FROM last_in li
    LEFT JOIN counts c ON c.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN last_event le ON le.gmail_thread_id = li.gmail_thread_id
    WHERE (COALESCE(c.any_sent_count, 0) > 0
           OR COALESCE(c.pending_drafts_count, 0) > 0
           OR li.status IN ('pending', 'new', 'edited', 'approved'))
      -- Исключаем закрытые оператором треды (кнопка «🏁 Завершить беседу»)
      AND NOT EXISTS (
          SELECT 1 FROM closed_threads ct
           WHERE ct.gmail_thread_id = li.gmail_thread_id
      )
    ORDER BY le.last_event_at ASC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def list_threads() -> list[sqlite3.Row]:
    """Сводка по тредам: для каждого gmail_thread_id — последний статус,
    кол-во сообщений, время последнего, заголовок объявления, имя покупателя.
    Сортировка: новые треды первыми (по last_at).
    Треды без gmail_thread_id (пустая строка) не попадают.
    """
    sql = """
        SELECT
            m.gmail_thread_id AS thread_id,
            COUNT(*) AS msg_count,
            MAX(m.created_at) AS last_at,
            (SELECT status FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
             ORDER BY s.id DESC LIMIT 1) AS last_status,
            (SELECT ad_title FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_title IS NOT NULL AND ad_title != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_title,
            (SELECT ad_url FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_url IS NOT NULL AND ad_url != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_url,
            (SELECT ad_price FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_price IS NOT NULL AND ad_price != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_price,
            (SELECT buyer_name FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND direction = 'in'
             ORDER BY s.id DESC LIMIT 1) AS buyer_name,
            (SELECT buyer_display_name FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND direction = 'in'
               AND buyer_display_name IS NOT NULL
             ORDER BY s.id DESC LIMIT 1) AS buyer_display_name,
            (SELECT seller_name FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND seller_name IS NOT NULL
             ORDER BY s.id DESC LIMIT 1) AS seller_name
        FROM messages m
        WHERE m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
        GROUP BY m.gmail_thread_id
        ORDER BY last_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


# --- SETTINGS ---

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Прочитать значение настройки. Возвращает default если ключ не найден."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: Optional[str]) -> None:
    """Записать значение настройки (UPSERT)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, datetime.utcnow().isoformat()),
        )


def all_settings() -> dict[str, str]:
    """Все настройки как словарь."""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


# --- AD BRIEFS ---

def get_ad_brief(ad_id: str) -> Optional[sqlite3.Row]:
    """Получить кэшированный бриф объявления по ad_id."""
    if not ad_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM ad_briefs WHERE ad_id = ?", (ad_id,)
        ).fetchone()


def upsert_ad_brief(
    ad_id: str,
    ad_title: Optional[str],
    ad_url: Optional[str],
    ad_price: Optional[str],
    brief_md: str,
    key_facts_json: Optional[str],
) -> None:
    """Записать или обновить бриф объявления."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ad_briefs (ad_id, ad_title, ad_url, ad_price,
                                   brief_md, key_facts_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                ad_title = excluded.ad_title,
                ad_url = excluded.ad_url,
                ad_price = excluded.ad_price,
                brief_md = excluded.brief_md,
                key_facts_json = excluded.key_facts_json,
                updated_at = excluded.updated_at
            """,
            (ad_id, ad_title, ad_url, ad_price, brief_md, key_facts_json, now, now),
        )


def list_ad_briefs(limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM ad_briefs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()


def mark_ad_sold(ad_id: str, sold: bool = True) -> None:
    """Пометить объявление проданным (или снять отметку sold=False).

    Если бриф ещё не существует — создаёт минимальный stub чтобы запомнить состояние.
    Будущие inquiries для этого ad_id будут авто-skip.
    """
    if not ad_id:
        return
    now = datetime.utcnow().isoformat() if sold else None
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM ad_briefs WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE ad_briefs SET sold_at = ?, updated_at = ? WHERE ad_id = ?",
                (now, datetime.utcnow().isoformat(), ad_id),
            )
        else:
            ts = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO ad_briefs (ad_id, brief_md, sold_at, created_at, updated_at) "
                "VALUES (?, '', ?, ?, ?)",
                (ad_id, now, ts, ts),
            )


def is_ad_sold(ad_id: str) -> bool:
    """Помечено ли объявление проданным."""
    if not ad_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sold_at FROM ad_briefs WHERE ad_id = ?", (ad_id,)
        ).fetchone()
    return bool(row and row["sold_at"])


def mark_thread_sold(
    msg_id: int,
    sold_price_eur: float,
    close_other_threads_for_ad: bool = False,
) -> dict[str, Any]:
    """Зафиксировать продажу и свернуть тред.

    Поведение:
      - msg_id → status='skipped_sold', sold_price_eur=<price>.
      - Все pending in-rows этого же gmail_thread_id → status='skipped_sold' (без цены).
      - Если автопилот треда активен → stop_thread_autopilot(reason='sold').
      - close_other_threads_for_ad=True → то же самое (без цены) для ВСЕХ других
        тредов с тем же ad_id, у которых есть pending in-rows.
      - mark_ad_sold НЕ зовётся — товар может быть продан повторно следующему клиенту.

    Возвращает {"thread_id": ..., "ad_id": ..., "closed_other_threads": [thread_ids]}.
    """
    with get_conn() as conn:
        src = conn.execute(
            "SELECT id, gmail_thread_id, ad_id FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not src:
            raise ValueError(f"message {msg_id} not found")
        thread_id = src["gmail_thread_id"] or ""
        ad_id = src["ad_id"] or ""

        conn.execute(
            "UPDATE messages SET status='skipped_sold', sold_price_eur=? WHERE id=?",
            (float(sold_price_eur), msg_id),
        )
        if thread_id:
            conn.execute(
                "UPDATE messages SET status='skipped_sold' "
                "WHERE gmail_thread_id=? AND direction='in' AND status='pending'",
                (thread_id,),
            )

        closed_other: list[str] = []
        if close_other_threads_for_ad and ad_id:
            other = conn.execute(
                "SELECT DISTINCT gmail_thread_id FROM messages "
                "WHERE ad_id=? AND direction='in' AND status='pending' "
                "AND gmail_thread_id != ?",
                (ad_id, thread_id),
            ).fetchall()
            closed_other = [r["gmail_thread_id"] for r in other if r["gmail_thread_id"]]
            for other_thread in closed_other:
                conn.execute(
                    "UPDATE messages SET status='skipped_sold' "
                    "WHERE gmail_thread_id=? AND direction='in' AND status='pending'",
                    (other_thread,),
                )

    # Остановить автопилоты + закрыть треды ВНЕ транзакции (свои conn).
    if thread_id:
        ap = get_thread_autopilot(thread_id)
        if ap and ap["active"]:
            stop_thread_autopilot(thread_id, "sold")
        close_thread(thread_id, closed_by="sold")
    for other_thread in closed_other:
        ap = get_thread_autopilot(other_thread)
        if ap and ap["active"]:
            stop_thread_autopilot(other_thread, "sold")
        close_thread(other_thread, closed_by="sold")

    return {
        "thread_id": thread_id,
        "ad_id": ad_id,
        "closed_other_threads": closed_other,
    }


# --- LESSONS ---

def add_lesson(**fields: Any) -> int:
    """Сохранить пару (плохой_черновик, оператора_исправление). fields = столбцы lessons."""
    fields = {**fields, "created_at": datetime.utcnow().isoformat()}
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO lessons ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        return cur.lastrowid


def find_relevant_lessons(
    ad_id: Optional[str] = None,
    account_id: Optional[int] = None,
    client_lang: Optional[str] = None,
    limit: int = 5,
) -> list[sqlite3.Row]:
    """Топ-N уроков по релевантности.

    Ранжирование (через ORDER BY): сначала уроки по этому же ad_id,
    затем по этому же account_id, затем общие — внутри групп по recency (DESC id).
    """
    sql = """
        SELECT *,
            CASE WHEN ad_id = ? THEN 0
                 WHEN account_id = ? THEN 1
                 ELSE 2
            END AS rel_rank
        FROM lessons
        WHERE 1=1
    """
    params: list[Any] = [ad_id or "", account_id or -1]
    if client_lang:
        sql += " AND (client_lang = ? OR client_lang IS NULL)"
        params.append(client_lang)
    sql += " ORDER BY rel_rank ASC, id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def list_lessons_for_ad(ad_id: str, limit: int = 20) -> list[sqlite3.Row]:
    """Все уроки по конкретному объявлению."""
    if not ad_id:
        return []
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM lessons WHERE ad_id = ? ORDER BY id DESC LIMIT ?",
            (ad_id, limit),
        ).fetchall()


if __name__ == "__main__":
    # Ручная инициализация: python database.py
    init_db()
    print(f"БД инициализирована: {DB_PATH}")
