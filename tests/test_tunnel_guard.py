# Через туннель cloudflared (заголовки cf-ray / cf-connecting-ip, хост *.trycloudflare.com) наружу видны
# только /api/ma/* и /health; локальные запросы — как раньше.
import pytest
from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)
TUNNEL = {"cf-ray": "8d1f-FRA", "cf-connecting-ip": "203.0.113.7"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/settings"), ("GET", "/api/settings"), ("POST", "/api/settings"),
    ("GET", "/api/logs"), ("GET", "/api/messages"), ("POST", "/accounts/1/delete"),
    ("POST", "/debug/poll-now"), ("GET", "/debug/send-approved-now"), ("POST", "/backup/run"),
    ("GET", "/docs"), ("GET", "/openapi.json"),
])
def test_tunnel_blocks_non_ma(method, path):
    r = client.request(method, path, headers=TUNNEL)
    assert r.status_code == 404


def test_tunnel_by_host_header_blocked():
    r = client.get("/api/settings", headers={"host": "abc-def.trycloudflare.com"})
    assert r.status_code == 404


def test_tunnel_allows_health_and_ma():
    assert client.get("/health", headers=TUNNEL).status_code == 200
    # /api/ma/* доходит до роутера (без initData — 401/422), а не 404 от защиты туннеля
    assert client.get("/api/ma/pipeline", headers=TUNNEL).status_code in (401, 422)


def test_tunnel_allows_ma_preflight():
    r = client.options("/api/ma/pipeline", headers={
        **TUNNEL, "Origin": "https://pgolitech-lab.github.io",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data"})
    assert r.status_code == 200


def test_local_requests_unchanged():
    assert client.get("/health").status_code == 200
