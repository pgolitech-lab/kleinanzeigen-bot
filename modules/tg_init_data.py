"""Telegram WebApp initData HMAC validator + FastAPI dependency.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Используется в /api/ma/* endpoint'ах через `Depends(verify_init_data_dep)`.
Прямая функция `verify_init_data` — для unit-тестов.
"""
from __future__ import annotations
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qs

from fastapi import Header, HTTPException

import config


AUTH_DATE_MAX_AGE_SEC = 3600  # 1 час


def verify_init_data(raw: str) -> dict:
    """Валидирует initData строку, возвращает user dict.

    Raises HTTPException:
      - 401 если hash не совпал, отсутствует user, или auth_date устарел
      - 403 если user.id не в config.telegram_authorized_ids()
    """
    parsed = parse_qs(raw, strict_parsing=False, keep_blank_values=True)
    if "hash" not in parsed or "user" not in parsed or "auth_date" not in parsed:
        raise HTTPException(401, "init_data malformed: missing required field")

    received_hash = parsed.pop("hash")[0]
    try:
        auth_date = int(parsed["auth_date"][0])
    except (ValueError, IndexError):
        raise HTTPException(401, "init_data: bad auth_date")

    now = time.time()
    # Future auth_date с small clock-skew tolerance считаем валидным;
    # past — только в пределах AUTH_DATE_MAX_AGE_SEC.
    if auth_date > now + 60 or now - auth_date > AUTH_DATE_MAX_AGE_SEC:
        raise HTTPException(401, "init_data expired or future-dated")

    # data-check-string: k=v\nk=v... отсортированных по ключу.
    data_check = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))

    bot_token = config.telegram_bot_token()
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    # Bug fix: non-ASCII hash crashes compare_digest with TypeError → guard first.
    if not received_hash.isascii():
        raise HTTPException(401, "init_data: hash mismatch")
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "init_data: hash mismatch")

    try:
        user = json.loads(parsed["user"][0])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(401, "init_data: cannot parse user")

    # Bug fix: user field must be a JSON object, not a scalar/array.
    if not isinstance(user, dict):
        raise HTTPException(401, "init_data: user field is not an object")

    # Bug fix: telegram_authorized_ids() returns set[str], user["id"] is int — stringify.
    if str(user.get("id")) not in config.telegram_authorized_ids():
        raise HTTPException(403, "user not authorized")

    return user


async def verify_init_data_dep(
    x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data"),
) -> dict:
    """FastAPI dependency. Возвращает user dict."""
    return verify_init_data(x_telegram_init_data)
