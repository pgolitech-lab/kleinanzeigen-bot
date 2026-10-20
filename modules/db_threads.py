# Thread-state, history, clients, pipeline queries.
# Выделено из database.py.

import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from database import DB_PATH, get_conn, norm_name, transaction

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


# --- SOLD-REOPEN GUARD ---

# closed_by значения, означающие продажу: новое письмо в такой тред НЕ должно
# авто-реоткрывать его (не воскрешать pipeline/автопилот). См. Bug 6.
SALE_CLOSE_REASONS = {"sold", "detected-sale"}


def thread_close_reason(gmail_thread_id: str) -> Optional[str]:
    """Причина закрытия треда (closed_by) или None, если тред не закрыт."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT closed_by FROM closed_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()
    return row["closed_by"] if row else None


def should_reopen_closed_thread(gmail_thread_id: str) -> bool:
    """Можно ли авто-реоткрывать закрытый тред при новом incoming.

    False, если тред закрыт как продажа (sold / detected-sale) — такое письмо
    уйдёт оператору на ревью, но тред остаётся закрытым и автопилот не воскресает.
    False также если тред вообще не закрыт (реоткрывать нечего).
    """
    reason = thread_close_reason(gmail_thread_id)
    if reason is None:
        return False
    return reason not in SALE_CLOSE_REASONS


# --- NEGOTIATION STATE (Track B) ---

NEGOTIATION_PHASES = {"opening", "negotiating", "closing", "escalated", "stopped"}
NEGOTIATION_MODES = {"manual", "auto"}

# Колонки negotiation_state, которые разрешено писать через upsert (кроме PK/updated_at).
_NEG_STATE_COLS = {
    "phase", "mode", "list_price_eur", "floor_eur",
    "our_last_offer_eur", "buyer_last_offer_eur", "escalation_reason",
}


def get_negotiation_state(gmail_thread_id: str) -> Optional[sqlite3.Row]:
    """Состояние сделки треда или None, если ещё не создано."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM negotiation_state WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()


def upsert_negotiation_state(gmail_thread_id: str, **fields: Any) -> None:
    """Создать/обновить состояние сделки. Пишутся только известные колонки
    (`_NEG_STATE_COLS`); неизвестные ключи молча игнорируются. Всегда обновляет
    updated_at. Пустой thread_id — no-op."""
    if not gmail_thread_id:
        return
    known = {k: v for k, v in fields.items() if k in _NEG_STATE_COLS}
    now = datetime.utcnow().isoformat()
    # Один атомарный UPSERT вместо INSERT OR IGNORE + UPDATE на каждую колонку.
    cols = list(known)
    insert_cols = ", ".join(["gmail_thread_id", "updated_at", *cols])
    placeholders = ", ".join("?" * (2 + len(cols)))
    set_sql = ", ".join([*(f"{c} = excluded.{c}" for c in cols), "updated_at = excluded.updated_at"])
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO negotiation_state ({insert_cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(gmail_thread_id) DO UPDATE SET {set_sql}",
            (gmail_thread_id, now, *known.values()),
        )


def record_our_offer(gmail_thread_id: str, offer_eur: float) -> None:
    """Зафиксировать последнюю цену, которую озвучили МЫ (якорь ratchet).
    Пишется кодом при реальной отправке оферты."""
    if not gmail_thread_id:
        return
    upsert_negotiation_state(gmail_thread_id, our_last_offer_eur=float(offer_eur))


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
    fields: dict[str, Any] = {}
    if is_pinned is not None:
        fields["is_pinned"] = is_pinned
    if operator_unread is not None:
        fields["operator_unread"] = operator_unread
    now = datetime.utcnow().isoformat()
    # Один атомарный UPSERT: новые строки получают DEFAULT 0 для непереданных флагов.
    cols = list(fields)
    insert_cols = ", ".join(["gmail_thread_id", "updated_at", *cols])
    placeholders = ", ".join("?" * (2 + len(cols)))
    set_sql = ", ".join([*(f"{c} = excluded.{c}" for c in cols), "updated_at = excluded.updated_at"])
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO thread_flags ({insert_cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(gmail_thread_id) DO UPDATE SET {set_sql}",
            (gmail_thread_id, now, *fields.values()),
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
    # Read-modify-write под блокировкой записи — параллельная заметка не потеряется.
    with transaction() as conn:
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


def _normalize_body(text: Optional[str]) -> str:
    """Нормализовать тело письма для сравнения: collapse whitespace, strip, lower."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def has_recent_identical_incoming(
    gmail_thread_id: str,
    buyer_email: str,
    de_client: str,
    within_hours: float = 12.0,
) -> bool:
    """Есть ли уже недавнее incoming с идентичным телом в этом треде от этого
    отправителя — защита от relay-повторов Kleinanzeigen с новым Message-ID (Bug 9).

    Сравнение по нормализованному тексту (collapse whitespace / lower). Окно
    within_hours ограничивает совпадение свежими повторами, чтобы легитимные
    одинаковые короткие сообщения («Danke») спустя дни не глотались.
    """
    if not (gmail_thread_id and buyer_email and de_client):
        return False
    norm = _normalize_body(de_client)
    if not norm:
        return False
    cutoff = (datetime.utcnow() - timedelta(hours=float(within_hours))).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT de_client FROM messages "
            "WHERE gmail_thread_id = ? AND buyer_name = ? AND direction = 'in' "
            "AND created_at > ? AND de_client IS NOT NULL",
            (gmail_thread_id, buyer_email, cutoff),
        ).fetchall()
    return any(_normalize_body(r["de_client"]) == norm for r in rows)


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
        -- MAX() OVER вместо коррелированного подзапроса по CTE (тот был O(N²)).
        -- При равенстве времени остаются все «последние» события, как и раньше.
        ranked AS (
            SELECT e.*, MAX(e.at_time) OVER (PARTITION BY e.gmail_thread_id) AS max_at
              FROM events e
        ),
        last_event AS (
            SELECT id, gmail_thread_id, at_time, kind FROM ranked WHERE at_time = max_at
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
    # Один проход оконными функциями вместо двух коррелированных подзапросов на
    # каждого клиента. display_name / last_status — по последнему id среди ВСЕХ
    # строк клиента (любое direction), агрегаты — только по входящим, как раньше.
    sql = """
        WITH b AS (
            SELECT buyer_name, direction, ad_id, gmail_thread_id, created_at, cost_usd,
                   status, buyer_display_name,
                   ROW_NUMBER() OVER (PARTITION BY buyer_name ORDER BY id DESC) AS rn,
                   ROW_NUMBER() OVER (
                       PARTITION BY buyer_name
                       ORDER BY (buyer_display_name IS NULL), id DESC
                   ) AS rn_dn
              FROM messages
             WHERE buyer_name IS NOT NULL AND buyer_name != ''
        )
        SELECT
            buyer_name AS email,
            MAX(CASE WHEN rn_dn = 1 THEN buyer_display_name END) AS display_name,
            COUNT(DISTINCT CASE WHEN direction = 'in' THEN ad_id END) AS ad_count,
            COUNT(DISTINCT CASE WHEN direction = 'in' THEN gmail_thread_id END) AS thread_count,
            SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END) AS msg_count,
            MAX(CASE WHEN direction = 'in' THEN created_at END) AS last_at,
            MAX(CASE WHEN rn = 1 THEN status END) AS last_status,
            COALESCE(SUM(CASE WHEN direction = 'in' THEN cost_usd END), 0) AS total_cost
        FROM b
        GROUP BY buyer_name
        HAVING SUM(CASE WHEN direction = 'in' THEN 1 ELSE 0 END) > 0
        ORDER BY last_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def find_related_inquiries(
    buyer_email: Optional[str],
    exclude_thread_id: Optional[str] = None,
    limit: int = 10,
    *,
    display_name: Optional[str] = None,
    account_id: Optional[int] = None,
    include_maybe: bool = False,
) -> list[sqlite3.Row]:
    """Найти ДРУГИЕ треды того же клиента.

    Уровень 1 (точно, maybe_same=0): тот же email (buyer_name). Раньше матчили по
    buyer_display_name, из-за чего однофамильцы склеивались (Bug 8).
    Уровень 2 (возможно, maybe_same=1): relay-email Kleinanzeigen живёт на паре
    «покупатель + объявление», поэтому тот же человек по другому объявлению
    приходит с НОВЫМ email. Ловим его по нормализованному buyer_display_name
    (norm_name) в ТОМ ЖЕ аккаунте. Это догадка — UI должен показывать её как
    «возможно тот же клиент».

    Уровень 2 включается ТОЛЬКО с include_maybe=True — вызывающий обязан показать
    maybe_same оператору (иначе догадка выглядит как факт и возвращается Bug 8).
    display_name / account_id берутся из аргументов, а если не заданы — из
    последнего входящего треда exclude_thread_id.

    Возвращает по одной row на gmail_thread_id (последний по id incoming в треде),
    сначала точные, внутри — DESC по времени последнего события.
    """
    if not buyer_email or not buyer_email.strip():
        return []
    email = buyer_email.strip()
    with get_conn() as conn:
        if include_maybe and exclude_thread_id and (display_name is None or account_id is None):
            cur = conn.execute(
                "SELECT buyer_display_name, account_id FROM messages "
                "WHERE gmail_thread_id = ? AND direction = 'in' "
                "ORDER BY id DESC LIMIT 1",
                (exclude_thread_id,),
            ).fetchone()
            if cur:
                if display_name is None:
                    display_name = cur["buyer_display_name"]
                if account_id is None:
                    account_id = cur["account_id"]
        name_key = (norm_name(display_name)
                    if include_maybe and account_id is not None else None)
        # Сначала кандидатные треды (по индексам buyer_name / account_id), и только
        # по ним — последний входящий, а не ROW_NUMBER по всей таблице.
        sql = """
        WITH cand AS (
            SELECT DISTINCT gmail_thread_id FROM messages
             WHERE direction = 'in'
               AND gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
               AND (buyer_name = ?
                    OR (? IS NOT NULL AND account_id = ?
                        AND norm_name(buyer_display_name) = ?))
        ),
        last_ids AS (
            SELECT m.id, ROW_NUMBER() OVER (
                       PARTITION BY m.gmail_thread_id ORDER BY m.id DESC) AS rn
              FROM messages m JOIN cand c ON c.gmail_thread_id = m.gmail_thread_id
             WHERE m.direction = 'in'
        )
        SELECT m.*,
               CASE WHEN m.buyer_name = ? THEN 0 ELSE 1 END AS maybe_same
          FROM messages m
          JOIN last_ids r ON r.id = m.id AND r.rn = 1
         WHERE (m.buyer_name = ?
                OR (? IS NOT NULL AND m.account_id = ?
                    AND norm_name(m.buyer_display_name) = ?))
        """
        params: list[Any] = [email, name_key, account_id, name_key,
                             email, email, name_key, account_id, name_key]
        if exclude_thread_id:
            sql += " AND m.gmail_thread_id != ?"
            params.append(exclude_thread_id)
        sql += " ORDER BY maybe_same ASC, COALESCE(m.sent_at, m.created_at) DESC LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()


def list_threads_for_client(buyer_email: str) -> list[sqlite3.Row]:
    """Все треды конкретного покупателя (по buyer_name = email)."""
    if not buyer_email:
        return []
    # msg_count/last_at — по строкам ЭТОГО клиента; last_status и поля объявления —
    # по последней (по id) непустой строке всего треда, как было с подзапросами.
    sql = """
        WITH mine AS (
            SELECT gmail_thread_id,
                   COUNT(*) AS msg_count,
                   MAX(created_at) AS last_at
              FROM messages
             WHERE buyer_name = ?
               AND gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
             GROUP BY gmail_thread_id
        ),
        t AS (
            SELECT m.gmail_thread_id, m.status, m.ad_title, m.ad_id, m.ad_price,
                   m.deal_brief_json,
                   ROW_NUMBER() OVER w AS rn,
                   ROW_NUMBER() OVER (PARTITION BY m.gmail_thread_id
                       ORDER BY (m.ad_title IS NULL OR m.ad_title = ''), m.id DESC) AS rn_title,
                   ROW_NUMBER() OVER (PARTITION BY m.gmail_thread_id
                       ORDER BY (m.ad_id IS NULL OR m.ad_id = ''), m.id DESC) AS rn_ad,
                   ROW_NUMBER() OVER (PARTITION BY m.gmail_thread_id
                       ORDER BY (m.ad_price IS NULL OR m.ad_price = ''), m.id DESC) AS rn_price,
                   ROW_NUMBER() OVER (PARTITION BY m.gmail_thread_id
                       ORDER BY (m.deal_brief_json IS NULL OR m.deal_brief_json = ''), m.id DESC)
                       AS rn_brief
              FROM messages m
              JOIN mine ON mine.gmail_thread_id = m.gmail_thread_id
            WINDOW w AS (PARTITION BY m.gmail_thread_id ORDER BY m.id DESC)
        )
        SELECT
            mine.gmail_thread_id AS thread_id,
            mine.msg_count,
            mine.last_at,
            MAX(CASE WHEN t.rn = 1 THEN t.status END) AS last_status,
            MAX(CASE WHEN t.rn_title = 1 AND t.ad_title != '' THEN t.ad_title END) AS ad_title,
            MAX(CASE WHEN t.rn_ad = 1 AND t.ad_id != '' THEN t.ad_id END) AS ad_id,
            MAX(CASE WHEN t.rn_price = 1 AND t.ad_price != '' THEN t.ad_price END) AS ad_price,
            MAX(CASE WHEN t.rn_brief = 1 AND t.deal_brief_json != ''
                     THEN t.deal_brief_json END) AS deal_brief_json
        FROM mine
        JOIN t ON t.gmail_thread_id = mine.gmail_thread_id
        GROUP BY mine.gmail_thread_id
        ORDER BY mine.last_at DESC
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
    -- ROW_NUMBER() вместо коррелированного MAX по CTE. При равном времени
    -- (на проде не встречается) детерминированно выигрывает 'out'.
    last_event AS (
        SELECT gmail_thread_id, at_time AS last_event_at, kind AS last_event_kind
          FROM (
            SELECT e.*, ROW_NUMBER() OVER (
                       PARTITION BY e.gmail_thread_id
                       ORDER BY e.at_time DESC, e.kind DESC) AS rn
              FROM events e
          )
         WHERE rn = 1
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
    -- Последний входящий треда: ROW_NUMBER по (created_at, id) — по индексу
    -- (gmail_thread_id, direction, created_at). id — tiebreaker при равном времени.
    last_in_ids AS (
        SELECT id, ROW_NUMBER() OVER (
                   PARTITION BY gmail_thread_id
                   ORDER BY created_at DESC, id DESC) AS rn
          FROM messages
         WHERE gmail_thread_id IS NOT NULL AND gmail_thread_id != ''
           AND direction = 'in'
    ),
    last_in AS (
        SELECT m.* FROM messages m JOIN last_in_ids r ON r.id = m.id AND r.rn = 1
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
           COALESCE(f.operator_unread, 0) AS operator_unread,
           -- Автопилот прямо в строке (раньше API делал get_thread_autopilot на тред).
           COALESCE(ta.active, 0)   AS autopilot_active,
           ta.messages_sent         AS autopilot_messages_sent,
           ta.floor_price_eur       AS autopilot_floor_price_eur,
           ta.notify_mode           AS autopilot_notify_mode
    FROM last_in li
    LEFT JOIN counts c ON c.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN last_event le ON le.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN flags f ON f.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN thread_autopilot ta ON ta.gmail_thread_id = li.gmail_thread_id
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


# --- CLIENT DOSSIERS ---

def _dossier_name_key(display_name: Optional[str], emails: list[str]) -> str:
    key = norm_name(display_name)
    if key:
        return key
    if not emails:
        raise ValueError("досье без имени и без email — нечем идентифицировать клиента")
    return f"email:{emails[0]}"


def upsert_client_dossier(
    *,
    account_id: int,
    display_name: Optional[str],
    summary_md: str,
    facts_json: Any = None,
    thread_ids: Optional[list[str]] = None,
    emails: Optional[list[str]] = None,
    model: Optional[str] = None,
    max_msg_id: Optional[int] = None,
) -> int:
    """Создать/обновить досье клиента. Идемпотентно, одна транзакция. Возвращает id.

    Поиск существующего: сначала по любому из emails (связь email → досье точнее
    совпадения имён), затем по (account_id, norm_name(display_name)). thread_ids
    объединяются с уже учтёнными; emails привязываются к досье (email, ранее
    привязанный к другому досье, переезжает сюда). facts_json — dict/list или
    готовая JSON-строка. max_msg_id → dossier_max_msg_id (не уменьшается).
    """
    import json as _json
    emails = [e.strip() for e in (emails or []) if e and e.strip()]
    name_key = _dossier_name_key(display_name, emails)
    if facts_json is not None and not isinstance(facts_json, str):
        facts_json = _json.dumps(facts_json, ensure_ascii=False)
    now = datetime.utcnow().isoformat()
    with transaction() as conn:
        row = None
        if emails:
            row = conn.execute(
                "SELECT d.* FROM client_dossier_emails e "
                "JOIN client_dossiers d ON d.id = e.dossier_id "
                f"WHERE e.buyer_email IN ({','.join('?' * len(emails))}) "
                "AND d.account_id = ? ORDER BY d.updated_at DESC LIMIT 1",
                (*emails, account_id),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM client_dossiers WHERE account_id = ? AND name_key = ?",
                (account_id, name_key),
            ).fetchone()
        threads = set(thread_ids or [])
        if row is not None:
            try:
                threads |= set(_json.loads(row["source_threads_json"] or "[]"))
            except ValueError:
                pass  # битый JSON в старой строке — перезапишем объединением с новыми
            new_max = max(x for x in (row["dossier_max_msg_id"], max_msg_id, 0) if x is not None)
            conn.execute(
                "UPDATE client_dossiers SET display_name = COALESCE(?, display_name), "
                "summary_md = ?, facts_json = COALESCE(?, facts_json), "
                "source_threads_json = ?, dossier_max_msg_id = ?, "
                "model = COALESCE(?, model), updated_at = ? WHERE id = ?",
                (display_name, summary_md, facts_json,
                 _json.dumps(sorted(threads)), new_max or None, model, now, row["id"]),
            )
            dossier_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO client_dossiers (account_id, name_key, display_name, summary_md, "
                "facts_json, source_threads_json, dossier_max_msg_id, model, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, name_key, display_name, summary_md, facts_json,
                 _json.dumps(sorted(threads)), max_msg_id, model, now, now),
            )
            dossier_id = cur.lastrowid
        for e in emails:
            conn.execute(
                "INSERT INTO client_dossier_emails (buyer_email, dossier_id, added_at) "
                "VALUES (?, ?, ?) ON CONFLICT(buyer_email) DO UPDATE SET "
                "dossier_id = excluded.dossier_id",
                (e, dossier_id, now),
            )
    return dossier_id


def get_dossier_for_email(buyer_email: Optional[str]) -> Optional[sqlite3.Row]:
    """Досье, к которому привязан email покупателя, или None."""
    if not buyer_email or not buyer_email.strip():
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT d.* FROM client_dossier_emails e "
            "JOIN client_dossiers d ON d.id = e.dossier_id WHERE e.buyer_email = ?",
            (buyer_email.strip(),),
        ).fetchone()


def get_dossier(account_id: int, display_name: Optional[str]) -> Optional[sqlite3.Row]:
    """Досье по имени в аккаунте (norm_name). None если имени нет или досье нет."""
    key = norm_name(display_name)
    if not key:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM client_dossiers WHERE account_id = ? AND name_key = ?",
            (account_id, key),
        ).fetchone()


def list_dossier_emails(dossier_id: int) -> list[str]:
    with get_conn() as conn:
        return [r["buyer_email"] for r in conn.execute(
            "SELECT buyer_email FROM client_dossier_emails WHERE dossier_id = ? "
            "ORDER BY added_at, buyer_email", (dossier_id,),
        ).fetchall()]


def latest_msg_id_for_emails(emails: list[str]) -> int:
    """MAX(messages.id) по письмам этих покупателей (0 если нет). Сравнить с
    dossier_max_msg_id: больше — значит есть новые письма, досье пора пересчитать."""
    emails = [e for e in emails if e]
    if not emails:
        return 0
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT MAX(id) AS m FROM messages WHERE buyer_name IN ({','.join('?' * len(emails))})",
            emails,
        ).fetchone()
    return row["m"] or 0
