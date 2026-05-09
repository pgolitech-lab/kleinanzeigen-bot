"""Тесты HMAC-валидатора Telegram initData."""
from __future__ import annotations
import json
import time
from urllib.parse import urlencode
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from modules import tg_init_data
from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER_AUTHORIZED = {"id": 999, "first_name": "Pg", "username": "pgtest"}
TEST_USER_FOREIGN = {"id": 666, "first_name": "Stranger"}


@pytest.fixture
def patched_config():
    """Подменяет config.telegram_bot_token и telegram_authorized_ids."""
    with patch("modules.tg_init_data.config") as m:
        m.telegram_bot_token.return_value = TEST_BOT_TOKEN
        m.telegram_authorized_ids.return_value = {"999"}  # str — match production
        yield m


def test_valid_init_data_returns_user(patched_config):
    init = make_init_data(TEST_USER_AUTHORIZED)
    user = tg_init_data.verify_init_data(init)
    assert user["id"] == 999
    assert user["username"] == "pgtest"


def test_bad_hash_raises_401(patched_config):
    init = make_init_data(TEST_USER_AUTHORIZED, bad_hash=True)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
    assert "hash" in ei.value.detail.lower()


def test_expired_auth_date_raises_401(patched_config):
    expired = int(time.time()) - 3700  # >1 hour
    init = make_init_data(TEST_USER_AUTHORIZED, auth_date=expired)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
    assert "expired" in ei.value.detail.lower()


def test_unauthorized_user_raises_403(patched_config):
    init = make_init_data(TEST_USER_FOREIGN)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 403
    assert "auth" in ei.value.detail.lower()


def test_missing_user_field_raises_401(patched_config):
    """Гарантируем что отсутствие user в parsed данных не падает на KeyError."""
    fields = {"auth_date": str(int(time.time())), "hash": "0" * 64}
    init = urlencode(fields)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401


def test_token_change_invalidates_old_data(patched_config):
    """Если бот-токен переустановили — старые initData невалидны."""
    init = make_init_data(TEST_USER_AUTHORIZED, bot_token="OLD_TOKEN_XYZ")
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401


def test_future_auth_date_raises_401(patched_config):
    """Future auth_date (отрицательная дельта) тоже должна отбиваться."""
    future = int(time.time()) + 9999
    init = make_init_data(TEST_USER_AUTHORIZED, auth_date=future)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
    assert "expired" in ei.value.detail.lower() or "future" in ei.value.detail.lower()


def test_non_ascii_hash_raises_401(patched_config):
    """Non-ASCII в hash field не должен крашить compare_digest до 500."""
    fields = {
        "user": json.dumps(TEST_USER_AUTHORIZED, separators=(",", ":")),
        "auth_date": str(int(time.time())),
        "hash": "☃" * 64,  # 64-char non-ascii string
    }
    init = urlencode(fields)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401


def test_user_field_non_dict_json_raises_401(patched_config):
    """user=42 (валидный JSON но не object) → 401, не 500."""
    import hmac as _hmac
    import hashlib as _hashlib
    fields = {
        "user": "42",  # valid JSON, not a dict
        "auth_date": str(int(time.time())),
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = _hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), _hashlib.sha256).digest()
    h = _hmac.new(secret, data_check.encode(), _hashlib.sha256).hexdigest()
    fields["hash"] = h
    init = urlencode(fields)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401


def test_str_authorized_ids_match_int_user_id(patched_config):
    """Production: telegram_authorized_ids() returns set[str], user.id is int — должны совпадать через str()."""
    # Mock уже patched — но патчим заново чтобы убедиться в типе set[str]:
    patched_config.telegram_authorized_ids.return_value = {"999"}  # str-set, реальное поведение
    init = make_init_data(TEST_USER_AUTHORIZED)  # id=999 (int)
    user = tg_init_data.verify_init_data(init)
    assert user["id"] == 999
