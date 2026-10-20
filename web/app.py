# FastAPI-бэкенд Mini App.
#   • /api/ma/*  — API Mini App (web/api_ma.py), только с валидной Telegram initData
#   • /health    — liveness для watchdog'ов (bot_health.sh, tunnel_watchdog.sh)
#   • /api/logs, /api/status, /debug/run-job/{id} — служебное, ТОЛЬКО с самого сервера
#     (127.0.0.1, не через туннель). Старая Jinja-морда удалена 2026-10-20 — UI оператора = MA.

import time
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import log_buffer
import scheduler as sched_mod
from web.api_ma import invalidate_pipeline_cache, router as ma_router

# Документация OpenAPI наружу не нужна (и через туннель её не видно); локально — тоже лишнее
app = FastAPI(title="Kleinanzeigen Bot", docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pgolitech-lab.github.io",
        "https://web.telegram.org",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data", "Content-Type", "If-None-Match"],
    expose_headers=["ETag"],
    max_age=600,
)
app.include_router(ma_router)


# Через cloudflared-туннель (публичный адрес) наружу видны только Mini App API и /health.
_TUNNEL_ALLOWED_PREFIXES = ("/api/ma/", "/health")
_LOOPBACK = {"127.0.0.1", "::1"}


def _via_tunnel(request: Request) -> bool:
    h = request.headers
    host = (h.get("host") or "").split(":")[0].lower()
    return bool(h.get("cf-ray") or h.get("cf-connecting-ip") or host.endswith(".trycloudflare.com"))


def _is_local(request: Request) -> bool:
    """Запрос с самого сервера. cloudflared тоже ходит с 127.0.0.1, поэтому туннель исключаем отдельно."""
    return (not _via_tunnel(request)) and bool(request.client) and request.client.host in _LOOPBACK


@app.middleware("http")
async def _tunnel_guard(request: Request, call_next):
    path = request.url.path
    if _via_tunnel(request) and not (path == "/health" or path.startswith(_TUNNEL_ALLOWED_PREFIXES)):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    response = await call_next(request)
    # Любое действие оператора в MA меняет pipeline — сбрасываем короткий кэш
    if request.method == "POST" and path.startswith("/api/ma/"):
        invalidate_pipeline_cache()
    return response


def _require_local(request: Request) -> None:
    if not _is_local(request):
        raise HTTPException(404, "Not Found")


# ============================================================
# /health — лёгкий: без тяжёлых запросов на каждый вызов watchdog'а
# ============================================================

_POLLUTED_TTL_SEC = 600
_polluted_cache: dict[str, Any] = {"value": None, "at": 0.0}


def _polluted_incoming() -> Optional[int]:
    """count_polluted_incoming (LIKE по всем входящим) — не чаще раза в 10 мин.
    SQL идёт вне лока: медленная БД не должна вешать остальные /health."""
    if _polluted_cache["value"] is None or time.time() - _polluted_cache["at"] > _POLLUTED_TTL_SEC:
        from modules.incoming import count_polluted_incoming
        _polluted_cache.update(value=count_polluted_incoming(), at=time.time())
    return _polluted_cache["value"]


def _mail_status() -> dict[str, Any]:
    """Возраст последнего удачного опроса почты + состояние очереди (scheduler модуля mail).

    poll_age_sec=None при poll_tracking=True — с запуска ещё ни одного удачного опроса.
    poll_tracking=False — в этой сборке scheduler не отдаёт статус опроса (не «всё хорошо»).
    """
    out: dict[str, Any] = {"polling_paused": bool(config.polling_paused())}
    last_ok = getattr(sched_mod, "last_poll_ok_at", None)
    if not callable(last_ok):
        out.update(poll_tracking=False, poll_age_sec=None)
        return out
    ts = last_ok()
    out.update(poll_tracking=True, poll_age_sec=None if ts is None else max(0, int(time.time() - ts)))
    status = getattr(sched_mod, "poll_status", None)
    if callable(status):
        st = status()
        q = st.get("queue") or {}
        out["mail"] = {
            "last_pass_sec": st.get("last_pass_sec"),
            "last_errors": st.get("last_errors"),
            "worker_alive": q.get("worker_alive"),
            "busy_sec": q.get("busy_sec"),
            "queue_len": q.get("queue_len"),
            "accounts": {str(k): {"last_ok_at": v.get("last_ok_at"), "fails_in_row": v.get("fails_in_row")}
                         for k, v in (st.get("accounts") or {}).items()},
        }
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + сигналы для мониторинга. HTTP 200, пока процесс жив (watchdog смотрит код).

    - polling_paused: опрос почты на паузе (/pause) — это не зависание.
    - poll_age_sec / poll_tracking / mail: см. _mail_status.
    - polluted_incoming: входящие с неочищенным relay-шаблоном Kleinanzeigen (0 — норма;
      стабильно >0 — регресс parser._strip_relay_template). Кэш 10 мин.
    """
    return {"status": "ok", **_mail_status(), "polluted_incoming": _polluted_incoming()}


# ============================================================
# Служебное — только локально (curl на самом сервере)
# ============================================================

@app.get("/api/logs")
def api_logs(request: Request, since: Optional[int] = None, limit: int = 500) -> dict[str, Any]:
    """Новые записи кольцевого буфера логов с указанного id (или последние limit штук)."""
    _require_local(request)
    return {
        "entries": log_buffer.get_since(since, limit=min(max(limit, 1), 2000)),
        "last_id": log_buffer.last_id(),
    }


@app.get("/api/status")
def api_status(request: Request) -> dict[str, Any]:
    """Снимок задач APScheduler: следующий запуск, последний результат/ошибка."""
    _require_local(request)
    return {"jobs": sched_mod.get_jobs_info()}


@app.post("/debug/run-job/{job_id}")
def debug_run_job(job_id: str, request: Request) -> dict[str, Any]:
    """Запустить джоб APScheduler сейчас — через сам планировщик (max_instances=1 соблюдается:
    если джоб уже идёт, APScheduler пропустит запуск, а не запустит второй параллельно)."""
    _require_local(request)
    sched = getattr(sched_mod, "_active_sched", None)
    if sched is None:
        raise HTTPException(503, "scheduler не запущен")
    job = sched.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"нет джоба {job_id!r}; есть: {[j.id for j in sched.get_jobs()]}")
    job.modify(next_run_time=datetime.now(sched.timezone))
    return {"ok": True, "job_id": job_id, "scheduled": "now"}
