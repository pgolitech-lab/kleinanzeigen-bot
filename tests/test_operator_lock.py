"""Тесты примитивов operator_lock module."""
from __future__ import annotations
from unittest.mock import patch

import pytest

from modules import operator_lock


@pytest.fixture(autouse=True)
def _reset_locks():
    """Каждый тест начинается с пустого dict."""
    operator_lock._LOCKS.clear()
    yield
    operator_lock._LOCKS.clear()


def test_state_returns_none_when_empty():
    assert operator_lock.state(123) is None


def test_remember_and_state_roundtrip():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
        st = operator_lock.state(123)
    assert st is not None
    actor, acquired_at = st
    assert actor == "@alice"
    assert acquired_at == 1000.0


def test_state_returns_none_after_timeout():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    # 5 минут + 1 сек
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 301):
        assert operator_lock.state(123) is None


def test_state_purges_expired_entry_on_read():
    """После first state-call с expired locks, dict должен быть очищен."""
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 301):
        operator_lock.state(123)
        # После state-вызова entry должен быть удалён
        assert 123 not in operator_lock._LOCKS


def test_forget_idempotent():
    operator_lock.forget(123)  # не существует — не падает
    operator_lock.remember(123, "@alice")
    operator_lock.forget(123)
    operator_lock.forget(123)  # повторный — не падает
    assert operator_lock.state(123) is None


def test_remaining_min_decreases_over_time():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    # Сразу после remember — почти полные 5 минут (4 + 1 = 5)
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 1):
        assert operator_lock.remaining_min(123) == 5
    # Через 100 сек — осталось ~3+1=4
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 100):
        assert operator_lock.remaining_min(123) == 4
    # Через 200 сек — ~1+1=2
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 200):
        assert operator_lock.remaining_min(123) == 2
    # После expiry — 0
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 400):
        assert operator_lock.remaining_min(123) == 0


def test_remaining_min_zero_when_no_lock():
    assert operator_lock.remaining_min(123) == 0


def test_try_acquire_only_one_winner_across_threads():
    """Два оператора жмут одновременно — лок получает ровно один."""
    import threading
    operator_lock.forget(777)
    barrier = threading.Barrier(16)
    results = []

    def grab(i):
        barrier.wait()
        results.append(operator_lock.try_acquire(777, f"@op{i}"))

    ts = [threading.Thread(target=grab, args=(i,)) for i in range(16)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(None) == 1
    operator_lock.forget(777)


def test_try_acquire_refreshes_own_lock():
    operator_lock.forget(778)
    assert operator_lock.try_acquire(778, "@a") is None
    assert operator_lock.try_acquire(778, "@a") is None
    assert operator_lock.try_acquire(778, "@b") == "@a"
    operator_lock.forget(778)


def test_forget_if_owner_keeps_foreign_lock():
    operator_lock.forget(779)
    operator_lock.remember(779, "@a")
    assert operator_lock.forget_if_owner(779, "@b") is False
    assert operator_lock.state(779)[0] == "@a"
    assert operator_lock.forget_if_owner(779, "@a") is True
    assert operator_lock.state(779) is None
    assert operator_lock.forget_if_owner(779, "@b") is True  # лока нет — снимать нечего
