# Через туннель cloudflared (заголовки cf-ray / cf-connecting-ip, хост *.trycloudflare.com) наружу видны
# только /api/ma/* и /health. Служебные /api/logs, /api/status, /debug/* — только с 127.0.0.1 без туннеля.
import pytest
from fastapi.testclient import TestClient

from web.app import app

remote = TestClient(app)  # client.host = "testclient" — как запрос из LAN
local = TestClient(app, client=("127.0.0.1", 50000))
TUNNEL = {"cf-ray": "8d1f-FRA", "cf-connecting-ip": "203.0.113.7"}


@pytest.fixture(autouse=True)
def _no_db_config():
    from unittest.mock import patch
    with patch("web.app.config") as mc:
        mc.polling_paused.return_value = False
        yield mc


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
    assert body["polling_paused"] is False
    assert "poll_age_sec" in body and "poll_tracking" in body and "polluted_incoming" in body


def test_health_uses_scheduler_poll_status(_no_db_config):
    import time
    from unittest.mock import patch
    import scheduler
    st = {"last_pass_sec": 1.5, "last_errors": 0, "accounts": {1: {"last_ok_at": "x", "fails_in_row": 0}},
          "queue": {"worker_alive": True, "busy_sec": 0.0, "queue_len": 2}}
    _no_db_config.polling_paused.return_value = True
    with patch.object(scheduler, "last_poll_ok_at", lambda: time.time() - 42, create=True), \
         patch.object(scheduler, "poll_status", lambda: st, create=True):
        body = local.get("/health").json()
    assert body["poll_tracking"] is True and 41 <= body["poll_age_sec"] <= 44
    assert body["polling_paused"] is True
    assert body["mail"]["worker_alive"] is True and body["mail"]["queue_len"] == 2
    assert body["mail"]["accounts"]["1"]["fails_in_row"] == 0
    # через туннель — без разбивки по аккаунтам
    with patch.object(scheduler, "last_poll_ok_at", lambda: time.time(), create=True), \
         patch.object(scheduler, "poll_status", lambda: st, create=True):
        pub = local.get("/health", headers=TUNNEL).json()
    assert pub["mail"] == {"worker_alive": True, "queue_len": 2}


def test_health_survives_diagnostics_failure(_no_db_config):
    _no_db_config.polling_paused.side_effect = RuntimeError("db locked")
    r = local.get("/health")
    assert r.status_code == 200 and r.json()["mail_status_error"] == "RuntimeError"
