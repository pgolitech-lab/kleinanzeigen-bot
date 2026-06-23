# Общий фоновый раннер разведки рынка. Единое in-memory состояние, которое
# делят и веб-морда (/scout), и Mini App (/api/ma/scout) — чтобы «идёт прогон»
# было видно из обоих UI. Сбрасывается при рестарте сервиса.

import logging
import threading
from datetime import datetime
from typing import Any, Optional

import config
from modules import scout

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {"running": False, "summary": None, "started_at": None}
_lock = threading.Lock()


def _run(query_ids: Optional[list[int]]) -> None:
    try:
        _state["summary"] = scout.run_scout(
            query_ids, page_delay_sec=config.scout_page_delay_sec()
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("scout_runner: прогон упал")
        _state["summary"] = {"errors": [str(e)]}
    finally:
        _state["running"] = False


def start(query_ids: Optional[list[int]] = None) -> bool:
    """Запустить разведку в фоне. False если уже идёт прогон."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["started_at"] = datetime.utcnow().isoformat()
        _state["summary"] = None
    threading.Thread(target=_run, args=(query_ids,), daemon=True).start()
    return True


def is_running() -> bool:
    return bool(_state["running"])


def status() -> dict[str, Any]:
    """Снимок состояния (копия)."""
    return dict(_state)
