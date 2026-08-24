# Scout-функции базы данных: scout_queries, scout_listings, классификация, корректировки.
# Выделено из database.py. Импортирует get_conn и DB_PATH оттуда.

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from database import DB_PATH, get_conn

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
            f"UPDATE scout_listings SET active = 0, deactivated_at = ? "
            f"WHERE {_EFF_KIND} = ? AND active = 1 AND last_seen_at < ?",
            (datetime.utcnow().isoformat(), kind, before_iso),
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
    unseen_cutoff: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Объявления разведки с фильтрами (по ЭФФЕКТИВНОМУ виду). Сортировка: земля, город, цена.

    unseen_cutoff — ISO-порог: объявления, которых нет в выдаче с этого момента,
    считаются снятыми и не возвращаются. Флаг active обновляется только при
    ПОЛНОМ прогоне, поэтому опираться на него одного нельзя: пока авто-прогон
    был выключен, снятые месяц назад объявления оставались active=1 (2026-07-21).
    """
    sql = "SELECT * FROM scout_listings WHERE 1=1"
    params: list[Any] = []
    if kind:
        sql += f" AND {_EFF_KIND} = ?"; params.append(kind)
    if only_active:
        sql += " AND active = 1"
    if unseen_cutoff:
        sql += " AND last_seen_at >= ?"; params.append(unseen_cutoff)
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


def scout_counts(unseen_cutoff: Optional[str] = None) -> dict[str, int]:
    """Сводка по живым: машины, запчасти (effective kind), other, не проверено.

    unseen_cutoff — снятые (нет в выдаче с этого момента) в счётчики не идут,
    иначе оператор видит завышенные цифры по объявлениям, которых уже нет.
    Отдельным ключом removed — сколько таких скрыто.
    """
    with get_conn() as conn:
        alive = "active=1"
        base_params: list[Any] = []
        if unseen_cutoff:
            alive += " AND last_seen_at >= ?"
            base_params.append(unseen_cutoff)

        def c(where: str) -> int:
            return conn.execute(
                f"SELECT COUNT(*) c FROM scout_listings WHERE {alive} AND {where}",
                base_params,
            ).fetchone()["c"]

        removed = 0
        if unseen_cutoff:
            removed = conn.execute(
                "SELECT COUNT(*) c FROM scout_listings "
                "WHERE active=1 AND last_seen_at < ?", (unseen_cutoff,),
            ).fetchone()["c"]
        return {
            "cars": c(f"{_EFF_KIND}='car'"),
            "parts": c(f"{_EFF_KIND}='part'"),
            "other": c(f"{_EFF_KIND}='other'"),
            "unverified": c("verified_kind IS NULL"),
            "removed": removed,
        }


def scout_daily_stats(date_str: str) -> dict[str, dict[str, int]]:
    """Новые находки и снятые/проданные объявления scout за конкретный день (YYYY-MM-DD).

    'Снято' = active flip 1→0 в deactivate_stale_scout_listings (не виден в выдаче
    дольше scout_stale_days) — не строго подтверждённая продажа, но лучший доступный
    сигнал без похода на страницу объявления. Возвращает {"new": {kind: cnt}, "removed": {kind: cnt}}
    по ЭФФЕКТИВНОМУ виду (car/part/other).
    """
    with get_conn() as conn:
        new_rows = conn.execute(
            f"SELECT {_EFF_KIND} AS k, COUNT(*) AS c FROM scout_listings "
            f"WHERE substr(first_seen_at, 1, 10) = ? GROUP BY k",
            (date_str,),
        ).fetchall()
        removed_rows = conn.execute(
            f"SELECT {_EFF_KIND} AS k, COUNT(*) AS c FROM scout_listings "
            f"WHERE substr(deactivated_at, 1, 10) = ? GROUP BY k",
            (date_str,),
        ).fetchall()
    return {
        "new": {r["k"]: r["c"] for r in new_rows},
        "removed": {r["k"]: r["c"] for r in removed_rows},
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


def get_scout_effective_kinds(ad_ids: list[str]) -> dict[str, str]:
    """Эффективный вид (COALESCE(verified_kind, kind)) для набора ad_id.

    Нужен чтобы уведомление о прогоне отражало результат Haiku-проверки,
    а не сырой kind поискового запроса (иначе ложные совпадения вроде
    Citroën C4 Picasso/Spacetourer под запрос 'citroen spacetourer' лезут
    в дайджест как «новая машина» несмотря на то что verify их отбраковал).
    """
    ids = [str(a) for a in ad_ids if a]
    if not ids:
        return {}
    with get_conn() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT ad_id, {_EFF_KIND} AS k FROM scout_listings WHERE ad_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {r["ad_id"]: r["k"] for r in rows}


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
