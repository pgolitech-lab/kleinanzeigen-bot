# Продажи, детекции, уроки оператора.
# Выделено из database.py.

import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

from database import DB_PATH, SENT_STATUSES, get_conn, get_thread_autopilot, stop_thread_autopilot, transaction
from modules.db_threads import close_thread

logger = logging.getLogger(__name__)


def mark_ad_sold(ad_id: str, sold: bool = True) -> None:
    """Пометить объявление проданным (или снять отметку sold=False).

    Если бриф ещё не существует — создаёт минимальный stub чтобы запомнить состояние.
    Будущие inquiries для этого ad_id будут авто-skip.
    """
    if not ad_id:
        return
    now = datetime.utcnow().isoformat() if sold else None
    ts = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ad_briefs (ad_id, brief_md, sold_at, created_at, updated_at) "
            "VALUES (?, '', ?, ?, ?) "
            "ON CONFLICT(ad_id) DO UPDATE SET sold_at = excluded.sold_at, "
            "updated_at = excluded.updated_at",
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


def set_ad_min_price(ad_id: str, min_price_eur: Optional[float]) -> None:
    """Задать (или снять None) минимальную цену объявления. Нет брифа — stub."""
    if not ad_id:
        return
    val = float(min_price_eur) if min_price_eur is not None else None
    if val is not None and val < 0:
        raise ValueError("min_price_eur < 0")
    ts = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ad_briefs (ad_id, brief_md, min_price_eur, created_at, updated_at) "
            "VALUES (?, '', ?, ?, ?) "
            "ON CONFLICT(ad_id) DO UPDATE SET min_price_eur = excluded.min_price_eur, "
            "updated_at = excluded.updated_at",
            (ad_id, val, ts, ts),
        )


# --- ПРОДАЖИ: единый источник ---
#
# Продажа = строка messages со status='skipped_sold' И sold_price_eur (по одной на
# тред). Так пишут все три пути: mark_thread_sold (кнопка «Продано» в треде),
# add_manual_sale (сделка вне бота, тред 'manual-…'), apply_detection_to_messages
# (подтверждённая найденная продажа). ad_briefs.sold_at — НЕ продажа, а флаг
# «объявление снято, новые запросы авто-skip» — для подсчёта продаж не годится.

BERLIN = ZoneInfo("Europe/Berlin")


def berlin_day_bounds_utc(day: "date | str") -> tuple[str, str]:
    """Границы календарного дня по Берлину как naive-UTC ISO [from, to) — в том
    формате, в каком в БД лежат sold_at/created_at (datetime.utcnow().isoformat())."""
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    lo = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    hi = lo + timedelta(days=1)
    # +1 день по стене часов, а не 24 ч: переход на летнее/зимнее время
    hi = datetime(hi.year, hi.month, hi.day, tzinfo=BERLIN)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
    return to_utc(lo), to_utc(hi)


def _sale_source_sql() -> str:
    return (
        "CASE WHEN m.gmail_thread_id LIKE 'manual-%' THEN 'manual' "
        "WHEN EXISTS (SELECT 1 FROM detected_sales ds WHERE ds.gmail_thread_id = m.gmail_thread_id "
        "AND ds.applied_at IS NOT NULL) THEN 'detected' ELSE 'thread' END"
    )


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
    source: 'thread' | 'manual' | 'detected'. Единый источник для Обзора, Продаж и
    бота — см. sales_summary (дни по Берлину).
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
            COUNT(*) AS rows_count,
            {_sale_source_sql()} AS source
        FROM messages m
        LEFT JOIN accounts a ON a.id = m.account_id
        WHERE {where_sql}
        GROUP BY m.gmail_thread_id
        ORDER BY sold_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def sales_summary(
    since: "date | str | None" = None,
    until: "date | str | None" = None,
    account_id: Optional[int] = None,
) -> dict[str, Any]:
    """Сводка продаж за период [since, until] — ДНИ ПО БЕРЛИНУ включительно.

    since/until — 'YYYY-MM-DD' или date (полные ISO-строки тоже: берётся дата).
    None — без границы. «Сегодня»: sales_summary(today_berlin, today_berlin).
    Возвращает {count, total_eur, by_source: {thread, manual, detected}, sales}.
    """
    frm = berlin_day_bounds_utc(since)[0] if since else None
    to = berlin_day_bounds_utc(until)[1] if until else None
    sales = list_sales(period_from=frm, period_to=to, account_id=account_id)
    by_source = {"thread": 0, "manual": 0, "detected": 0}
    for r in sales:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    total = sum(float(r["sold_price_eur"]) for r in sales if r["sold_price_eur"] is not None)
    return {"count": len(sales), "total_eur": round(total, 2),
            "by_source": by_source, "sales": sales}


def today_berlin() -> date:
    return datetime.now(BERLIN).date()


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

    Всё — одна транзакция: либо тред целиком помечен проданным и закрыт, либо ничего.
    """
    with transaction() as conn:
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
        # close_thread присоединяется к этой же транзакции (то же соединение потока).
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
    with transaction() as conn:
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


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def accept_detected_bulk(min_confidence: str = "high") -> dict[str, Any]:
    """Применить ВСЕ ожидающие найденные продажи с confidence ≥ min_confidence
    одной транзакцией. Каждая — в своём savepoint: неприменимая (нет цены, нет
    входящего в треде) пропускается с причиной, остальные применяются.

    Возвращает {"applied": [{detection_id, thread_id, msg_id, price}],
                "skipped": [{detection_id, reason}]}.
    """
    if min_confidence not in _CONFIDENCE_RANK:
        raise ValueError(f"bad min_confidence: {min_confidence!r}")
    ok = [c for c, r in _CONFIDENCE_RANK.items() if r >= _CONFIDENCE_RANK[min_confidence]]
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    with transaction() as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM detected_sales WHERE is_sale = 1 AND applied_at IS NULL "
            f"AND rejected_at IS NULL AND confidence IN ({','.join('?' * len(ok))}) "
            f"ORDER BY id",
            ok,
        ).fetchall()]
        for det_id in ids:
            try:
                res = apply_detection_to_messages(det_id)
            except ValueError as e:
                skipped.append({"detection_id": det_id, "reason": str(e)})
                continue
            applied.append({"detection_id": det_id, **res})
    return {"applied": applied, "skipped": skipped}


def _close_sold_threads(conn: sqlite3.Connection, thread_ids: list[str]) -> None:
    """Остановить автопилот и закрыть треды как проданные (в транзакции вызывающего)."""
    for t in thread_ids:
        ap = get_thread_autopilot(t)
        if ap and ap["active"]:
            stop_thread_autopilot(t, "sold")
        close_thread(t, closed_by="sold")


def mark_thread_sold(
    msg_id: int,
    sold_price_eur: float,
    close_other_threads_for_ad: bool = False,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Зафиксировать продажу и свернуть тред.

    Поведение:
      - msg_id — черновик/новое → status='skipped_sold', sold_price_eur=<price>, sold_at=now.
      - msg_id уже отправлен (sent/sent_debug/sent_external/sending/send_queued) — его статус
        НЕ меняется: продажа пишется отдельной строкой-продажей в том же треде (source='sale',
        direction='out', без текста — не «письмо клиента», в ленте и событиях не видна).
        Повтор обновляет ту же строку.
      - Все pending in-rows этого же gmail_thread_id → status='skipped_sold' (без цены).
      - Если автопилот треда активен → stop_thread_autopilot(reason='sold'); тред закрыт.
      - Найденная продажа по треду, ждущая ревью, снимается.
      - Склад: inventory.record_sale(source='thread') — после транзакции; ошибка склада
        продажу НЕ откатывает (logger.exception, джоб inventory_sync догонит).
      - close_other_threads_for_ad=True → другие треды с тем же ad_id (pending) тоже
        сворачиваются, но ТОЛЬКО если склад говорит «нет в наличии» (out_of_stock):
        иначе товар ещё есть и другим покупателям можно продавать.
      - mark_ad_sold НЕ зовётся — ad_briefs.sold_at больше не источник правды.

    Возвращает {"thread_id", "ad_id", "closed_other_threads": [...], "stock": {...}|None}.
    stock — {qty_after, stock_tracked, product_id} от склада или None, если склад
    недоступен/товар не найден.
    """
    with transaction() as conn:
        src = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not src:
            raise ValueError(f"message {msg_id} not found")
        thread_id = src["gmail_thread_id"] or ""
        ad_id = src["ad_id"] or ""

        now_iso = datetime.utcnow().isoformat()
        sale_id = _put_sale_row(conn, src, float(sold_price_eur), now_iso, note)
        if thread_id:
            conn.execute(
                "UPDATE messages SET status='skipped_sold' "
                "WHERE gmail_thread_id=? AND direction='in' AND status='pending'",
                (thread_id,),
            )
            # Найденная продажа по этому треду, ещё ждущая ревью, — уже не нужна.
            # rejected_at, а не applied_at: applied_at означает «продажа пришла из
            # детекции» (source='detected' в list_sales), а здесь её поставил оператор.
            conn.execute(
                "UPDATE detected_sales SET rejected_at = ? "
                "WHERE gmail_thread_id = ? AND applied_at IS NULL AND rejected_at IS NULL",
                (now_iso, thread_id),
            )
            _close_sold_threads(conn, [thread_id])

    # Склад — после коммита продажи: его сбой не должен её откатить.
    stock: Optional[dict[str, Any]] = None
    try:
        from modules import inventory
        res = inventory.record_sale(
            price_eur=float(sold_price_eur), gmail_thread_id=thread_id or None,
            ad_id=ad_id or None, message_id=msg_id, source="thread",
        )
        if res.get("product_id") is not None:
            stock = {"product_id": res["product_id"], "qty_after": res.get("qty_after"),
                     "stock_tracked": res.get("stock_tracked")}
    except Exception:
        logger.exception("mark_thread_sold: склад не записал продажу msg=%s (догонит sync)", msg_id)

    closed_other: list[str] = []
    if close_other_threads_for_ad and ad_id:
        from modules import inventory
        ctx = inventory.ad_context_safe(ad_id)
        if ctx.get("stock") == "out_of_stock":
            with transaction() as conn:
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
                _close_sold_threads(conn, closed_other)
        else:
            logger.info("mark_thread_sold: другие треды по %s не закрыты — склад: %s",
                        ad_id, "ошибка" if ctx.get("error") else ctx.get("stock"))

    return {
        "thread_id": thread_id,
        "ad_id": ad_id,
        "sale_msg_id": sale_id,
        "closed_other_threads": closed_other,
        "stock": stock,
    }


# Статусы, при которых строку при продаже не трогаем: ответ ушёл или вот-вот уйдёт.
_KEEP_ON_SALE = set(SENT_STATUSES) | {"sending", "send_queued"}
SALE_ROW_SOURCE = "sale"
_SALE_ROW_COPY = ("account_id", "ad_url", "ad_id", "ad_title", "ad_price", "seller_name",
                  "buyer_name", "buyer_display_name", "buyer_email", "client_lang")


def _put_sale_row(conn: sqlite3.Connection, src: sqlite3.Row, price: float,
                  now_iso: str, note: Optional[str]) -> int:
    """Записать продажу треда, не трогая отправленное. Возвращает id строки с продажей."""
    thread_id = src["gmail_thread_id"] or ""
    # Без треда отдельную строку не к чему привязать (sync склада идёт по треду) — как раньше.
    if src["status"] not in _KEEP_ON_SALE or not thread_id:
        conn.execute(
            "UPDATE messages SET status='skipped_sold', sold_price_eur=?, sold_at=? WHERE id=?",
            (price, now_iso, src["id"]),
        )
        if note:
            _append_note(conn, src["id"], note)
        return int(src["id"])
    prev = conn.execute(
        "SELECT id FROM messages WHERE gmail_thread_id=? AND source=? AND status='skipped_sold' "
        "ORDER BY id DESC LIMIT 1", (thread_id, SALE_ROW_SOURCE),
    ).fetchone()
    if prev:
        conn.execute("UPDATE messages SET sold_price_eur=?, sold_at=? WHERE id=?",
                     (price, now_iso, prev["id"]))
        sale_id = int(prev["id"])
    else:
        keys = src.keys()
        cols = [c for c in _SALE_ROW_COPY if c in keys]
        vals = [src[c] for c in cols]
        cols += ["direction", "status", "gmail_thread_id", "sold_price_eur", "sold_at",
                 "created_at", "source"]
        # direction='out' без текста: выборки «последнее письмо клиента» (In-Reply-To, тема,
        # якорь черновика, счётчики входящих) её не видят, лента и события — тоже.
        vals += ["out", "skipped_sold", thread_id, price, now_iso, now_iso, SALE_ROW_SOURCE]
        cur = conn.execute(
            f"INSERT INTO messages ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        sale_id = int(cur.lastrowid)
    if note:
        _append_note(conn, sale_id, note)
    return sale_id


def _append_note(conn: sqlite3.Connection, msg_id: int, note: str) -> None:
    conn.execute(
        "UPDATE messages SET extra_notes = CASE WHEN COALESCE(extra_notes, '') = '' THEN ? "
        "ELSE extra_notes || char(10) || ? END WHERE id=?",
        (f"Продано: {note}", f"Продано: {note}", msg_id),
    )


def add_thread_sale(
    gmail_thread_id: str,
    sold_price_eur: float,
    *,
    note: Optional[str] = None,
    close_other_threads_for_ad: bool = False,
) -> dict[str, Any]:
    """«Продано» на треде: по последнему входящему, статусы отправленных строк не меняются.
    Обёртка над mark_thread_sold (склад списывает один раз, source='thread')."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE gmail_thread_id=? AND direction='in' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (gmail_thread_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"в треде {gmail_thread_id} нет входящих")
    return mark_thread_sold(int(row["id"]), sold_price_eur,
                            close_other_threads_for_ad=close_other_threads_for_ad,
                            note=(note or "").strip() or None)


# --- LESSONS ---

def add_lesson(**fields: Any) -> int:
    """Сохранить пару (плохой_черновик, оператора_исправление). fields = столбцы lessons.

    source_ref должен быть уникален НА УРОК (например f"{thread_id}:{category}"),
    а не на тред: иначе уроки одного треда молча перезапишут друг друга.
    source ('mined' | 'operator_edit') без source_type используется как тип.
    Если заданы source_type и source_ref — идемпотентно: повторный вызов с тем же
    источником обновляет существующий урок (created_at сохраняется) и возвращает
    его id, а не плодит дубль. Без источника — обычная вставка, как раньше.
    """
    fields = {**fields, "created_at": datetime.utcnow().isoformat()}
    # source ('mined' | 'operator_edit') годится и как тип источника для идемпотентности.
    if fields.get("source") and not fields.get("source_type"):
        fields["source_type"] = fields["source"]
    if fields.get("source_ref") is not None:
        fields["source_ref"] = str(fields["source_ref"])
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    sql = f"INSERT INTO lessons ({cols}) VALUES ({placeholders})"
    if fields.get("source_type") and fields.get("source_ref"):
        upd = [c for c in fields if c not in ("created_at", "source_type", "source_ref")]
        set_sql = ", ".join(f"{c} = excluded.{c}" for c in upd) or "source_ref = excluded.source_ref"
        sql += (
            " ON CONFLICT(source_type, source_ref) "
            "WHERE source_type IS NOT NULL AND source_ref IS NOT NULL "
            f"DO UPDATE SET {set_sql}"
        )
    with get_conn() as conn:
        row = conn.execute(sql + " RETURNING id", tuple(fields.values())).fetchone()
        return row["id"]


def has_mined_lessons(gmail_thread_id: str) -> bool:
    """Разобран ли тред при майнинге истории (есть уроки source='mined')."""
    if not gmail_thread_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM lessons WHERE gmail_thread_id = ? AND source = 'mined' LIMIT 1",
            (gmail_thread_id,),
        ).fetchone()
    return bool(row)


def find_relevant_lessons(
    ad_id: Optional[str] = None,
    account_id: Optional[int] = None,
    client_lang: Optional[str] = None,
    limit: int = 5,
    buyer_email: Optional[str] = None,
    category: Optional[str] = None,
    include_mined: bool = False,
) -> list[sqlite3.Row]:
    """Топ-N уроков по релевантности.

    Ранжирование (rel_rank): 0 — тот же ad_id; 1 — тот же покупатель (buyer_email
    или любой email того же досье); 2 — та же category; 3 — тот же account_id;
    4 — прочие. Внутри группы — свежие первыми (id DESC). Без buyer_email/category
    порядок тот же, что раньше (ad → account → прочие).

    include_mined=False (по умолчанию) — только уроки оператора (source NULL или
    'operator_edit'): у смайненных заполнен lesson_ru, а не пары bad/good, и
    промпт, который их не умеет выводить, получил бы пустые слоты вместо правок.
    """
    sql = """
        SELECT *,
            CASE WHEN ad_id = :ad THEN 0
                 WHEN :be != '' AND (buyer_email = :be OR buyer_email IN (
                        SELECT e2.buyer_email
                          FROM client_dossier_emails e1
                          JOIN client_dossier_emails e2 ON e2.dossier_id = e1.dossier_id
                         WHERE e1.buyer_email = :be)) THEN 1
                 WHEN :cat != '' AND category = :cat THEN 2
                 WHEN account_id = :acc THEN 3
                 ELSE 4
            END AS rel_rank
        FROM lessons
        WHERE 1=1
    """
    if not include_mined:
        sql += " AND (source IS NULL OR source = 'operator_edit')"
    params: dict[str, Any] = {
        "ad": ad_id or "", "acc": account_id if account_id is not None else -1,
        "be": (buyer_email or "").strip(), "cat": (category or "").strip(),
    }
    if client_lang:
        sql += " AND (client_lang = :lang OR client_lang IS NULL)"
        params["lang"] = client_lang
    sql += " ORDER BY rel_rank ASC, id DESC LIMIT :lim"
    params["lim"] = limit
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


