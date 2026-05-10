"""Shared in-memory lock на review-карточку (msg_id).

Используется и telegram_bot, и web/api_ma. Bot wrappers в telegram_bot.py
(_acquire_lock/_release_lock/_check_lock/_lock_remaining_min) держат
orchestration с thread_busy + drain_deferred — здесь только примитивы.
"""
from __future__ import annotations
import time

LOCK_TIMEOUT_SEC = 300

# msg_id -> (actor_str, acquired_at_unix)
_LOCKS: dict[int, tuple[str, float]] = {}


def state(msg_id: int) -> tuple[str, float] | None:
    """Текущий holder, None если свободно или auto-expired.

    На read обнаруженный expired-lock автоматически удаляется из dict.
    """
    e = _LOCKS.get(msg_id)
    if e is None:
        return None
    if time.time() - e[1] > LOCK_TIMEOUT_SEC:
        _LOCKS.pop(msg_id, None)
        return None
    return e


def remember(msg_id: int, actor: str) -> None:
    """Низкоуровневый set. Caller отвечает за thread_busy + drain orchestration."""
    _LOCKS[msg_id] = (actor, time.time())


def forget(msg_id: int) -> None:
    """Низкоуровневый del. No-op если key отсутствует."""
    _LOCKS.pop(msg_id, None)


def remaining_min(msg_id: int) -> int:
    """Минут до auto-release (для UI). 0 если нет lock или expired."""
    e = state(msg_id)
    if e is None:
        return 0
    age = time.time() - e[1]
    return max(0, int((LOCK_TIMEOUT_SEC - age) // 60) + 1)
