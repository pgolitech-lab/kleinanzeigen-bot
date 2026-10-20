"""Рынок в MA: тонкие обёртки над market_advisor — проброс параметров, not_found → 404."""
from __future__ import annotations
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN

H = lambda: {"X-Telegram-Init-Data": make_init_data({"id": 999, "first_name": "Pg"})}  # noqa: E731


@pytest.fixture
def c():
    with patch("modules.tg_init_data.config") as mc:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        yield TestClient(app)


def test_market_endpoints(c):
    with patch("web.api_market.market_advisor") as m:
        m.advise_price.return_value = {"ok": True, "status": "ok", "median": 100}
        assert c.get("/api/ma/market/advice/5", headers=H()).json()["median"] == 100
        m.advise_price.assert_called_once_with(product_id=5)
        m.duplicates_report.return_value = {"groups": [], "danger": 0}
        assert c.get("/api/ma/market/duplicates", headers=H()).json()["danger"] == 0
        m.repost_suggestions.return_value = [{"item_id": "x"}]
        assert c.get("/api/ma/market/repost?limit=3", headers=H()).json() == {"items": [{"item_id": "x"}], "count": 1}
        assert m.repost_suggestions.call_args.kwargs["limit"] == 3
        m.visibility_tips.return_value = {"ok": True, "missing_terms": []}
        assert c.get("/api/ma/market/tips/5?use_llm=false", headers=H()).status_code == 200
        assert m.visibility_tips.call_args.kwargs == {"product_id": 5, "use_llm": False}


def test_market_not_found_404(c):
    nf = {"ok": False, "status": "not_found", "item": None}
    tips_nf = {"ok": False, "warning": "товар не найден"}
    with patch("web.api_market.market_advisor") as m:
        m.advise_price.return_value = nf
        m.visibility_tips.return_value = tips_nf
        assert c.get("/api/ma/market/advice/9", headers=H()).status_code == 404
        assert c.get("/api/ma/market/tips/9", headers=H()).status_code == 404
    assert c.get("/api/ma/market/duplicates").status_code in (401, 403, 422)
