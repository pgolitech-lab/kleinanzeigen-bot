# Склад: таблицы и SQL-запросы. Логика (автопривязка, отчёт, правила) — в modules/inventory.py.
#
# Модель:
#   inv_products        — товар (то, что лежит на складе; у одного товара много объявлений во времени)
#   inv_listings        — объявление Kleinanzeigen (ad_id) → товар. Перепощенное объявление = тот же товар
#   inv_movements       — движения: приход (+N), продажа (−N), списание (−N), корректировка (±N)
#   inv_link_questions  — сомнительные привязки «это тот же товар?» для оператора
#
# Остаток НЕ хранится колонкой — это SUM(qty) по невоидированным движениям (один источник правды),
# но только С НАЧАЛА УЧЁТА: начало = самый ранний приход/корректировка товара. Продажи из истории
# до этого момента (перенесённые из messages) в остаток не входят — иначе первый приход «2 шт.»
# после трёх старых продаж дал бы −1 и бот сказал бы клиенту «нет в наличии».
# Пока по товару не внесён ни приход, ни инвентаризация (stock_tracked=0), остаток «неизвестен»:
# другие модули НЕ должны из этого делать вывод «нет в наличии».

import sqlite3
from datetime import datetime
from typing import Any, Iterable, Optional

import database
from database import get_conn, transaction

MOVEMENT_KINDS = ("receipt", "sale", "writeoff", "adjust")
LINK_METHODS = ("exact", "normalized", "llm", "operator", "new", "seed")

# Пути БД, для которых схема уже проверена в этом процессе (тесты подменяют DB_PATH).
_ready: set[str] = set()


def ensure_inventory_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентная миграция таблиц склада. Вызывается из database.init_db (модуль db)
    и лениво из ensure_schema() — чтобы склад работал и до того, как вызов туда добавят."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inv_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,                 -- название для оператора
            category TEXT,                       -- sitzbank / einzelsitz / tisch / ahk / … (inventory.detect_category)
            condition TEXT,                      -- neu / gebraucht / defekt
            unit_cost_eur REAL,                  -- оценка себестоимости единицы, если у прихода нет своей цены
            list_price_eur REAL,                 -- цена в объявлении
            min_price_eur REAL,                  -- минимальная цена, ниже которой не продаём
            note TEXT,
            match_key TEXT,                      -- нормализованный заголовок (inventory.match_key)
            stock_tracked INTEGER NOT NULL DEFAULT 0,  -- 1 = остаток внесён оператором и ему можно верить
            archived_at TEXT,                    -- товар больше не продаём (или слит с другим)
            merged_into INTEGER,                 -- id товара, в который слит
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_products_key ON inv_products(match_key)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inv_listings (
            ad_id TEXT PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES inv_products(id),
            account_id INTEGER,
            title TEXT,                          -- последний чистый заголовок (без «Gelöscht •»)
            price_eur REAL,
            url TEXT,
            state TEXT NOT NULL DEFAULT 'active',  -- active / deleted / reserved
            link_method TEXT NOT NULL,           -- exact / normalized / llm / operator / new / seed
            link_score REAL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            linked_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_listings_product ON inv_listings(product_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inv_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES inv_products(id),
            kind TEXT NOT NULL,                  -- receipt / sale / writeoff / adjust
            qty INTEGER NOT NULL,                -- со знаком: приход +N, продажа/списание −N
            unit_cost_eur REAL,                  -- приход: почём купили единицу
            unit_price_eur REAL,                 -- продажа: почём продали единицу
            source TEXT NOT NULL DEFAULT 'manual',  -- manual / thread / detected / seed
            gmail_thread_id TEXT,
            message_id INTEGER,
            ad_id TEXT,
            note TEXT,
            occurred_at TEXT NOT NULL,           -- когда случилось (UTC ISO)
            created_at TEXT NOT NULL,
            created_by TEXT,
            voided_at TEXT,                      -- отменено (продажу откатили) — в остатке не учитывается
            void_reason TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_mov_product ON inv_movements(product_id, occurred_at)")
    # Одна продажа на тред: повторная отметка «Продано» / применение найденной продажи
    # не списывают товар второй раз. Отменённые (voided) не мешают записать заново.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_inv_mov_thread_sale ON inv_movements(gmail_thread_id) "
        "WHERE kind = 'sale' AND gmail_thread_id IS NOT NULL AND voided_at IS NULL"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inv_link_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            product_id INTEGER NOT NULL,         -- куда объявление привязано сейчас
            suggested_product_id INTEGER NOT NULL,  -- «возможно, это тот же товар»
            score REAL,
            reason TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT                      -- same / different
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_inv_q_open ON inv_link_questions(ad_id, suggested_product_id) "
        "WHERE resolved_at IS NULL"
    )


def ensure_schema() -> None:
    """Создать таблицы склада для текущей БД (один раз на процесс и путь)."""
    path = str(database.DB_PATH)
    if path in _ready:
        return
    with get_conn() as conn:
        ensure_inventory_schema(conn)
    _ready.add(path)


def _now() -> str:
    return datetime.utcnow().isoformat()


# ─── товары ───────────────────────────────────────────────────────────────────

def _qty_sql(pid_expr: str) -> str:
    """SQL-выражение остатка товара pid_expr с начала учёта (0, если учёт не начат)."""
    return f"""COALESCE((SELECT SUM(m.qty) FROM inv_movements m
                 WHERE m.product_id = {pid_expr} AND m.voided_at IS NULL
                   AND m.occurred_at >= (SELECT MIN(r.occurred_at) FROM inv_movements r
                                          WHERE r.product_id = {pid_expr} AND r.voided_at IS NULL
                                            AND r.kind IN ('receipt', 'adjust'))), 0)"""


def tracked_since(product_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Начало учёта остатка товара (ISO) или None — остаток не внесён."""
    sql = ("SELECT MIN(occurred_at) AS t FROM inv_movements WHERE product_id = ? AND voided_at IS NULL "
           "AND kind IN ('receipt', 'adjust')")
    if conn is not None:
        return conn.execute(sql, (int(product_id),)).fetchone()["t"]
    ensure_schema()
    with get_conn() as c:
        return c.execute(sql, (int(product_id),)).fetchone()["t"]


_PRODUCT_FIELDS = ("title", "category", "condition", "unit_cost_eur", "list_price_eur",
                   "min_price_eur", "note", "match_key")


def create_product(*, title: str, **fields: Any) -> int:
    ensure_schema()
    bad = set(fields) - set(_PRODUCT_FIELDS)
    if bad:
        raise ValueError(f"неизвестные поля товара: {', '.join(sorted(bad))}")
    if not (title or "").strip():
        raise ValueError("пустое название товара")
    now = _now()
    cols = ["title", *fields.keys(), "created_at", "updated_at"]
    vals = [title.strip(), *fields.values(), now, now]
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO inv_products ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
        return int(cur.lastrowid)


def update_product(product_id: int, **fields: Any) -> None:
    ensure_schema()
    bad = set(fields) - set(_PRODUCT_FIELDS)
    if bad:
        raise ValueError(f"неизвестные поля товара: {', '.join(sorted(bad))}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE inv_products SET {sets}, updated_at = ? WHERE id = ?",
            [*fields.values(), _now(), int(product_id)],
        )
    if cur.rowcount == 0:
        raise ValueError(f"товар {product_id} не найден")


def get_product(product_id: int) -> Optional[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute("SELECT * FROM inv_products WHERE id = ?", (int(product_id),)).fetchone()


def list_products(include_archived: bool = False) -> list[sqlite3.Row]:
    """Товары с остатком (SUM невоидированных движений) и числом объявлений."""
    ensure_schema()
    where = "" if include_archived else "WHERE p.archived_at IS NULL"
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT p.*,
                   {_qty_sql("p.id")} AS qty,
                   (SELECT COUNT(*) FROM inv_listings l WHERE l.product_id = p.id) AS listings,
                   (SELECT MIN(first_seen_at) FROM inv_listings l WHERE l.product_id = p.id) AS first_listed_at,
                   (SELECT MAX(last_seen_at) FROM inv_listings l WHERE l.product_id = p.id) AS last_listed_at
              FROM inv_products p {where}
             ORDER BY p.category, p.title
        """).fetchall()


def products_by_key(match_key: str) -> list[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM inv_products WHERE match_key = ? AND archived_at IS NULL ORDER BY id",
            (match_key,),
        ).fetchall()


def stock_qty(product_id: int, conn: Optional[sqlite3.Connection] = None) -> int:
    """Остаток с начала учёта (0, если учёт не начат — смотрите stock_tracked)."""
    sql = f"SELECT {_qty_sql('?1')} AS q"
    if conn is not None:
        return int(conn.execute(sql, (int(product_id),)).fetchone()["q"])
    ensure_schema()
    with get_conn() as c:
        return int(c.execute(sql, (int(product_id),)).fetchone()["q"])


# ─── объявления ───────────────────────────────────────────────────────────────

def get_listing(ad_id: str) -> Optional[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute("SELECT * FROM inv_listings WHERE ad_id = ?", (str(ad_id),)).fetchone()


def list_listings(product_id: Optional[int] = None) -> list[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        if product_id is None:
            return conn.execute("SELECT * FROM inv_listings ORDER BY first_seen_at").fetchall()
        return conn.execute(
            "SELECT * FROM inv_listings WHERE product_id = ? ORDER BY first_seen_at", (int(product_id),)
        ).fetchall()


def upsert_listing(*, ad_id: str, product_id: int, link_method: str, seen_at: Optional[str] = None,
                   account_id: Optional[int] = None, title: Optional[str] = None,
                   price_eur: Optional[float] = None, url: Optional[str] = None,
                   state: str = "active", link_score: Optional[float] = None) -> None:
    """Вставить/обновить объявление. Привязку к товару меняет только relink_listing —
    здесь при повторном появлении обновляются заголовок, цена, состояние, last_seen."""
    ensure_schema()
    if link_method not in LINK_METHODS:
        raise ValueError(f"неизвестный способ привязки: {link_method}")
    seen = seen_at or _now()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO inv_listings (ad_id, product_id, account_id, title, price_eur, url, state,
                                      link_method, link_score, first_seen_at, last_seen_at, linked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                account_id = COALESCE(excluded.account_id, inv_listings.account_id),
                title = CASE WHEN excluded.last_seen_at >= inv_listings.last_seen_at
                             THEN COALESCE(excluded.title, inv_listings.title) ELSE inv_listings.title END,
                price_eur = CASE WHEN excluded.last_seen_at >= inv_listings.last_seen_at
                             THEN COALESCE(excluded.price_eur, inv_listings.price_eur) ELSE inv_listings.price_eur END,
                url = COALESCE(inv_listings.url, excluded.url),
                state = CASE WHEN excluded.last_seen_at >= inv_listings.last_seen_at
                             THEN excluded.state ELSE inv_listings.state END,
                first_seen_at = MIN(inv_listings.first_seen_at, excluded.first_seen_at),
                last_seen_at = MAX(inv_listings.last_seen_at, excluded.last_seen_at)
        """, (str(ad_id), int(product_id), account_id, title, price_eur, url, state,
              link_method, link_score, seen, seen, _now()))


def relink_listing(ad_id: str, product_id: int, link_method: str = "operator") -> None:
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE inv_listings SET product_id = ?, link_method = ?, link_score = NULL, linked_at = ? "
            "WHERE ad_id = ?",
            (int(product_id), link_method, _now(), str(ad_id)),
        )
    if cur.rowcount == 0:
        raise ValueError(f"объявление {ad_id} не найдено на складе")


# ─── движения ─────────────────────────────────────────────────────────────────

def add_movement(*, product_id: int, kind: str, qty: int, occurred_at: Optional[str] = None,
                 unit_cost_eur: Optional[float] = None, unit_price_eur: Optional[float] = None,
                 source: str = "manual", gmail_thread_id: Optional[str] = None,
                 message_id: Optional[int] = None, ad_id: Optional[str] = None,
                 note: Optional[str] = None, created_by: Optional[str] = None,
                 conn: Optional[sqlite3.Connection] = None) -> int:
    """Записать движение. Знак qty проверяется по виду: приход > 0, продажа/списание < 0,
    корректировка — любая. Приход и корректировка включают учёт остатка (stock_tracked=1)."""
    ensure_schema()
    if kind not in MOVEMENT_KINDS:
        raise ValueError(f"неизвестный вид движения: {kind}")
    qty = int(qty)
    if kind == "receipt" and qty <= 0:
        raise ValueError("приход должен быть > 0")
    if kind in ("sale", "writeoff") and qty >= 0:
        raise ValueError(f"{kind}: количество должно быть < 0")
    # adjust с qty=0 допустим: инвентаризация «0 шт.» у товара без учёта — отметка начала учёта
    now = _now()

    def _write(c: sqlite3.Connection) -> int:
        if c.execute("SELECT 1 FROM inv_products WHERE id = ?", (int(product_id),)).fetchone() is None:
            raise ValueError(f"товар {product_id} не найден")
        cur = c.execute("""
            INSERT INTO inv_movements (product_id, kind, qty, unit_cost_eur, unit_price_eur, source,
                                       gmail_thread_id, message_id, ad_id, note, occurred_at,
                                       created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (int(product_id), kind, qty, unit_cost_eur, unit_price_eur, source, gmail_thread_id,
              message_id, ad_id, note, occurred_at or now, now, created_by))
        if kind in ("receipt", "adjust"):
            c.execute("UPDATE inv_products SET stock_tracked = 1, updated_at = ? WHERE id = ?",
                      (now, int(product_id)))
        return int(cur.lastrowid)

    if conn is not None:
        return _write(conn)
    with transaction() as c:
        return _write(c)


def sale_for_thread(gmail_thread_id: str) -> Optional[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM inv_movements WHERE kind = 'sale' AND gmail_thread_id = ? AND voided_at IS NULL",
            (gmail_thread_id,),
        ).fetchone()


def update_sale_price(movement_id: int, price_eur: Optional[float]) -> None:
    ensure_schema()
    with get_conn() as conn:
        conn.execute("UPDATE inv_movements SET unit_price_eur = ? WHERE id = ? AND kind = 'sale'",
                     (price_eur, int(movement_id)))


def void_movement(movement_id: int, reason: str) -> bool:
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE inv_movements SET voided_at = ?, void_reason = ? WHERE id = ? AND voided_at IS NULL",
            (_now(), reason, int(movement_id)),
        )
    return cur.rowcount > 0


def list_movements(product_id: Optional[int] = None, *, include_voided: bool = False,
                   until: Optional[str] = None) -> list[sqlite3.Row]:
    ensure_schema()
    where, args = [], []
    if product_id is not None:
        where.append("product_id = ?")
        args.append(int(product_id))
    if not include_voided:
        where.append("voided_at IS NULL")
    if until:
        where.append("occurred_at < ?")
        args.append(until)
    sql = "SELECT * FROM inv_movements"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY occurred_at, id"
    with get_conn() as conn:
        return conn.execute(sql, args).fetchall()


# ─── вопросы оператору ────────────────────────────────────────────────────────

def add_link_question(*, ad_id: str, product_id: int, suggested_product_id: int,
                      score: Optional[float], reason: str) -> Optional[int]:
    """Открыть вопрос «это тот же товар?». Повтор открытого вопроса — None (не дублируем)."""
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO inv_link_questions (ad_id, product_id, suggested_product_id, score, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """, (str(ad_id), int(product_id), int(suggested_product_id), score, reason, _now()))
        return int(cur.lastrowid) if cur.rowcount else None


def list_open_questions() -> list[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute("""
            SELECT q.*, l.title AS ad_title, l.price_eur AS ad_price_eur, l.account_id,
                   p.title AS product_title, s.title AS suggested_title,
                   s.list_price_eur AS suggested_price_eur
              FROM inv_link_questions q
              LEFT JOIN inv_listings l ON l.ad_id = q.ad_id
              LEFT JOIN inv_products p ON p.id = q.product_id
              LEFT JOIN inv_products s ON s.id = q.suggested_product_id
             WHERE q.resolved_at IS NULL
             ORDER BY q.score DESC, q.id
        """).fetchall()


def get_question(question_id: int) -> Optional[sqlite3.Row]:
    ensure_schema()
    with get_conn() as conn:
        return conn.execute("SELECT * FROM inv_link_questions WHERE id = ?", (int(question_id),)).fetchone()


def close_question(question_id: int, resolution: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    if resolution not in ("same", "different"):
        raise ValueError(f"неизвестный ответ: {resolution}")
    ensure_schema()
    sql = ("UPDATE inv_link_questions SET resolved_at = ?, resolution = ? "
           "WHERE id = ? AND resolved_at IS NULL")
    if conn is not None:
        return conn.execute(sql, (_now(), resolution, int(question_id))).rowcount > 0
    with get_conn() as c:
        return c.execute(sql, (_now(), resolution, int(question_id))).rowcount > 0


# ─── слияние ──────────────────────────────────────────────────────────────────

def merge_products(src_id: int, dst_id: int) -> dict[str, int]:
    """Слить товар src в dst: объявления, движения и открытые вопросы переезжают, src архивируется.
    Одна транзакция. Учёт остатка dst включается, если он был у src."""
    ensure_schema()
    if int(src_id) == int(dst_id):
        raise ValueError("нельзя слить товар сам с собой")
    now = _now()
    with transaction() as conn:
        src = conn.execute("SELECT * FROM inv_products WHERE id = ?", (int(src_id),)).fetchone()
        dst = conn.execute("SELECT * FROM inv_products WHERE id = ?", (int(dst_id),)).fetchone()
        if src is None or dst is None:
            raise ValueError("товар не найден")
        if dst["archived_at"]:
            raise ValueError(f"товар {dst_id} в архиве — сливать в него нельзя")
        n_list = conn.execute("UPDATE inv_listings SET product_id = ? WHERE product_id = ?",
                              (int(dst_id), int(src_id))).rowcount
        n_mov = conn.execute("UPDATE inv_movements SET product_id = ? WHERE product_id = ?",
                             (int(dst_id), int(src_id))).rowcount
        # Вопросы «src ли это?» теряют смысл: оба варианта теперь один товар
        conn.execute("UPDATE inv_link_questions SET resolved_at = ?, resolution = 'same' "
                     "WHERE resolved_at IS NULL AND ((product_id = ? AND suggested_product_id = ?) "
                     "OR (product_id = ? AND suggested_product_id = ?))",
                     (now, int(src_id), int(dst_id), int(dst_id), int(src_id)))
        conn.execute("UPDATE inv_link_questions SET product_id = ? WHERE product_id = ? AND resolved_at IS NULL",
                     (int(dst_id), int(src_id)))
        conn.execute("UPDATE inv_link_questions SET suggested_product_id = ? "
                     "WHERE suggested_product_id = ? AND resolved_at IS NULL", (int(dst_id), int(src_id)))
        # Заполнить пустые поля dst данными src (себестоимость, цены, заметка)
        fill = {k: src[k] for k in ("category", "condition", "unit_cost_eur", "list_price_eur",
                                    "min_price_eur", "note") if dst[k] is None and src[k] is not None}
        tracked = 1 if (dst["stock_tracked"] or src["stock_tracked"]) else 0
        sets = "".join(f", {k} = ?" for k in fill)
        conn.execute(f"UPDATE inv_products SET stock_tracked = ?, updated_at = ?{sets} WHERE id = ?",
                     [tracked, now, *fill.values(), int(dst_id)])
        conn.execute("UPDATE inv_products SET archived_at = ?, merged_into = ?, updated_at = ? WHERE id = ?",
                     (now, int(dst_id), now, int(src_id)))
    return {"listings": n_list, "movements": n_mov}


def archive_product(product_id: int, archived: bool = True) -> None:
    ensure_schema()
    with get_conn() as conn:
        cur = conn.execute("UPDATE inv_products SET archived_at = ?, updated_at = ? WHERE id = ?",
                           (_now() if archived else None, _now(), int(product_id)))
    if cur.rowcount == 0:
        raise ValueError(f"товар {product_id} не найден")


def known_ad_ids() -> set[str]:
    ensure_schema()
    with get_conn() as conn:
        return {r["ad_id"] for r in conn.execute("SELECT ad_id FROM inv_listings")}


def thread_ids_with_sale() -> set[str]:
    ensure_schema()
    with get_conn() as conn:
        return {r["gmail_thread_id"] for r in conn.execute(
            "SELECT gmail_thread_id FROM inv_movements WHERE kind = 'sale' "
            "AND gmail_thread_id IS NOT NULL AND voided_at IS NULL")}


def iter_rows(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    """Произвольный SELECT (для отчётов inventory.py)."""
    ensure_schema()
    with get_conn() as conn:
        return conn.execute(sql, list(args)).fetchall()
