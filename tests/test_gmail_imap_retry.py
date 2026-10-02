"""Тесты для gmail._imap_retry — ретрай транзиентных IMAP-ошибок Gmail."""
from __future__ import annotations

import imaplib
from unittest.mock import patch

import pytest

from modules import gmail


def test_retries_lookup_failed_then_succeeds():
    """«Lookup failed» при login — транзиент Gmail, ретраим и успешно отдаём."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise imaplib.IMAP4.error(b"Lookup failed 5b1f17b1804b1-abc")
        return "ok"

    with patch.object(gmail._time, "sleep"):
        assert gmail._imap_retry(fn, retries=1) == "ok"
    assert calls["n"] == 2


def test_auth_error_not_retried():
    """Auth-отказ («Invalid credentials») — НЕ транзиент, падает сразу."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    with patch.object(gmail._time, "sleep"):
        with pytest.raises(imaplib.IMAP4.error):
            gmail._imap_retry(fn, retries=1)
    # Один вызов, без ретрая
    assert calls["n"] == 1


def test_timeout_retried():
    """TimeoutError — транзиент, ретраим."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return "ok"

    with patch.object(gmail._time, "sleep"):
        assert gmail._imap_retry(fn, retries=1) == "ok"
    assert calls["n"] == 2


def test_abort_retried():
    """imaplib.IMAP4.abort (EOF) — транзиент, ретраим."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return "ok"

    with patch.object(gmail._time, "sleep"):
        assert gmail._imap_retry(fn, retries=1) == "ok"
    assert calls["n"] == 2


def test_transient_exhausts_retries_and_raises():
    """Если транзиент не прошёл за все попытки — пробрасываем ошибку."""
    def fn():
        raise imaplib.IMAP4.error(b"Lookup failed xyz")

    with patch.object(gmail._time, "sleep"):
        with pytest.raises(imaplib.IMAP4.error):
            gmail._imap_retry(fn, retries=1)
