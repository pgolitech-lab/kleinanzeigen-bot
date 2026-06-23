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
        # Момент фиксации продажи в MA (sold-action). NULL для рядов проданных до фичи.
        _add_column_if_missing(conn, "messages", "sold_at", "TEXT")
        # Detected sales — кандидаты из LLM-сканирования старой переписки. Auto-commit
        # high-confidence сразу в messages; low/medium — review в MA.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detected_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_thread_id TEXT NOT NULL UNIQUE,
                is_sale INTEGER NOT NULL,
                confidence TEXT NOT NULL,         -- 'high' | 'medium' | 'low'
                sold_price_eur REAL,
                detected_ad_title TEXT,
                detected_sold_at TEXT,            -- ISO date (YYYY-MM-DD)
                evidence TEXT,
                model TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                applied_at TEXT,                  -- ISO когда применено к messages
                rejected_at TEXT,                 -- ISO когда отклонено оператором
                created_at TEXT NOT NULL
            )
        """)
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

        # --- MARKET SCOUT (разведка рынка Kleinanzeigen) ---
        # scout_queries — поисковые запросы (генерит LLM, редактирует оператор).
        # kind: 'car' | 'part'. category: 'c216' (Autos) | 'c223' (Autoteile).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                  -- 'car' | 'part'
                label TEXT,                          -- человекочитаемое имя
                keywords TEXT NOT NULL,              -- поисковая фраза (как вводят на сайте)
                category TEXT NOT NULL DEFAULT 'c216',
                enabled INTEGER NOT NULL DEFAULT 1,
                max_pages INTEGER NOT NULL DEFAULT 5,
                source TEXT,                         -- 'llm' | 'operator'
                notes TEXT,
                last_run_at TEXT,
                last_count INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # scout_listings — найденные объявления. PK=ad_id (дедуп между запросами/прогонами).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_listings (
                ad_id TEXT PRIMARY KEY,              -- 10-значный id Kleinanzeigen
                kind TEXT NOT NULL,                  -- 'car' | 'part'
                title TEXT,
                url TEXT,
                price_eur REAL,                      -- распарсенная числовая цена
                price_raw TEXT,                      -- '10.900 € VB'
                negotiable INTEGER,                  -- 1 если VB (Verhandlungsbasis)
                plz TEXT,                            -- 5-значный индекс
                city TEXT,
                bundesland TEXT,                     -- выведено из plz
                year INTEGER,                        -- год EZ (Erstzulassung) / год запчасти
                ez_raw TEXT,                         -- '08/2021'
                mileage_km INTEGER,
                fuel TEXT,                           -- diesel|electric|petrol|hybrid|null
                gearbox TEXT,                        -- automatik|manuell|null
                model_family TEXT,                   -- traveller|proace|zafira_life|...
                part_type TEXT,                      -- seat|bench|rail|other (для kind=part)
                condition TEXT,                      -- neu|gebraucht|null
                description TEXT,                     -- из JSON-LD (обрезано)
                posted_raw TEXT,                     -- 'Heute, 15:07' / '14.06.2026'
                shipping INTEGER,                    -- 1 если 'Versand möglich'
                query_id INTEGER,                    -- последний запрос который нашёл
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_kind ON scout_listings(kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_land ON scout_listings(bundesland)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_model ON scout_listings(model_family)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scout_active ON scout_listings(active)")
        # verified_kind — результат Haiku-проверки: 'car'|'part'|'other'|NULL(не проверено).
        # Эффективный вид объявления = COALESCE(verified_kind, kind). verified_at — когда.
        _add_column_if_missing(conn, "scout_listings", "verified_kind", "TEXT")
        _add_column_if_missing(conn, "scout_listings", "verified_at", "TEXT")
        # rejected=1 — оператор пометил объявление неверным и удалил. Скрыто везде +
        # НЕ реактивируется при повторном скрапе (upsert не трогает rejected-строки).
        _add_column_if_missing(conn, "scout_listings", "rejected", "INTEGER NOT NULL DEFAULT 0")
        # scout_corrections — операторские правки классификации для in-context обучения
        # Haiku. correct_kind: 'car'|'part'|'other'|'remove'.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id TEXT,
                title TEXT,
                description TEXT,
                was_kind TEXT,
                correct_kind TEXT NOT NULL,
                note TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL
            )
        """)


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


def list_thread_dispatches(gmail_thread_id: str) -> list[sqlite3.Row]:
    """Все fanout-копии всех мини-карточек треда (для broadcast thread-state)."""
    if not gmail_thread_id:
        return []
    with get_conn() as conn:
        return conn.execute(
            "SELECT cd.* FROM card_dispatches cd "
            "JOIN messages m ON m.id = cd.message_id "
            "WHERE m.gmail_thread_id = ? ORDER BY cd.id",
            (gmail_thread_id,),
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


def list_sales(
    period_from: Optional[str] = None,
    period_to: Optional[str] = None,
    account_id: Optional[int] = None,
    query: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Список продаж (по одной строке на gmail_thread_id).

    period_from/period_to — ISO timestamps (UTC). Фильтрует по sold_at если оно есть,
    иначе по created_at (fallback для legacy-рядов без sold_at).

    query — текстовое совпадение по ad_title или buyer_display_name (LIKE %q%).

    Возвращает список dict-ов: одна продажа на тред (MAX sold_price_eur per thread_id).
    """
    where: list[str] = ["m.status = 'skipped_sold'", "m.sold_price_eur IS NOT NULL"]
    args: list[Any] = []
    if period_from:
        where.append("COALESCE(m.sold_at, m.created_at) >= ?")
        args.append(period_from)
    if period_to:
        where.append("COALESCE(m.sold_at, m.created_at) < ?")
        args.append(period_to)
    if account_id is not None:
        where.append("m.account_id = ?")
        args.append(int(account_id))
    if query:
        where.append("(m.ad_title LIKE ? OR m.buyer_display_name LIKE ? OR m.buyer_name LIKE ?)")
        like = f"%{query}%"
        args.extend([like, like, like])

    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
            m.gmail_thread_id AS thread_id,
            m.ad_id,
            m.ad_title,
            m.ad_price,
            m.ad_url,
            m.buyer_name,
            m.buyer_display_name,
            m.account_id,
            a.name AS account_name,
            MAX(m.sold_price_eur) AS sold_price_eur,
            MAX(COALESCE(m.sold_at, m.created_at)) AS sold_at,
            COUNT(*) AS rows_count
        FROM messages m
        LEFT JOIN accounts a ON a.id = m.account_id
        WHERE {where_sql}
        GROUP BY m.gmail_thread_id
        ORDER BY sold_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def record_detection(
    *,
    gmail_thread_id: str,
    is_sale: bool,
    confidence: str,
    sold_price_eur: Optional[float],
    detected_ad_title: Optional[str],
    detected_sold_at: Optional[str],
    evidence: Optional[str],
    model: Optional[str],
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> int:
    """UPSERT detection-результат. Возвращает row id."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO detected_sales (gmail_thread_id, is_sale, confidence, "
            "sold_price_eur, detected_ad_title, detected_sold_at, evidence, "
            "model, tokens_in, tokens_out, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(gmail_thread_id) DO UPDATE SET "
            "is_sale=excluded.is_sale, confidence=excluded.confidence, "
            "sold_price_eur=excluded.sold_price_eur, "
            "detected_ad_title=excluded.detected_ad_title, "
            "detected_sold_at=excluded.detected_sold_at, "
            "evidence=excluded.evidence, model=excluded.model, "
            "tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out, "
            "cost_usd=excluded.cost_usd",
            (gmail_thread_id, 1 if is_sale else 0, confidence, sold_price_eur,
             detected_ad_title, detected_sold_at, evidence, model,
             tokens_in, tokens_out, cost_usd, now),
        )
        return cur.lastrowid


def list_pending_detections(*, include_rejected: bool = False) -> list[sqlite3.Row]:
    """Detection-кандидаты ожидающие review (is_sale=1 AND applied_at IS NULL AND rejected_at IS NULL).

    JOIN-ит ad_title/ad_price/buyer_display_name из последнего in-row треда для контекста.
    """
    extra = "" if include_rejected else "AND ds.rejected_at IS NULL"
    sql = f"""
        SELECT ds.*,
               m.ad_title AS thread_ad_title,
               m.ad_price AS thread_ad_price,
               m.ad_url   AS thread_ad_url,
               m.buyer_display_name,
               m.buyer_name,
               m.account_id,
               a.name AS account_name
          FROM detected_sales ds
          LEFT JOIN messages m ON m.id = (
              SELECT id FROM messages WHERE gmail_thread_id = ds.gmail_thread_id
               AND direction='in' ORDER BY created_at DESC LIMIT 1
          )
          LEFT JOIN accounts a ON a.id = m.account_id
         WHERE ds.is_sale = 1
           AND ds.applied_at IS NULL
           {extra}
         ORDER BY
           CASE ds.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
           ds.detected_sold_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def apply_detection_to_messages(
    detection_id: int,
    *,
    sold_price_eur: Optional[float] = None,
) -> dict[str, Any]:
    """Применить detection к messages: помечает latest in-row треда как skipped_sold,
    записывает sold_price_eur (с возможностью override) + sold_at (detected или now),
    закрывает тред в closed_threads. Возвращает {thread_id, msg_id, price}.
    """
    with get_conn() as conn:
        det = conn.execute(
            "SELECT * FROM detected_sales WHERE id = ?", (detection_id,)
        ).fetchone()
        if not det:
            raise ValueError(f"detection {detection_id} not found")
        if det["applied_at"]:
            raise ValueError(f"detection {detection_id} already applied")
        thread_id = det["gmail_thread_id"]
        price = sold_price_eur if sold_price_eur is not None else det["sold_price_eur"]
        if price is None:
            raise ValueError("no price provided and detection has no price")

        # Найти latest in-row для маркировки
        row = conn.execute(
            "SELECT id FROM messages WHERE gmail_thread_id = ? AND direction='in' "
            "ORDER BY created_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"no in-row found in thread {thread_id}")
        msg_id = row["id"]

        # sold_at: предпочитаем detected ISO-date + 12:00:00, fallback now
        if det["detected_sold_at"]:
            sold_at = f"{det['detected_sold_at']}T12:00:00"
        else:
            sold_at = datetime.utcnow().isoformat()

        conn.execute(
            "UPDATE messages SET status='skipped_sold', sold_price_eur=?, sold_at=? WHERE id=?",
            (float(price), sold_at, msg_id),
        )
        # Pending in-rows этого же треда → skipped (на случай legacy состояний)
        conn.execute(
            "UPDATE messages SET status='skipped' "
            "WHERE gmail_thread_id=? AND direction='in' AND id != ? "
            "AND status IN ('pending','new','edited','approved')",
            (thread_id, msg_id),
        )
        conn.execute(
            "UPDATE detected_sales SET applied_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), detection_id),
        )

    # close_thread в своей транзакции
    close_thread(thread_id, closed_by="detected-sale")
    return {"thread_id": thread_id, "msg_id": msg_id, "price": float(price)}


def add_manual_sale(
    *,
    account_id: int,
    ad_title: str,
    ad_price: Optional[str],
    sold_price_eur: float,
    sold_at: str,
    buyer_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Создать manual sale-запись (сделка вне бота, без gmail_thread_id из переписки).

    Вставляет в messages синтетическую row direction='in', status='skipped_sold',
    gmail_thread_id='manual-<timestamp>-<rowid>'. После — close_thread чтобы запись
    не попала в pipeline.
    """
    import uuid
    now_iso = datetime.utcnow().isoformat()
    synthetic_thread = f"manual-{uuid.uuid4().hex[:16]}"
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "ad_title, ad_price, buyer_display_name, sold_price_eur, sold_at, "
            "extra_notes, created_at) "
            "VALUES (?, 'in', 'skipped_sold', ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, synthetic_thread, ad_title, ad_price, buyer_name,
             float(sold_price_eur), sold_at, notes, now_iso),
        )
        msg_id = cur.lastrowid

    close_thread(synthetic_thread, closed_by="manual-sale")
    return {"msg_id": msg_id, "thread_id": synthetic_thread}


def reject_detection(detection_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE detected_sales SET rejected_at=? WHERE id=? AND applied_at IS NULL",
            (datetime.utcnow().isoformat(), detection_id),
        )


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

        now_iso = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE messages SET status='skipped_sold', sold_price_eur=?, sold_at=? WHERE id=?",
            (float(sold_price_eur), now_iso, msg_id),
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


# --- MARKET SCOUT: QUERIES ---

def add_scout_query(
    kind: str,
    keywords: str,
    category: str = "c216",
    label: Optional[str] = None,
    max_pages: int = 5,
    source: str = "operator",
    enabled: bool = True,
    notes: Optional[str] = None,
) -> int:
    """Создать поисковый запрос разведки. Возвращает id."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scout_queries "
            "(kind, label, keywords, category, enabled, max_pages, source, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, label, keywords, category, 1 if enabled else 0,
             int(max_pages), source, notes, now, now),
        )
        return cur.lastrowid


def list_scout_queries(only_enabled: bool = False) -> list[sqlite3.Row]:
    """Все поисковые запросы разведки."""
    sql = "SELECT * FROM scout_queries"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY kind, id"
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def get_scout_query(query_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM scout_queries WHERE id = ?", (query_id,)
        ).fetchone()


def update_scout_query(query_id: int, **fields: Any) -> None:
    """Обновить произвольные поля запроса."""
    allowed = {"kind", "label", "keywords", "category", "enabled",
               "max_pages", "source", "notes", "last_run_at", "last_count"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE scout_queries SET {cols} WHERE id = ?",
                     (*fields.values(), query_id))


def delete_scout_query(query_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM scout_queries WHERE id = ?", (query_id,))


def scout_query_exists(kind: str, keywords: str, category: str) -> bool:
    """Дедуп при LLM-генерации: запрос с такими kind+keywords+category уже есть?"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM scout_queries WHERE kind=? AND lower(keywords)=lower(?) AND category=?",
            (kind, keywords.strip(), category),
        ).fetchone()
    return bool(row)


# --- MARKET SCOUT: LISTINGS ---

def upsert_scout_listing(listing: dict[str, Any]) -> bool:
    """UPSERT объявления по ad_id. Возвращает True если строка новая (insert).

    `listing` — dict с ключами-колонками scout_listings (без first/last_seen).
    first_seen_at ставится при первом появлении, last_seen_at обновляется всегда.
    """
    ad_id = str(listing.get("ad_id") or "").strip()
    if not ad_id:
        return False
    now = datetime.utcnow().isoformat()
    cols = ["kind", "title", "url", "price_eur", "price_raw", "negotiable",
            "plz", "city", "bundesland", "year", "ez_raw", "mileage_km",
            "fuel", "gearbox", "model_family", "part_type", "condition",
            "description", "posted_raw", "shipping", "query_id"]
    vals = [listing.get(c) for c in cols]
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT rejected FROM scout_listings WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        if existing:
            # Отклонённые оператором не реактивируем (active остаётся 0).
            is_rejected = bool(existing["rejected"]) if "rejected" in existing.keys() else False
            active_set = "" if is_rejected else ", active = 1"
            set_sql = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE scout_listings SET {set_sql}, last_seen_at = ?{active_set} WHERE ad_id = ?",
                (*vals, now, ad_id),
            )
            return False
        all_cols = ["ad_id"] + cols + ["first_seen_at", "last_seen_at", "active"]
        placeholders = ", ".join("?" * len(all_cols))
        conn.execute(
            f"INSERT INTO scout_listings ({', '.join(all_cols)}) VALUES ({placeholders})",
            (ad_id, *vals, now, now, 1),
        )
        return True


# Эффективный вид объявления: результат Haiku-проверки имеет приоритет над
# kind, выставленным запросом-источником.
_EFF_KIND = "COALESCE(verified_kind, kind)"


def deactivate_stale_scout_listings(kind: str, before_iso: str) -> int:
    """Пометить active=0 объявления данного (effective) kind, не виденные с before_iso.

    before_iso — порог ПО ВОЗРАСТУ (now - scout_stale_days), НЕ время старта прогона.
    Так один упавший/заблокированный прогон не обнуляет всю базу: деактивируются
    лишь объявления, которых нет уже несколько дней. Возвращает кол-во.
    """
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE scout_listings SET active = 0 "
            f"WHERE {_EFF_KIND} = ? AND active = 1 AND last_seen_at < ?",
            (kind, before_iso),
        )
        return cur.rowcount


def list_scout_listings(
    kind: Optional[str] = None,
    only_active: bool = True,
    bundesland: Optional[str] = None,
    model_family: Optional[str] = None,
    fuel: Optional[str] = None,
    gearbox: Optional[str] = None,
    part_type: Optional[str] = None,
    condition: Optional[str] = None,
    city: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Объявления разведки с фильтрами (по ЭФФЕКТИВНОМУ виду). Сортировка: земля, город, цена."""
    sql = "SELECT * FROM scout_listings WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += f" AND {_EFF_KIND} = ?"; params.append(kind)
    if only_active:
        sql += " AND active = 1"
    if bundesland:
        sql += " AND bundesland = ?"; params.append(bundesland)
    if city:
        sql += " AND city = ?"; params.append(city)
    if model_family:
        sql += " AND model_family = ?"; params.append(model_family)
    if fuel:
        sql += " AND fuel = ?"; params.append(fuel)
    if gearbox:
        sql += " AND gearbox = ?"; params.append(gearbox)
    if part_type:
        sql += " AND part_type = ?"; params.append(part_type)
    if condition:
        sql += " AND condition = ?"; params.append(condition)
    sql += (" ORDER BY COALESCE(bundesland, 'яя'), COALESCE(city, ''), "
            "COALESCE(price_eur, 1e12)")
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def scout_region_summary(kind: str) -> list[sqlite3.Row]:
    """Агрегат по землям (effective kind): кол-во активных, мин/сред/макс цены."""
    sql = f"""
        SELECT COALESCE(bundesland, '— неизвестно —') AS bundesland,
               COUNT(*) AS cnt,
               MIN(price_eur) AS min_price,
               AVG(price_eur) AS avg_price,
               MAX(price_eur) AS max_price
        FROM scout_listings
        WHERE {_EFF_KIND} = ? AND active = 1
        GROUP BY bundesland
        ORDER BY cnt DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (kind,)).fetchall()


def scout_city_summary(kind: str) -> list[sqlite3.Row]:
    """Агрегат по городам (effective kind): город, земля, кол-во, мин/сред/макс цены.
    Отсортировано по убыванию количества."""
    sql = f"""
        SELECT COALESCE(city, '— неизвестно —') AS city,
               bundesland,
               COUNT(*) AS cnt,
               MIN(price_eur) AS min_price,
               AVG(price_eur) AS avg_price,
               MAX(price_eur) AS max_price
        FROM scout_listings
        WHERE {_EFF_KIND} = ? AND active = 1
        GROUP BY city, bundesland
        ORDER BY cnt DESC, city
    """
    with get_conn() as conn:
        return conn.execute(sql, (kind,)).fetchall()


def scout_counts() -> dict[str, int]:
    """Сводка по активным: машины, запчасти (effective kind), other, не проверено."""
    with get_conn() as conn:
        def c(where: str, *p: Any) -> int:
            return conn.execute(
                f"SELECT COUNT(*) c FROM scout_listings WHERE active=1 AND {where}", p
            ).fetchone()["c"]
        return {
            "cars": c(f"{_EFF_KIND}='car'"),
            "parts": c(f"{_EFF_KIND}='part'"),
            "other": c(f"{_EFF_KIND}='other'"),
            "unverified": c("verified_kind IS NULL"),
        }


# --- MARKET SCOUT: Haiku-проверка типа ---

def list_unverified_scout_listings(limit: int = 500) -> list[sqlite3.Row]:
    """Активные объявления без verified_kind (для батч-проверки Haiku)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT ad_id, kind, title, description FROM scout_listings "
            "WHERE active = 1 AND verified_kind IS NULL ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def set_scout_verified_kind(ad_id: str, verified_kind: str) -> None:
    """Записать результат Haiku-проверки ('car'|'part'|'other')."""
    if not ad_id:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE scout_listings SET verified_kind = ?, verified_at = ? WHERE ad_id = ?",
            (verified_kind, datetime.utcnow().isoformat(), ad_id),
        )


def reset_scout_verification() -> int:
    """Сбросить verified_kind у всех (для перепроверки). Возвращает кол-во строк."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE scout_listings SET verified_kind = NULL, verified_at = NULL "
            "WHERE verified_kind IS NOT NULL"
        )
        return cur.rowcount


# --- MARKET SCOUT: операторские корректировки (+ обучение Haiku) ---

def apply_scout_correction(
    ad_id: str,
    correct_kind: str,
    note: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """Операторская правка результата.

    correct_kind:
      'remove'             → пометить rejected (скрыть + не реактивировать),
      'car'|'part'|'other' → выставить verified_kind (переклассификация).
    Записывает правку в scout_corrections для in-context обучения Haiku.
    Возвращает {ok, action, ad_id}.
    """
    if not ad_id:
        return {"ok": False, "error": "no ad_id"}
    correct_kind = (correct_kind or "").strip().lower()
    if correct_kind not in ("remove", "car", "part", "other"):
        return {"ok": False, "error": "bad correct_kind"}

    with get_conn() as conn:
        row = conn.execute(
            "SELECT ad_id, title, description, COALESCE(verified_kind, kind) AS eff_kind "
            "FROM scout_listings WHERE ad_id = ?", (ad_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "not found"}
        was_kind = row["eff_kind"]
        # лог правки
        conn.execute(
            "INSERT INTO scout_corrections "
            "(ad_id, title, description, was_kind, correct_kind, note, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ad_id, row["title"], row["description"], was_kind, correct_kind,
             note, created_by, datetime.utcnow().isoformat()),
        )
        if correct_kind == "remove":
            conn.execute(
                "UPDATE scout_listings SET rejected = 1, active = 0 WHERE ad_id = ?", (ad_id,))
            action = "removed"
        else:
            conn.execute(
                "UPDATE scout_listings SET verified_kind = ?, verified_at = ? WHERE ad_id = ?",
                (correct_kind, datetime.utcnow().isoformat(), ad_id))
            action = f"reclassified→{correct_kind}"
    return {"ok": True, "action": action, "ad_id": ad_id, "was_kind": was_kind}


def recent_scout_corrections(limit: int = 30) -> list[sqlite3.Row]:
    """Свежие операторские правки — few-shot примеры для классификатора Haiku."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT title, description, was_kind, correct_kind FROM scout_corrections "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()


def list_scout_corrections(limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM scout_corrections ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()


if __name__ == "__main__":
    # Ручная инициализация: python database.py
    init_db()
    print(f"БД инициализирована: {DB_PATH}")
