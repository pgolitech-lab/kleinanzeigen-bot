"""Интеграционные тесты слоя БД на временной SQLite.

Переписанные на оконные функции запросы (pipeline_threads, find_reminder_candidates,
list_clients, list_threads_for_client) сверяются с прежними SQL (скопированы ниже
как эталон) на сгенерированной БД с несколькими десятками тредов. Плюс: транзакции,
соединение на поток, кэш настроек, пакетный upsert scout.
"""
from __future__ import annotations

import random
import threading
from datetime import datetime, timedelta

import pytest


# --- Эталон: SQL до оптимизации (commit 30fb4f4) ---------------------------------

_LEGACY_FIND_REMINDER_CANDIDATES = """
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

_LEGACY_LIST_CLIENTS = """
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

_LEGACY_LIST_THREADS_FOR_CLIENT = """
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

_LEGACY_PIPELINE_THREADS = """
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


# --- Фикстуры ----------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "q.db")
    db_mod.init_db()
    return db_mod


def _account(db) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (id, name, gmail_email, gmail_app_password, "
            "created_at) VALUES (1, 'a', 'a@x', 'p', '2026-01-01')")


STATUSES_IN = ["new", "pending", "edited", "approved", "sent", "sent_debug",
               "skipped", "skipped_sold", "archived"]


def _populate(db, seed: int = 7, n_threads: int = 60) -> None:
    """Случайные треды: несколько покупателей, входящие/исходящие, ack, wait/closed,
    автопилот, флаги. Таймстампы уникальны (у старого SQL порядок при равенстве
    не определён — сравнивать там нечего)."""
    rnd = random.Random(seed)
    base = datetime(2026, 9, 1)
    tick = [0]

    def ts() -> str:
        tick[0] += rnd.randint(1, 5000)
        return (base + timedelta(seconds=tick[0])).isoformat()

    _account(db)
    with db.get_conn() as conn:
        buyers = [f"b{i}@relay" for i in range(12)]
        for t in range(n_threads):
            tid = f"t{t}"
            buyer = rnd.choice(buyers)
            ad = f"ad{rnd.randint(1, 15)}"
            for _ in range(rnd.randint(1, 5)):
                if rnd.random() < 0.75:
                    status = rnd.choice(STATUSES_IN)
                    sent_at = ts() if status in ("sent", "sent_debug") else None
                    conn.execute(
                        "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
                        "buyer_name, buyer_display_name, ad_id, ad_title, ad_price, de_client, "
                        "de_answer, created_at, sent_at, cost_usd, deal_brief_json, "
                        "reminder_state, is_reminder, is_auto_ack) "
                        "VALUES (1, 'in', ?, ?, ?, ?, ?, ?, ?, 'q', ?, ?, ?, ?, ?, ?, 0, 0)",
                        (status, tid, buyer,
                         rnd.choice([None, f"Name{buyer[1:3]}"]), ad,
                         rnd.choice([None, "", f"Title {ad}"]),
                         rnd.choice([None, "", "100 €"]),
                         rnd.choice([None, "a"]) if not sent_at else "a",
                         ts(), sent_at, rnd.choice([None, 0.01]),
                         rnd.choice([None, "", '{"x":1}']),
                         rnd.choice([None, "none", "offered"])),
                    )
                else:
                    t_out = ts()
                    conn.execute(
                        "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
                        "buyer_name, de_answer, created_at, sent_at, is_auto_ack, is_reminder) "
                        "VALUES (1, 'out', ?, ?, ?, 'r', ?, ?, ?, 0)",
                        (rnd.choice(["sent", "sent_debug", "pending"]), tid, buyer,
                         t_out, t_out, rnd.choice([0, 1])),
                    )
            r = rnd.random()
            if r < 0.1:
                conn.execute("INSERT INTO closed_threads VALUES (?, ?, 'op')", (tid, ts()))
            elif r < 0.2:
                conn.execute("INSERT INTO wait_threads VALUES (?, ?, 'op')", (tid, ts()))
            if rnd.random() < 0.15:
                conn.execute(
                    "INSERT INTO thread_autopilot (gmail_thread_id, active, floor_price_eur, "
                    "notify_mode, messages_sent, started_at) VALUES (?, ?, 50, 'silent', 3, ?)",
                    (tid, rnd.choice([0, 1]), ts()))
            if rnd.random() < 0.2:
                conn.execute(
                    "INSERT INTO thread_flags VALUES (?, ?, ?, ?)",
                    (tid, rnd.choice([0, 1]), rnd.choice([0, 1]), ts()))


def _rows(rows, drop_prefix: str = "autopilot_"):
    return [{k: r[k] for k in r.keys() if not k.startswith(drop_prefix)} for r in rows]


# --- Сверка с эталоном ---------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 7, 42])
def test_pipeline_threads_matches_legacy(tmp_db, seed):
    _populate(tmp_db, seed)
    with tmp_db.get_conn() as conn:
        legacy = conn.execute(_LEGACY_PIPELINE_THREADS).fetchall()
    new = tmp_db.pipeline_threads()
    assert len(new) > 5
    assert _rows(new) == _rows(legacy)


def test_pipeline_threads_autopilot_columns(tmp_db):
    _populate(tmp_db, 7)
    rows = tmp_db.pipeline_threads()
    for r in rows:
        ap = tmp_db.get_thread_autopilot(r["gmail_thread_id"])
        assert r["autopilot_active"] == (ap["active"] if ap else 0)
        assert r["autopilot_messages_sent"] == (ap["messages_sent"] if ap else None)
        assert r["autopilot_floor_price_eur"] == (ap["floor_price_eur"] if ap else None)
        assert r["autopilot_notify_mode"] == (ap["notify_mode"] if ap else None)


@pytest.mark.parametrize("seed", [1, 7, 42])
@pytest.mark.parametrize("after_days", [0.0001, 1, 10, 40])
def test_reminder_candidates_match_legacy(tmp_db, seed, after_days):
    _populate(tmp_db, seed)
    cutoff = (datetime.utcnow() - timedelta(days=after_days)).isoformat()
    with tmp_db.get_conn() as conn:
        legacy = conn.execute(_LEGACY_FIND_REMINDER_CANDIDATES,
                              (cutoff, datetime.utcnow().isoformat())).fetchall()
    assert _rows(tmp_db.find_reminder_candidates(after_days)) == _rows(legacy)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_list_clients_matches_legacy(tmp_db, seed):
    _populate(tmp_db, seed)
    with tmp_db.get_conn() as conn:
        legacy = conn.execute(_LEGACY_LIST_CLIENTS).fetchall()
    new = tmp_db.list_clients()
    assert len(new) > 3
    assert _rows(new) == _rows(legacy)


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_list_threads_for_client_matches_legacy(tmp_db, seed):
    _populate(tmp_db, seed)
    with tmp_db.get_conn() as conn:
        for c in tmp_db.list_clients():
            legacy = conn.execute(_LEGACY_LIST_THREADS_FOR_CLIENT, (c["email"],)).fetchall()
            assert _rows(tmp_db.list_threads_for_client(c["email"])) == _rows(legacy)


def test_pipeline_last_in_tiebreak_by_id(tmp_db):
    """Два входящих с одинаковым created_at — берётся строка с большим id."""
    t = "2026-10-01T10:00:00"
    _account(tmp_db)
    with tmp_db.get_conn() as conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
                "de_client, created_at) VALUES (1, 'in', 'pending', 'tie', 'q', ?)", (t,))
        last_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    rows = tmp_db.pipeline_threads()
    assert [r["id"] for r in rows] == [last_id]


# --- Индексы ---------------------------------------------------------------------

def test_indexes_created_and_idempotent(tmp_db):
    tmp_db.init_db()  # повторный вызов не падает
    with tmp_db.get_conn() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'")}
        plan = " ".join(r["detail"] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE gmail_message_id = ?", ("x",)))
    assert {"idx_messages_gmail_msg", "idx_messages_created",
            "idx_messages_thread_dir_created", "idx_messages_buyer",
            "idx_messages_ad"} <= names
    assert "idx_messages_gmail_msg" in plan


# --- Транзакции -------------------------------------------------------------------

def test_mark_thread_sold_rolls_back_on_failure(tmp_db, monkeypatch):
    _account(tmp_db)
    with tmp_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, ad_id, "
            "created_at) VALUES (1, 'in', 'pending', 'ts', 'ad1', ?)",
            (datetime.utcnow().isoformat(),))
        msg_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]

    import modules.db_sales as sales

    def boom(*a, **k):
        raise RuntimeError("close failed")

    monkeypatch.setattr(sales, "close_thread", boom)
    with pytest.raises(RuntimeError):
        sales.mark_thread_sold(msg_id, 100.0)
    row = tmp_db.get_message(msg_id)
    assert row["status"] == "pending" and row["sold_price_eur"] is None
    # Соединение потока не осталось в транзакции — следующая запись проходит.
    tmp_db.update_message(msg_id, status="new")
    assert tmp_db.get_message(msg_id)["status"] == "new"


def test_mark_thread_sold_commits_all(tmp_db):
    _account(tmp_db)
    with tmp_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, ad_id, "
            "created_at) VALUES (1, 'in', 'pending', 'ts', 'ad1', ?)",
            (datetime.utcnow().isoformat(),))
        msg_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
    tmp_db.start_thread_autopilot("ts", 10.0, "silent")
    tmp_db.mark_thread_sold(msg_id, 100.0)
    assert tmp_db.get_message(msg_id)["status"] == "skipped_sold"
    assert tmp_db.thread_close_reason("ts") == "sold"
    assert tmp_db.get_thread_autopilot("ts")["active"] == 0


def test_transaction_nested_joins_outer(tmp_db):
    with pytest.raises(ValueError):
        with tmp_db.transaction() as conn:
            tmp_db.set_setting("k_outer", "1")
            with tmp_db.transaction():
                tmp_db.set_setting("k_inner", "1")
            raise ValueError
    assert tmp_db.get_setting("k_outer") is None
    assert tmp_db.get_setting("k_inner") is None


def test_nested_transaction_savepoint_isolates_inner_failure(tmp_db):
    with tmp_db.transaction():
        tmp_db.set_setting("outer", "1")
        try:
            with tmp_db.transaction():
                tmp_db.set_setting("inner", "1")
                raise ValueError
        except ValueError:
            pass
    assert tmp_db.get_setting("outer") == "1"
    assert tmp_db.get_setting("inner") is None


def test_send_mode_bypasses_cache(tmp_db):
    import config
    tmp_db.set_setting("send_mode", "production")
    assert config.send_mode() == "production"
    with tmp_db.get_conn() as conn:  # запись мимо set_setting (другой процесс)
        conn.execute("UPDATE settings SET value='disabled' WHERE key='send_mode'")
    assert config.send_mode() == "disabled"


def test_increment_autopilot_returning(tmp_db):
    tmp_db.start_thread_autopilot("ap", 10.0, "silent")
    assert tmp_db.increment_autopilot_messages("ap") == 1
    assert tmp_db.increment_autopilot_messages("ap") == 2
    assert tmp_db.increment_autopilot_messages("missing") == 0


def test_upsert_negotiation_state_and_flags(tmp_db):
    tmp_db.upsert_negotiation_state("n1", phase="negotiating", floor_eur=80)
    tmp_db.upsert_negotiation_state("n1", our_last_offer_eur=95)
    st = tmp_db.get_negotiation_state("n1")
    assert (st["phase"], st["floor_eur"], st["our_last_offer_eur"]) == ("negotiating", 80, 95)
    tmp_db.upsert_negotiation_state("n2")
    assert tmp_db.get_negotiation_state("n2")["phase"] == "opening"
    tmp_db.set_thread_flags("f1", is_pinned=1)
    tmp_db.set_thread_flags("f1", operator_unread=1)
    f = tmp_db.get_thread_flags("f1")
    assert (f["is_pinned"], f["operator_unread"]) == (1, 1)


# --- Соединение на поток и кэш настроек --------------------------------------------

def test_connection_reused_per_thread(tmp_db):
    with tmp_db.get_conn() as c1, tmp_db.get_conn() as c2:
        assert c1 is c2
    other = []

    def worker():
        with tmp_db.get_conn() as c:
            other.append(c)
            c.execute("SELECT 1").fetchone()

    th = threading.Thread(target=worker)
    th.start(); th.join()
    assert other and other[0] is not c1


def test_synchronous_normal(tmp_db):
    with tmp_db.get_conn() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_settings_cache_invalidated_on_set(tmp_db):
    tmp_db.set_setting("send_mode", "disabled")
    assert tmp_db.get_setting("send_mode") == "disabled"
    tmp_db.set_setting("send_mode", "redirect")
    assert tmp_db.get_setting("send_mode") == "redirect"
    assert tmp_db.get_setting("nope", "dflt") == "dflt"


def test_settings_cache_ttl_picks_external_write(tmp_db, monkeypatch):
    tmp_db.set_setting("x", "1")
    assert tmp_db.get_setting("x") == "1"
    with tmp_db.get_conn() as conn:  # запись в обход set_setting (другой процесс)
        conn.execute("UPDATE settings SET value='2' WHERE key='x'")
    assert tmp_db.get_setting("x") == "1"  # ещё в кэше
    monkeypatch.setattr(tmp_db, "SETTINGS_CACHE_TTL_SEC", 0.0)
    assert tmp_db.get_setting("x") == "2"


def test_claude_fast_model_default(tmp_db):
    import config
    assert config.claude_fast_model() == "claude-haiku-4-5"
    tmp_db.set_setting("claude_fast_model", "claude-haiku-x")
    assert config.claude_fast_model() == "claude-haiku-x"


# --- Scout ------------------------------------------------------------------------

def _listing(ad_id, price=100):
    return {"ad_id": ad_id, "kind": "car", "title": f"T{ad_id}", "price_eur": price}


def test_upsert_scout_listing_new_vs_update_and_rejected(tmp_db):
    assert tmp_db.upsert_scout_listing(_listing("1")) is True
    assert tmp_db.upsert_scout_listing(_listing("1", 200)) is False
    assert tmp_db.upsert_scout_listing({"ad_id": ""}) is False
    tmp_db.apply_scout_correction("1", "remove")
    assert tmp_db.upsert_scout_listing(_listing("1", 300)) is False
    with tmp_db.get_conn() as conn:
        r = conn.execute("SELECT * FROM scout_listings WHERE ad_id='1'").fetchone()
    assert (r["active"], r["rejected"], r["price_eur"]) == (0, 1, 300)
    assert r["first_seen_at"] < r["last_seen_at"]


def test_upsert_scout_listings_batch(tmp_db):
    tmp_db.upsert_scout_listing(_listing("old"))
    res = tmp_db.upsert_scout_listings(
        [_listing("a"), _listing("old"), _listing("a"), {"ad_id": None}, _listing("b")])
    assert res == [True, False, False, False, True]
    with tmp_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scout_listings").fetchone()[0] == 3


# --- Уроки ------------------------------------------------------------------------

def test_add_lesson_idempotent_by_source(tmp_db):
    a = tmp_db.add_lesson(ad_id="ad1", good_answer_ru="v1",
                          source_type="history_review", source_ref=42)
    b = tmp_db.add_lesson(ad_id="ad1", good_answer_ru="v2",
                          source_type="history_review", source_ref="42")
    assert a == b
    rows = tmp_db.list_lessons_for_ad("ad1")
    assert len(rows) == 1 and rows[0]["good_answer_ru"] == "v2"
    # другой тип источника с тем же ref — отдельный урок
    c = tmp_db.add_lesson(ad_id="ad1", source_type="operator_edit", source_ref="42")
    assert c != a


def test_add_lesson_without_source_still_appends(tmp_db):
    tmp_db.add_lesson(ad_id="ad2", good_answer_ru="x")
    tmp_db.add_lesson(ad_id="ad2", good_answer_ru="x")
    assert len(tmp_db.list_lessons_for_ad("ad2")) == 2
