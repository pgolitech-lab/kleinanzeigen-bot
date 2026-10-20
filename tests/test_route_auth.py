"""Каждый маршрут, кроме явного allowlist, требует Telegram initData (verify_init_data_dep)."""
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from modules.tg_init_data import verify_init_data_dep
from web.app import app

# Без initData-зависимости: liveness, служебное (только 127.0.0.1) и beacon (initData в теле)
ALLOWLIST = {
    "/health",
    "/api/logs",
    "/api/status",
    "/debug/run-job/{job_id}",
    "/api/ma/messages/{msg_id}/lock/release-beacon",
}


def _deps(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _deps(d)


ROUTES = [r for r in app.routes if isinstance(r, APIRoute)]


def test_allowlist_is_current():
    paths = {r.path for r in ROUTES}
    assert ALLOWLIST <= paths, f"в allowlist лишнее: {ALLOWLIST - paths}"


@pytest.mark.parametrize("route", [r for r in ROUTES if r.path not in ALLOWLIST],
                         ids=lambda r: f"{sorted(r.methods)[0]} {r.path}")
def test_route_requires_init_data(route):
    assert verify_init_data_dep in set(_deps(route.dependant)), f"{route.path} без авторизации"
    # и на деле: без заголовка через туннель — 401/422, а не 200/500
    path = re.sub(r"\{[^}]+\}", "1", route.path)
    method = sorted(route.methods)[0]
    r = TestClient(app).request(method, path, headers={"cf-ray": "x"})
    assert r.status_code in (401, 422), f"{method} {path} → {r.status_code}"


def test_beacon_rejects_garbage_body():
    from unittest.mock import patch
    from tests.conftest import TEST_BOT_TOKEN
    with patch("modules.tg_init_data.config") as mc:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        r = TestClient(app).post("/api/ma/messages/1/lock/release-beacon", content="hash=x",
                                 headers={"cf-ray": "x"})
    assert r.status_code == 401


def test_scout_verify_not_started_twice():
    """Повторный POST /scout/verify, пока проверка идёт, второй поток не запускает."""
    import threading
    from unittest.mock import patch
    from web import api_ma
    gate = threading.Event()
    calls = []

    def slow_verify():
        calls.append(1)
        gate.wait(5)

    with patch("modules.scout.verify_listings", slow_verify):
        assert api_ma.ma_scout_verify(user={})["started"] is True
        assert api_ma.ma_scout_verify(user={})["started"] is False
        gate.set()
    for _ in range(50):
        if not api_ma._scout_verify_lock.locked():
            break
        threading.Event().wait(0.05)
    assert calls == [1] and not api_ma._scout_verify_lock.locked()
