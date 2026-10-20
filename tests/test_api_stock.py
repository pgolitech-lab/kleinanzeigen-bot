"""Склад в MA: товары, приход/списание/инвентаризация, ошибки → 400, подсказка цены, отчёты."""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import database
from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}
H = lambda: {"X-Telegram-Init-Data": make_init_data(TEST_USER)}  # noqa: E731


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    with patch("modules.tg_init_data.config") as mc:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        yield TestClient(app)


def test_product_lifecycle(c):
    r = c.post("/api/ma/stock/products", json={"title": "Sitzbank 2+1", "list_price_eur": 450}, headers=H())
    assert r.status_code == 200
    pid = r.json()["id"]
    assert c.post(f"/api/ma/stock/products/{pid}/receipt", json={"qty": 3, "unit_cost_eur": 100},
                  headers=H()).json()["qty"] == 3
    assert c.post(f"/api/ma/stock/products/{pid}/writeoff", json={"qty": 1}, headers=H()).json()["qty"] == 2
    assert c.post(f"/api/ma/stock/products/{pid}/count", json={"qty": 5}, headers=H()).json()["qty"] == 5
    assert c.patch(f"/api/ma/stock/products/{pid}", json={"min_price_eur": 380},
                   headers=H()).json()["product"]["min_price_eur"] == 380
    card = c.get(f"/api/ma/stock/products/{pid}", headers=H()).json()
    assert card["product"]["title"] == "Sitzbank 2+1" and len(card["movements"]) == 3
    items = c.get("/api/ma/stock/products", headers=H()).json()["items"]
    assert [i["id"] for i in items] == [pid]
    assert c.post(f"/api/ma/stock/products/{pid}/archive", json={}, headers=H()).status_code == 200
    assert c.get("/api/ma/stock/products", headers=H()).json()["count"] == 0


def test_errors_are_400_and_404(c):
    assert c.post("/api/ma/stock/products", json={"title": " "}, headers=H()).status_code == 400
    assert c.post("/api/ma/stock/products/999/receipt", json={"qty": 1}, headers=H()).status_code in (400, 404)
    pid = c.post("/api/ma/stock/products", json={"title": "X"}, headers=H()).json()["id"]
    assert c.post(f"/api/ma/stock/products/{pid}/receipt", json={"qty": 0}, headers=H()).status_code == 400
    assert c.post("/api/ma/stock/merge", json={"src_id": pid, "dst_id": pid}, headers=H()).status_code == 400
    assert c.get("/api/ma/stock/products/12345", headers=H()).status_code == 404
    assert c.get("/api/ma/stock/month?year=2026&month=13", headers=H()).status_code == 400


def test_reports_questions_sale_price(c):
    assert "totals" in c.get("/api/ma/stock/report", headers=H()).json()
    assert c.get("/api/ma/stock/month?year=2026&month=10", headers=H()).status_code == 200
    assert c.get("/api/ma/stock/questions", headers=H()).json()["count"] == 0
    assert c.get("/api/ma/stock/unlinked-sales", headers=H()).status_code == 200
    r = c.get("/api/ma/threads/T1/sale-price", headers=H()).json()
    assert "price_eur" in r and "candidates" in r


def test_patch_allowed_by_cors(c):
    r = c.options("/api/ma/stock/products/1", headers={
        "Origin": "https://pgolitech-lab.github.io", "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data, Content-Type"})
    assert r.status_code == 200


def test_report_from_param_and_patch_title(c):
    with patch("modules.inventory.report", return_value={"totals": {}}) as rep:
        c.get("/api/ma/stock/report?from=2026-10-01&to=2026-10-20", headers=H())
    assert rep.call_args.args == ("2026-10-01", "2026-10-20")
    pid = c.post("/api/ma/stock/products", json={"title": "Alt"}, headers=H()).json()["id"]
    assert c.patch(f"/api/ma/stock/products/{pid}", json={"title": None}, headers=H()).status_code == 400
    assert c.patch(f"/api/ma/stock/products/{pid}", json={"title": "  "}, headers=H()).status_code == 400
    from modules.inventory import match_key
    p = c.patch(f"/api/ma/stock/products/{pid}", json={"title": "Neu Sitzbank"}, headers=H()).json()["product"]
    assert p["title"] == "Neu Sitzbank" and p["match_key"] == match_key("Neu Sitzbank")


def _mk(c, title, **kw):
    return c.post("/api/ma/stock/products", json={"title": title, **kw}, headers=H()).json()["id"]


def test_types_category_costs_kit_batch_nonstock(c):
    a, b = _mk(c, "Sitzbank 2+1", list_price_eur=300), _mk(c, "Fahrersitz", list_price_eur=100)
    kit = _mk(c, "Komplett")
    t = c.get("/api/ma/stock/types", headers=H()).json()
    assert "questions" in t and t["table"]
    cats = c.put("/api/ma/stock/category-costs", json={"category": "fahrersitze", "unit_cost_eur": 40},
                 headers=H()).json()["table"]
    assert any(x["category"] == "fahrersitze" and x["standard_cost_eur"] == 40 for x in cats)
    assert c.post("/api/ma/stock/types", json={"product_id": 9999, "category": "x"}, headers=H()).status_code == 404
    assert c.post("/api/ma/stock/types", json={"product_id": b, "category": "nonsense-zzz"},
                  headers=H()).status_code == 400
    assert c.post("/api/ma/stock/types", json={"product_id": b, "category": "fahrersitze"},
                  headers=H()).json()["category"] == "fahrersitze"
    r = c.put(f"/api/ma/stock/products/{kit}/kit", json={"components": [{"product_id": a, "qty": 1},
                                                                         {"product_id": b, "qty": 2}]}, headers=H())
    assert r.status_code == 200 and r.json()["stock"]["kit"] is True
    assert c.put(f"/api/ma/stock/products/{kit}/kit", json={"components": [{"product_id": 9999, "qty": 1}]},
                 headers=H()).status_code == 404
    r = c.post("/api/ma/stock/batches", json={"items": [{"product_id": a, "qty": 1}, {"product_id": b, "qty": 2}],
                                              "total_cost_eur": 200}, headers=H())
    assert r.status_code == 200 and len(r.json()["movements"]) == 2
    assert sum(m["unit_cost_eur"] * m["qty"] for m in r.json()["movements"]) == pytest.approx(200, abs=0.05)
    assert c.post("/api/ma/stock/batches", json={"items": [{"product_id": a, "qty": 1}, {"product_id": a, "qty": 1}]},
                  headers=H()).status_code == 400
    assert c.post(f"/api/ma/stock/products/{a}/non-stock", json={}, headers=H()).json()["product"]["non_stock"] == 1
    assert a not in [i["id"] for i in c.get("/api/ma/stock/products", headers=H()).json()["items"]]


def test_receipt_parse_matches_known_products_only(c):
    a = _mk(c, "Fahrersitz Passat")
    out = ({"items": [{"product_id": a, "title": "сиденье", "qty": 2}, {"product_id": 777, "title": "фаркоп", "qty": 1},
                      {"product_id": None, "title": "мусор", "qty": 0}], "total_cost_eur": 150}, {})
    with patch("modules.claude_utils.call_json", return_value=out):
        r = c.post("/api/ma/stock/receipt/parse", json={"text": "сиденья 2, фаркоп, всего 150"}, headers=H())
    j = r.json()
    assert r.status_code == 200 and j["total_cost_eur"] == 150 and j["unmatched"] == 1
    assert [i["product_id"] for i in j["items"]] == [a, None]  # чужой id отброшен, qty=0 пропущена
    from modules import llm
    with patch("modules.claude_utils.call_json", side_effect=llm.LLMLimitError("лимит")):
        assert c.post("/api/ma/stock/receipt/parse", json={"text": "x"}, headers=H()).status_code == 503
    with patch("modules.claude_utils.call_json", return_value=({"items": [], "total_cost_eur": None}, {})):
        assert c.post("/api/ma/stock/receipt/parse", json={"text": "x"}, headers=H()).status_code == 422


def test_kit_get_and_hidden_list(c):
    a, kit = _mk(c, "Sitzbank"), _mk(c, "Komplett")
    assert c.get(f"/api/ma/stock/products/{kit}/kit", headers=H()).json()["components"] == []
    c.put(f"/api/ma/stock/products/{kit}/kit", json={"components": [{"product_id": a, "qty": 2}]}, headers=H())
    j = c.get(f"/api/ma/stock/products/{kit}/kit", headers=H()).json()
    assert j["components"] == [{"product_id": a, "title": "Sitzbank", "qty": 2}] and j["stock"]["kit"]
    assert c.get("/api/ma/stock/products/9999/kit", headers=H()).status_code == 404
    assert c.get("/api/ma/stock/hidden", headers=H()).json()["count"] == 0
    c.post(f"/api/ma/stock/products/{a}/non-stock", json={}, headers=H())
    assert [i["id"] for i in c.get("/api/ma/stock/hidden", headers=H()).json()["items"]] == [a]


def test_hard_run_ids_and_not_found(c):
    big = "99999999999999999999"
    assert c.get(f"/api/ma/stock/products/{big}", headers=H()).status_code in (404, 422)
    assert c.get(f"/api/ma/messages/{big}", headers=H()).status_code in (404, 422)
    assert c.post("/api/ma/stock/products/987654/receipt", json={"qty": 1}, headers=H()).status_code == 404
    assert c.post("/api/ma/stock/products/987654/archive", json={}, headers=H()).status_code == 404
    r = c.post("/api/ma/stock/products", content='{"title": "x", "list_price_eur": NaN}',
               headers={**H(), "Content-Type": "application/json"})
    assert r.status_code == 422
