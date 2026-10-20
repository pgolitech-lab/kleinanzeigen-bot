"""Shared in-memory lock на review-карточку (msg_id).

Используется и telegram_bot, и web/api_ma. Bot wrappers в telegram_bot.py
(_claim_lock/_release_lock) держат
orchestration с thread_busy + drain_deferred — здесь только примитивы.
"""
from __future__ import annotations
import threading
import time

LOCK_TIMEOUT_SEC = 300

# msg_id -> (actor_str, acquired_at_unix). Трогают потоки threadpool uvicorn — только под _MUTEX.
_LOCKS: dict[int, tuple[str, float]] = {}
_MUTEX = threading.RLock()


def state(msg_id: int) -> tuple[str, float] | None:
    """Текущий holder, None если свободно или auto-expired.

    На read обнаруженный expired-lock автоматически удаляется из dict.
    """
    with _MUTEX:
        e = _LOCKS.get(msg_id)
        if e is None:
            return None
        if time.time() - e[1] > LOCK_TIMEOUT_SEC:
            _LOCKS.pop(msg_id, None)
            return None
        return e


def try_acquire(msg_id: int, actor: str) -> str | None:
    """Атомарно «проверить и взять»: None — лок теперь наш (продлён), иначе имя чужого владельца.

    Раздельные state() + remember() из двух потоков пропускали обоих операторов.
    """
    with _MUTEX:
        e = state(msg_id)
        if e is not None and e[0] != actor:
            return e[0]
        _LOCKS[msg_id] = (actor, time.time())
        return None


def remember(msg_id: int, actor: str) -> None:
    """Низкоуровневый set. Caller отвечает за thread_busy + drain orchestration."""
    with _MUTEX:
        _LOCKS[msg_id] = (actor, time.time())


def forget_if_owner(msg_id: int, actor: str) -> bool:
    """Атомарно снять лок, только если он наш (или его уже нет). False — держит другой."""
    with _MUTEX:
        e = state(msg_id)
        if e is not None and e[0] != actor:
            return False
        _LOCKS.pop(msg_id, None)
        return True


def forget(msg_id: int) -> None:
    """Низкоуровневый del. No-op если key отсутствует."""
    with _MUTEX:
        _LOCKS.pop(msg_id, None)


def remaining_min(msg_id: int) -> int:
    """Минут до auto-release (для UI). 0 если нет lock или expired."""
    e = state(msg_id)
    if e is None:
        return 0
    age = time.time() - e[1]
    return max(0, int((LOCK_TIMEOUT_SEC - age) // 60) + 1)
