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
        # negotiation_state — структурное состояние сделки per-thread (Track B).
        # «Код владеет числами»: our_last_offer_eur пишется ТОЛЬКО кодом при
        # реальной отправке оферты — надёжный якорь ratchet вместо чтения из
        # неоднозначного deal_brief.negotiated_price_eur. Заполняется/используется
        # в Инкременте 3; здесь только схема + базовые CRUD-хелперы.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS negotiation_state (
                gmail_thread_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'opening',
                mode TEXT NOT NULL DEFAULT 'manual',
                list_price_eur REAL,
                floor_eur REAL,
                our_last_offer_eur REAL,
                buyer_last_offer_eur REAL,
                escalation_reason TEXT,
                updated_at TEXT NOT NULL
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
                read_by TEXT DEFAULT NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_card_dispatches_msg ON card_dispatches(message_id)"
        )
        # migration: add read_by if missing (for existing DBs)
        try:
            conn.execute("ALTER TABLE card_dispatches ADD COLUMN read_by TEXT DEFAULT NULL")
        except Exception:
            pass

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
        # deactivated_at — когда active flip'нулся 1→0 в deactivate_stale_scout_listings
        # (объявление пропало из выдачи дольше scout_stale_days — считаем продано/снято).
        # Нужно для дневной сводки («что продалось из находок»).
        _add_column_if_missing(conn, "scout_listings", "deactivated_at", "TEXT")
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
        # client_profiles — теги и заметки оператора по покупателю
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_profiles (
                buyer_email TEXT PRIMARY KEY,
                tags_json   TEXT NOT NULL DEFAULT '[]',
                note        TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # thread_flags — операторские флаги тредов: закрепить, непрочитано.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_flags (
                gmail_thread_id TEXT PRIMARY KEY,
                is_pinned       INTEGER NOT NULL DEFAULT 0,
                operator_unread INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT    NOT NULL
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


def mark_card_dispatch_read(message_id: int, chat_id: str, reader_label: str) -> None:
    """Пометить что оператор (chat_id) прочитал уведомление. reader_label = 'Имя · ЧЧ:ММ'."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE card_dispatches SET read_by = ? WHERE message_id = ? AND chat_id = ?",
            (reader_label, message_id, str(chat_id)),
        )


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




# re-export scout-функций (перенесены в modules/db_scout.py)
from modules.db_scout import (  # noqa: F401
    add_scout_query,
    list_scout_queries,
    get_scout_query,
    update_scout_query,
    delete_scout_query,
    scout_query_exists,
    upsert_scout_listing,
    deactivate_stale_scout_listings,
    list_scout_listings,
    scout_region_summary,
    scout_city_summary,
    scout_counts,
    scout_daily_stats,
    list_unverified_scout_listings,
    get_scout_effective_kinds,
    set_scout_verified_kind,
    reset_scout_verification,
    apply_scout_correction,
    recent_scout_corrections,
    list_scout_corrections,
)

# re-export thread-функций (перенесены в modules/db_threads.py)
from modules.db_threads import (  # noqa: F401
    close_thread,
    reopen_thread,
    is_thread_closed,
    mark_thread_waiting,
    unmark_thread_waiting,
    is_thread_waiting,
    thread_close_reason,
    should_reopen_closed_thread,
    get_negotiation_state,
    upsert_negotiation_state,
    record_our_offer,
    mark_processed,
    known_message_ids_since,
    append_extra_note,
    find_by_gmail_message_id,
    has_recent_identical_incoming,
    find_reminder_candidates,
    thread_history,
    thread_events,
    list_clients,
    find_related_inquiries,
    list_threads_for_client,
    pipeline_threads,
    list_threads,
    get_client_profile,
    upsert_client_profile,
    get_thread_flags,
    set_thread_flags,
)

# re-export sales-функций (перенесены в modules/db_sales.py)
from modules.db_sales import (  # noqa: F401
    mark_ad_sold,
    is_ad_sold,
    list_sales,
    record_detection,
    list_pending_detections,
    apply_detection_to_messages,
    add_manual_sale,
    reject_detection,
    mark_thread_sold,
    add_lesson,
    find_relevant_lessons,
    list_lessons_for_ad,
)
