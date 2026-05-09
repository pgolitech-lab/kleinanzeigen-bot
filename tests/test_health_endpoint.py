"""Smoke-тесты для /api/ma/health через FastAPI TestClient (без живого uvicorn)."""
from __future__ import annotations
import time
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    """TestClient над web.app с замоканным config."""
    mock_config = MagicMock()
    mock_config.telegram_bot_token.return_value = TEST_BOT_TOKEN
    mock_config.telegram_authorized_ids.return_value = {"999"}
    with patch("modules.tg_init_data.config", mock_config):
        from web.app import app
        yield TestClient(app)


def test_health_no_header_returns_422(client):
    """FastAPI ругается на missing header."""
    res = client.get("/api/ma/health")
    assert res.status_code == 422


def test_health_bad_init_data_returns_401(client):
    res = client.get(
        "/api/ma/health",
        headers={
            "X-Telegram-Init-Data": (
                "hash=fake&user=%7B%22id%22%3A1%7D&auth_date=" + str(int(time.time()))
            )
        },
    )
    assert res.status_code == 401


def test_health_valid_returns_user(client):
    init = make_init_data(TEST_USER)
    res = client.get("/api/ma/health", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["user_id"] == 999
    assert body["username"] == "pgtest"
    assert body["first_name"] == "Pg"


def test_cors_preflight_for_github_pages(client):
    """OPTIONS preflight от github.io должен пройти."""
    res = client.options(
        "/api/ma/health",
        headers={
            "Origin": "https://pgolitech-lab.github.io",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Telegram-Init-Data",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://pgolitech-lab.github.io"


def test_cors_rejects_unknown_origin(client):
    """Неизвестный origin не получает Allow-Origin header (Starlette returns 400)."""
    res = client.options(
        "/api/ma/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert res.status_code == 400
    assert res.headers.get("access-control-allow-origin") is None
