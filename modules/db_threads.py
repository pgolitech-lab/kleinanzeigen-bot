# Thread-state, history, clients, pipeline queries.
# Выделено из database.py.

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from database import DB_PATH, get_conn

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


# --- THREAD FLAGS ---

def get_thread_flags(gmail_thread_id: str):
    """Получить флаги треда (is_pinned, operator_unread). None если строки нет."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM thread_flags WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()


def set_thread_flags(
    gmail_thread_id: str,
    *,
    is_pinned=None,
    operator_unread=None,
) -> None:
    """Upsert флаги треда. Передавай только те поля, что нужно изменить."""
    if not gmail_thread_id:
        return
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO thread_flags (gmail_thread_id, updated_at) VALUES (?, ?)",
            (gmail_thread_id, now),
        )
        if is_pinned is not None:
            conn.execute(
                "UPDATE thread_flags SET is_pinned=?, updated_at=? WHERE gmail_thread_id=?",
                (is_pinned, now, gmail_thread_id),
            )
        if operator_unread is not None:
            conn.execute(
                "UPDATE thread_flags SET operator_unread=?, updated_at=? WHERE gmail_thread_id=?",
                (operator_unread, now, gmail_thread_id),
            )


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
          -- Треды под активным автопилотом не пингуем — автопилот сам ведёт диалог
          AND NOT EXISTS (
              SELECT 1 FROM thread_autopilot ta
               WHERE ta.gmail_thread_id = m.gmail_thread_id
                 AND ta.active = 1
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
             ORDER BY s.id DESC LIMIT 1) AS ad_price,
            (SELECT deal_brief_json FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND s.deal_brief_json IS NOT NULL AND s.deal_brief_json != ''
             ORDER BY s.id DESC LIMIT 1) AS deal_brief_json
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
    ),
    flags AS (
        SELECT gmail_thread_id,
               COALESCE(is_pinned, 0)       AS is_pinned,
               COALESCE(operator_unread, 0) AS operator_unread
          FROM thread_flags
    )
    SELECT li.*,
           le.last_event_at,
           le.last_event_kind,
           COALESCE(c.any_sent_count, 0)         AS any_sent_count,
           COALESCE(c.real_sent_count, 0)        AS real_sent_count,
           COALESCE(c.pending_drafts_count, 0)   AS pending_drafts_count,
           CASE WHEN COALESCE(c.any_sent_count, 0) > 0 THEN 1 ELSE 0 END   AS has_any_sent,
           CASE WHEN COALESCE(c.real_sent_count, 0) > 0 THEN 1 ELSE 0 END  AS has_real_reply,
           CASE WHEN COALESCE(c.pending_drafts_count, 0) > 0 THEN 1 ELSE 0 END AS has_pending_draft,
           COALESCE(f.is_pinned, 0)       AS is_pinned,
           COALESCE(f.operator_unread, 0) AS operator_unread
    FROM last_in li
    LEFT JOIN counts c ON c.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN last_event le ON le.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN flags f ON f.gmail_thread_id = li.gmail_thread_id
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



# --- CLIENT PROFILES ---

def get_client_profile(buyer_email: str) -> Optional[sqlite3.Row]:
    """Теги и заметка оператора по покупателю. None если профиль не создан."""
    if not buyer_email:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM client_profiles WHERE buyer_email = ?",
            (buyer_email,),
        ).fetchone()


def upsert_client_profile(buyer_email: str, tags: list[str], note: str) -> None:
    """Сохранить или обновить теги + заметку для покупателя."""
    if not buyer_email:
        return
    import json as _json
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO client_profiles (buyer_email, tags_json, note, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(buyer_email) DO UPDATE SET
                tags_json  = excluded.tags_json,
                note       = excluded.note,
                updated_at = excluded.updated_at
            """,
            (buyer_email, _json.dumps(tags, ensure_ascii=False), note),
        )
