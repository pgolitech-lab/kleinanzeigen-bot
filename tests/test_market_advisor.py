"""Тесты модуля «Рынок-советник» (modules/market_advisor.py, modules/db_market.py).

Временная SQLite: наши объявления — в messages, рынок — в scout_listings,
места в выдаче — через db_market.record_scrape. LLM не вызывается (use_llm=False
или подмена call_json).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "t.db")
    db_mod.init_db()
    from modules import db_market, market_advisor as ma
    db_market._schema_ready.clear()
    now = datetime.utcnow()
    with db_mod.get_conn() as conn:
        conn.execute("INSERT INTO accounts (id, name, gmail_email, gmail_app_password, created_at) "
                     "VALUES (1, 'Acc1', 'a@x', 'p', ?), (2, 'Acc2', 'b@x', 'p', ?)",
                     (now.isoformat(), now.isoformat()))
    return db_mod, db_market, ma, now


def _iso(now, days=0, hours=0):
    return (now - timedelta(days=days, hours=hours)).isoformat()


def add_msg(db_mod, *, ad_id, title, price, account_id=1, created_at, thread=None, direction="in"):
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, ad_id, ad_title, ad_price, ad_url, ad_description, "
            "status, created_at, gmail_thread_id, de_client) VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, 'Hallo')",
            (account_id, direction, ad_id, title, price, f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}",
             "Verkaufe 3er Sitzbank aus Leder, sehr guter Zustand, passend für Peugeot Traveller.",
             created_at, thread or f"t{ad_id}{created_at}"))


def add_market(db_mod, *, ad_id, title, price, plz="10115", city="Berlin", description="", first, last,
               active=1, deactivated_at=None):
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO scout_listings (ad_id, kind, title, url, price_eur, price_raw, plz, city, description, "
            "first_seen_at, last_seen_at, active, deactivated_at, verified_kind) "
            "VALUES (?, 'part', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'part')",
            (ad_id, title, f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}", price, f"{price} €", plz, city,
             description, first, last, active, deactivated_at))


def serp_row(ad_id, pos, title="x", price=100.0, page=None, is_top=0, city="Berlin", plz="10115"):
    return {"ad_id": ad_id, "pos": pos, "page": page or (1 + (pos - 1) // 25), "is_top": is_top,
            "title": title, "price_eur": price, "price_raw": f"{price} €", "city": city, "plz": plz}


# --- db_market -------------------------------------------------------------------

def test_price_history_written_only_on_change(env):
    db_mod, dbm, ma, now = env
    for price in (1300.0, 1300.0, 1250.0, 1250.0, None):
        dbm.record_scrape(source="scout", keywords="Sitzbank Traveller", category="c223", query_ref=1,
                          rows=[serp_row("111", 1, price=price)], pages=1)
    hist = dbm.price_history("111")
    assert [h["price_eur"] for h in hist] == [1300.0, 1250.0, None]
    assert dbm.price_changes(["111"])["111"][0]["price_eur"] == 1300.0


def test_serp_organic_pos_skips_top_ads(env):
    _db, dbm, _ma, _now = env
    rid = dbm.record_scrape(source="market", keywords="sitzbank traveller", category="c223", query_ref=1,
                            rows=[serp_row("1", 1, is_top=1), serp_row("2", 2), serp_row("3", 3, is_top=None)],
                            pages=1)
    rows = dbm.serp_for_runs([rid])[rid]
    assert [(r["ad_id"], r["organic_pos"]) for r in rows] == [("1", None), ("2", 1), ("3", 2)]
    assert dbm.runs_since("2000-01-01")[0]["keywords"] == "sitzbank traveller"


def test_failed_run_not_counted_and_purge(env):
    _db, dbm, _ma, _now = env
    dbm.record_scrape(source="market", keywords="tisch traveller", category="c223", query_ref=1,
                      rows=[], pages=0, error="http 403")
    assert dbm.runs_since("2000-01-01") == []
    with _db.get_conn() as conn:
        conn.execute("UPDATE market_runs SET started_at='2000-01-01'")
    assert dbm.purge_old_serp(days=30) == 1


def test_market_query_operator_not_overwritten(env):
    _db, dbm, _ma, _now = env
    qid = dbm.upsert_market_query("Sitzbank  Traveller", source="operator", note="моё")
    assert dbm.upsert_market_query("sitzbank traveller", note="авто") == qid
    q = dbm.list_market_queries()[0]
    assert q["keywords"] == "sitzbank traveller" and q["note"] == "моё"


# --- признаки и «наше» ------------------------------------------------------------

def test_regex_features_from_title_and_description():
    from modules import market_advisor as ma
    f = ma.regex_features("Sitzbank Peugeot Traveller", "3er Bank, Echtleder schwarz, mit Isofix, Spacetourer")
    assert f["category"] == "sitzbank" and f["material"] == "leder" and f["seats"] == 3
    assert f["isofix"] and set(f["models"]) >= {"traveller", "spacetourer"}
    f2 = ma.regex_features("VIP Einzelsitz Kunstleder", "")
    assert f2["category"] == "einzelsitz" and f2["material"] == "kunstleder" and f2["vip"] and f2["seats"] == 1
    assert ma.regex_features("Sitzbank defekt Traveller")["condition"] == "defekt"
    assert ma.regex_features("Sitzbank wie neu")["condition"] == "gebraucht"


def test_clean_title_states():
    from modules import market_advisor as ma
    assert ma.clean_title('Gelöscht • 3er Leder Sitzbank') == ("3er Leder Sitzbank", "deleted")
    assert ma.clean_title('Nutzer-Anfrage zu deiner Anzeige "Tisch Traveller"')[0] == "Tisch Traveller"


def _seed_market(env, n_comp=5):
    """Наше: 2 объявления в Eschweiler (одно найдено только в выдаче). Рынок: n_comp кожаных 3er."""
    db_mod, dbm, ma, now = env
    title = "3er Leder Sitzbank Peugeot Traveller"
    add_msg(db_mod, ad_id="9000000001", title=title, price="1.299 € VB", created_at=_iso(now, 10))
    add_market(db_mod, ad_id="9000000001", title=title, price=1299, plz="52249", city="Eschweiler",
               first=_iso(now, 12), last=_iso(now, 0, 1))
    # перепост без писем — наш по городу и заголовку
    add_market(db_mod, ad_id="9000000002", title="Peugeot Traveller 3er Sitzbank Leder", price=1299,
               plz="52249", city="Eschweiler", first=_iso(now, 30), last=_iso(now, 20), active=0,
               deactivated_at=_iso(now, 15))
    for i, p in enumerate([800, 1300, 1375, 1550, 1610][:n_comp]):
        add_market(db_mod, ad_id=f"80000000{i:02d}", title=f"Sitzbank 3er Leder Traveller Spacetourer #{i}",
                   price=p, description="Echtleder", first=_iso(now, 20), last=_iso(now, 1))
    # не сопоставимое: ткань, VIP
    add_market(db_mod, ad_id="8100000001", title="Sitzbank Stoff Traveller 3er", price=300, first=_iso(now, 5),
               last=_iso(now, 1))
    add_market(db_mod, ad_id="8100000002", title="VIP Einzelsitz Leder", price=500, first=_iso(now, 5),
               last=_iso(now, 1))
    return title


def test_is_ours_by_ad_id_and_location(env):
    db_mod, _dbm, ma, now = env
    _seed_market(env)
    ctx = ma._Ctx()
    assert ctx.is_ours("9000000001") == "наш ad_id"
    assert ctx.is_ours("9000000002")  # тот же город и заголовок
    assert ctx.is_ours("8000000001") is None


def test_advise_price_excludes_own_and_compares(env):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    r = ma.advise_price(ad_id="9000000001")
    assert r["status"] == "ok" and r["n"] == 5
    assert r["median"] == 1375 and r["p25"] == 1300 and r["p75"] == 1550
    assert r["verdict"] == "below" and "дешевле" in r["verdict_ru"]  # 1299 < P25 = 1300
    assert ma.advise_price(title="3er Leder Sitzbank Traveller", price_eur=1400)["verdict"] == "in_market"
    assert r["min_eur"] == 1300 and r["min_basis"] == "p25"
    assert all(not s["ad_id"].startswith("9") for s in r["similar"])
    assert r["excluded_own"] == 2
    hi = ma.advise_price(title="3er Leder Sitzbank Traveller", price_eur=1900)
    assert hi["verdict"] == "above" and hi["delta_pct"] == 38 and "дороже" in hi["verdict_ru"]


def test_advise_price_few_data(env):
    _db, _dbm, ma, _now = env
    _seed_market(env, n_comp=2)
    r = ma.advise_price(ad_id="9000000001")
    assert r["status"] == "few_data" and r["median"] is None and len(r["similar"]) == 2
    assert ma.advise_price(ad_id="nope")["status"] == "not_found"


# --- позиция и перепост ------------------------------------------------------------

def _runs(env, our_pos_list, keywords="sitzbank traveller"):
    _db, dbm, _ma, now = env
    for pos in our_pos_list:
        rows = [serp_row(f"70000{i:05d}", i, title=f"Sitzbank 2+1 Original Traveller {i}") for i in range(1, 60)]
        if pos:
            rows[pos - 1] = serp_row("9000000001", pos, title="3er Leder Sitzbank Peugeot Traveller",
                                     city="Eschweiler", plz="52249")
        dbm.record_scrape(source="market", keywords=keywords, category="c223", query_ref=1, rows=rows, pages=3)


def test_rank_report_sinking_with_trend(env):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [3, 12, 30])
    rep = ma.rank_report(ad_id="9000000001")
    it = rep["items"][0]
    assert it["status"] == "sinking" and it["best"]["pos"] == 30 and it["best"]["page"] == 2
    assert it["queries"][0]["trend"] == 27


def test_rank_report_missing_and_top(env):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [0])
    assert ma.rank_report(ad_id="9000000001")["items"][0]["status"] == "missing"
    _runs(env, [2])
    assert ma.rank_report(ad_id="9000000001")["items"][0]["status"] == "top"


def test_unrelated_query_ignored(env):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [0], keywords="tisch traveller")
    assert ma.rank_report(ad_id="9000000001")["items"][0]["status"] == "no_data"


def test_repost_suggestion_for_sinking_quiet_ad(env):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [40])
    sug = ma.repost_suggestions(limit=5, use_llm=False)
    assert len(sug) == 1
    s = sug[0]
    assert s["action"] == "repost" and s["old_ad"]["ad_id"] == "9000000001"
    assert any("опустилось" in r for r in s["reasons_ru"]) and any("без писем" in r for r in s["reasons_ru"])
    assert s["text"]["title"] != "3er Leder Sitzbank Peugeot Traveller"
    assert s["text"]["source"] == "rules" and len(s["text"]["title"]) <= 65
    # 1299 дешевле P25 = 1300 → при перепосте предлагаем нижнюю границу рынка
    assert s["price"]["suggested"] == 1300 and "дешевле" in s["price"]["advice_ru"]
    assert s["window"]["label_ru"]


def test_repost_skips_fresh_ad(env):
    db_mod, _dbm, ma, now = env
    title = "Tisch Peugeot Traveller"
    add_msg(db_mod, ad_id="9100000001", title=title, price="150 €", created_at=_iso(now, 1))
    _runs(env, [0], keywords="tisch traveller")
    assert ma.repost_suggestions(use_llm=False) == []


def test_vary_price_respects_floor():
    from modules import market_advisor as ma
    assert ma._vary_price(1299, None) < 1299
    assert ma._vary_price(500, 499) == 499 or ma._vary_price(500, 499) == 500
    assert ma._vary_price(1000, 1000) == 1000
    assert ma._vary_price(None, None) is None


def test_repost_window_berlin():
    from modules import market_advisor as ma
    # чт 2026-10-22 10:00 UTC → ближайшее окно вс 25.10 16–19
    w = ma.next_repost_window(datetime(2026, 10, 22, 10, 0))
    assert w["start"].startswith("2026-10-25T16:00") and not w["now"]
    # пн 2026-10-26 16:30 UTC = 17:30 Берлин (зимнее время) — внутри окна
    assert ma.next_repost_window(datetime(2026, 10, 26, 16, 30))["now"]


# --- дубли -----------------------------------------------------------------------

def test_duplicates_danger_across_accounts(env):
    db_mod, dbm, ma, now = env
    title = "Tisch Peugeot Traveller Original"
    add_msg(db_mod, ad_id="9200000001", title=title, price="150 €", account_id=1, created_at=_iso(now, 3))
    add_msg(db_mod, ad_id="9200000002", title=title, price="150 €", account_id=2, created_at=_iso(now, 9))
    add_msg(db_mod, ad_id="9200000003", title="Original Tisch Traveller Peugeot", price="140 €", account_id=2,
            created_at=_iso(now, 15))
    rep = ma.duplicates_report()
    assert rep["danger"] == 1 and rep["ads_to_remove"] == 2
    g = rep["groups"][0]
    assert g["keep"]["ad_id"] == "9200000001"
    assert {r["ad_id"] for r in g["remove"]} == {"9200000002", "9200000003"}
    assert "снимите 2 из 3" in g["message_ru"] and "description" not in g["keep"]
    # перепост такого товара — сначала убрать дубли
    sug = [s for s in ma.repost_suggestions(use_llm=False) if s["item_id"] == g["item_id"]]
    assert not sug or sug[0]["action"] == "remove_duplicates"


def test_deleted_ad_not_duplicate(env):
    db_mod, _dbm, ma, now = env
    add_msg(db_mod, ad_id="9300000001", title="Tisch Traveller", price="150 €", created_at=_iso(now, 2))
    add_msg(db_mod, ad_id="9300000002", title="Gelöscht • Tisch Traveller", price="150 €", created_at=_iso(now, 3))
    assert ma.duplicates_report()["groups"] == []


# --- заметность ------------------------------------------------------------------

def test_visibility_missing_terms_and_llm_cache(env, monkeypatch):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [30])
    calls = []

    def fake_call_json(**kw):
        calls.append(kw)
        return ({"titles": ["3er Leder Sitzbank 2+1 Original Traveller"], "first_lines": ["Original 2+1 Bank"],
                 "notes_ru": ["добавьте 2+1"]}, {})
    from modules import claude_utils
    monkeypatch.setattr(claude_utils, "call_json", fake_call_json)
    monkeypatch.setattr(claude_utils, "smart_model", lambda: "claude-sonnet-4-6")
    r = ma.visibility_tips(ad_id="9000000001")
    terms = {m["term"] for m in r["missing_terms"]}
    assert {"2+1", "Original"} <= terms
    assert r["suggestions"]["source"] == "llm" and len(r["leaders"]["above"]) == 8
    r2 = ma.visibility_tips(ad_id="9000000001")
    assert r2["suggestions"]["source"] == "cache" and len(calls) == 1


def test_visibility_llm_failure_falls_back(env, monkeypatch):
    _db, _dbm, ma, _now = env
    _seed_market(env)
    _runs(env, [30])
    from modules import claude_utils

    def boom(**kw):
        raise RuntimeError("нет подписки")
    monkeypatch.setattr(claude_utils, "call_json", boom)
    monkeypatch.setattr(claude_utils, "smart_model", lambda: "claude-sonnet-4-6")
    r = ma.visibility_tips(ad_id="9000000001")
    assert r["ok"] and r["suggestions"]["source"] == "rules" and "LLM недоступен" in r["warning"]
    assert any("2+1" in t for t in r["suggestions"]["titles"])


# --- признаки (Haiku) --------------------------------------------------------------

def test_enrich_features_llm_error_saves_regex(env, monkeypatch):
    _db, dbm, ma, _now = env
    _seed_market(env)

    def boom(items):
        raise RuntimeError("timeout")
    monkeypatch.setattr(ma, "_llm_features", boom)
    r = ma.enrich_features(limit=100)
    assert r["llm"] == 0 and r["regex"] >= 7 and r["errors"] >= 1
    # повтор: regex-записи снова в работе (ждут Haiku)
    monkeypatch.setattr(ma, "_llm_features", lambda items: {
        it["ad_id"]: {"category": "sitzbank", "material": "leder", "vip": False, "seats": 3,
                      "condition": "gebraucht", "models": ["traveller"]} for it in items})
    r2 = ma.enrich_features(limit=100)
    assert r2["llm"] == r["regex"]
    f = ma.features_for([{"ad_id": "8100000001", "title": "Sitzbank Stoff Traveller 3er", "description": ""}])
    assert f["8100000001"]["material"] == "leder"  # LLM перекрыл regex


# --- scout: места в выдаче --------------------------------------------------------

def _card(ad_id, title, price):
    return (f'<article data-adid="{ad_id}" data-href="/s-anzeige/x/{ad_id}-223-1">'
            f'<h2>{title}</h2><p>{price} €</p><p>52249 Eschweiler</p><p>Heute, 10:00</p></article>')


def test_scout_scrape_query_records_positions(env, monkeypatch):
    _db, dbm, _ma, _now = env
    from modules import scout
    pages = {
        1: "".join(_card(f"55500000{i:02d}", f"Sitzbank {i}", 100 + i) for i in range(20))
           + '<script>{"id":[0,"5550000000"],"topAd":[0,true]}</script>',
        2: "".join(_card(f"55500001{i:02d}", f"Sitzbank b{i}", 200 + i) for i in range(5)),
    }

    def fake_fetch(client, url, browser=None):
        return pages[2] if "seite:2" in url else pages[1]
    monkeypatch.setattr(scout, "fetch_search_page", fake_fetch)
    monkeypatch.setattr(scout.time, "sleep", lambda s: None)
    q = {"id": 7, "kind": "part", "category": "c223", "keywords": "sitzbank traveller", "max_pages": 3}
    stats = {"pages": 0}
    rows = scout.scrape_query(None, q, stats=stats)
    assert stats["pages"] == 2 and len(rows) == 25
    assert rows[0]["is_top"] == 1 and rows[1]["is_top"] is None and rows[20]["page"] == 2 and rows[20]["pos"] == 21
    scout._record_market(q, rows, stats["pages"], None, datetime.utcnow().isoformat())
    run = dbm.runs_since("2000-01-01")[0]
    assert run["source"] == "scout" and run["cards"] == 25
    # машины не пишем
    scout._record_market({**q, "kind": "car"}, rows, 2, None, datetime.utcnow().isoformat())
    assert len(dbm.runs_since("2000-01-01")) == 1


def test_parse_top_flags():
    from modules import scout
    html = ('<x props="{&quot;id&quot;:[0,&quot;3167594640&quot;],&quot;highlight&quot;:[0,false],'
            '&quot;topAd&quot;:[0,false]}"></x><x props="{&quot;id&quot;:[0,&quot;3409753958&quot;],'
            '&quot;topAd&quot;:[0,true]}"></x>')
    assert scout.parse_top_flags(html) == {"3167594640": False, "3409753958": True}


def test_summary_lines_no_crash_on_empty(env):
    _db, _dbm, ma, _now = env
    assert ma.summary_lines() == []


def test_repost_of_duplicated_item_asks_to_remove_first(env):
    db_mod, _dbm, ma, now = env
    title = "Tisch Peugeot Traveller Original"
    add_msg(db_mod, ad_id="9400000001", title=title, price="150 €", account_id=1, created_at=_iso(now, 10))
    add_msg(db_mod, ad_id="9400000002", title=title, price="150 €", account_id=2, created_at=_iso(now, 12))
    sug = ma.repost_suggestions(use_llm=False)
    assert len(sug) == 1 and sug[0]["action"] == "remove_duplicates"
    assert sug[0]["text"] is None and "снимите 1 из 2" in sug[0]["steps_ru"][0]


def test_market_report_script_builds(env):
    _db, _dbm, _ma, _now = env
    _seed_market(env)
    _runs(env, [40])
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "market_report", Path(__file__).resolve().parent.parent / "scripts" / "market_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.build(limit=5, use_llm=False)
    assert "## Дубли" in text and "3er Leder Sitzbank" in text and "## Перепост (1)" in text
