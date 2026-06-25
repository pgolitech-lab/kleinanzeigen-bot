# Продажи, детекции, уроки оператора.
# Выделено из database.py.

import sqlite3
from datetime import datetime
from typing import Any, Optional

from database import DB_PATH, get_conn, get_thread_autopilot, stop_thread_autopilot
from modules.db_threads import close_thread


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


