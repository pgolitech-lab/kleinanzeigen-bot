# БД модуля «Рынок-советник» (market): история цен объявлений рынка, позиции
# в выдаче по прогонам, наши поисковые запросы, кэш признаков (Haiku) и кэш
# LLM-советов. Только свои таблицы market_*; scout_listings лишь читаем.
#
# Миграция — ensure_market_schema(conn): её вызов в database.init_db добавляет
# модуль db; до этого таблицы создаются лениво через ensure_schema().

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from database import get_conn, transaction

logger = logging.getLogger(__name__)

# Сколько дней хранить снимки выдачи (market_serp). Историю цен не чистим — она маленькая.
SERP_RETENTION_DAYS = 120


def _now() -> str:
    return datetime.utcnow().isoformat()


def ensure_market_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентная миграция таблиц market_*."""
    # История цены: строка пишется только при изменении цены (или первом наблюдении).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id TEXT NOT NULL,
            price_eur REAL,
            price_raw TEXT,
            seen_at TEXT NOT NULL               -- UTC ISO: когда увидели эту цену впервые
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mph_ad ON market_price_history(ad_id, seen_at)")
    # Один прогон одного поискового запроса (scout или наш). Нужен, чтобы отличить
    # «нашего объявления нет на первых N страницах» от «запрос не прогоняли».
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,               -- scout / market
            keywords TEXT NOT NULL,             -- поисковая фраза в нижнем регистре
            category TEXT,
            query_ref INTEGER,                  -- scout_queries.id или market_queries.id
            pages INTEGER NOT NULL DEFAULT 0,   -- сколько страниц реально загрузили
            cards INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mr_kw ON market_runs(keywords, started_at)")
    # Снимок выдачи: место каждого объявления в прогоне.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_serp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES market_runs(id) ON DELETE CASCADE,
            ad_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            pos INTEGER NOT NULL,               -- место в выдаче с 1, сквозное по страницам
            organic_pos INTEGER,                -- место без платных TOP-объявлений
            is_top INTEGER,                     -- 1 = платное TOP, NULL = не удалось понять
            title TEXT,
            price_eur REAL,
            plz TEXT,
            city TEXT,
            posted_raw TEXT,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_run ON market_serp(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_ad ON market_serp(ad_id, seen_at)")
    # Наши поисковые запросы для отслеживания позиции (как ищут покупатели наших товаров).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keywords TEXT NOT NULL UNIQUE,      -- нижний регистр
            category TEXT NOT NULL DEFAULT 'c223',
            max_pages INTEGER NOT NULL DEFAULT 3,
            enabled INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'auto',  -- auto (из наших товаров) / operator
            note TEXT,                          -- для чего: товар / категория
            last_run_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Признаки объявления (кожа, VIP, 2+1, модель…): Haiku один раз на объявление или regex.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_ad_features (
            ad_id TEXT PRIMARY KEY,
            features_json TEXT NOT NULL,
            method TEXT NOT NULL,               -- llm / regex
            src_hash TEXT,                      -- хэш заголовка+описания: изменился текст → пересчитать
            created_at TEXT NOT NULL
        )
    """)
    # Кэш LLM-советов (заметность, тексты перепоста) по ключу входных данных.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_llm_cache (
            cache_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,                 -- visibility / repost
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)


_schema_lock = threading.Lock()
_schema_ready: set[str] = set()


def ensure_schema() -> None:
    """Ленивая миграция (один раз на файл БД за процесс)."""
    import database
    key = str(database.DB_PATH)
    if key in _schema_ready:
        return
    with _schema_lock:
        if key in _schema_ready:
            return
        with transaction() as conn:
            ensure_market_schema(conn)
        _schema_ready.add(key)


def table_exists(name: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


# --- ИСТОРИЯ ЦЕН ---

def _record_prices(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]], now: str) -> int:
    """Записать цену, если она отличается от последней записанной. Возвращает число записей."""
    n = 0
    for r in rows:
        ad_id = str(r.get("ad_id") or "").strip()
        if not ad_id:
            continue
        price = r.get("price_eur")
        last = conn.execute(
            "SELECT price_eur FROM market_price_history WHERE ad_id=? ORDER BY seen_at DESC, id DESC LIMIT 1",
            (ad_id,),
        ).fetchone()
        if last is not None and last["price_eur"] == price:
            continue
        if last is None and price is None:
            continue  # «цена не указана» с первого раза — истории нет
        conn.execute(
            "INSERT INTO market_price_history (ad_id, price_eur, price_raw, seen_at) VALUES (?, ?, ?, ?)",
            (ad_id, price, r.get("price_raw"), now),
        )
        n += 1
    return n


def price_history(ad_id: str) -> list[dict[str, Any]]:
    ensure_schema()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT price_eur, price_raw, seen_at FROM market_price_history WHERE ad_id=? ORDER BY seen_at, id",
            (str(ad_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def price_changes(ad_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """ad_id → история цены (только где записей ≥ 2, т. е. цена менялась)."""
    ids = [str(a) for a in ad_ids if a]
    if not ids:
        return {}
    ensure_schema()
    out: dict[str, list[dict[str, Any]]] = {}
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT ad_id, price_eur, seen_at FROM market_price_history WHERE ad_id IN ({ph}) "
                f"ORDER BY ad_id, seen_at, id", chunk,
            ):
                out.setdefault(r["ad_id"], []).append({"price_eur": r["price_eur"], "seen_at": r["seen_at"]})
    return {k: v for k, v in out.items() if len(v) >= 2}


# --- ПРОГОНЫ И ВЫДАЧА ---

def record_scrape(*, source: str, keywords: str, category: Optional[str], query_ref: Optional[int],
                  rows: list[dict[str, Any]], pages: int, error: Optional[str] = None,
                  started_at: Optional[str] = None) -> int:
    """Сохранить прогон одного запроса: run + места в выдаче + изменения цен. Возвращает run_id.

    rows — в порядке выдачи, с ключами page/pos/is_top (их проставляет scout.scrape_query).
    """
    ensure_schema()
    now = _now()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO market_runs (source, keywords, category, query_ref, pages, cards, error, "
            "started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, (keywords or "").strip().lower(), category, query_ref, int(pages), len(rows),
             error, started_at or now, now),
        )
        run_id = int(cur.lastrowid)
        organic = 0
        for i, r in enumerate(rows, start=1):
            ad_id = str(r.get("ad_id") or "").strip()
            if not ad_id:
                continue
            is_top = r.get("is_top")
            if is_top != 1:
                organic += 1
            conn.execute(
                "INSERT INTO market_serp (run_id, ad_id, page, pos, organic_pos, is_top, title, price_eur, "
                "plz, city, posted_raw, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, ad_id, int(r.get("page") or 1), int(r.get("pos") or i),
                 None if is_top == 1 else organic, is_top, r.get("title"), r.get("price_eur"),
                 r.get("plz"), r.get("city"), r.get("posted_raw"), now),
            )
        _record_prices(conn, rows, now)
    return run_id


def runs_since(since_iso: str, keywords: Optional[str] = None) -> list[dict[str, Any]]:
    """Прогоны (без ошибки первой страницы) начиная с since_iso, от новых к старым."""
    ensure_schema()
    sql = "SELECT * FROM market_runs WHERE started_at >= ? AND pages > 0"
    args: list[Any] = [since_iso]
    if keywords:
        sql += " AND keywords = ?"
        args.append(keywords.strip().lower())
    sql += " ORDER BY started_at DESC, id DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def serp_for_runs(run_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    """run_id → строки выдачи по порядку."""
    ids = [int(x) for x in run_ids]
    if not ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT * FROM market_serp WHERE run_id IN ({ph}) ORDER BY run_id, pos", chunk,
            ):
                out[r["run_id"]].append(dict(r))
    return out


def last_serp_seen(ad_ids: Iterable[str]) -> dict[str, str]:
    """ad_id → когда последний раз видели в любой выдаче (UTC ISO)."""
    ids = [str(a) for a in ad_ids if a]
    if not ids:
        return {}
    ensure_schema()
    out: dict[str, str] = {}
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT ad_id, MAX(seen_at) AS t FROM market_serp WHERE ad_id IN ({ph}) GROUP BY ad_id", chunk,
            ):
                out[r["ad_id"]] = r["t"]
    return out


def purge_old_serp(days: int = SERP_RETENTION_DAYS) -> int:
    """Удалить снимки выдачи старше days. Возвращает число удалённых прогонов."""
    ensure_schema()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with transaction() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM market_runs WHERE started_at < ?", (cutoff,))]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM market_serp WHERE run_id IN ({ph})", chunk)
            conn.execute(f"DELETE FROM market_runs WHERE id IN ({ph})", chunk)
    return len(ids)


# --- НАШИ ПОИСКОВЫЕ ЗАПРОСЫ ---

def upsert_market_query(keywords: str, *, category: str = "c223", max_pages: int = 3,
                        source: str = "auto", note: Optional[str] = None) -> int:
    """Добавить запрос, если такого нет. Операторский запрос автоматикой не перезаписывается."""
    ensure_schema()
    kw = " ".join((keywords or "").lower().split())
    if not kw:
        raise ValueError("пустой поисковый запрос")
    now = _now()
    with transaction() as conn:
        row = conn.execute("SELECT id, source FROM market_queries WHERE keywords=?", (kw,)).fetchone()
        if row:
            if source == "operator" or row["source"] != "operator":
                conn.execute("UPDATE market_queries SET note=COALESCE(?, note), updated_at=? WHERE id=?",
                             (note, now, row["id"]))
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO market_queries (keywords, category, max_pages, enabled, source, note, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (kw, category, int(max_pages), source, note, now, now),
        )
        return int(cur.lastrowid)


def list_market_queries(only_enabled: bool = True) -> list[dict[str, Any]]:
    ensure_schema()
    sql = "SELECT * FROM market_queries"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql)]


def set_market_query_enabled(query_id: int, enabled: bool) -> None:
    ensure_schema()
    with get_conn() as conn:
        conn.execute("UPDATE market_queries SET enabled=?, updated_at=? WHERE id=?",
                     (1 if enabled else 0, _now(), int(query_id)))


def touch_market_query(query_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE market_queries SET last_run_at=? WHERE id=?", (_now(), int(query_id)))


# --- КЭШ ПРИЗНАКОВ И LLM ---

def text_hash(*parts: Optional[str]) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def get_features_bulk(ad_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """ad_id → {features, method, src_hash}."""
    ids = [str(a) for a in ad_ids if a]
    if not ids:
        return {}
    ensure_schema()
    out: dict[str, dict[str, Any]] = {}
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT * FROM market_ad_features WHERE ad_id IN ({ph})", chunk):
                try:
                    feats = json.loads(r["features_json"])
                except (TypeError, ValueError):
                    logger.warning("market: битый features_json у %s — пересчитаю", r["ad_id"])
                    continue
                out[r["ad_id"]] = {"features": feats, "method": r["method"], "src_hash": r["src_hash"]}
    return out


def set_features(ad_id: str, features: dict[str, Any], method: str, src_hash: Optional[str]) -> None:
    ensure_schema()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_ad_features (ad_id, features_json, method, src_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(ad_id) DO UPDATE SET features_json=excluded.features_json, "
            "method=excluded.method, src_hash=excluded.src_hash, created_at=excluded.created_at",
            (str(ad_id), json.dumps(features, ensure_ascii=False), method, src_hash, _now()),
        )


def cache_get(cache_key: str, max_age_days: Optional[float] = None) -> Optional[dict[str, Any]]:
    ensure_schema()
    with get_conn() as conn:
        r = conn.execute("SELECT payload_json, created_at FROM market_llm_cache WHERE cache_key=?",
                         (cache_key,)).fetchone()
    if r is None:
        return None
    if max_age_days is not None:
        try:
            age = datetime.utcnow() - datetime.fromisoformat(r["created_at"])
        except ValueError:
            return None
        if age > timedelta(days=max_age_days):
            return None
    try:
        return json.loads(r["payload_json"])
    except ValueError:
        logger.warning("market: битый кэш %s — пересчитаю", cache_key)
        return None


def cache_set(cache_key: str, kind: str, payload: dict[str, Any]) -> None:
    ensure_schema()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO market_llm_cache (cache_key, kind, payload_json, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at",
            (cache_key, kind, json.dumps(payload, ensure_ascii=False), _now()),
        )
