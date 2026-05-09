"""Smoke-тесты для /api/ma/health через FastAPI TestClient (без живого uvicorn)."""
from __future__ import annotations
import hmac
import hashlib
import json
import time
from urllib.parse import urlencode
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


TEST_BOT_TOKEN = "123456:test_token_AbCdEfG"
TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _make_init_data(user: dict, auth_date: int | None = None, bot_token: str = TEST_BOT_TOKEN) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    fields = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(auth_date)}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    fields["hash"] = h
    return urlencode(fields)


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
    init = _make_init_data(TEST_USER)
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
    """Неизвестный origin не получает Allow-Origin header."""
    res = client.options(
        "/api/ma/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI/Starlette CORS middleware just doesn't set the header for unknown origin.
    # Status может быть 400 (CORS reject) или 200 без allow-origin header.
    assert res.headers.get("access-control-allow-origin") != "https://evil.example.com"
