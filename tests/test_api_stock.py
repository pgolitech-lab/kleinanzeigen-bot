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
