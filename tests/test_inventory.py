"""Склад: нормализация заголовков, автопривязка, движения, синхронизация продаж, правила, отчёт."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def inv(tmp_path, monkeypatch):
    import database as db_mod
    from modules import db_inventory
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    db_mod.init_db()
    db_inventory.ensure_schema()
    with db_mod.get_conn() as conn:
        conn.execute("INSERT INTO accounts (id, name, gmail_email, gmail_app_password, is_active, created_at) "
                     "VALUES (1, 'a', 'a@x', 'p', 1, ?)", (datetime.utcnow().isoformat(),))
    from modules import inventory
    return inventory


def _msg(db, *, ad_id, title, price=None, thread="t1", status="pending", created_at=None,
         sold_price=None, sold_at=None, desc=None):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, ad_id, ad_title, ad_price, "
            "ad_description, sold_price_eur, sold_at, created_at) VALUES (1, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (status, thread, ad_id, title, price, desc, sold_price, sold_at,
             created_at or datetime.utcnow().isoformat()))
        return cur.lastrowid


# ─── чистые функции ───────────────────────────────────────────────────────────

def test_clean_title_variants():
    from modules.inventory import clean_ad_title as c
    t = c('Nutzer-Anfrage zu deiner Anzeige "Klapptisch Opel Zafira Life,  Toyota ProAce Verso"')
    assert (t.title, t.quality) == ("Klapptisch Opel Zafira Life, Toyota ProAce Verso", 2)
    t = c("Gelöscht • Fahrer-Beifahrersitz Peugeot Traveller", has_ad_data=True)
    assert (t.title, t.state, t.quality) == ("Fahrer-Beifahrersitz Peugeot Traveller", "deleted", 3)
    assert c("Reserviert • Klapptisch", has_ad_data=True).state == "reserved"
    t = c("3 er Sitzbank 2+1 Spacetourer ... bis zu 25 % schneller verkaufen*")
    assert (t.title, t.quality) == ("3 er Sitzbank 2+1 Spacetourer", 1)
    # Тема рассылки без данных объявления — мусор
    assert c("Deine wöchentliche Anzeigenübersicht").title == ""
    assert c(None).quality == 0


def test_match_key_survives_repost_noise():
    from modules.inventory import match_key
    a = match_key("Fahrer und Beifahrersitz Traveller  ProAce SpaceTourer Zafira")
    b = match_key("Fahrer und Beifahrersitz  Traveller ProAce SpaceTourer Zafira")
    assert a == b
    assert match_key("3-er Sitzbank Citroën") == match_key("3 er Sitzbank Citroen")
    assert match_key("Opel Zafirа") == match_key("Opel Zafira")  # кириллическая «а»


def test_parse_price():
    from modules.inventory import parse_price
    assert parse_price("1.299 € VB") == 1299.0
    assert parse_price("80 €") == 80.0
    assert parse_price("12,50 €") == 12.5
    assert parse_price(450) == 450.0
    assert parse_price("") is None and parse_price(None) is None and parse_price("VB") is None


def test_similarity_blocks_other_category_and_defect():
    from modules.inventory import similarity
    assert similarity("Klapptisch Traveller", 300, "Sitzbank Traveller", 300) == 0.0
    ok = similarity("Ledersitze 2+1 Traveller", 1000, "Ledersitze 2+1 Proace", 1000)
    bad = similarity("Ledersitze 2+1 defekt Traveller", 1000, "Ledersitze 2+1 Proace", 1000)
    assert ok >= 0.9 and bad < 0.55


# ─── автопривязка ─────────────────────────────────────────────────────────────

def test_link_new_then_repost_same_product(inv):
    r1 = inv.link_listing("100", title="Peugeot Traveller Expert,Opel Zafira Life,Citroen Sitzbank",
                          price="1.299 €", account_id=1)
    assert r1["method"] == "new"
    r2 = inv.link_listing("101", title="Peugeot Traveller Expert, Opel Zafira Life, Citroen Sitzbank",
                          price="1.500 €", account_id=1)
    assert r2["method"] == "normalized" and r2["product_id"] == r1["product_id"]
    p = inv.inv.get_product(r1["product_id"])
    # цена товара = цена свежего объявления (перепост с новой ценой)
    assert p["category"] == "sitzbank" and p["list_price_eur"] == 1500.0 and p["stock_tracked"] == 0


def test_link_known_updates_state(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price="300 €")
    again = inv.link_listing("100", title="Gelöscht • Klapptisch Traveller", price="300 €",
                             seen_at=(datetime.utcnow() + timedelta(minutes=1)).isoformat())
    assert again["method"] == "known" and again["product_id"] == r["product_id"]
    assert inv.inv.get_listing("100")["state"] == "deleted"


def test_link_junk_title_skipped(inv):
    r = inv.link_listing("999", title="Deine wöchentliche Anzeigenübersicht")
    assert r["product_id"] is None and inv.inv.get_listing("999") is None


def test_doubtful_pair_creates_question_and_answer_merges(inv):
    a = inv.link_listing("100", title="Peugeot Traveller Expert,Opel Zafira Life,Citroen Sitzbank", price=1299)
    b = inv.link_listing("200", title="Citroën Spacetourer,Toyota ProAce Verso,Fiat Ulysse Sitzbank", price=1299)
    assert b["method"] == "new" and b["question_id"]
    qs = inv.inv.list_open_questions()
    assert len(qs) == 1 and qs[0]["suggested_product_id"] == a["product_id"]
    res = inv.answer_question(qs[0]["id"], same=True)
    assert res["merged"] is True
    assert inv.inv.get_listing("200")["product_id"] == a["product_id"]
    assert inv.inv.get_product(b["product_id"])["archived_at"] is not None
    assert inv.inv.list_open_questions() == []
    with pytest.raises(ValueError):
        inv.answer_question(qs[0]["id"], same=True)


def test_answer_different_keeps_products(inv):
    inv.link_listing("100", title="Peugeot Traveller Sitzbank", price=1299)
    b = inv.link_listing("200", title="Citroen Spacetourer Sitzbank", price=1299)
    res = inv.answer_question(b["question_id"], same=False)
    assert res["merged"] is False and inv.inv.get_listing("200")["product_id"] == b["product_id"]


def test_llm_high_confidence_links(inv):
    a = inv.link_listing("100", title="Peugeot Traveller Sitzbank", price=1299)
    calls = []

    def judge(x, y):
        calls.append((x, y))
        return {"same": True, "confidence": "high", "reason": "та же скамья"}
    b = inv.link_listing("200", title="Citroen Spacetourer Sitzbank", price=1250, use_llm=True, judge=judge)
    assert calls and b["method"] == "llm" and b["product_id"] == a["product_id"]
    assert not b.get("question_id")


def test_llm_confident_no_creates_product_without_question(inv):
    inv.link_listing("100", title="Peugeot Traveller Sitzbank", price=1299)
    b = inv.link_listing("200", title="Citroen Spacetourer Sitzbank", price=1250, use_llm=True,
                         judge=lambda x, y: {"same": False, "confidence": "high", "reason": "другая"})
    assert b["method"] == "new" and not b.get("question_id")


def test_llm_failure_falls_back_to_operator_question(inv):
    inv.link_listing("100", title="Peugeot Traveller Sitzbank", price=1299)

    def boom(x, y):
        raise RuntimeError("подписка недоступна")
    b = inv.link_listing("200", title="Citroen Spacetourer Sitzbank", price=1250, use_llm=True, judge=boom)
    assert b["method"] == "new" and b["question_id"]


def test_llm_same_product_uses_call_json(monkeypatch):
    from modules import claude_utils, inventory
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"same": True, "confidence": "high", "reason": "ok"}, {}
    monkeypatch.setattr(claude_utils, "call_json", fake)
    out = inventory.llm_same_product({"title": "A", "price": 10}, {"title": "B", "price": None})
    assert out["same"] is True
    assert seen["schema"]["additionalProperties"] is False and "haiku" in seen["model"]


# ─── движения и остаток ───────────────────────────────────────────────────────

def test_receive_sale_writeoff_set_stock(inv):
    pid = inv.inv.create_product(title="Klapptisch")
    assert inv.ad_context(None)["stock"] == "unknown"
    inv.receive(pid, 3, 100.0)
    assert inv.inv.stock_qty(pid) == 3 and inv.inv.get_product(pid)["stock_tracked"] == 1
    inv.record_sale(product_id=pid, price_eur=300, gmail_thread_id="t1")
    inv.write_off(pid, 1, note="брак")
    assert inv.inv.stock_qty(pid) == 1
    inv.set_stock(pid, 5)
    assert inv.inv.stock_qty(pid) == 5
    assert inv.set_stock(pid, 5) is None
    with pytest.raises(ValueError):
        inv.receive(pid, 0)
    with pytest.raises(ValueError):
        inv.inv.add_movement(product_id=pid, kind="sale", qty=1)
    with pytest.raises(ValueError):
        inv.inv.add_movement(product_id=9999, kind="receipt", qty=1)


def test_record_sale_idempotent_per_thread(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    s1 = inv.record_sale(ad_id="100", price_eur=280, gmail_thread_id="t1")
    s2 = inv.record_sale(ad_id="100", price_eur=280, gmail_thread_id="t1")
    assert s1["created"] and not s2["created"] and s1["movement_id"] == s2["movement_id"]
    # учёт не начат — продажа в остаток не входит, остаток «неизвестен»
    assert inv.inv.stock_qty(r["product_id"]) == 0
    assert s1["stock_tracked"] is False


def test_record_sale_unknown_product(inv):
    res = inv.record_sale(ad_id="nope", price_eur=10, gmail_thread_id="t9")
    assert res["product_id"] is None and res["movement_id"] is None


def test_set_stock_after_untracked_sales(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    inv.record_sale(ad_id="100", price_eur=280, gmail_thread_id="t1")
    inv.set_stock(r["product_id"], 4)
    assert inv.inv.stock_qty(r["product_id"]) == 4


def test_sync_listings_and_sales_with_void(inv):
    import database as db
    _msg(db, ad_id="100", title='Nutzer-Anfrage zu deiner Anzeige "Klapptisch Opel Zafira Life"', thread="t1")
    _msg(db, ad_id="101", title="Klapptisch Opel Zafira Life", price="250 €", thread="t2",
         status="skipped_sold", sold_price=230.0, sold_at="2026-10-01T12:00:00")
    _msg(db, ad_id="555", title="Deine wöchentliche Anzeigenübersicht", thread="t3")
    st = inv.sync_listings()
    assert st["new"] == 1 and st["normalized"] == 1
    assert inv.inv.get_listing("100")["product_id"] == inv.inv.get_listing("101")["product_id"]
    sales = inv.sync_sales()
    assert sales["recorded"] == 1 and not sales["unmatched"]
    assert inv.sync_sales()["already"] == 1
    # оператор снял продажу — движение воидится, остаток возвращается
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET status='pending' WHERE gmail_thread_id='t2'")
    assert inv.sync_sales()["voided"] == 1
    assert inv.inv.stock_qty(inv.inv.get_listing("101")["product_id"]) == 0
    # снова продано — записывается заново (уникальность только среди невоидированных)
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET status='skipped_sold' WHERE gmail_thread_id='t2'")
    assert inv.sync_sales()["recorded"] == 1


def test_sync_sales_manual_by_title_and_unmatched(inv):
    import database as db
    inv.link_listing("100", title="Klapptisch Opel Zafira Life", price=250)
    _msg(db, ad_id=None, title="Klapptisch Opel Zafira Life", thread="manual-abc", status="skipped_sold",
         sold_price=200.0, sold_at="2026-10-01T12:00:00")
    _msg(db, ad_id=None, title="Что-то неизвестное", thread="manual-xyz", status="skipped_sold",
         sold_price=50.0, sold_at="2026-10-01T12:00:00")
    st = inv.sync_sales()
    assert st["recorded"] == 1 and [u["thread_id"] for u in st["unmatched"]] == ["manual-xyz"]
    assert inv.inv.sale_for_thread("manual-abc")["source"] == "manual"


# ─── правила для LLM / почты ──────────────────────────────────────────────────

def test_effective_state_and_prompt_note(inv):
    old = (datetime.utcnow() - timedelta(days=3)).isoformat()
    a = inv.link_listing("100", title="Gelöscht • Klapptisch Traveller", price=300, seen_at=old)
    # Склад ничего не знает: прежнее поведение, но «sold» объявления ≠ «продано»
    assert inv.effective_ad_state("100", "deleted") == "deleted"
    assert inv.effective_ad_state("100", "sold") == "deleted"
    assert inv.prompt_note("100") == ""
    # Перепостили — Gelöscht старого объявления больше не значит «нет»
    inv.link_listing("101", title="Klapptisch Traveller", price=300)
    assert inv.ad_context("100")["has_other_active_listing"] is True
    assert inv.effective_ad_state("100", "deleted") == ""
    assert "не говори" in inv.prompt_note("100").lower()
    # Остаток внесён > 0
    inv.set_stock(a["product_id"], 2)
    ctx = inv.ad_context("100")
    assert ctx["stock"] == "in_stock" and ctx["qty"] == 2
    assert inv.effective_ad_state("100", "sold") == ""
    assert "2 шт" in inv.prompt_note("100")
    # Остаток 0 при ведущемся учёте
    inv.set_stock(a["product_id"], 0)
    assert inv.ad_context("101")["stock"] == "out_of_stock"
    assert inv.effective_ad_state("101", "") == "out_of_stock"
    assert inv.ad_context("unknown-ad")["stock"] == "unknown"


def test_ad_context_safe_on_error(inv, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(inv.inv, "get_listing", boom)
    with caplog.at_level("ERROR"):
        out = inv.ad_context_safe("100")
    assert out["stock"] == "unknown" and out["known"] is False
    assert any(r.levelname == "ERROR" and "ad_context" in r.getMessage() for r in caplog.records)


# ─── отчёт ────────────────────────────────────────────────────────────────────

def test_report_fifo_profit_and_expectations(inv):
    now = datetime(2026, 10, 20, 12, 0)
    pid = inv.inv.create_product(title="Sitzbank", list_price_eur=1300.0, min_price_eur=1000.0)
    inv.receive(pid, 2, 500.0, occurred_at="2026-08-01T00:00:00")
    inv.receive(pid, 1, 800.0, occurred_at="2026-09-01T00:00:00")
    inv.record_sale(product_id=pid, price_eur=1200.0, gmail_thread_id="t1", occurred_at="2026-08-11T00:00:00")
    inv.record_sale(product_id=pid, price_eur=1100.0, gmail_thread_id="t2", occurred_at="2026-09-11T00:00:00")
    rep = inv.report("2026-08-01T00:00:00", "2026-11-01T00:00:00", now=now)
    it = rep["products"][0]
    assert it["qty"] == 1
    assert it["stock_cost_eur"] == 800.0                 # FIFO: осталась партия по 800
    assert it["expected_revenue_list"] == 1300.0 and it["expected_profit_list"] == 500.0
    assert it["expected_profit_min"] == 200.0
    assert it["sold_qty"] == 2 and it["revenue"] == 2300.0 and it["cogs"] == 1000.0 and it["profit"] == 1300.0
    assert it["avg_days_to_sell"] == pytest.approx((10 + 41) / 2, abs=0.01)
    assert it["oldest_unit_days"] == pytest.approx(49.5, abs=0.01)
    t = rep["totals"]
    assert t["profit"] == 1300.0 and t["qty"] == 1 and t["expected_profit_list"] == 500.0
    # Период без продаж
    assert inv.report("2026-10-01T00:00:00", None, now=now)["totals"]["sold_qty"] == 0
    assert "Склад: 1 шт." in inv.summary_text(rep)


def test_report_unknown_cost_is_none_not_zero(inv):
    pid = inv.inv.create_product(title="Tisch", list_price_eur=300.0)
    inv.set_stock(pid, 2)
    inv.record_sale(product_id=pid, price_eur=280.0, gmail_thread_id="t1")
    rep = inv.report()
    it = rep["products"][0]
    assert it["qty"] == 1 and it["units_without_cost"] == 1 and it["expected_profit_list"] is None
    assert it["sales_without_cost"] == 1 and it["profit"] is None
    assert rep["totals"]["expected_profit_list"] is None and rep["totals"]["profit"] is None
    assert "неизвестна" in inv.summary_text(rep)
    # оценка себестоимости закрывает дыру
    inv.inv.update_product(pid, unit_cost_eur=100.0)
    it = inv.report()["products"][0]
    assert it["expected_profit_list"] == 200.0 and it["profit"] == 180.0


def test_report_stale_and_negative(inv):
    now = datetime(2026, 10, 20)
    pid = inv.inv.create_product(title="AHK")
    inv.receive(pid, 1, 50.0, occurred_at="2026-06-01T00:00:00")
    neg = inv.inv.create_product(title="Gurte")
    inv.receive(neg, 1, 10.0, occurred_at="2026-10-01T00:00:00")
    inv.record_sale(product_id=neg, price_eur=20.0, gmail_thread_id="a")
    inv.record_sale(product_id=neg, price_eur=20.0, gmail_thread_id="b")
    t = inv.report(now=now)["totals"]
    assert t["stale"] == 1 and t["negative_stock"] == 1


# ─── начальное заполнение ─────────────────────────────────────────────────────

def test_plan_and_apply_seed_idempotent(inv):
    import database as db
    _msg(db, ad_id="1", title="Peugeot Traveller Sitzbank", price="1.299 €", created_at="2026-05-01T00:00:00")
    _msg(db, ad_id="2", title="Peugeot Traveller  Sitzbank", price="1.300 €", created_at="2026-06-01T00:00:00")
    _msg(db, ad_id="3", title="Citroen Spacetourer Sitzbank", price="1.299 €", created_at="2026-07-01T00:00:00")
    _msg(db, ad_id="4", title="Deine wöchentliche Anzeigenübersicht", created_at="2026-07-01T00:00:00")
    ads = inv.seed_ads_from_messages()
    assert {a.ad_id for a in ads} == {"1", "2", "3"}
    plan = inv.plan_seed(ads)
    assert len(plan["groups"]) == 2 and len(plan["doubtful"]) == 1
    st = inv.apply_seed(plan)
    assert (st["products"], st["listings"], st["questions"], st["skipped_known"]) == (2, 3, 1, 0)
    assert inv.inv.get_listing("1")["first_seen_at"].startswith("2026-05-01")
    st2 = inv.apply_seed(inv.plan_seed(inv.seed_ads_from_messages()))
    assert st2["products"] == 0 and st2["skipped_known"] == 3


def test_merge_products_moves_everything(inv):
    a = inv.inv.create_product(title="A", unit_cost_eur=None)
    b = inv.inv.create_product(title="B", unit_cost_eur=70.0)
    inv.inv.upsert_listing(ad_id="x", product_id=b, link_method="new")
    inv.receive(b, 2)
    out = inv.inv.merge_products(b, a)
    assert out == {"listings": 1, "movements": 1}
    pa = inv.inv.get_product(a)
    assert pa["unit_cost_eur"] == 70.0 and pa["stock_tracked"] == 1 and inv.inv.stock_qty(a) == 2
    assert inv.ad_context("x")["product_id"] == a
    with pytest.raises(ValueError):
        inv.inv.merge_products(a, b)   # b в архиве


# ─── после ревью: учёт с начала, FIFO, перенос продаж, синк ───────────────────

def test_history_sales_do_not_drive_first_receipt_negative(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    pid = r["product_id"]
    for i in range(3):
        inv.record_sale(product_id=pid, price_eur=250, gmail_thread_id=f"old{i}",
                        occurred_at=f"2026-0{i + 1}-10T12:00:00", source="thread")
    inv.receive(pid, 2, 100.0)
    ctx = inv.ad_context("100")
    assert ctx["stock"] == "in_stock" and ctx["qty"] == 2
    inv.record_sale(product_id=pid, price_eur=300, gmail_thread_id="new")
    assert inv.ad_context("100")["qty"] == 1


def test_backdated_receipt_counts_later_sales(inv):
    pid = inv.inv.create_product(title="AHK")
    inv.record_sale(product_id=pid, price_eur=200, gmail_thread_id="a", occurred_at="2026-08-01T00:00:00")
    inv.receive(pid, 5, 100.0, occurred_at="2026-07-01T00:00:00")   # купили в июле — продажа в августе из неё
    assert inv.inv.stock_qty(pid) == 4


def test_fifo_cost_after_count_with_prior_sales(inv):
    pid = inv.inv.create_product(title="Sitzbank", unit_cost_eur=100.0, list_price_eur=300.0)
    for i in range(3):
        inv.record_sale(product_id=pid, price_eur=250, gmail_thread_id=f"s{i}",
                        occurred_at=f"2026-0{i + 1}-10T12:00:00")
    inv.set_stock(pid, 5)
    it = inv.report()["products"][0]
    assert it["qty"] == 5 and it["stock_cost_eur"] == 500.0 and it["expected_profit_list"] == 1000.0


def test_set_stock_zero_starts_tracking(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    inv.record_sale(product_id=r["product_id"], price_eur=250, gmail_thread_id="x",
                    occurred_at="2026-01-01T00:00:00")
    assert inv.set_stock(r["product_id"], 0) is not None
    assert inv.ad_context("100")["stock"] == "out_of_stock"


def test_negative_stock_is_unknown_not_out_of_stock(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    inv.receive(r["product_id"], 1, 10.0, occurred_at="2026-01-01T00:00:00")
    inv.record_sale(product_id=r["product_id"], price_eur=20, gmail_thread_id="a")
    inv.record_sale(product_id=r["product_id"], price_eur=20, gmail_thread_id="b")
    ctx = inv.ad_context("100")
    assert ctx["stock"] == "unknown" and ctx["negative_stock"] is True and ctx["qty"] == -1
    assert inv.prompt_note("100") == ""


def test_safe_error_makes_no_claims(inv, monkeypatch):
    inv.link_listing("100", title="Gelöscht • Klapptisch Traveller", price=300)
    monkeypatch.setattr(inv.inv, "get_listing", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert inv.ad_context_safe("100")["error"] is True
    assert inv.effective_ad_state("100", "deleted") == "" and inv.prompt_note("100") == ""


def test_reserved_kept_when_last_unit(inv):
    r = inv.link_listing("100", title="Klapptisch Traveller", price=300)
    inv.set_stock(r["product_id"], 1)
    assert inv.effective_ad_state("100", "reserved") == "reserved"
    inv.set_stock(r["product_id"], 3)
    assert inv.effective_ad_state("100", "reserved") == ""


def test_answer_same_moves_sales_of_that_ad(inv):
    a = inv.link_listing("100", title="Peugeot Traveller Sitzbank", price=1299)
    b = inv.link_listing("200", title="Citroen Spacetourer Sitzbank", price=1299)
    inv.record_sale(ad_id="200", price_eur=1200, gmail_thread_id="t1", source="thread")
    res = inv.answer_question(b["question_id"], same=True)
    assert res["merged"] is True
    assert inv.inv.sale_for_thread("t1")["product_id"] == a["product_id"]


def test_sync_reprices_and_isolates_errors(inv, monkeypatch):
    import database as db
    _msg(db, ad_id="101", title="Klapptisch Opel Zafira Life", price="250 €", thread="t2",
         status="skipped_sold", sold_price=230.0, sold_at="2026-10-01T12:00:00")
    inv.sync_all()
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET sold_price_eur = 240 WHERE gmail_thread_id='t2'")
    st = inv.sync_sales()
    assert st["repriced"] == 1 and inv.inv.sale_for_thread("t2")["unit_price_eur"] == 240.0
    # sync_listings падает целиком — продажи всё равно синхронизируются
    monkeypatch.setattr(inv, "sync_listings", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = inv.sync_all()
    assert out["listings"]["errors"] == 1 and out["sales"]["already"] == 1


def test_sync_listings_one_bad_ad_does_not_stop_others(inv, monkeypatch):
    import database as db
    _msg(db, ad_id="1", title="Klapptisch Traveller", price="300 €")
    _msg(db, ad_id="2", title="Anhängerkupplung Traveller", price="200 €")
    real = inv.link_listing

    def flaky(ad_id, **kw):
        if ad_id == "1":
            raise RuntimeError("bad")
        return real(ad_id, **kw)
    monkeypatch.setattr(inv, "link_listing", flaky)
    st = inv.sync_listings()
    assert st["errors"] == 1 and st["new"] == 1


def test_void_logged_as_warning(inv, caplog):
    import database as db
    _msg(db, ad_id="101", title="Klapptisch Opel Zafira Life", price="250 €", thread="t2",
         status="skipped_sold", sold_price=230.0, sold_at="2026-10-01T12:00:00")
    inv.sync_all()
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET status='pending' WHERE gmail_thread_id='t2'")
    with caplog.at_level("WARNING"):
        assert inv.sync_sales()["voided"] == 1
    assert any(r.levelname == "WARNING" and "отменена" in r.getMessage() for r in caplog.records)


def test_apply_seed_joins_existing_product(inv):
    import database as db
    first = inv.link_listing("1", title="Peugeot Traveller Sitzbank", price=1299)
    _msg(db, ad_id="1", title="Peugeot Traveller Sitzbank", price="1.299 €", created_at="2026-05-01T00:00:00")
    _msg(db, ad_id="2", title="Peugeot Traveller  Sitzbank", price="1.300 €", created_at="2026-06-01T00:00:00")
    st = inv.apply_seed(inv.plan_seed(inv.seed_ads_from_messages()))
    assert st["products"] == 0 and st["joined_existing"] == 1
    assert inv.inv.get_listing("2")["product_id"] == first["product_id"]


# ─── отчёт за месяц, профиль, подсказка цены ─────────────────────────────────

def test_monthly_report_lots_payback(inv):
    pid = inv.inv.create_product(title="Sitzbank", list_price_eur=1300.0, min_price_eur=1000.0)
    inv.receive(pid, 2, 500.0, occurred_at="2026-09-05T00:00:00")
    inv.receive(pid, 2, 600.0, occurred_at="2026-10-02T00:00:00")
    inv.record_sale(product_id=pid, price_eur=1200.0, gmail_thread_id="a", occurred_at="2026-09-20T00:00:00")
    inv.record_sale(product_id=pid, price_eur=None, gmail_thread_id="b", occurred_at="2026-10-05T00:00:00")
    inv.record_sale(product_id=pid, price_eur=1100.0, gmail_thread_id="c", occurred_at="2026-10-10T00:00:00")
    sep = inv.monthly_report(2026, 9)
    assert sep["invested"] == 1000.0 and sep["revenue"] == 1200.0 and sep["cogs"] == 500.0
    assert sep["profit"] == 700.0 and sep["stock_end"]["qty"] == 1 and sep["stock_end"]["cost"] == 500.0
    assert sep["stock_end"]["list_value"] == 1300.0
    lot = sep["lots"][0]
    assert lot["payback"] == "paid" and lot["payback_pct"] == 120.0 and lot["left"] == 1
    octo = inv.monthly_report(2026, 10)
    assert octo["invested"] == 1200.0 and octo["sales"] == 2 and octo["sales_without_price"] == 1
    assert octo["revenue"] == 1100.0 and octo["cogs"] == 1100.0      # 500 (сент. партия) + 600
    assert octo["profit"] == 500.0                                     # только продажа с ценой: 1100 − 600
    assert [l["payback"] for l in octo["lots"]] == ["unknown", "not_yet"]
    assert octo["stock_end"]["qty"] == 1 and octo["stock_end"]["cost"] == 600.0
    with pytest.raises(ValueError):
        inv.monthly_report(2026, 13)


def test_product_profile_contract(inv):
    import database as db
    a = inv.link_listing("100", title="Peugeot Traveller Expert, Opel Zafira Life Leder Sitzbank", price=1299)
    inv.link_listing("101", title="Peugeot Traveller Expert,  Opel Zafira Life Leder Sitzbank", price=1350)
    _msg(db, ad_id="101", title="x", desc="Sehr guter Zustand")
    inv.inv.update_product(a["product_id"], unit_cost_eur=400.0, min_price_eur=1100.0)
    inv.receive(a["product_id"], 2, 500.0)
    prof = inv.product_profile(a["product_id"])
    assert prof["version"] == 1 and prof["category"] == "sitzbank"
    assert prof["unit_cost_eur"] == 500.0 and prof["unit_cost_source"] == "lots"
    assert prof["stock"] == {"tracked": True, "qty": 2}
    assert {l["ad_id"] for l in prof["listings"]} == {"100", "101"}
    assert "traveller" in prof["models"] and "sitzbank" in prof["core_tokens"] and "traveller" not in prof["core_tokens"]
    assert prof["features"]["leder"] is True and prof["description"] == "Sehr guter Zustand"
    assert prof["list_price_eur"] == 1350.0 and prof["min_price_eur"] == 1100.0
    assert inv.product_profile(9999) is None
    assert [p["product_id"] for p in inv.list_product_profiles()] == [a["product_id"]]


def test_suggest_sale_price(inv):
    import database as db
    import json
    _msg(db, ad_id="1", title="Klapptisch", price="400 €", thread="t1", created_at="2026-10-01T00:00:00")
    assert inv.suggest_sale_price("t1")["source"] == "ad_price"
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET de_client = 'Würden Sie 320 € nehmen?' WHERE gmail_thread_id='t1'")
    assert inv.suggest_sale_price("t1")["price_eur"] == 320.0
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET status='sent', de_answer='Für 350 Euro gerne.' WHERE gmail_thread_id='t1'")
    assert inv.suggest_sale_price("t1")["source"] == "our_text"
    with db.get_conn() as conn:
        conn.execute("UPDATE messages SET deal_brief_json = ? WHERE gmail_thread_id='t1'",
                     (json.dumps({"negotiated_price_eur": 340}),))
    out = inv.suggest_sale_price("t1")
    assert (out["price_eur"], out["source"]) == (340.0, "deal_brief")
    assert [c["source"] for c in out["candidates"]] == ["deal_brief", "our_text", "buyer_text", "ad_price"]
    assert inv.suggest_sale_price("nope") == {"price_eur": None, "source": None, "candidates": []}
