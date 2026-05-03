"""In-memory кольцевой буфер логов.

Подключается через install() в main.py — после этого все вызовы logging.* пишут
в глобальный deque. Веб-морда читает его через /api/logs.

Порог 2000 записей обусловлен тем, что один INFO ~200 байт → ~400 KB макс,
не съест RAM даже при agresivnomu polling.
"""

import logging
from collections import deque
from datetime import datetime
from typing import Optional


_BUFFER: deque[dict] = deque(maxlen=2000)
_counter = 0


class _RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _counter
        _counter += 1
        try:
            _BUFFER.append({
                "id": _counter,
                "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
        except Exception:
            pass


def install() -> None:
    """Установить handler на root logger. Идемпотентна — повторный вызов ничего не сломает."""
    root = logging.getLogger()
    if any(isinstance(h, _RingBufferHandler) for h in root.handlers):
        return
    h = _RingBufferHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h)


def get_since(since_id: Optional[int] = None, limit: int = 500) -> list[dict]:
    """Вернуть записи с id > since_id. Если since_id=None — последние limit штук."""
    if since_id is None:
        return list(_BUFFER)[-limit:]
    return [r for r in _BUFFER if r["id"] > since_id][-limit:]


def last_id() -> int:
    """Текущий монотонный id (для инициализации фронтенда)."""
    return _counter
