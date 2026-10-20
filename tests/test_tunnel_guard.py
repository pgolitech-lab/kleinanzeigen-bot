# Через туннель cloudflared (заголовки cf-ray / cf-connecting-ip, хост *.trycloudflare.com) наружу видны
# только /api/ma/* и /health. Служебные /api/logs, /api/status, /debug/* — только с 127.0.0.1 без туннеля.
import pytest
from fastapi.testclient import TestClient

from web.app import app

remote = TestClient(app)  # client.host = "testclient" — как запрос из LAN
local = TestClient(app, client=("127.0.0.1", 50000))
TUNNEL = {"cf-ray": "8d1f-FRA", "cf-connecting-ip": "203.0.113.7"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/settings"), ("GET", "/api/settings"), ("POST", "/api/settings"),
    ("GET", "/api/logs"), ("GET", "/api/status"), ("GET", "/api/messages"), ("POST", "/accounts/1/delete"),
    ("POST", "/debug/run-job/poll_gmail"), ("POST", "/backup/run"),
    ("GET", "/docs"), ("GET", "/openapi.json"),
])
def test_tunnel_blocks_non_ma(method, path):
    # cloudflared ходит на 127.0.0.1 — защита не должна полагаться на адрес клиента
    assert local.request(method, path, headers=TUNNEL).status_code == 404


def test_tunnel_by_host_header_blocked():
    assert local.get("/api/logs", headers={"host": "abc-def.trycloudflare.com"}).status_code == 404


def test_tunnel_allows_health_and_ma():
    assert local.get("/health", headers=TUNNEL).status_code == 200
    # /api/ma/* доходит до роутера (без initData — 401/422), а не 404 от защиты туннеля
    assert local.get("/api/ma/pipeline", headers=TUNNEL).status_code in (401, 422)


def test_tunnel_allows_ma_preflight():
    r = local.options("/api/ma/pipeline", headers={
        **TUNNEL, "Origin": "https://pgolitech-lab.github.io",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Telegram-Init-Data"})
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/logs"), ("GET", "/api/status"), ("POST", "/debug/run-job/poll_gmail"),
])
def test_service_routes_only_from_localhost(method, path):
    assert remote.request(method, path).status_code == 404


def test_service_routes_work_locally():
    assert local.get("/api/logs").status_code == 200
    assert local.get("/api/status").json() == {"jobs": []}  # scheduler в тестах не запущен
    assert local.post("/debug/run-job/poll_gmail").status_code == 503


def test_old_web_ui_removed():
    for path in ("/", "/settings", "/threads", "/clients", "/scout", "/logs", "/accounts"):
        assert local.get(path).status_code == 404


def test_health_shape():
    body = local.get("/health").json()
    assert body["status"] == "ok"
    assert "poll_age_sec" in body and "polluted_incoming" in body
