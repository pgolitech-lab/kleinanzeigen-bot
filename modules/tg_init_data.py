"""Telegram WebApp initData HMAC validator + FastAPI dependency.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Используется в /api/ma/* endpoint'ах через `Depends(verify_init_data_dep)`.
Прямая функция `verify_init_data` — для unit-тестов.
"""
from __future__ import annotations
import hmac
import hashlib
import json
import logging
import time
from urllib.parse import parse_qs

from fastapi import Header, HTTPException

import config


# initData подписывается Telegram один раз при открытии Mini App и не обновляется, пока окно
# открыто. При 1 ч оператор через час работы получал «сессия потеряна». 24 ч: initData ходит
# только по HTTPS в заголовке; украденная строка дала бы те же сутки, что и серверный токен сессии.
AUTH_DATE_MAX_AGE_SEC = 24 * 3600

logger = logging.getLogger(__name__)


def verify_init_data(raw: str) -> dict:
    """Валидирует initData строку, возвращает user dict.

    Raises HTTPException:
      - 503 если в настройках нет токена бота (проверить подпись нечем)
      - 401 если hash не совпал, отсутствует user, или auth_date устарел
      - 403 если user.id не в config.telegram_authorized_ids()
    """
    bot_token = config.telegram_bot_token()
    if not bot_token:
        logger.error("initData: не задан telegram_bot_token — Mini App недоступна")
        raise HTTPException(503, "bot token not configured")

    parsed = parse_qs(raw, strict_parsing=False, keep_blank_values=True)
    if "hash" not in parsed or "user" not in parsed or "auth_date" not in parsed:
        logger.warning("initData 401: нет обязательных полей, keys=%s", sorted(parsed.keys()))
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
        logger.warning("initData 401: auth_date вне окна, возраст %d с", int(now) - auth_date)
        raise HTTPException(401, "init_data expired or future-dated")

    # data-check-string: k=v\nk=v... отсортированных по ключу.
    data_check = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    # non-ASCII hash роняет compare_digest TypeError'ом → проверяем заранее.
    # Ни полученный, ни ожидаемый hash не логируем: ожидаемый — валидная подпись.
    if not received_hash.isascii() or not hmac.compare_digest(expected_hash, received_hash):
        logger.warning("initData 401: подпись не совпала, keys=%s", sorted(parsed.keys()))
        raise HTTPException(401, "init_data: hash mismatch")

    try:
        user = json.loads(parsed["user"][0])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(401, "init_data: cannot parse user")

    # user должен быть JSON-объектом, а не скаляром/массивом.
    if not isinstance(user, dict):
        raise HTTPException(401, "init_data: user field is not an object")

    # telegram_authorized_ids() возвращает set[str], user["id"] — int.
    if str(user.get("id")) not in config.telegram_authorized_ids():
        raise HTTPException(403, "user not authorized")

    return user


def verify_init_data_dep(
    x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data"),
) -> dict:
    """FastAPI dependency (sync — FastAPI зовёт её в threadpool: читает настройки из БД)."""
    return verify_init_data(x_telegram_init_data)
