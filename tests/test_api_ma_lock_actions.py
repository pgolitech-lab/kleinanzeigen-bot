"""TestClient тесты для POST /api/ma/messages/{id}/lock/{acquire,release}."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER_PG = {"id": 999, "first_name": "Pg", "username": "pgtest"}
TEST_USER_NO_NAME = {"id": 555, "first_name": "Sam"}


def _row(**fields):
    row = MagicMock()
    row.__getitem__.side_effect = fields.__getitem__
    row.keys.return_value = list(fields.keys())
    return row


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999", "555"}
        mdb.get_message.return_value = _row(id=123, gmail_thread_id="abc")
        mtb._check_lock.return_value = None  # default: lock free
        mtb._acquire_lock.return_value = None
        mtb._release_lock.return_value = None
        mol.remaining_min.return_value = 5
        from web.app import app
        yield TestClient(app), mtb, mol, mdb


def test_actor_format_with_username():
    from web.api_ma import actor_from_user
    assert actor_from_user(TEST_USER_PG) == "@pgtest#999"


def test_actor_format_without_username():
    from web.api_ma import actor_from_user
    assert actor_from_user(TEST_USER_NO_NAME) == "Sam#555"


def test_acquire_succeeds_when_free(client):
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = None
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["holder"] == "@pgtest#999"
    assert body["remaining_min"] == 5
    mtb._acquire_lock.assert_called_once_with(123, "@pgtest#999")


def test_acquire_409_when_held_by_other(client):
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = "@other#111"
    mol.remaining_min.return_value = 4
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["holder"] == "@other#111"
    assert body["detail"]["remaining_min"] == 4
    mtb._acquire_lock.assert_not_called()


def test_acquire_idempotent_when_already_self(client):
    """check_lock возвращает None если holder=self — мы re-acquire-аем (refresh ttl)."""
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = None
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mtb._acquire_lock.assert_called_once()


def test_acquire_404_when_msg_missing(client):
    c, mtb, mol, mdb = client
    mdb.get_message.return_value = None
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/999/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_release_succeeds(client):
    c, mtb, mol, mdb = client
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/release",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 204
    mtb._release_lock.assert_called_once_with(123)


def test_release_idempotent_no_holder(client):
    """Permissive — release не проверяет holder."""
    c, mtb, mol, mdb = client
    init = make_init_data(TEST_USER_NO_NAME)
    res = c.post("/api/ma/messages/123/lock/release",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 204


def test_lock_endpoints_require_auth(client):
    c, mtb, mol, mdb = client
    res = c.post("/api/ma/messages/123/lock/acquire")
    assert res.status_code == 422
    res = c.post("/api/ma/messages/123/lock/release")
    assert res.status_code == 422


def test_beacon_release_valid_initdata_in_body(client):
    """pagehide-beacon: initData в теле (не в заголовке) валидируется и снимает лок."""
    c, mtb, mol, mdb = client
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/release-beacon",
                 content=init, headers={"Content-Type": "text/plain"})
    assert res.status_code == 204
    mtb._release_lock.assert_called_once_with(123)


def test_beacon_release_rejects_garbage_body(client):
    """Битая/пустая initData в теле → 401, лок не трогаем (endpoint остаётся авторизован)."""
    c, mtb, mol, mdb = client
    res = c.post("/api/ma/messages/123/lock/release-beacon",
                 content="not-valid-init-data", headers={"Content-Type": "text/plain"})
    assert res.status_code == 401
    mtb._release_lock.assert_not_called()


def test_beacon_release_rejects_empty_body(client):
    c, mtb, mol, mdb = client
    res = c.post("/api/ma/messages/123/lock/release-beacon")
    assert res.status_code == 401
    mtb._release_lock.assert_not_called()
