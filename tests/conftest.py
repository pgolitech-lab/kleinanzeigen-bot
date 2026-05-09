"""Pytest config + общие фикстуры.

sys.path bootstrap чтобы тесты могли импортить modules/* и web/app.py
без устанавливаемого пакета.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))



# ---------------------------------------------------------------------------
# Shared initData helper
# ---------------------------------------------------------------------------
import hmac
import hashlib
import json
import time
from urllib.parse import urlencode

TEST_BOT_TOKEN = "123456:test_token_AbCdEfG"


def make_init_data(
    user: dict,
    auth_date=None,
    bot_token: str = TEST_BOT_TOKEN,
    bad_hash: bool = False,
) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    fields = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(auth_date)}
    data_check = '\n'.join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if bad_hash:
        h = "0" * 64
    fields["hash"] = h
    return urlencode(fields)
