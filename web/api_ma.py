"""Mini App API router.

Все endpoint'ы под префиксом /api/ma/. Тонкие обёртки над database.* и
existing scheduler.* helpers — никакой бизнес-логики.

Все endpoint'ы требуют валидной Telegram initData через verify_init_data_dep.
"""
from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

import hashlib
import sqlite3
import os
import json
import logging
import re
import threading
import time

import database as db
import config
from modules import operator_lock, telegram_bot
import scheduler
from modules import claude
from modules import claude_scout
from modules import scout_runner
from modules.db_sales import today_berlin
from modules.tg_init_data import verify_init_data_dep, verify_init_data


router = APIRouter(prefix="/api/ma", tags=["Mini App"])
logger = logging.getLogger(__name__)

_CLOSED_STATUSES = {"skipped", "skipped_sold", "archived"}
_SENT_STATUSES = frozenset(db.SENT_STATUSES)  # ушло клиенту (в т.ч. sent_external — с сайта)
# Строка уже отвечена/закрыта: перезапись черновика затёрла бы отправленный клиенту текст
# (инцидент аудита №45: «Предложить» переводил sent → edited и затирал de_answer).
_ANSWERED_STATUSES = {"sent", "sent_debug", "sending", "send_queued", "skipped_sold", "archived"}


def _already_sent(row) -> bool:
    """Клиенту уже ушло (или уходит) письмо по этой строке — включая строки, испорченные
    до фикса (status edited, но sent_at/sent_message_id стоят)."""
    keys = row.keys()
    if row["status"] in {"sent", "sent_debug", "sending"}:
        return True
    return bool(("sent_at" in keys and row["sent_at"])
                or ("sent_message_id" in keys and row["sent_message_id"]))


def _is_answered(row) -> bool:
    """Черновик менять нельзя: отправлено или тред закрыт (продано/архив).
    «Пропущено» (skipped) — можно: оператор передумал и хочет ответить."""
    return row["status"] in _ANSWERED_STATUSES or _already_sent(row)


def _ensure_nothing_queued(thread_id: "str | None") -> None:
    """В треде письмо ждёт отложенной отправки — compose/«Продано»/«Пропустить» сначала через «Отменить»
    (иначе клиенту ушло бы и письмо из очереди)."""
    if not thread_id:
        return
    with db.get_conn() as conn:
        queued = conn.execute("SELECT id FROM messages WHERE gmail_thread_id=? AND status='send_queued' LIMIT 1",
                              (thread_id,)).fetchone()
    if queued:
        raise HTTPException(409, "В треде письмо в очереди на отправку — сначала нажмите «Отменить»")


def _ensure_draft_editable(row) -> None:
    if row["status"] == "send_queued":
        raise HTTPException(409, "Письмо в очереди на отправку — сначала нажмите «Отменить»")
    if _is_answered(row):
        raise HTTPException(
            409, f"Сообщение уже отправлено или закрыто ({row['status']}) — черновик не меняем. "
                 "Для нового ответа используйте «Написать».")
_ALLOWED_TAGS = {"Серьёзный", "Торгуется", "Тянет время", "Мошенник"}


@router.get("/health")
def ma_health(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Health endpoint для Mini App. Возвращает идентичность оператора."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }


# Превью письма в карточке инбокса — фронт показывает ~110 символов; полный текст — в треде
_PIPELINE_PREVIEW_CHARS = 200

# Короткий кэш pipeline_threads: дашборд MA параллельно грузит /dashboard и /pipeline —
# один тяжёлый запрос на двоих. Любой POST /api/ma/* сбрасывает кэш (web/app.py).
_PIPELINE_TTL_SEC = 3.0
_pipeline_cache: dict[str, Any] = {"rows": None, "at": 0.0, "gen": 0, "inflight": None}
_pipeline_lock = threading.Lock()  # только на чтение/запись словаря, не на время SQL


def invalidate_pipeline_cache() -> None:
    with _pipeline_lock:
        _pipeline_cache["rows"] = None
        _pipeline_cache["gen"] += 1


def _pipeline_rows() -> list:
    """pipeline_threads с кэшем: параллельные запросы ждут один SQL (single-flight)."""
    with _pipeline_lock:
        c = _pipeline_cache
        if c["rows"] is not None and time.monotonic() - c["at"] <= _PIPELINE_TTL_SEC:
            return c["rows"]
        fut = c["inflight"]
        leader = fut is None
        if leader:
            fut = c["inflight"] = Future()
            gen = c["gen"]
    if not leader:
        return fut.result(timeout=60)
    try:
        rows = db.pipeline_threads()
    except BaseException as e:
        with _pipeline_lock:
            _pipeline_cache["inflight"] = None
        fut.set_exception(e)
        raise
    with _pipeline_lock:
        # Пока шёл запрос, POST мог сбросить кэш — тогда результат не кладём (мог устареть)
        if _pipeline_cache["gen"] == gen:
            _pipeline_cache.update(rows=rows, at=time.monotonic())
        _pipeline_cache["inflight"] = None
    fut.set_result(rows)
    return rows


def _preview(text: "str | None") -> "str | None":
    if not text:
        return text
    t = " ".join(text.split())
    return t if len(t) <= _PIPELINE_PREVIEW_CHARS else t[:_PIPELINE_PREVIEW_CHARS] + "…"


_ctx_cache: dict[str, Any] = {"rows": None, "ctx": None}


def _pipeline_context_cached(rows: list) -> dict[str, Any]:
    """Контекст считается раз на «поколение» строк pipeline (тот же TTL/сброс, что _pipeline_rows)."""
    with _pipeline_lock:
        if _ctx_cache["rows"] is rows and _ctx_cache["ctx"] is not None:
            return _ctx_cache["ctx"]
    ctx = _pipeline_context(rows)
    with _pipeline_lock:
        _ctx_cache.update(rows=rows, ctx=ctx)  # ссылка, а не id(): id переиспользуется после GC
    return ctx


def _pipeline_context(rows: list) -> dict[str, Any]:
    """Всё, что нужно карточкам «Входящих», — несколькими запросами на весь список (без N+1):
    состояние торга по тредам, минимумы объявлений, теги покупателей, имена с несколькими email."""
    with db.get_conn() as conn:
        neg = {r["gmail_thread_id"]: r for r in conn.execute(
            "SELECT gmail_thread_id, floor_eur, our_last_offer_eur, buyer_last_offer_eur FROM negotiation_state")}
        mins = {r["ad_id"]: r["min_price_eur"] for r in conn.execute(
            "SELECT ad_id, min_price_eur FROM ad_briefs WHERE min_price_eur IS NOT NULL")}
        tags = {r["buyer_email"]: r["tags_json"] for r in conn.execute(
            "SELECT buyer_email, tags_json FROM client_profiles")}
        # (аккаунт, нормализованное имя) с >1 email: relay-адрес свой на каждое объявление →
        # «возможно тот же покупатель» по другим объявлениям
        multi = {(r["account_id"], r["k"]) for r in conn.execute(
            "SELECT account_id, norm_name(buyer_display_name) AS k FROM messages "
            "WHERE direction='in' AND buyer_display_name IS NOT NULL "
            "GROUP BY 1, 2 HAVING k IS NOT NULL AND COUNT(DISTINCT buyer_name) > 1")}
        # минимум цены товара со склада (правило владельца 20.10): объявление → товар
        try:
            from modules.inventory import effective_min_price
            stock_min = {r["ad_id"]: effective_min_price(r)[0] for r in conn.execute(
                "SELECT l.ad_id, p.min_price_eur, p.list_price_eur FROM inv_listings l "
                "JOIN inv_products p ON p.id = l.product_id")}
        except sqlite3.OperationalError:
            stock_min = {}  # склад ещё не заведён (seed не запускали)
    return {"neg": neg, "mins": mins, "tags": tags, "multi": multi, "stock_min": stock_min}


def _row_to_pipeline_item(row: Any, ctx: "dict[str, Any] | None" = None) -> dict[str, Any]:
    """Конвертация row из db.pipeline_threads (LEFT JOIN thread_autopilot) в API-форму."""
    keys = row.keys()
    ctx = ctx or {"neg": {}, "mins": {}, "tags": {}, "multi": set(), "stock_min": {}}
    neg = ctx["neg"].get(row["gmail_thread_id"])
    ad_id = row["ad_id"] if "ad_id" in keys else None
    floor_candidates = [x for x in (
        neg["floor_eur"] if neg is not None else None, ctx["mins"].get(ad_id), ctx["stock_min"].get(ad_id),
        row["autopilot_floor_price_eur"] if "autopilot_floor_price_eur" in keys else None) if x]
    try:
        tags = json.loads(ctx["tags"].get(row["buyer_name"] if "buyer_name" in keys else None) or "[]")
    except ValueError:
        tags = []
    return {
        "thread_id": row["gmail_thread_id"],
        "msg_id": row["id"],
        "ad_title": row["ad_title"],
        "ad_price": row["ad_price"],
        "ad_price_eur": _parse_listed_price_eur(row["ad_price"]),
        "ad_url": row["ad_url"] if "ad_url" in keys else None,
        "buyer_display_name": row["buyer_display_name"],
        "client_lang": row["client_lang"] if "client_lang" in keys else None,
        "deal_brief_json": row["deal_brief_json"] if "deal_brief_json" in keys else None,
        # числа торга: предложение покупателя, наша последняя цена, минимум (наибольший из известных)
        "offer_eur": neg["buyer_last_offer_eur"] if neg is not None else None,
        "our_last_eur": neg["our_last_offer_eur"] if neg is not None else None,
        "floor_eur": max(floor_candidates) if floor_candidates else None,
        "last_event_at": row["last_event_at"],
        "last_event_kind": row["last_event_kind"],
        "pending_drafts_count": row["pending_drafts_count"],
        "is_autopilot": bool(row["autopilot_active"]),
        # «горячий покупатель» (mail помечает is_hot/hot_reason) — наверх в «ждут нас»
        "is_hot": bool(row["is_hot"]) if "is_hot" in keys else False,
        "hot_reason": row["hot_reason"] if "hot_reason" in keys else None,
        # is_late (письмо дошло с опозданием) — когда mail/db сохранят признак в messages
        "is_late": bool(row["is_late"]) if "is_late" in keys else None,
        "tags": tags if isinstance(tags, list) else [],
        "maybe_same": (row["account_id"] if "account_id" in keys else None,
                       db.norm_name(row["buyer_display_name"])) in ctx["multi"],
        "account_id": row["account_id"] if "account_id" in keys else None,
        "ru_client": _preview(row["ru_client"]) if "ru_client" in keys else None,
        "is_pinned": bool(row["is_pinned"]) if "is_pinned" in keys else False,
        "operator_unread": bool(row["operator_unread"]) if "operator_unread" in keys else False,
    }


@router.get("/pipeline")
def ma_pipeline(request: Request, user: dict = Depends(verify_init_data_dep)) -> Response:
    """Pipeline активных тредов: pinned / red (ждут нас) / green (ждём клиента).

    Сортировка внутри секции — DESC по last_event_at (новые сверху).
    ETag: если ничего не изменилось — 304 без тела (экран опрашивается часто).
    """
    rows = _pipeline_rows()
    ctx = _pipeline_context_cached(rows)
    pinned: list[dict[str, Any]] = []
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_pipeline_item(row, ctx)
        if item["is_pinned"]:
            pinned.append(item)
        elif item["last_event_kind"] == "in":
            red.append(item)
        else:
            green.append(item)
    for section in (pinned, red, green):
        section.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    red.sort(key=lambda x: x["is_hot"], reverse=True)  # стабильная: горячие сверху, внутри — по времени
    accounts = [{"id": a["id"], "name": a["name"]} for a in db.list_accounts()]
    today = today_berlin()
    sold = db.sales_summary(today, today)
    summary = {
        "waiting": len(red) + sum(1 for x in pinned if x["last_event_kind"] == "in"),  # как бейдж вкладки
        "waiting_client": len(green),
        "sold_today_count": sold["count"],
        "sold_today_eur": sold["total_eur"],
        "detected_pending": len(db.list_pending_detections()),
    }
    body = json.dumps({"pinned": pinned, "red": red, "green": green, "accounts": accounts, "summary": summary},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


from fastapi import HTTPException


def _event_to_api(event: dict[str, Any]) -> dict[str, Any]:
    """Конвертация event-dict из db.thread_events в API-форму."""
    row = event.get("row")
    return {
        "ts": event.get("ts"),
        "kind": event.get("kind"),
        "text": event.get("text"),
        "ru_text": event.get("ru_text"),
        "is_auto_ack": bool(event.get("is_auto_ack")),
        "msg_id": row["id"] if row is not None else None,
        "status": event.get("status") or (row["status"] if row is not None else None),
        # реплика продавца, написанная на сайте/в приложении Kleinanzeigen (импорт)
        "from_app": bool(row is not None and event.get("kind") == "out"
                         and "source" in row.keys() and row["source"] == "site"),
        "is_late": bool(row["is_late"]) if row is not None and "is_late" in row.keys() else None,
    }


def _related_match(row: Any) -> dict[str, Any]:
    return {
        "thread_id": row["gmail_thread_id"],
        "ad_title": row["ad_title"] if "ad_title" in row.keys() else None,
        "ad_price": row["ad_price"] if "ad_price" in row.keys() else None,
        "last_at": (row["sent_at"] if "sent_at" in row.keys() else None) or (
            row["created_at"] if "created_at" in row.keys() else None),
    }


def _related(buyer_email: "str | None", display_name: "str | None", *,
             exclude_thread_id: "str | None" = None, account_id: "int | None" = None) -> dict[str, Any]:
    """Другие треды клиента: `matches` — точно он (тот же email), `maybe_same` — возможно тот же
    покупатель (то же имя в том же аккаунте: relay-email Kleinanzeigen свой на каждое объявление)."""
    if not buyer_email:
        return {"buyer_display_name": display_name, "matches": [], "maybe_same": []}
    rows = db.find_related_inquiries(
        buyer_email, exclude_thread_id=exclude_thread_id, limit=10,
        display_name=display_name, account_id=account_id, include_maybe=True)
    exact, maybe = [], []
    for r in rows:
        is_maybe = "maybe_same" in r.keys() and bool(r["maybe_same"])
        (maybe if is_maybe else exact).append(_related_match(r))
    return {"buyer_display_name": display_name, "matches": exact, "maybe_same": maybe}


def _thread_dict(thread_id: str) -> dict[str, Any]:
    """Полный thread payload (header + events + related). Used by GET threads and autopilot endpoints."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    last_in = next((r for r in reversed(history) if r["direction"] == "in"), history[-1])
    account = db.get_account(last_in["account_id"]) if "account_id" in last_in.keys() else None
    autopilot_row = db.get_thread_autopilot(thread_id)
    is_autopilot = False
    if autopilot_row is not None:
        try:
            is_autopilot = bool(autopilot_row["active"])
        except (KeyError, IndexError):
            is_autopilot = False
    header = {
        "thread_id": thread_id,
        "ad_title": last_in["ad_title"],
        "ad_price": last_in["ad_price"],
        "ad_url": last_in["ad_url"] if "ad_url" in last_in.keys() else None,
        "buyer_display_name": last_in["buyer_display_name"],
        "buyer_email": last_in["buyer_name"] if "buyer_name" in last_in.keys() else None,
        "account_id": last_in["account_id"] if "account_id" in last_in.keys() else None,
        "account_name": account["name"] if account is not None else None,
        "account_email": account["gmail_email"] if account is not None else None,
        "is_autopilot": is_autopilot,
    }
    # В ленте — только реально ушедшее; черновик показывается в своей карточке, а не
    # зелёным «нашим ответом» (QA B-02: db.thread_events отдаёт и pending/edited как out)
    events = [ev for ev in (_event_to_api(e) for e in db.thread_events(thread_id))
              if ev["kind"] != "out" or ev["status"] in _SENT_STATUSES]
    related = _related(
        last_in["buyer_name"] if "buyer_name" in last_in.keys() else None,
        last_in["buyer_display_name"], exclude_thread_id=thread_id,
        account_id=last_in["account_id"] if "account_id" in last_in.keys() else None)
    return {"header": header, "events": events, "related": related}


# ──────────────────────── РАЗБОР (triage) ────────────────────────
# Очередь «ждут нас», отсортированная по срочности и шансу сделки. Баллы прозрачные —
# фронт показывает reasons, чтобы оператор видел, почему тред наверху.
_HOT_WORDS = ("серьёз", "серьез", "готов", "горяч", "быстро", "сегодня", "завтра", "забер")
_COLD_WORDS = ("мошен", "тянет", "спам", "скам", "подозр")


def _triage_item(row: Any, now: datetime, ctx: "dict[str, Any] | None" = None) -> dict[str, Any]:
    keys = row.keys()
    base = _row_to_pipeline_item(row, ctx)  # те же числа торга, теги, язык, что во «Входящих»
    score = 0.0
    reasons: list[str] = []
    # 1) ожидание: до 40 баллов за первые сутки
    wait_h = None
    try:
        last = datetime.fromisoformat(str(row["last_event_at"]).replace("Z", "").split("+")[0])
        wait_h = max(0.0, (now - last).total_seconds() / 3600)
    except (TypeError, ValueError):
        pass
    if wait_h is not None:
        score += min(wait_h / 24, 1.0) * 40
        if wait_h >= 2:
            reasons.append(f"ждёт {int(wait_h)} ч" if wait_h < 48 else f"ждёт {int(wait_h // 24)} дн.")
    # 2) горячесть из сводки сделки
    brief = _parse_deal_brief(row["deal_brief_json"] if "deal_brief_json" in keys else None) or {}
    assessment = " ".join(str(brief.get(k) or "") for k in ("client_assessment", "expected_next")).lower()
    if any(w in assessment for w in _HOT_WORDS):
        score += 25
        reasons.append("горячий")
    if any(w in assessment for w in _COLD_WORDS):
        score -= 25
        reasons.append("сомнительный")
    # 3) цена: предложение покупателя против цены объявления и минимума
    listed = base["ad_price_eur"]
    offered = base["offer_eur"] if base["offer_eur"] is not None else brief.get("negotiated_price_eur")
    floor = base["floor_eur"]
    if isinstance(offered, (int, float)) and offered > 0:
        if listed:
            ratio = offered / listed
            if ratio >= 0.9:
                score += 25
                reasons.append("цена почти полная")
            elif ratio >= 0.75:
                score += 10
                reasons.append(f"предлагает {int(ratio * 100)}%")
        if floor and offered >= floor:
            score += 15
            reasons.append("не ниже минимума")
    # 4) готовый черновик — ответить можно сразу
    has_draft = bool(row["has_pending_draft"]) and bool(row["de_answer"] if "de_answer" in keys else None)
    if has_draft:
        score += 5
    return {
        "thread_id": row["gmail_thread_id"],
        "msg_id": row["id"],
        "account_id": row["account_id"] if "account_id" in keys else None,
        "buyer_display_name": row["buyer_display_name"],
        "ad_title": row["ad_title"],
        "ad_price": row["ad_price"],
        "ad_price_eur": listed,
        "offered_eur": offered if isinstance(offered, (int, float)) else None,
        "offer_eur": base["offer_eur"],
        "our_last_eur": base["our_last_eur"],
        "floor_eur": floor,
        "client_lang": base["client_lang"],
        "tags": base["tags"],
        "maybe_same": base["maybe_same"],
        "is_hot": base["is_hot"],
        "hot_reason": base["hot_reason"],
        "client_message_ru": _preview(row["ru_client"] if "ru_client" in keys else None),
        "last_event_at": row["last_event_at"],
        "wait_hours": round(wait_h, 1) if wait_h is not None else None,
        "deal_summary_ru": brief.get("summary_ru"),
        "draft": {
            "msg_id": row["id"],
            "status": row["status"],
            "client_text": row["de_answer"],
            "ru_text": (row["ru_translation"] if "ru_translation" in keys else None) or row["ru_answer"],
        } if has_draft else None,
        "score": round(score, 1),
        "reasons": reasons,
    }


@router.get("/triage")
def ma_triage(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """«Разбор»: треды, где ход за нами (последним писал покупатель), без активного автопилота —
    по убыванию балла (ожидание, горячесть, цена против объявления/минимума, готовый черновик)."""
    now = datetime.utcnow()
    rows = _pipeline_rows()
    ctx = _pipeline_context_cached(rows)
    items = [_triage_item(r, now, ctx) for r in rows
             if r["last_event_kind"] == "in" and not r["autopilot_active"]]
    items.sort(key=lambda x: (x["score"], x["last_event_at"] or ""), reverse=True)
    return {"items": items, "count": len(items)}


class BulkActionBody(BaseModel):
    thread_ids: list[str] = Field(..., min_length=1)
    action: str  # "pin" | "unpin" | "read" | "unread" | "close"


@router.post("/threads/bulk-action")
def ma_bulk_action(
    body: BulkActionBody,
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Bulk-действия над несколькими тредами."""
    _ALLOWED = {"pin", "unpin", "read", "unread", "close"}
    if body.action not in _ALLOWED:
        raise HTTPException(400, f"unknown action: {body.action!r}")
    actor = user.get("username") or str(user.get("id", ""))
    for thread_id in body.thread_ids:
        if body.action == "close":
            db.close_thread(thread_id, closed_by=actor)
        elif body.action == "pin":
            db.set_thread_flags(thread_id, is_pinned=1)
        elif body.action == "unpin":
            db.set_thread_flags(thread_id, is_pinned=0)
        elif body.action == "read":
            db.set_thread_flags(thread_id, operator_unread=0)
        elif body.action == "unread":
            db.set_thread_flags(thread_id, operator_unread=1)
    return {"ok": True, "affected": len(body.thread_ids)}


_DRAFT_STATUSES = {"pending", "new", "edited", "approved"}


@router.get("/threads/{thread_id}")
def ma_thread(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _thread_dict(thread_id)


class ThreadOpenBody(BaseModel):
    focus: "int | None" = None  # msg_id черновика; нет — последний неотправленный черновик треда
    acquire: bool = True        # взять лок на этот черновик


def _dossier_items(d: Any) -> "list[Any]":
    """Досье (client_dossiers) в форме, которую рисует экран треда: список строк или {date, text}."""
    if d is None:
        return []
    items: list[Any] = []
    if d["summary_md"]:
        items.append(" ".join(str(d["summary_md"]).split())[:400])
    try:
        facts = json.loads(d["facts_json"] or "[]")
    except ValueError:
        logger.error("досье #%s: facts_json не JSON — показываю только сводку", d["id"])
        facts = []
    if isinstance(facts, dict):
        facts = [f"{k}: {v}" for k, v in facts.items()]
    for f in facts if isinstance(facts, list) else []:
        if isinstance(f, (str, dict)):
            items.append(f)
    return items[:5]


def _enrich_header(thread_id: str, header: "dict[str, Any]") -> None:
    """Поля шапки треда, которые читает экран: числа торга (как во «Входящих»), теги, заметка, досье."""
    rows = _pipeline_rows()
    row = next((r for r in rows if r["gmail_thread_id"] == thread_id), None)
    header.setdefault("ad_price_eur", _parse_listed_price_eur(header.get("ad_price")))
    for k in ("offer_eur", "our_last_eur", "floor_eur", "client_lang", "is_hot", "hot_reason", "maybe_same"):
        header.setdefault(k, None)
    if row is not None:
        item = _row_to_pipeline_item(row, _pipeline_context_cached(rows))
        for k in ("offer_eur", "our_last_eur", "floor_eur", "ad_price_eur", "client_lang",
                  "is_hot", "hot_reason", "maybe_same"):
            header[k] = item[k]
    email = header.get("buyer_email")
    profile = db.get_client_profile(email) if email else None
    try:
        tags = json.loads(profile["tags_json"]) if profile is not None else []
    except (ValueError, TypeError):
        tags = []
    header["tags"] = tags if isinstance(tags, list) else []
    header["client_note"] = (profile["note"] if profile is not None else "") or None
    header["dossier"] = _dossier_items(db.get_dossier_for_email(email) if email else None)


@router.post("/threads/{thread_id}/open")
def ma_thread_open(thread_id: str, body: "ThreadOpenBody | None" = None,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Всё для открытия экрана треда одним запросом (вместо thread → message → lock/acquire).

    Ответ = GET /threads/{id} + review (карточка черновика или null) + lock (при acquire:
    {acquired, holder, remaining_min}; занят другим — acquired=false, экран только для чтения).
    POST, а не GET: берёт лок, повтор/префетч GET не должен его захватывать.
    """
    b = body or ThreadOpenBody()
    data = _thread_dict(thread_id)
    data["review"], data["lock"] = None, None
    _enrich_header(thread_id, data["header"])
    msg_id = b.focus
    if msg_id is None:
        drafts = [e["msg_id"] for e in data["events"]
                  if e["kind"] == "in" and e["msg_id"] and e["status"] in _DRAFT_STATUSES]
        msg_id = drafts[-1] if drafts else None
    if msg_id is None:
        return data
    row = db.get_message(msg_id)
    if row is None or row["gmail_thread_id"] != thread_id:
        raise HTTPException(404, "message not found in thread")
    if b.acquire:
        actor = actor_from_user(user)
        # Отправленное/закрытое не лочим — его всё равно нельзя править
        foreign = None if _is_answered(row) else telegram_bot._claim_lock(msg_id, actor)
        acquired = not _is_answered(row) and foreign is None
        data["lock"] = {"acquired": acquired,
                        "holder": actor if acquired else foreign,
                        "remaining_min": operator_lock.remaining_min(msg_id)}
    data["review"] = _message_review_dict(msg_id)
    return data


@router.get("/clients/{email}/history")
def ma_client_history(email: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Профиль клиента: треды + deal_brief + теги + агрегаты."""
    rows = db.list_threads_for_client(email)

    # display_name и total_cost — один коннект
    display_name = email
    account_id = None
    total_cost_usd = 0.0
    with db.get_conn() as conn:
        dn_row = conn.execute(
            "SELECT buyer_display_name, account_id FROM messages "
            "WHERE buyer_name = ? AND buyer_display_name IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if dn_row:
            display_name = dn_row["buyer_display_name"]
            account_id = dn_row["account_id"]
        cost_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM messages WHERE buyer_name = ?",
            (email,),
        ).fetchone()
        if cost_row:
            total_cost_usd = round(float(cost_row["total"]), 5)

    # Теги и заметка из client_profiles
    profile = db.get_client_profile(email)
    tags: list[str] = []
    note: str = ""
    if profile:
        try:
            tags = json.loads(profile["tags_json"]) or []
        except (json.JSONDecodeError, TypeError):
            tags = []
        note = profile["note"] or ""

    # Собрать треды + посчитать агрегаты
    threads = []
    sold_count = 0
    total_negotiated_eur = 0
    last_active_thread_id = None
    for r in rows:
        brief = _parse_deal_brief(dict(r).get("deal_brief_json"))
        status = r["last_status"] or ""
        # last_active_thread_id — первый (свежий) тред не в закрытых статусах
        if last_active_thread_id is None and status not in _CLOSED_STATUSES:
            last_active_thread_id = r["thread_id"]
        # sold_count / total_negotiated_eur
        if status == "skipped_sold":
            sold_count += 1
            if brief:
                price = brief.get("negotiated_price_eur") or 0
                if price and price > 0:
                    total_negotiated_eur += price
        threads.append({
            "thread_id": r["thread_id"],
            "ad_title": r["ad_title"],
            "ad_id": r["ad_id"],
            "ad_price": r["ad_price"],
            "msg_count": r["msg_count"],
            "last_at": r["last_at"],
            "last_status": status,
            "deal_brief": brief,
        })

    return {
        "buyer_email": email,
        "display_name": display_name,
        "total_cost_usd": total_cost_usd,
        "tags": tags,
        "note": note,
        "sold_count": sold_count,
        "total_negotiated_eur": total_negotiated_eur,
        "last_active_thread_id": last_active_thread_id,
        "threads": threads,
        # Треды «возможно того же покупателя» (другой relay-email, то же имя и аккаунт)
        "maybe_same": _related(email, display_name if dn_row else None,
                               account_id=account_id)["maybe_same"],
    }




class ClientProfilePayload(BaseModel):
    tags: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=4000)


@router.post("/clients/{email}/profile")
def ma_client_profile_save(
    email: str,
    payload: ClientProfilePayload,
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Сохранить теги и заметку оператора для покупателя."""
    clean_tags = [t for t in payload.tags if t in _ALLOWED_TAGS]
    db.upsert_client_profile(email, clean_tags, payload.note)
    return {"ok": True}


def _parse_deal_brief(raw) -> dict | None:
    """Parse messages.deal_brief_json. None при NULL/empty/invalid JSON."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _autopilot_view(autopilot_row) -> dict:
    """Standard autopilot dict для frontend. Defaults если row=None."""
    if autopilot_row is None:
        return {"active": False, "messages_sent": 0, "floor_eur": None, "notify_mode": None}
    try:
        return {
            "active": bool(autopilot_row["active"]),
            "messages_sent": autopilot_row["messages_sent"] if "messages_sent" in autopilot_row.keys() else 0,
            "floor_eur": autopilot_row["floor_eur"] if "floor_eur" in autopilot_row.keys() else None,
            "notify_mode": autopilot_row["notify_mode"] if "notify_mode" in autopilot_row.keys() else None,
        }
    except (KeyError, IndexError):
        return {"active": False, "messages_sent": 0, "floor_eur": None, "notify_mode": None}


def actor_from_user(user: dict) -> str:
    """Format actor for lock — matches what bot uses in callbacks."""
    uid = user.get("id")
    name = user.get("username") or user.get("first_name") or "?"
    prefix = "@" if user.get("username") else ""
    return f"{prefix}{name}#{uid}"



VALID_STRATEGIES = {"fest", "harsh", "friend", "short", "regen"}


def _message_review_dict(msg_id: int) -> "dict[str, Any]":
    """Полный review payload (используется и в GET, и в action POSTs).
    Raises HTTPException(404) если msg отсутствует."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    thread_id = row["gmail_thread_id"]
    autopilot_row = db.get_thread_autopilot(thread_id) if thread_id else None
    lock_state = operator_lock.state(msg_id)
    lock_holder = lock_state[0] if lock_state else None
    related = _related(
        row["buyer_name"] if "buyer_name" in row.keys() else None,
        row["buyer_display_name"], exclude_thread_id=thread_id,
        account_id=row["account_id"] if "account_id" in row.keys() else None)
    return {
        "msg_id": row["id"],
        "thread_id": thread_id,
        "status": row["status"],
        "ad": {
            "title": row["ad_title"],
            "price": row["ad_price"],
            "url": row["ad_url"] if "ad_url" in row.keys() else None,
            "id": row["ad_id"] if "ad_id" in row.keys() else None,
            "buyer_display_name": row["buyer_display_name"],
            "buyer_email": row["buyer_name"] if "buyer_name" in row.keys() else None,
        },
        "client_lang": row["client_lang"] if "client_lang" in row.keys() else None,
        "client_message": {
            "raw": row["de_client"] if "de_client" in row.keys() else None,
            "ru": row["ru_client"] if "ru_client" in row.keys() else None,
        },
        "draft": {
            "ru_answer": row["ru_answer"] if "ru_answer" in row.keys() else None,
            "de_answer": row["de_answer"] if "de_answer" in row.keys() else None,
            "ru_translation": row["ru_translation"] if "ru_translation" in row.keys() else None,
        },
        "deal_brief": _parse_deal_brief(row["deal_brief_json"] if "deal_brief_json" in row.keys() else None),
        "related": related,
        "lock": {
            "holder": lock_holder,
            "remaining_min": operator_lock.remaining_min(msg_id),
        },
        "autopilot": _autopilot_view(autopilot_row),
        "extra_notes": row["extra_notes"] if "extra_notes" in row.keys() else None,
        "is_auto_ack": bool(row["is_auto_ack"]) if "is_auto_ack" in row.keys() else False,
    }


class RegenerateBody(BaseModel):
    strategy: str

    @field_validator("strategy")
    @classmethod
    def _check_strategy(cls, v: str) -> str:
        if v not in VALID_STRATEGIES:
            raise ValueError(f"strategy must be one of {sorted(VALID_STRATEGIES)}")
        return v


@router.get("/messages/{msg_id}")
def ma_message_review(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Полный review payload."""
    return _message_review_dict(msg_id)


@router.get("/messages/{msg_id}/lock")
def ma_message_lock_state(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Lock-state poll endpoint — лёгкий, без full review payload."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    st = operator_lock.state(msg_id)
    return {
        "holder": st[0] if st else None,
        "remaining_min": operator_lock.remaining_min(msg_id),
    }

@router.post("/messages/{msg_id}/lock/acquire")
def ma_lock_acquire(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict:
    """Acquire lock on review card. 409 if held by someone else."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign_holder = telegram_bot._claim_lock(msg_id, actor)
    if foreign_holder is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "holder": foreign_holder,
                "remaining_min": operator_lock.remaining_min(msg_id),
            },
        )
    return {
        "holder": actor,
        "remaining_min": operator_lock.remaining_min(msg_id),
    }


@router.post("/messages/{msg_id}/lock/release", status_code=204)
def ma_lock_release(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> None:
    """Снять СВОЙ лок. Лок другого оператора не трогаем (204 всё равно — экран просто уходит)."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    if not telegram_bot._release_lock(msg_id, actor_from_user(user)):
        logger.info("lock/release msg=%s: лок держит другой оператор — не снимаем", msg_id)
    return None


@router.post("/messages/{msg_id}/lock/release-beacon", status_code=204)
async def ma_lock_release_beacon(msg_id: int, request: Request) -> None:
    """Release lock через navigator.sendBeacon на pagehide (закрытие MA).

    sendBeacon не умеет ставить заголовки, поэтому initData приходит в теле
    запроса (text/plain), а не в X-Telegram-Init-Data. Валидируем ту же
    строку тем же HMAC-путём — endpoint остаётся авторизованным (иначе любой
    мог бы снимать чужие локи на публичном тоннеле). Оператор снимает СВОЙ
    лок при закрытии — раньше beacon бил в /lock/release без заголовка и
    ловил 422, лок висел до auto-expire (5 мин), блокируя других операторов.
    """
    raw = (await request.body()).decode("utf-8", "replace").strip()
    # HMAC + чтение настроек и БД — не в event loop
    user = await run_in_threadpool(verify_init_data, raw)  # 401/403 если протух/чужой — ответ beacon клиент игнорит
    # Только свой лок: закрытие MA у одного оператора не снимает лок другого
    await run_in_threadpool(telegram_bot._release_lock, msg_id, actor_from_user(user))
    return None


def _claim_or_409(msg_id: int, actor: str) -> None:
    """Взять лок карточки атомарно; занят другим оператором — 409 с его именем."""
    foreign = telegram_bot._claim_lock(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})


# ── Языковой guard отправки ──────────────────────────────────────────────
# Сообщение клиенту должно уходить ТОЛЬКО на языке клиента (как правило DE).
# Типичная ошибка: оператор печатает русский текст во вкладку «DE → клиенту»
# и жмёт отправить — клиент получает кириллицу. Кириллический текст при
# не-кириллическом client_lang почти наверняка ошибка → блокируем без force.
_CYRILLIC_CLIENT_LANGS = {"ru", "uk", "be", "bg", "sr", "mk"}


def _mostly_cyrillic(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cyr = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return cyr / len(letters) > 0.3


def _wrong_lang_for_client(text: str, client_lang: "str | None") -> bool:
    lang = (client_lang or "de").lower()
    if lang in _CYRILLIC_CLIENT_LANGS:
        return False
    return _mostly_cyrillic(text)


class SendBody(BaseModel):
    # 'as_is' — отправить текст как есть; 'translate' — перевести на язык клиента и отправить
    mode: str = "as_is"
    # для mode='translate': актуальный текст из формы (иначе берётся de_answer из БД)
    text: "str | None" = Field(None, max_length=4000)
    # явное подтверждение оператора «отправить как есть» несмотря на языковой mismatch
    force: bool = False
    # >0 — отложенная отправка: окно «Отменить» в MA; по таймеру — обычная отправка
    delay_sec: int = Field(0, ge=0, le=60)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        if v not in {"as_is", "translate"}:
            raise ValueError("mode must be 'as_is' or 'translate'")
        return v


@router.post("/messages/{msg_id}/send")
def ma_send(msg_id: int, body: "SendBody | None" = None,
            user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    # Повторная отправка уже ушедшего письма (в т.ч. строки, испорченные до фикса №45)
    if _already_sent(row):
        raise HTTPException(409, f"Письмо по этому сообщению уже отправлено ({row['status']}) — "
                                 "повторно не шлём. Для нового ответа используйте «Написать».")
    if row["status"] == SEND_QUEUED:
        raise HTTPException(409, "Письмо уже в очереди на отправку")
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    b = body or SendBody()
    client_lang = (row["client_lang"] if "client_lang" in row.keys() else None) or "de"

    if b.mode == "translate":
        # Оператор явно попросил перевести на язык клиента перед отправкой.
        src_text = (b.text or "").strip() or ((row["de_answer"] if "de_answer" in row.keys() else "") or "").strip()
        if not src_text:
            raise HTTPException(400, "нечего переводить: текст пуст")
        tr = claude.translate_only(src_text, target_lang=client_lang, source_lang="ru")
        translated = (tr.get("translation", "") if isinstance(tr, dict) else str(tr)).strip()
        if not translated:
            raise HTTPException(500, "перевод вернулся пустым — отправка отменена")
        if not b.force and _wrong_lang_for_client(translated, client_lang):
            raise HTTPException(409, f"Перевод, похоже, не на языке клиента ({client_lang.upper()}) — "
                                     "отправка отменена. Проверьте текст.")
        # Исходный текст оператора сохраняем как RU-зеркало для верификации
        db.update_message(msg_id, de_answer=translated, ru_answer=src_text,
                          ru_translation=src_text, status="edited")
    else:
        out_text = ((row["de_answer"] if "de_answer" in row.keys() else "") or "")
        if not b.force and _wrong_lang_for_client(out_text, client_lang):
            raise HTTPException(
                409,
                f"Текст, похоже, не на языке клиента ({client_lang.upper()}). "
                "Подтвердите отправку «как есть» или выберите «Перевести и отправить».",
            )

    if b.delay_sec > 0:
        return _enqueue_send(msg_id, actor, b.delay_sec)
    return _send_now(msg_id, actor)


def _send_now(msg_id: int, actor: str) -> "dict[str, Any]":
    """SMTP-отправка (все guard'ы пройдены вызывающим) + закрыть старые черновики треда +
    снять лок + урок в фоне. Ошибка SMTP → HTTPException(500)."""
    result = scheduler.send_one(msg_id)
    if result.get("kind") == "error":
        raise HTTPException(500, result.get("message", "send failed"))

    # Ответ клиенту закрывает БОЛЕЕ СТАРЫЕ pending-черновики этого треда: они
    # относятся к предыдущим письмам клиента и уже неактуальны. Иначе сразу после
    # отправки экран показывает следующий устаревший черновик с активной кнопкой
    # «Отправить», и оператор думает, что письмо не ушло (инцидент 2026-07-20).
    # Более новые входящие (id > msg_id) не трогаем — это неотвеченные вопросы.
    # Та же логика, что в /threads/{id}/compose.
    fresh = db.get_message(msg_id)
    thread_id = (fresh["gmail_thread_id"] if fresh is not None else None) or None
    closed_drafts: list[int] = []
    if thread_id:
        with db.get_conn() as conn:
            stale = conn.execute(
                "SELECT id FROM messages WHERE gmail_thread_id=? AND direction='in' "
                "AND id < ? AND status IN ('pending','new','edited','approved')",
                (thread_id, msg_id),
            ).fetchall()
            closed_drafts = [r["id"] for r in stale]
            if closed_drafts:
                conn.execute(
                    "UPDATE messages SET status='skipped' WHERE gmail_thread_id=? "
                    "AND direction='in' AND id < ? "
                    "AND status IN ('pending','new','edited','approved')",
                    (thread_id, msg_id),
                )

    telegram_bot._release_lock(msg_id, actor)
    invalidate_pipeline_cache()  # таймер отложенной отправки — не HTTP POST, middleware кэш не сбросит

    status = fresh["status"] if fresh else "sent"
    if status in ("sent", "sent_debug"):
        # Оператор переписал черновик LLM → урок для следующих ответов (Sonnet, в фоне; ошибка — в лог)
        _LEARN_POOL.submit(_learn_from_sent, msg_id)
    # kind/mode — из результата send_one (sent+production / sent+redirect / skipped+disabled …):
    # фронт (utils.sendOutcome) честно пишет «отправлено» / «тестовый адрес» / «отправка выключена»
    return {"ok": True, "status": status, "closed_drafts": closed_drafts,
            "kind": result.get("kind"), "mode": result.get("mode")}


# ── Отложенная отправка ───────────────────────────────────────────────────
# Строка ждёт в status='send_queued' (её нельзя править/перегенерировать/слать повторно);
# job id = msg_id. Срок и прежний статус — в settings['ma_send_queue'] (JSON), чтобы
# очередь пережила рестарт: resume_send_queue() из старта приложения (web/app.py).
SEND_QUEUED = "send_queued"
SEND_QUEUE_KEY = "ma_send_queue"
SEND_QUEUE_MAX_LATE_SEC = 600  # после рестарта: просрочено сильнее — не шлём, возвращаем в черновики
_send_queue: dict[int, dict[str, Any]] = {}  # msg_id -> {due, prev, actor, timer}
_send_queue_lock = threading.RLock()


def _persist_send_queue() -> None:
    data = {str(k): {kk: v[kk] for kk in ("due", "prev", "actor")} for k, v in _send_queue.items()}
    db.set_setting(SEND_QUEUE_KEY, json.dumps(data))


def _schedule_send(msg_id: int, due: float, prev: str, actor: str) -> None:
    timer = threading.Timer(max(0.0, due - time.time()), _fire_queued_send, args=(msg_id,))
    timer.daemon = True
    _send_queue[msg_id] = {"due": due, "prev": prev, "actor": actor, "timer": timer}
    timer.start()


def _enqueue_send(msg_id: int, actor: str, delay_sec: int) -> "dict[str, Any]":
    row = db.get_message(msg_id)
    prev = row["status"]
    # По таймеру строка вернётся в prev и уйдёт через send_one. 'approved' подхватил бы ещё и
    # джоб send_approved_replies → дубль; поэтому в очередь из approved встаём как edited.
    back = "edited" if prev == "approved" else prev
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE messages SET status=? WHERE id=? AND status=?",
                           (SEND_QUEUED, msg_id, prev))
    if cur.rowcount == 0:
        raise HTTPException(409, "Статус письма изменился — обновите экран")
    due = time.time() + delay_sec
    try:
        with _send_queue_lock:
            _schedule_send(msg_id, due, back, actor)
            _persist_send_queue()
    except Exception:
        logger.exception("отложенная отправка msg=%s не поставлена — возвращаю черновик", msg_id)
        with _send_queue_lock:
            job = _send_queue.pop(msg_id, None)
        if job:
            job["timer"].cancel()
        _return_to_draft(msg_id, back)
        raise HTTPException(500, "Не удалось поставить письмо в очередь — ничего не отправлено, попробуйте ещё раз")
    logger.info("отложенная отправка msg=%s через %d с (%s)", msg_id, delay_sec, actor)
    return {"ok": True, "queued": True, "job_id": msg_id, "status": SEND_QUEUED,
            "send_at": datetime.utcfromtimestamp(due).isoformat(timespec="seconds")}


def _return_to_draft(msg_id: int, prev: str) -> bool:
    """send_queued → прежний статус. False — строка уже не в очереди (ушла / изменена)."""
    with db.get_conn() as conn:
        cur = conn.execute("UPDATE messages SET status=? WHERE id=? AND status=?",
                           (prev, msg_id, SEND_QUEUED))
    return cur.rowcount > 0


def _alert_send_failed(msg_id: int, detail: str) -> None:
    logger.error("отложенная отправка msg=%s не удалась: %s", msg_id, detail)
    telegram_bot.run_in_background(
        telegram_bot.notify,
        f"🟥 <b>Письмо не отправлено</b> (отложенная отправка): {telegram_bot._html(detail)[:300]}",
        f"review_{msg_id}", "📋 Открыть", "send_failed", True)


def _fire_queued_send(msg_id: int) -> None:
    """Таймер: отправить, если не отменили. Любой сбой — письмо возвращается в черновик (не застревает
    в send_queued) + ERROR + уведомление операторам."""
    job = None
    try:
        with _send_queue_lock:
            job = _send_queue.pop(msg_id, None)
            if job is None:
                return  # отменили
            if not _return_to_draft(msg_id, job["prev"]):
                _persist_send_queue()
                _alert_send_failed(msg_id, "письмо изменили, пока оно ждало отправки — не отправлено, "
                                           "проверьте и отправьте ещё раз")
                return
            _persist_send_queue()
        row = db.get_message(msg_id)
        if row is None or _already_sent(row):
            _alert_send_failed(msg_id, "письмо уже отмечено отправленным — повторно не шлю, проверьте тред")
            return
        _send_now(msg_id, job["actor"])
        logger.info("отложенная отправка msg=%s выполнена", msg_id)
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
        if job is not None:
            try:
                _return_to_draft(msg_id, job["prev"])  # если сбой случился ещё в очереди
            except Exception:
                logger.exception("отложенная отправка msg=%s: не вернул в черновик", msg_id)
        _alert_send_failed(msg_id, str(detail))


@router.post("/send/{job_id}/cancel")
def ma_send_cancel(job_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Отменить отложенную отправку (job_id = msg_id). Уже ушло — 409."""
    with _send_queue_lock:
        job = _send_queue.pop(job_id, None)
        if job is None:
            raise HTTPException(409, "Отменить нельзя: письмо уже отправляется или отправлено")
        job["timer"].cancel()
        _persist_send_queue()
        if not _return_to_draft(job_id, job["prev"]):
            raise HTTPException(409, "Отменить нельзя: письмо уже отправляется или отправлено")
    logger.info("отложенная отправка msg=%s отменена (%s)", job_id, actor_from_user(user))
    return {"ok": True, "cancelled": True, "status": job["prev"]}


def resume_send_queue() -> None:
    """При старте: письма, оставшиеся в очереди. Срок ещё не вышел или вышел недавно — отправить
    по расписанию; давно просрочены (сервис лежал) — вернуть в черновики и сообщить операторам."""
    raw = config.get(SEND_QUEUE_KEY) or "{}"
    try:
        saved = {int(k): v for k, v in json.loads(raw).items()}
    except (ValueError, AttributeError):
        logger.error("очередь отложенной отправки нечитаема: %r — все send_queued вернутся в черновики", raw[:200])
        saved = {}
    with db.get_conn() as conn:
        queued = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE status=?", (SEND_QUEUED,)).fetchall()]
    now = time.time()
    returned: list[int] = []
    with _send_queue_lock:
        for msg_id in queued:
            job = saved.get(msg_id)
            if job and now - float(job["due"]) <= SEND_QUEUE_MAX_LATE_SEC:
                _schedule_send(msg_id, float(job["due"]), job["prev"], job["actor"])
            else:
                _return_to_draft(msg_id, (job or {}).get("prev") or "edited")
                returned.append(msg_id)
        _persist_send_queue()
    if queued:
        logger.info("очередь отправки после старта: продолжено %d, возвращено в черновики %s",
                    len(queued) - len(returned), returned)
    if returned:
        telegram_bot.run_in_background(
            telegram_bot.notify,
            f"⚠️ <b>После перезапуска не отправлено писем: {len(returned)}</b> — вернул в черновики, "
            "отправьте ещё раз.", "pipeline", "📥 Входящие", "send_requeue")


# Свой однопоточный пул: Sonnet через CLI — десятки секунд, не занимать пул Telegram-уведомлений
_LEARN_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-learn")


def _learn_from_sent(msg_id: int) -> None:
    from modules import llm_memory
    try:
        llm_memory.learn_from_sent(msg_id)
    except Exception:
        logger.exception("learn_from_sent msg=%s упал — урок не извлечён", msg_id)


class TranslateDraftBody(BaseModel):
    # Текст для перевода (обычно RU-идея оператора); если пуст — берётся de_answer из БД
    text: "str | None" = Field(None, max_length=4000)


@router.post("/messages/{msg_id}/translate-draft")
def ma_translate_draft(msg_id: int, body: "TranslateDraftBody | None" = None,
                       user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Перевести черновик на язык клиента БЕЗ отправки (шаг send-флоу «Перевести и отправить»).

    Перевод сохраняется в de_answer, оператор видит финальный текст в превью
    и отправляет уже обычным /send mode=as_is — языковой guard там сработает штатно.
    """
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    b = body or TranslateDraftBody()
    client_lang = (row["client_lang"] if "client_lang" in row.keys() else None) or "de"
    src_text = (b.text or "").strip() or ((row["de_answer"] if "de_answer" in row.keys() else "") or "").strip()
    if not src_text:
        raise HTTPException(400, "нечего переводить: текст пуст")

    source_lang = "ru" if _mostly_cyrillic(src_text) else None
    tr = claude.translate_only(src_text, target_lang=client_lang, source_lang=source_lang)
    translated = (tr.get("translation", "") if isinstance(tr, dict) else str(tr)).strip()
    if not translated:
        raise HTTPException(500, "перевод вернулся пустым")

    fields: "dict[str, Any]" = {"de_answer": translated, "status": "edited"}
    if source_lang == "ru":
        # RU-исходник оператора сохраняем как зеркало для верификации
        fields["ru_answer"] = src_text
        fields["ru_translation"] = src_text
    db.update_message(msg_id, **fields)
    return _message_review_dict(msg_id)


class LangCheckBody(BaseModel):
    # Текст для проверки; если пуст — берётся de_answer из БД
    text: "str | None" = Field(None, max_length=4000)


@router.post("/messages/{msg_id}/lang-check")
def ma_lang_check(msg_id: int, body: "LangCheckBody | None" = None,
                  user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """LLM-проверка (Haiku) языка финального текста перед отправкой.

    Ловит несовпадения, которые кириллическая эвристика не видит (например,
    английский текст немецкому клиенту). Read-only: ничего не пишет и не шлёт.
    match=true также при lang='und' (языково-нейтральный текст — не блокируем).
    """
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")

    b = body or LangCheckBody()
    text = (b.text or "").strip() or ((row["de_answer"] if "de_answer" in row.keys() else "") or "").strip()
    if not text:
        raise HTTPException(400, "нечего проверять: текст пуст")
    client_lang = ((row["client_lang"] if "client_lang" in row.keys() else None) or "de").lower()

    try:
        res = claude.detect_text_lang(text)
    except Exception as e:
        # 502 — фронт покажет «проверка недоступна», но НЕ разблокирует чужой язык:
        # кириллическая эвристика и серверный guard в /send остаются
        raise HTTPException(502, f"проверка языка недоступна: {e}")

    detected = (res.get("lang") or "und").lower()
    match = detected == "und" or detected == client_lang
    return {"detected_lang": detected, "client_lang": client_lang, "match": match}


@router.post("/messages/{msg_id}/skip")
def ma_skip(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    if db.get_message(msg_id)["status"] == SEND_QUEUED:
        raise HTTPException(409, "Письмо в очереди на отправку — сначала нажмите «Отменить»")
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    db.update_message(msg_id, status="skipped")
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped"}


class SoldBody(BaseModel):
    price_eur: float = Field(..., gt=0, lt=1000000)
    close_other_threads_for_ad: bool = False


class ThreadSoldBody(BaseModel):
    # Цена обязательна (склад/прибыль); фронт предзаполняет из GET /threads/{id}/sale-price
    price_eur: float = Field(..., gt=0, lt=1000000)
    note: str = Field("", max_length=500)
    close_other_threads_for_ad: bool = False


@router.post("/threads/{thread_id}/sold")
def ma_thread_sold(thread_id: str, body: ThreadSoldBody,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """«Продано» на любом треде — с черновиком или без (QA S-01). Цена обязательна; товар
    списывается со склада (stock в ответе; product_id=None — товар не найден, привязать)."""
    b = body
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    latest_in = next((r for r in reversed(history) if r["direction"] == "in"), None)
    if latest_in is None:
        raise HTTPException(404, "no incoming message in thread")
    price = b.price_eur
    _ensure_nothing_queued(thread_id)
    msg_id = latest_in["id"]
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)
    try:
        if _already_sent(latest_in):
            # Последнее письмо уже отвечено: mark_thread_sold перевёл бы НАШ отправленный ответ
            # в skipped_sold (он пропал бы из ленты, «ответили», напоминаний). Пока db не умеет
            # отдельную запись продажи по треду — продажа отдельной строкой + тред закрыт.
            # TODO(api): перейти на db-функцию «продажа по треду без смены статусов», когда появится
            sale = db.add_manual_sale(
                account_id=latest_in["account_id"], ad_title=latest_in["ad_title"] or "(без названия)",
                ad_price=latest_in["ad_price"], sold_price_eur=float(price),
                sold_at=datetime.utcnow().isoformat(),
                buyer_name=latest_in["buyer_display_name"] or latest_in["buyer_name"],
                notes=(f"тред {thread_id}" + (f"; {b.note.strip()}" if b.note.strip() else "")))
            db.close_thread(thread_id, closed_by=actor)
            closed_other: list[str] = []
            sale_ref = sale
        else:
            result = db.mark_thread_sold(msg_id, sold_price_eur=float(price),
                                         close_other_threads_for_ad=b.close_other_threads_for_ad)
            sale_ref = None
            closed_other = result["closed_other_threads"]
            if b.note.strip():
                prev = (latest_in["extra_notes"] if "extra_notes" in latest_in.keys() else None) or ""
                db.update_message(msg_id, extra_notes=(prev + "\n" if prev else "") + f"Продано: {b.note.strip()}")
    finally:
        telegram_bot._release_lock(msg_id, actor)
    if sale_ref:
        # Продажа отдельной строкой (тред уже отвечен) живёт под своим тредом manual-… — склад пишем под ним
        # и с source='manual', как его увидит sync_sales (иначе джоб списал бы товар второй раз)
        stock = _record_stock_sale(price=float(price), thread_id=sale_ref["thread_id"], msg_row=latest_in,
                                   source="manual", actor=actor)
    else:
        # db.mark_thread_sold уже списал по ad_id; повтор идемпотентен по треду, но с заголовком
        # находит товар и без привязки объявления, и отдаёт product_id=None («привяжите»)
        stock = _record_stock_sale(price=float(price), thread_id=thread_id, msg_row=latest_in,
                                   source="thread", actor=actor) or result.get("stock")
    return {"ok": True, "status": "skipped_sold", "sold_price_eur": float(price),
            "msg_id": msg_id, "closed_other_threads": closed_other, "separate_sale": sale_ref,
            "stock": stock, **_others_kept(b.close_other_threads_for_ad, closed_other, stock)}


def _others_kept(requested: bool, closed_other: list, stock: "dict | None") -> "dict[str, Any]":
    """Просили закрыть другие треды по объявлению, но db не закрыла: товар ещё есть —
    фронт показывает «на складе ещё N» (None — остаток не ведётся / неизвестен)."""
    if not requested or closed_other:
        return {"others_kept_stock_left": None}
    left = stock.get("qty_after") if stock and stock.get("stock_tracked") else None
    return {"others_kept_stock_left": left}


def _record_stock_sale(*, price: float, thread_id: str, msg_row: Any, source: str, actor: str) -> "dict[str, Any] | None":
    """Списать товар со склада (идемпотентно по треду — если db уже сделала это в mark_thread_sold,
    второй раз не спишется). Сбой склада продажу не откатывает: ERROR в лог, джоб sync_all догонит."""
    from modules import inventory
    try:
        res = inventory.record_sale(
            price_eur=price, gmail_thread_id=thread_id,
            ad_id=msg_row["ad_id"] if "ad_id" in msg_row.keys() else None,
            ad_title=msg_row["ad_title"], message_id=msg_row["id"], source=source, created_by=actor)
    except Exception:
        logger.exception("склад: продажа треда %s не списана — догонит sync_all", thread_id)
        return None
    return {"product_id": res.get("product_id"), "qty_after": res.get("qty_after"),
            "stock_tracked": res.get("stock_tracked")}


@router.post("/messages/{msg_id}/sold")
def ma_sold(msg_id: int, body: SoldBody,
            user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    if db.get_message(msg_id)["status"] == SEND_QUEUED:
        raise HTTPException(409, "Письмо в очереди на отправку — сначала нажмите «Отменить»")
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    try:
        result = db.mark_thread_sold(
            msg_id,
            sold_price_eur=body.price_eur,
            close_other_threads_for_ad=body.close_other_threads_for_ad,
        )
    finally:
        telegram_bot._release_lock(msg_id, actor)
    sold_row = db.get_message(msg_id)
    stock = (_record_stock_sale(price=float(body.price_eur), thread_id=sold_row["gmail_thread_id"],
                                msg_row=sold_row, source="thread", actor=actor)
             if sold_row is not None and sold_row["gmail_thread_id"] else None) or result.get("stock")
    return {
        "ok": True,
        "status": "skipped_sold",
        "sold_price_eur": body.price_eur,
        "closed_other_threads": result["closed_other_threads"],
        "stock": stock,
        **_others_kept(body.close_other_threads_for_ad, result["closed_other_threads"], stock),
    }


def _run_regenerate(res: "dict[str, Any]") -> None:
    """Регенерация идёт через drafts.regenerate_draft* — с полным контекстом (история треда,
    бриф объявления, уроки) и сохранением deal_brief; здесь только проверка результата."""
    if res.get("kind") == "error":
        raise HTTPException(500, res.get("message", "regenerate failed"))


def _deal_brief_from(result: "dict[str, Any]") -> "dict[str, Any]":
    """deal_brief из ответа LLM: вложенный `deal_brief`, плоские deal_summary_ru/… — фолбэк."""
    nested = result.get("deal_brief")
    if isinstance(nested, dict) and nested:
        return nested
    return {
        "summary_ru": result.get("deal_summary_ru", ""),
        "expected_next": result.get("expected_next", ""),
        "negotiated_price_eur": result.get("negotiated_price_eur"),
        "client_assessment": result.get("client_assessment", ""),
    }


@router.post("/messages/{msg_id}/regenerate")
def ma_regenerate(msg_id: int, body: RegenerateBody,
                  user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Регенерировать draft под strategy. Intermediate — lock keeps."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    _run_regenerate(scheduler.regenerate_draft(msg_id, body.strategy))

    return _message_review_dict(msg_id)


# ──────────────────────── ЧИПЫ ЦЕНЫ (варианты ответа) ────────────────────────
# drafts.variants (llm): все тоны одним вызовом, цены считает код, ниже пола варианты отбрасываются.
# Кэш на (msg_id, версия треда); выбранный вариант применяет POST /variants/{tone}/apply.
_TONES = ("hold", "minus5", "floor", "decline")
_variant_cache: dict[tuple, dict[str, Any]] = {}
_variant_cache_lock = threading.Lock()
_VARIANT_CACHE_MAX = 100


def _suggest_call(fn, *args, **kwargs):
    """Функции подсказок llm: ошибки → понятные HTTP-коды с текстом для оператора."""
    from modules import llm
    try:
        return fn(*args, **kwargs)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except claude.SuggestError as e:  # нельзя по данным: письмо обработано, нет цены/факта
        raise HTTPException(409, str(e))
    except llm.LLMLimitError as e:
        raise HTTPException(503, str(e))
    except llm.LLMError as e:
        logger.warning("подсказка LLM не построена: %s", e)
        raise HTTPException(502, f"LLM не ответил: {e}")


class VariantsBody(BaseModel):
    tones: list[str] = Field(default_factory=lambda: list(_TONES), min_length=1, max_length=4)

    @field_validator("tones")
    @classmethod
    def _check_tones(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in _TONES]
        if bad:
            raise ValueError(f"неизвестные тоны {bad}; есть {list(_TONES)}")
        return list(dict.fromkeys(v))


def _thread_version(row: Any) -> tuple:
    """Версия треда для кэша: любое новое письмо/правка/отправка даёт другую версию."""
    hist = db.thread_history(row["gmail_thread_id"]) if row["gmail_thread_id"] else []
    return tuple((h["id"], h["status"], hash(h["de_answer"] or "")) for h in hist)


@router.post("/messages/{msg_id}/variants")
def ma_variants(msg_id: int, body: "VariantsBody | None" = None,
                user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Черновики под ценовые тоны (чипы «держу / −5 % / минимум / отказ»). В БД не пишет."""
    from modules import drafts
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    tones = (body or VariantsBody()).tones
    key = (msg_id, tuple(tones), _thread_version(row))
    with _variant_cache_lock:
        res = _variant_cache.get(key)
    cached = res is not None
    if res is None:
        res = _suggest_call(drafts.variants, msg_id, tones)
        with _variant_cache_lock:
            if len(_variant_cache) >= _VARIANT_CACHE_MAX:
                _variant_cache.pop(next(iter(_variant_cache)))
            _variant_cache[key] = res
    return {
        "msg_id": msg_id,
        "variants": [{"tone": v["tone"], "price_eur": v.get("price_eur"), "client_text": v["client_answer"],
                      "ru_text": v.get("ru_answer"), "ru_translation": v.get("ru_translation"),
                      "error": None, "cached": cached} for v in res.get("variants", [])],
        "dropped": res.get("dropped", []),
        "base_price_eur": res.get("base_price_eur"),
        "floor_eur": res.get("floor_eur"),
    }


@router.post("/messages/{msg_id}/variants/{tone}/apply")
def ma_variant_apply(msg_id: int, tone: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сделать выбранный вариант черновиком (как перегенерация: status edited, llm_draft_*)."""
    if tone not in _TONES:
        raise HTTPException(404, "unknown tone")
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    version = _thread_version(row)
    variant = None
    with _variant_cache_lock:
        for (mid, _tones, ver), res in reversed(list(_variant_cache.items())):
            if mid == msg_id and ver == version:
                variant = next((v for v in res.get("variants", []) if v["tone"] == tone), None)
                if variant:
                    break
    if variant is None:
        raise HTTPException(409, "Вариант устарел (в треде что-то изменилось) — запросите варианты заново")
    _claim_or_409(msg_id, actor_from_user(user))
    # Стоимость генерации уже учтена при запросе вариантов — здесь не прибавляем второй раз
    scheduler._save_regen_result(msg_id, row, {**variant, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0})
    return _message_review_dict(msg_id)


class EditTextBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


def _client_lang(row: Any) -> str:
    """Язык клиента строки; NULL/пусто → 'de' (иначе перевод уходил «никуда»)."""
    return ((row["client_lang"] if "client_lang" in row.keys() else None) or "de").lower()


def _translate_with_back(text: str, target_lang: str, *, context: "str | None" = None) -> "tuple[str, str]":
    """RU → язык клиента + обратный перевод на RU одним вызовом LLM."""
    res = claude.translate_with_back(text, target_lang, source_lang="ru", context=context)
    return (res.get("translation") or "", res.get("back_translation") or "")


@router.post("/messages/{msg_id}/edit-ru")
def ma_edit_ru(msg_id: int, body: EditTextBody,
               user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Operator edits RU answer. Forward+back translate."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    target_lang = _client_lang(row)
    de_text, ru_back = _translate_with_back(body.text, target_lang, context=row["ad_title"])
    if not de_text.strip():
        raise HTTPException(502, "перевод вернулся пустым — черновик не изменён")
    db.update_message(msg_id, ru_answer=body.text, de_answer=de_text,
                      ru_translation=ru_back, status="edited")
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/edit-de")
def ma_edit_de(msg_id: int, body: EditTextBody,
               user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Operator edits DE answer directly. Back-translate for verification."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    source_lang = _client_lang(row)
    ru_res = claude.translate_only(body.text, target_lang="ru", source_lang=source_lang)
    ru_back = ru_res.get("translation", "") if isinstance(ru_res, dict) else str(ru_res)
    db.update_message(msg_id, de_answer=body.text, ru_translation=ru_back,
                      status="edited")
    return _message_review_dict(msg_id)


class PriceBody(BaseModel):
    eur: float = Field(..., gt=0, lt=100000)


class InstructionBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


@router.post("/messages/{msg_id}/price")
def ma_price(msg_id: int, body: PriceBody,
             user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать с конкретной ценой от оператора."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    _run_regenerate(scheduler.regenerate_draft_with_price(msg_id, body.eur))
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/instruction")
def ma_instruction(msg_id: int, body: InstructionBody,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать со свободной инструкцией."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    _ensure_draft_editable(row)
    actor = actor_from_user(user)
    _claim_or_409(msg_id, actor)

    _run_regenerate(scheduler.regenerate_draft_with_instruction(msg_id, body.text))
    return _message_review_dict(msg_id)


class ComposeBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class ComposeSendBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000, description="ru_text из ответа /compose-preview")
    final_text: str = Field(..., min_length=1, max_length=4000, description="итоговый текст клиенту — из превью, возможно отредактированный оператором")
    target_lang: str = Field(..., min_length=2, max_length=5, description="target_lang из ответа /compose-preview")


@router.post("/threads/{thread_id}/compose")
def ma_compose(thread_id: str, body: ComposeSendBody,
               user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Operator-initiated message in thread (compose mode).

    Требует, чтобы оператор сначала вызвал /compose-preview и подтвердил (возможно
    отредактировав) итоговый текст — final_text уходит клиенту КАК ЕСТЬ, без повторного
    перевода. Это осознанное решение после инцидента 2026-07-02 (см. modules/outgoing.py).
    """
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    source_msg_id = history[-1]["id"]
    _ensure_nothing_queued(thread_id)

    result = scheduler.send_manual_compose(source_msg_id, body.text, body.final_text, body.target_lang)
    if result.get("kind") == "error":
        raise HTTPException(500, result.get("message", "compose failed"))

    # Compose уже отправил клиенту — pending drafts в этом треде больше не нужны.
    # Без этого они висят в pipeline как «есть готовый черновик» бесконечно.
    closed_drafts: list[int] = []
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE gmail_thread_id=? AND direction='in' "
            "AND status IN ('pending','new','edited','approved')",
            (thread_id,),
        ).fetchall()
        closed_drafts = [r["id"] for r in rows]
        if closed_drafts:
            conn.execute(
                "UPDATE messages SET status='skipped' WHERE gmail_thread_id=? "
                "AND direction='in' AND status IN ('pending','new','edited','approved')",
                (thread_id,),
            )


    return {
        "ok": True,
        "sent_msg_id": result.get("message_id"),
        "closed_drafts": closed_drafts,
        "thread_id": thread_id,
        "kind": result.get("kind"),
        "mode": result.get("mode"),
    }



class AutopilotStartBody(BaseModel):
    floor_eur: float = Field(..., gt=0, lt=100000)
    notify_mode: str
    preview: dict[str, Any] | None = None  # optional preview to apply before start

    @field_validator("notify_mode")
    @classmethod
    def _check_notify(cls, v: str) -> str:
        if v not in {"silent", "notify"}:
            raise ValueError("notify_mode must be 'silent' or 'notify'")
        return v


@router.post("/threads/{thread_id}/autopilot/start")
def ma_autopilot_start(thread_id: str, body: AutopilotStartBody,
                       user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить автопилот для треда. Если preview передан — применяется к latest in-row."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    actor = actor_from_user(user)

    # Find latest direction='in' row
    latest_in = None
    for r in reversed(history):
        if r["direction"] == "in":
            latest_in = r
            break
    msg_id = latest_in["id"] if latest_in else history[-1]["id"]

    # Если preview передан — применяем как approved-draft
    if body.preview and latest_in:
        preview = body.preview
        update_fields: dict[str, Any] = {"status": "approved"}
        if "ru_text" in preview:
            update_fields["ru_answer"] = preview["ru_text"]
        if "client_text" in preview:
            update_fields["de_answer"] = preview["client_text"]
        if "ru_translation" in preview:
            update_fields["ru_translation"] = preview["ru_translation"]
        if "deal_brief" in preview and isinstance(preview["deal_brief"], dict):
            update_fields["deal_brief_json"] = json.dumps(preview["deal_brief"], ensure_ascii=False)
        db.update_message(msg_id, **update_fields)

    db.start_thread_autopilot(thread_id, body.floor_eur, body.notify_mode, actor)

    if body.notify_mode == "notify":
        # Telegram — в фоне: ответ MA не ждёт сеть, ошибка уходит в лог
        telegram_bot.run_in_background(
            telegram_bot.send_autopilot_start_notification, msg_id, body.floor_eur, actor)

    return _thread_dict(thread_id)


class AutopilotPreviewBody(BaseModel):
    floor_eur: float = Field(..., gt=0, lt=100000)
    notify_mode: str

    @field_validator("notify_mode")
    @classmethod
    def _check_notify_preview(cls, v: str) -> str:
        if v not in {"silent", "notify"}:
            raise ValueError("notify_mode must be 'silent' or 'notify'")
        return v


def _build_regen_context(msg_row: Any) -> tuple[Any, str, Any]:
    """Minimal context loader for autopilot preview. Falls back to empty on any error."""
    try:
        from scheduler import _regen_context
        return _regen_context(msg_row)
    except Exception:
        # Превью без контекста хуже, но лучше, чем 500 — только не молча
        logger.exception("autopilot preview: контекст треда не собран, msg=%s", msg_row["id"])
        return [], "", []


@router.post("/threads/{thread_id}/autopilot/preview")
def ma_autopilot_preview(thread_id: str, body: AutopilotPreviewBody,
                         user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сгенерировать preview первого автопилот-ответа БЕЗ записи в БД."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    latest_in = None
    for r in reversed(history):
        if r["direction"] == "in":
            latest_in = r
            break
    if latest_in is None:
        raise HTTPException(404, "no incoming message in thread")

    history_ctx, brief_text, lessons = _build_regen_context(latest_in)


    def _get_field(row, key):
        try:
            return row[key] if key in row.keys() else ""
        except Exception:
            return ""

    try:
        result = claude.generate_autopilot_reply(
            de_client_text=_get_field(latest_in, "de_client"),
            ad_title=_get_field(latest_in, "ad_title"),
            ad_price=_get_field(latest_in, "ad_price"),
            ad_description=_get_field(latest_in, "ad_description"),
            seller_name=_get_field(latest_in, "seller_name"),
            history=history_ctx,
            brief_text=brief_text,
            lessons=lessons,
            floor_eur=body.floor_eur,
            last_our_price_eur=None,
        )
    except Exception as e:
        raise HTTPException(500, f"preview generation failed: {e}")

    return {
        "preview": {
            "ru_text": result.get("ru_answer", ""),
            "client_text": result.get("client_answer", ""),
            "ru_translation": result.get("ru_translation", ""),
            "deal_brief": _deal_brief_from(result),
            "should_stop": result.get("should_stop", False),
            "stop_reason": result.get("stop_reason"),
            "used_web_search": result.get("used_web_search", False),
        }
    }


@router.post("/threads/{thread_id}/autopilot/stop")
def ma_autopilot_stop(thread_id: str,
                      user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Остановить автопилот (manual stop)."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    db.stop_thread_autopilot(thread_id, "manual")
    return _thread_dict(thread_id)


class WaitBody(BaseModel):
    # «Жду до…»: не напоминать про молчание клиента до этой даты (ISO) или N дней
    until: "str | None" = None
    days: "float | None" = Field(None, gt=0, le=60)


@router.post("/threads/{thread_id}/wait")
def ma_thread_wait(thread_id: str, body: "WaitBody | None" = None,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """«Ждём клиента» (тред уходит из «ждут нас»). С until/days — ещё и без напоминаний до срока."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    actor = actor_from_user(user)
    db.mark_thread_waiting(thread_id, marked_by=actor)
    b = body or WaitBody()
    until = None
    if b.days:
        until = (datetime.utcnow() + timedelta(days=b.days)).isoformat(timespec="seconds")
    elif b.until:
        try:
            until = datetime.fromisoformat(b.until.replace("Z", "")).isoformat(timespec="seconds")
        except ValueError:
            raise HTTPException(400, "until: ожидается дата ISO, например 2026-10-25T10:00")
    if until:
        # find_reminder_candidates смотрит reminder_snooze_until строки последнего ответа — ставим всем строкам треда
        with db.get_conn() as conn:
            conn.execute("UPDATE messages SET reminder_snooze_until=? WHERE gmail_thread_id=?", (until, thread_id))
    data = _thread_dict(thread_id)
    data["wait_until"] = until
    return data


def _preview_from_llm(res: "dict[str, Any]", **extra: Any) -> "dict[str, Any]":
    """Ответ подсказки llm в формате /compose-preview (отправка — обычным /compose)."""
    return {"translated": res["client_answer"], "back_ru": res.get("ru_translation") or "",
            "target_lang": res.get("client_lang") or "de",
            "ru_text": res.get("ru_answer") or res.get("ru_translation") or "", **extra}


@router.post("/threads/{thread_id}/followup")
def ma_thread_followup(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Напоминание молчащему покупателю (llm, с историей треда): текст на его языке, не отправляет."""
    from modules import drafts
    res = _suggest_call(drafts.followup_ping, thread_id)
    return _preview_from_llm(res, days_silent=res.get("days_silent"), anchor_msg_id=res.get("anchor_msg_id"))


# ── Быстрые ответы: справка аккаунта (доставка, самовывоз, оплата) ──
_QUICK_INFO_KEYS = ("delivery", "pickup", "payment")
_QUICK_KINDS = ("available", "last_price", "delivery", "pickup", "payment")


def _quick_key(account_id: int) -> str:
    return f"account_quick_{account_id}"


def _quick_info(account_id: int) -> dict[str, str]:
    raw = config.get(_quick_key(account_id)) or "{}"
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("не объект")
    except ValueError as e:
        logger.error("справка быстрых ответов аккаунта %s повреждена (%s): %r", account_id, e, raw[:200])
        raise HTTPException(500, "Справка быстрых ответов повреждена — заполните её заново в «Аккаунты»")
    return {k: str(data.get(k) or "") for k in _QUICK_INFO_KEYS}


class QuickInfoBody(BaseModel):
    delivery: "str | None" = Field(None, max_length=500)  # «DHL 10 €, 2–3 дня»
    pickup: "str | None" = Field(None, max_length=300)    # «Köln-Lindenthal, вечером после 18»
    payment: "str | None" = Field(None, max_length=300)   # «наличными или PayPal»


@router.get("/accounts/{account_id}/quick-info")
def ma_quick_info_get(account_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, str]:
    if db.get_account(account_id) is None:
        raise HTTPException(404, "account not found")
    return _quick_info(account_id)


@router.post("/accounts/{account_id}/quick-info")
def ma_quick_info_set(account_id: int, body: QuickInfoBody,
                      user: dict = Depends(verify_init_data_dep)) -> dict[str, str]:
    """Факты продавца для быстрых ответов (по-русски; LLM формулирует на языке покупателя)."""
    if db.get_account(account_id) is None:
        raise HTTPException(404, "account not found")
    try:
        info = _quick_info(account_id)
    except HTTPException:
        info = {k: "" for k in _QUICK_INFO_KEYS}  # повреждена (ERROR уже в логе) — сохранение её чинит
    for k in _QUICK_INFO_KEYS:
        v = getattr(body, k)
        if v is not None:
            info[k] = v.strip()
    db.set_setting(_quick_key(account_id), json.dumps(info, ensure_ascii=False))
    return info


class QuickBody(BaseModel):
    kind: str

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in _QUICK_KINDS:
            raise ValueError(f"kind: один из {list(_QUICK_KINDS)}")
        return v


@router.post("/messages/{msg_id}/quick")
def ma_quick(msg_id: int, body: QuickBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Быстрый ответ (есть в наличии / последняя цена / доставка / самовывоз / оплата) на языке
    покупателя (llm, Haiku). Ничего не пишет: фронт вставляет в черновик или шлёт через /compose."""
    from modules import drafts
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    res = _suggest_call(drafts.quick_reply, msg_id, body.kind, _quick_info(row["account_id"]))
    return _preview_from_llm(res, kind=body.kind)


@router.post("/threads/{thread_id}/suggest-reply")
def ma_suggest_reply(thread_id: str,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сгенерировать свежий draft для последнего incoming в треде."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    # Find latest direction='in' row
    latest_in = None
    for r in reversed(history):
        if r["direction"] == "in":
            latest_in = r
            break
    if latest_in is None:
        raise HTTPException(404, "no incoming message in thread")
    # Последнее письмо клиента уже отвечено — черновик писать некуда: перегенерация этой
    # строки затёрла бы отправленный ответ. Новое сообщение — через «Написать» (compose).
    if _is_answered(latest_in):
        raise HTTPException(
            409, "Последнее письмо клиента уже отвечено — нового черновика нет. "
                 "Для нового сообщения используйте «Написать».")

    msg_id = latest_in["id"]
    _claim_or_409(msg_id, actor_from_user(user))  # не перезаписывать черновик, который правит другой
    result = scheduler.regenerate_draft(msg_id, "regen")
    if result.get("kind") == "error":
        telegram_bot._release_lock(msg_id)  # не держать карточку 5 мин после сбоя LLM
        raise HTTPException(500, result.get("message", "regenerate failed"))

    return _thread_dict(thread_id)


# ──────────────────────── SALES ────────────────────────

import re as _re
from datetime import datetime as _dt, timedelta as _td

_PRICE_RE = _re.compile(r"(\d+(?:[.,]\d{3})*(?:[.,]\d+)?)")


def _parse_listed_price_eur(raw: "str | None") -> "float | None":
    """Парсит messages.ad_price ('1500 €' / '1.500 €' / 'VB' / 'Auf Anfrage') в float.

    Возвращает None если число не извлекается.
    """
    if not raw:
        return None
    m = _PRICE_RE.search(str(raw))
    if not m:
        return None
    token = m.group(1)
    # «1.500» / «1,500» (тыс. разделитель) vs «99.99» / «99,99» (дроби).
    # Признак тысячных: ровно 3 цифры после разделителя и без второго разделителя.
    if "," in token and "." in token:
        # Самый «правый» разделитель — десятичный
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "." in token:
        right = token.split(".")[-1]
        if len(right) == 3:
            token = token.replace(".", "")
    elif "," in token:
        right = token.split(",")[-1]
        if len(right) == 3:
            token = token.replace(",", "")
        else:
            token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def _period_window(period: str, *, custom_from: "str | None", custom_to: "str | None") -> "tuple[str | None, str | None]":
    """ISO-границы для фильтра period. Возвращает (from_iso, to_iso) или (None, None) для all."""
    if period == "all":
        return (custom_from, custom_to)
    if period == "today":
        return berlin_day_bounds()  # те же границы, что «продано сегодня» в Обзоре
    now = _dt.utcnow()
    if period == "week":
        start = now - _td(days=now.weekday(), hours=now.hour, minutes=now.minute,
                          seconds=now.second, microseconds=now.microsecond)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "custom":
        return (custom_from, custom_to)
    else:
        return (None, None)
    return (start.isoformat(), None)


def _bucket_label(iso_ts: str, granularity: str) -> str:
    """ISO timestamp → бакет-label по granularity (day/week/month/year)."""
    try:
        d = _dt.fromisoformat(iso_ts.replace("Z", "+00:00").split("+")[0])
    except Exception:
        return "?"
    if granularity == "day":
        return d.strftime("%Y-%m-%d")
    if granularity == "week":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if granularity == "month":
        return d.strftime("%Y-%m")
    if granularity == "year":
        return d.strftime("%Y")
    return d.strftime("%Y-%m-%d")


@router.get("/sales")
def ma_sales(
    period: str = "all",
    from_: "str | None" = None,
    to_: "str | None" = None,
    account_id: "int | None" = None,
    q: "str | None" = None,
    group_by: str = "month",
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Все продажи (in-one-pot из всех аккаунтов) + сводка + breakdown по периоду."""
    period = period if period in {"all", "today", "week", "month", "year", "custom"} else "all"
    group_by = group_by if group_by in {"day", "week", "month", "year"} else "month"
    period_from, period_to = _period_window(period, custom_from=from_, custom_to=to_)

    raw = db.list_sales(
        period_from=period_from, period_to=period_to,
        account_id=account_id, query=(q.strip() if q else None),
    )

    sales: list[dict[str, Any]] = []
    for r in raw:
        listed = _parse_listed_price_eur(r.get("ad_price"))
        sold = float(r["sold_price_eur"]) if r.get("sold_price_eur") is not None else None
        discount_eur = (listed - sold) if (listed is not None and sold is not None) else None
        discount_pct = ((discount_eur / listed) * 100.0) if (discount_eur is not None and listed) else None
        sales.append({
            "thread_id": r["thread_id"],
            "ad_id": r.get("ad_id"),
            "ad_title": r.get("ad_title") or "(без названия)",
            "ad_url": r.get("ad_url"),
            "ad_price_listed": r.get("ad_price"),
            "ad_price_listed_eur": listed,
            "buyer_display_name": r.get("buyer_display_name") or r.get("buyer_name"),
            "account_id": r.get("account_id"),
            "account_name": r.get("account_name") or "?",
            "sold_at": r.get("sold_at"),
            "sold_price_eur": sold,
            "discount_eur": round(discount_eur, 2) if discount_eur is not None else None,
            "discount_pct": round(discount_pct, 1) if discount_pct is not None else None,
        })

    # Summary
    prices = [s["sold_price_eur"] for s in sales if s["sold_price_eur"] is not None]
    summary = {
        "count": len(sales),
        "total_eur": round(sum(prices), 2) if prices else 0.0,
        "avg_eur": round(sum(prices) / len(prices), 2) if prices else 0.0,
        "max_eur": max(prices) if prices else 0.0,
        "min_eur": min(prices) if prices else 0.0,
    }

    # Breakdown
    buckets: dict[str, dict[str, Any]] = {}
    for s in sales:
        if not s.get("sold_at"):
            continue
        label = _bucket_label(s["sold_at"], group_by)
        b = buckets.setdefault(label, {
            "period_label": label, "count": 0, "total_eur": 0.0, "items": [],
        })
        b["count"] += 1
        if s["sold_price_eur"] is not None:
            b["total_eur"] += s["sold_price_eur"]
        b["items"].append({
            "ad_title": s.get("ad_title") or "(без названия)",
            "sold_price_eur": s.get("sold_price_eur"),
            "thread_id": s.get("thread_id"),
        })
    breakdown = sorted(buckets.values(), key=lambda x: x["period_label"], reverse=True)
    for b in breakdown:
        b["total_eur"] = round(b["total_eur"], 2)

    # Accounts list (для фильтра-дропдауна)
    with db.get_conn() as conn:
        accounts = [
            {"id": a["id"], "name": a["name"]}
            for a in conn.execute("SELECT id, name FROM accounts WHERE is_active=1 ORDER BY name").fetchall()
        ]

    return {
        "sales": sales,
        "summary": summary,
        "breakdown": breakdown,
        "accounts": accounts,
        "filters": {
            "period": period, "from": period_from, "to": period_to,
            "account_id": account_id, "q": q, "group_by": group_by,
        },
    }


# ──────────────────────── DETECTED SALES (LLM-сканер) ────────────────────────

_EVIDENCE_RU: dict[int, str] = {}  # перевод цитаты не меняется — кэш на процесс
_EVIDENCE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evidence-ru")


def _evidence_ru(evidence: "dict[int, str | None]") -> "dict[int, str | None]":
    """Цитаты-доказательства продаж по-русски (QA S-04). Кириллица — как есть; остальное —
    translate_only (Haiku) параллельно, с кэшем. Сбой перевода — None для этой цитаты + лог."""
    out: dict[int, "str | None"] = {}
    todo: dict[int, str] = {}
    for det_id, text in evidence.items():
        if not text or _mostly_cyrillic(text):
            out[det_id] = text
        elif det_id in _EVIDENCE_RU:
            out[det_id] = _EVIDENCE_RU[det_id]
        else:
            todo[det_id] = text

    def _tr(text: str) -> str:
        res = claude.translate_only(text, target_lang="ru", source_lang=None)
        out_text = ((res.get("translation") if isinstance(res, dict) else str(res)) or "").strip()
        if not out_text:
            raise RuntimeError("пустой перевод")  # не кэшируем пустое — повторим в следующий раз
        return out_text

    futures = {det_id: _EVIDENCE_POOL.submit(_tr, text) for det_id, text in todo.items()}
    for det_id, fut in futures.items():
        try:
            out[det_id] = _EVIDENCE_RU[det_id] = fut.result(timeout=60)
        except Exception:
            logger.exception("перевод цитаты найденной продажи #%s не удался", det_id)
            out[det_id] = None
    return out


@router.get("/detected-sales")
def ma_detected_sales(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Список pending detections для review-экрана."""
    rows = db.list_pending_detections()
    ru = _evidence_ru({r["id"]: r["evidence"] for r in rows})
    items: list[dict[str, Any]] = []
    for r in rows:
        ad_price_raw = r["thread_ad_price"]
        listed = _parse_listed_price_eur(ad_price_raw)
        sold = float(r["sold_price_eur"]) if r["sold_price_eur"] is not None else None
        discount_eur = (listed - sold) if (listed is not None and sold is not None) else None
        items.append({
            "id": r["id"],
            "thread_id": r["gmail_thread_id"],
            "confidence": r["confidence"],
            "sold_price_eur": sold,
            "detected_ad_title": r["detected_ad_title"],
            "detected_sold_at": r["detected_sold_at"],
            "evidence": r["evidence"],
            "evidence_ru": ru.get(r["id"]),
            # перевод не удался — фронт покажет оригинал с пометкой (None иначе неотличим от «нет цитаты»)
            "evidence_ru_failed": bool(r["evidence"]) and ru.get(r["id"]) is None,
            "thread_ad_title": r["thread_ad_title"] or "(без названия)",
            "thread_ad_price": ad_price_raw,
            "thread_ad_price_eur": listed,
            "discount_eur": round(discount_eur, 2) if discount_eur is not None else None,
            "buyer_display_name": r["buyer_display_name"] or r["buyer_name"],
            "account_name": r["account_name"] or "?",
        })
    return {"items": items, "count": len(items)}


class DetectionApplyBody(BaseModel):
    price_eur: float | None = Field(None, ge=0, lt=1000000)


@router.post("/detected-sales/{detection_id}/apply")
def ma_detection_apply(detection_id: int, body: DetectionApplyBody,
                       user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Применить detection — записывает в messages + close_thread."""
    try:
        applied = db.apply_detection_to_messages(detection_id, sold_price_eur=body.price_eur)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **applied}


class AcceptConfidentBody(BaseModel):
    # 'high' — только высокая уверенность; 'medium' — high + medium
    min_confidence: str = "high"

    @field_validator("min_confidence")
    @classmethod
    def _check_conf(cls, v: str) -> str:
        if v not in ("high", "medium"):
            raise ValueError("min_confidence: 'high' или 'medium'")
        return v


@router.post("/detected-sales/accept-confident")
def ma_detections_accept_confident(body: "AcceptConfidentBody | None" = None,
                                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Принять разом все найденные продажи с уверенностью не ниже порога (db: одна транзакция,
    неприменимые пропускаются с причиной)."""
    res = db.accept_detected_bulk((body or AcceptConfidentBody()).min_confidence)
    logger.info("accept-confident: принято %d, пропущено %d (оператор %s)",
                len(res["applied"]), len(res["skipped"]), actor_from_user(user))
    return {"ok": True, **res}


@router.post("/detected-sales/{detection_id}/reject", status_code=204)
def ma_detection_reject(detection_id: int,
                        user: dict = Depends(verify_init_data_dep)) -> None:
    db.reject_detection(detection_id)


class ManualSaleBody(BaseModel):
    account_id: int = Field(..., gt=0)
    ad_title: str = Field(..., min_length=1, max_length=200)
    ad_price: str | None = Field(None, max_length=50)
    sold_price_eur: float = Field(..., ge=0, lt=1000000)
    sold_at: str = Field(..., description="ISO date or datetime")
    buyer_name: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=500)


@router.post("/sales/manual")
def ma_sales_manual(body: ManualSaleBody,
                    user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Зафиксировать продажу совершённую ВНЕ бота (по телефону / лично).

    Создаёт synthetic messages-row с gmail_thread_id='manual-<uuid>'. Сделка
    появляется в Sales-экране наравне с обычными.
    """
    # Normalize sold_at: если только дата (YYYY-MM-DD), добавляем T12:00:00
    sold_at = body.sold_at.strip()
    if len(sold_at) == 10 and sold_at.count("-") == 2:
        sold_at = f"{sold_at}T12:00:00"

    result = db.add_manual_sale(
        account_id=body.account_id,
        ad_title=body.ad_title,
        ad_price=body.ad_price,
        sold_price_eur=body.sold_price_eur,
        sold_at=sold_at,
        buyer_name=body.buyer_name,
        notes=body.notes,
    )
    return {"ok": True, **result}


ALLOWED_SETTING_KEYS = {
    "send_mode", "debug_email", "gmail_poll_interval_sec", "gmail_from_filter",
    "inquiry_max_age_days",
    "reminders_enabled", "reminder_after_days",
    "polling_paused",
    "telegram_authorized", "telegram_operator_dm_ids", "telegram_admin_chat_id",
    "tg_quiet_from", "tg_quiet_to",
    "max_discount_percent",
    "claude_model", "system_prompt",
    "chat_font_em", "chat_padding_v_rem", "chat_padding_h_rem",
    "chat_max_width_pct", "chat_radius_rem", "chat_row_gap_rem",
    "chat_meta_font_em", "chat_secondary_font_em",
    "api_balance_snapshot_usd", "api_balance_snapshot_at",
    "anthropic_api_key", "telegram_bot_token",
    "web_port", "web_host",
    "autopilot_message_cap",
    # Разведка рынка: без них авто-прогон нельзя было включить из MA вообще —
    # scout_job молча возвращал «auto disabled», данные протухали (2026-07-21)
    "scout_auto_enabled", "scout_interval_hours",
    "scout_page_delay_sec", "scout_stale_days",
}

SENSITIVE_KEYS = {
    "anthropic_api_key", "telegram_bot_token",
}

def _is_float_in(raw: str, lo: float, hi: float) -> bool:
    try:
        return lo <= float(raw.strip().replace(",", ".")) <= hi
    except (ValueError, AttributeError):
        return False


VALIDATORS = {
    "send_mode": lambda v: v in {"disabled", "redirect", "production"},
    "polling_paused": lambda v: v in {"0", "1"},
    "reminders_enabled": lambda v: v in {"0", "1"},
    "gmail_poll_interval_sec": lambda v: v.isdigit() and 10 <= int(v) <= 3600,
    "inquiry_max_age_days": lambda v: v.isdigit() and 1 <= int(v) <= 365,
    "max_discount_percent": lambda v: v.replace(".", "", 1).isdigit() and 0 <= float(v) <= 100,
    "autopilot_message_cap": lambda v: v.isdigit() and 0 <= int(v) <= 1000,
    "scout_auto_enabled": lambda v: v in {"0", "1"},
    "tg_quiet_from": lambda v: v.isdigit() and 0 <= int(v) <= 23,
    "tg_quiet_to": lambda v: v.isdigit() and 0 <= int(v) <= 23,
    # интервал применяется при старте сервиса (job регистрируется в build_scheduler)
    "scout_interval_hours": lambda v: v.isdigit() and 1 <= int(v) <= 168,
    "scout_page_delay_sec": lambda v: _is_float_in(v, 0.2, 10.0),
    "scout_stale_days": lambda v: v.isdigit() and 1 <= int(v) <= 90,
}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•••••"
    return "•••••" + value[-4:]


class SettingPostBody(BaseModel):
    key: str
    value: str = Field(..., max_length=100000)


@router.get("/settings")
def ma_settings_get(user: dict = Depends(verify_init_data_dep)) -> dict[str, str]:
    """Все whitelist-настройки. Sensitive ключи замаскированы."""
    out: dict[str, str] = {}
    for key in sorted(ALLOWED_SETTING_KEYS):
        raw = config.get(key) or ""
        if key in SENSITIVE_KEYS:
            out[key] = _mask_secret(raw)
        else:
            out[key] = raw
    return out


@router.post("/settings")
def ma_settings_post(body: SettingPostBody,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Установить одно значение. Whitelist + per-key validator."""
    if body.key not in ALLOWED_SETTING_KEYS:
        raise HTTPException(400, f"key '{body.key}' is not in whitelist")
    validator = VALIDATORS.get(body.key)
    if validator and not validator(body.value):
        raise HTTPException(400, f"invalid value for {body.key}")
    db.set_setting(body.key, body.value)
    return {"ok": True, "key": body.key, "value": body.value}


def berlin_day_bounds() -> "tuple[str, str]":
    """[начало, конец) сегодняшнего дня по Берлину, naive-UTC ISO — из db (один источник)."""
    return db.berlin_day_bounds_utc(today_berlin())


@router.get("/dashboard")
def ma_dashboard(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сводный дашборд: pipeline-счётчики, сегодня, баланс API, продажи 7/30д."""
    from datetime import datetime, timedelta

    # --- pipeline ---
    rows = _pipeline_rows()
    red = green = drafts = 0
    for r in rows:
        keys = r.keys()
        kind = (r["last_event_kind"] if "last_event_kind" in keys else None) or "in"
        if kind == "out":
            green += 1
        else:
            red += 1
        if "has_pending_draft" in keys and r["has_pending_draft"]:
            drafts += 1

    with db.get_conn() as conn:
        ap_active = conn.execute(
            "SELECT COUNT(*) AS c FROM thread_autopilot WHERE active=1"
        ).fetchone()["c"]
        # «Сегодня» — по Берлину (оператор там), границы в UTC как в БД
        t0, t1 = berlin_day_bounds()
        new_today = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE direction='in' "
            "AND created_at >= ? AND created_at < ?", (t0, t1)
        ).fetchone()["c"]
        sent_today = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE status IN ('sent','sent_debug') "
            "AND COALESCE(sent_at, created_at) >= ? AND COALESCE(sent_at, created_at) < ?", (t0, t1)
        ).fetchone()["c"]
        # Реплики продавца, написанные на сайте Kleinanzeigen (импорт) — не «отправил бот»
        sent_external_today = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE status='sent_external' "
            "AND COALESCE(sent_at, created_at) >= ? AND COALESCE(sent_at, created_at) < ?", (t0, t1)
        ).fetchone()["c"]
        burn_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM messages "
            "WHERE cost_usd IS NOT NULL AND created_at >= datetime('now','-7 days')"
        ).fetchone()

    # --- баланс API (как на веб-дашборде) ---
    snapshot_raw = config.get("api_balance_snapshot_usd") or ""
    snapshot_at = config.get("api_balance_snapshot_at") or ""
    try:
        snapshot_usd = float(snapshot_raw) if snapshot_raw else None
    except ValueError:
        snapshot_usd = None
    remaining = None
    if snapshot_usd is not None and snapshot_at:
        with db.get_conn() as conn:
            srow = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS s FROM messages "
                "WHERE cost_usd IS NOT NULL AND created_at > ?", (snapshot_at,)
            ).fetchone()
        remaining = round(snapshot_usd - float(srow["s"] or 0.0), 2)
    burn_per_day = round(float(burn_row["s"] or 0.0) / 7.0, 4)
    days_remaining = None
    if remaining is not None and burn_per_day > 0 and remaining > 0:
        days_remaining = round(remaining / burn_per_day, 1)

    # --- продажи 7д / 30д ---
    today = today_berlin()

    def _sales_window(days: int) -> dict[str, Any]:
        sm = db.sales_summary(today - timedelta(days=days - 1), today)  # N дней по Берлину, включая сегодня
        return {"count": sm["count"], "total_eur": sm["total_eur"]}

    return {
        "pipeline": {"red": red, "green": green, "drafts": drafts,
                     "autopilot_active": ap_active},
        "today": {"new": new_today, "sent": sent_today, "sent_external": sent_external_today,
                  "sold": db.sales_summary(today, today)["count"]},
        "api_balance": {"snapshot_usd": snapshot_usd, "remaining_usd": remaining,
                        "burn_per_day_usd": burn_per_day, "days_remaining": days_remaining},
        "sales_7d": _sales_window(7),
        "sales_30d": _sales_window(30),
    }


# ──────────────────────── АККАУНТЫ И БЭКАП ────────────────────────
# Пароли (Gmail App Password, пароль Kleinanzeigen) — только запись: наружу отдаём лишь факт,
# что пароль задан. Через туннель доступно оператору с initData — как и всё остальное в MA.

def _account_dict(a: Any) -> dict[str, Any]:
    keys = a.keys()
    return {
        "id": a["id"],
        "name": a["name"],
        "active": bool(a["is_active"]),
        "gmail_email": a["gmail_email"],
        "gmail_app_password_set": bool(a["gmail_app_password"]),
        "kleinanzeigen_email": a["kleinanzeigen_email"] if "kleinanzeigen_email" in keys else None,
        "kleinanzeigen_password_set": bool("kleinanzeigen_password" in keys and a["kleinanzeigen_password"]),
        "auto_ack_enabled": bool("auto_ack_enabled" in keys and a["auto_ack_enabled"]),
        "created_at": a["created_at"] if "created_at" in keys else None,
    }


@router.get("/accounts")
def ma_accounts(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Список аккаунтов (фильтр инбокса, бейджи, экран «Аккаунты»). Без паролей."""
    return {"accounts": [_account_dict(a) for a in db.list_accounts()]}


class AccountUpdateBody(BaseModel):
    # Только переданные поля меняются; пароли — пустая строка/None = не трогать
    name: "str | None" = Field(None, min_length=1, max_length=100)
    is_active: "bool | None" = None
    auto_ack_enabled: "bool | None" = None
    gmail_app_password: "str | None" = Field(None, max_length=200)
    kleinanzeigen_email: "str | None" = Field(None, max_length=200)
    kleinanzeigen_password: "str | None" = Field(None, max_length=200)


@router.post("/accounts/{account_id}")
def ma_account_update(account_id: int, body: AccountUpdateBody,
                      user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Изменить аккаунт: вкл/выкл, авто-приветствие, имя, пароли (только запись)."""
    if db.get_account(account_id) is None:
        raise HTTPException(404, "account not found")
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if body.auto_ack_enabled is not None:
        fields["auto_ack_enabled"] = 1 if body.auto_ack_enabled else 0
    if body.kleinanzeigen_email is not None:
        fields["kleinanzeigen_email"] = body.kleinanzeigen_email.strip() or None
    for key in ("gmail_app_password", "kleinanzeigen_password"):
        value = getattr(body, key)
        if value and value.strip():
            # App Password Gmail показывают группами через пробел — пробелы не часть пароля
            fields[key] = value.replace(" ", "") if key == "gmail_app_password" else value
    if not fields:
        raise HTTPException(400, "нечего менять")
    db.update_account(account_id, **fields)
    # Значения не логируем — только какие поля и кто
    logger.info("аккаунт #%s изменён (%s) оператором %s",
                account_id, ", ".join(sorted(fields)), actor_from_user(user))
    return _account_dict(db.get_account(account_id))


class TestImapBody(BaseModel):
    # Проверить новый пароль до сохранения; пусто — сохранённый
    gmail_app_password: "str | None" = Field(None, max_length=200)


@router.post("/accounts/{account_id}/test-imap")
def ma_account_test_imap(account_id: int, body: "TestImapBody | None" = None,
                         user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Проверить вход в Gmail по IMAP (до 30 с)."""
    from modules import gmail
    acc = db.get_account(account_id)
    if acc is None:
        raise HTTPException(404, "account not found")
    password = ((body.gmail_app_password if body else None) or "").replace(" ", "") or acc["gmail_app_password"]
    if not password:
        return {"ok": False, "message": "пароль приложения Gmail не задан"}
    ok, message = gmail.test_connection(acc["gmail_email"], password)
    return {"ok": ok, "message": message}


_BACKUP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backup.sh"
BACKUP_MARKER = Path(os.environ.get(
    "KAB_BACKUP_MARKER", "/home/pg/.local/state/kleinanzeigen-bot/last_backup_ok"))
_backup_state: dict[str, Any] = {"running": False, "started_at": None, "finished_at": None,
                                 "exit_code": None, "output_tail": None}
_backup_lock = threading.Lock()
_backup_marker_problem: list["str | None"] = [None]


def backup_age_sec() -> "int | None":
    """Возраст последнего ПОЛНОГО успешного бэкапа по метке scripts/backup.sh (строка 1 — unix-время).
    None — метки нет или она нечитаема (бэкапа не было / сломан путь)."""
    try:
        ts = float(BACKUP_MARKER.read_text(encoding="utf-8").splitlines()[0].strip())
    except (OSError, ValueError, IndexError) as e:
        problem = f"{type(e).__name__}: {e}"
        if problem != _backup_marker_problem[0]:  # /health дёргают каждые минуты — без повтора
            logger.warning("метка бэкапа %s не читается: %s", BACKUP_MARKER, problem)
            _backup_marker_problem[0] = problem
        return None
    _backup_marker_problem[0] = None
    return max(0, int(time.time() - ts))


def _run_backup() -> None:
    import subprocess
    try:
        p = subprocess.run([str(_BACKUP_SCRIPT)], capture_output=True, text=True, timeout=1800)
        code, out = p.returncode, (p.stdout + p.stderr)
    except Exception as e:  # noqa: BLE001
        code, out = -1, f"не запустился: {e}"
    if code != 0:
        logger.error("ручной бэкап из MA: код %s: %s", code, out[-500:])
    with _backup_lock:
        _backup_state.update(running=False, finished_at=datetime.utcnow().isoformat(timespec="seconds"),
                             exit_code=code, output_tail=out[-2000:])


def _backup_status() -> dict[str, Any]:
    with _backup_lock:
        st = dict(_backup_state)
    st["ok"] = None if st["exit_code"] is None else st["exit_code"] == 0
    st["backup_age_sec"] = backup_age_sec()
    return st


@router.post("/backup/run")
def ma_backup_run(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Ручной бэкап (scripts/backup.sh: снимок + OneDrive) в фоне. Статус — GET /backup/status."""
    with _backup_lock:
        if _backup_state["running"]:
            started = False
        else:
            _backup_state.update(running=True, started_at=datetime.utcnow().isoformat(timespec="seconds"),
                                 finished_at=None, exit_code=None, output_tail=None)
            started = True
    if started:
        logger.info("ручной бэкап запущен оператором %s", actor_from_user(user))
        threading.Thread(target=_run_backup, daemon=True, name="manual-backup").start()
    return {"started": started, **_backup_status()}


@router.get("/backup/status")
def ma_backup_status(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _backup_status()


@router.get("/clients")
def ma_clients(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """База обращений (CRM): уникальные покупатели с агрегатами."""
    return {"clients": [
        {
            "email": r["email"],
            "display_name": r["display_name"],
            "ad_count": r["ad_count"],
            "thread_count": r["thread_count"],
            "msg_count": r["msg_count"],
            "last_at": r["last_at"],
            "last_status": r["last_status"],
        }
        for r in db.list_clients()
    ]}


_CYRILLIC_RE = re.compile(r"[а-яА-ЯёЁ]")


@router.post("/threads/{thread_id}/compose-preview")
def ma_compose_preview(thread_id: str, body: ComposeBody,
                       user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Предпросмотр перевода ответа: RU → язык клиента + обратный перевод RU.

    Это ЕДИНСТВЕННОЕ место, где происходит перевод — /compose (отправка) больше не
    переводит повторно, а берёт итоговый (возможно отредактированный оператором здесь)
    текст как есть. См. инцидент 2026-07-02: оператор напечатал ответ сразу на немецком
    без директивы «на немецком: ...»; бэкенд слепо считал текст русским (translate_only
    без source_lang жёстко подставляет «русского» в промпт) и получил на выходе русский
    текст, который ушёл клиенту. Защита ниже: если в тексте оператора вообще нет
    кириллицы — это не может быть русский, перевод не выполняется, текст идёт как есть.
    """
    from fastapi import HTTPException
    if claude.is_empty_directive(body.text):
        raise HTTPException(400, "Указан язык («на X:»), но само сообщение не дописано")
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    client_lang = "de"
    ad_ctx = ""
    for m in reversed(history):
        try:
            if m["direction"] == "in" and m["client_lang"]:
                client_lang = m["client_lang"]
                ad_ctx = m["ad_title"] or ""
                break
        except (KeyError, IndexError):
            continue

    override_lang, ru_text = claude.detect_lang_override(body.text)
    target_lang = override_lang or client_lang

    note = None
    if target_lang == "ru":
        translated = ru_text
    elif not _CYRILLIC_RE.search(ru_text):
        # Текст не содержит кириллицы — оператор явно печатал не по-русски.
        # НЕ отдаём его переводчику как "русский" (см. докстрока выше) — шлём как есть.
        translated = ru_text
        note = (
            f"В тексте нет кириллицы — похоже, он уже не на русском. "
            f"Перевод НЕ выполнялся, текст будет отправлен как есть. Проверьте, что это {target_lang.upper()}."
        )
    else:
        translated, back_ru = _translate_with_back(ru_text, target_lang, context=ad_ctx)

    if target_lang == "ru":
        back_ru = translated
    elif note:
        # Текст без кириллицы ушёл как есть — RU-зеркало для оператора всё равно нужно
        back = claude.translate_only(translated, target_lang="ru", source_lang=target_lang)
        back_ru = back.get("translation", "") if isinstance(back, dict) else ""
    result = {"translated": translated, "back_ru": back_ru, "target_lang": target_lang, "ru_text": ru_text}
    if note:
        result["note"] = note
    return result


# ============================================================
# Разведка рынка (market scout) — Mini App
# ============================================================

def _scout_query_dict(q: Any) -> dict[str, Any]:
    return {
        "id": q["id"], "kind": q["kind"], "label": q["label"],
        "keywords": q["keywords"], "category": q["category"],
        "enabled": bool(q["enabled"]), "max_pages": q["max_pages"],
        "source": q["source"], "last_run_at": q["last_run_at"],
        "last_count": q["last_count"],
    }


def _scout_unseen_cutoff() -> str:
    """ISO-порог «объявление считается снятым»: now - scout_stale_days."""
    from datetime import datetime as _d, timedelta as _t
    return (_d.utcnow() - _t(days=config.scout_stale_days())).isoformat()


def _days_unseen(last_seen: "str | None") -> "int | None":
    if not last_seen:
        return None
    from datetime import datetime as _d
    try:
        seen = _d.fromisoformat(str(last_seen).replace("Z", "").split("+")[0])
    except ValueError:
        return None
    return max(0, (_d.utcnow() - seen).days)


def _scout_listing_dict(r: Any, cutoff: "str | None" = None) -> dict[str, Any]:
    last_seen = r["last_seen_at"] if "last_seen_at" in r.keys() else None
    active = bool(r["active"]) if "active" in r.keys() else True
    removed = (not active) or bool(cutoff and last_seen and str(last_seen) < cutoff)
    return {
        "ad_id": r["ad_id"], "kind": r["kind"], "title": r["title"], "url": r["url"],
        "price_eur": r["price_eur"], "negotiable": bool(r["negotiable"]),
        "plz": r["plz"], "city": r["city"], "bundesland": r["bundesland"],
        "year": r["year"], "mileage_km": r["mileage_km"], "fuel": r["fuel"],
        "gearbox": r["gearbox"], "model_family": r["model_family"],
        "part_type": r["part_type"], "condition": r["condition"],
        "posted_raw": r["posted_raw"],
        "removed": removed,
        "days_unseen": _days_unseen(last_seen),
    }


def _scout_region_dict(r: Any) -> dict[str, Any]:
    return {
        "bundesland": r["bundesland"], "cnt": r["cnt"],
        "min_price": r["min_price"], "avg_price": r["avg_price"],
        "max_price": r["max_price"],
    }


@router.get("/scout/overview")
def ma_scout_overview(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сводка разведки: счётчики, статус прогона, запросы, регионы."""
    st = scout_runner.status()
    return {
        "counts": db.scout_counts(_scout_unseen_cutoff()),
        "auto_enabled": config.scout_auto_enabled(),
        "interval_hours": config.scout_interval_hours(),
        "running": st["running"],
        "started_at": st["started_at"],
        "summary": st["summary"],
        "queries": [_scout_query_dict(q) for q in db.list_scout_queries()],
        "car_regions": [_scout_region_dict(r) for r in db.scout_region_summary("car")],
        "part_regions": [_scout_region_dict(r) for r in db.scout_region_summary("part")],
    }


@router.get("/scout/listings")
def ma_scout_listings(kind: str = "car", include_removed: bool = False,
                      user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Объявления разведки одного вида (car|part). Фильтрация — на клиенте.

    По умолчанию снятые (нет в выдаче дольше scout_stale_days) не отдаются.
    include_removed=true — отдать вместе с ними, каждое с флагом removed.
    """
    kind = "part" if kind == "part" else "car"
    cutoff = _scout_unseen_cutoff()
    rows = db.list_scout_listings(
        kind=kind,
        only_active=not include_removed,
        unseen_cutoff=None if include_removed else cutoff,
    )
    listings = [_scout_listing_dict(r, cutoff) for r in rows]
    return {
        "kind": kind,
        "listings": listings,
        "removed_count": sum(1 for x in listings if x["removed"]),
    }


@router.get("/scout/status")
def ma_scout_status(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Статус фонового прогона (для поллинга)."""
    st = scout_runner.status()
    return {"running": st["running"], "started_at": st["started_at"],
            "summary": st["summary"], "counts": db.scout_counts(_scout_unseen_cutoff())}


class ScoutRunBody(BaseModel):
    query_id: int | None = None


@router.post("/scout/run")
def ma_scout_run(body: ScoutRunBody,
                 user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить разведку в фоне (все enabled, либо один query_id)."""
    ids = [body.query_id] if body.query_id else None
    started = scout_runner.start(ids)
    return {"started": started, "running": scout_runner.is_running()}


class ScoutGenerateBody(BaseModel):
    extra: str = Field("", max_length=2000)


@router.post("/scout/generate")
def ma_scout_generate(body: ScoutGenerateBody,
                      user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сгенерить запросы через LLM и добавить новые (без дублей)."""
    existing = [q["keywords"] for q in db.list_scout_queries()]
    result = claude_scout.generate_scout_queries(
        existing_keywords=existing, extra_instruction=body.extra.strip(),
    )
    added = 0
    for q in result["queries"]:
        if db.scout_query_exists(q["kind"], q["keywords"], q["category"]):
            continue
        db.add_scout_query(kind=q["kind"], keywords=q["keywords"],
                           category=q["category"], label=q["label"],
                           max_pages=q["max_pages"], source="llm")
        added += 1
    return {"added": added, "cost_usd": result.get("cost_usd", 0.0),
            "total": len(result["queries"])}


class ScoutQueryBody(BaseModel):
    kind: str
    keywords: str = Field(..., min_length=1, max_length=200)
    category: str = "c216"
    label: str = Field("", max_length=200)
    max_pages: int = Field(5, ge=1, le=10)


@router.post("/scout/queries")
def ma_scout_query_add(body: ScoutQueryBody,
                       user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Добавить поисковый запрос вручную."""
    from fastapi import HTTPException
    kind = "part" if body.kind == "part" else "car"
    category = body.category if body.category in ("c216", "c223") else (
        "c223" if kind == "part" else "c216")
    kw = body.keywords.strip()
    if not kw:
        raise HTTPException(400, "empty keywords")
    qid = db.add_scout_query(kind=kind, keywords=kw, category=category,
                             label=body.label.strip() or kw,
                             max_pages=body.max_pages, source="operator")
    return {"id": qid}


class ScoutQueryUpdateBody(BaseModel):
    keywords: str = Field(..., min_length=1, max_length=200)
    category: str = "c216"
    label: str = Field("", max_length=200)
    max_pages: int = Field(5, ge=1, le=10)


@router.post("/scout/queries/{query_id}/update")
def ma_scout_query_update(query_id: int, body: ScoutQueryUpdateBody,
                          user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    from fastapi import HTTPException
    if not db.get_scout_query(query_id):
        raise HTTPException(404, "query not found")
    category = body.category if body.category in ("c216", "c223") else "c216"
    db.update_scout_query(query_id, keywords=body.keywords.strip(),
                          category=category, label=body.label.strip(),
                          max_pages=body.max_pages)
    return {"ok": True}


@router.post("/scout/queries/{query_id}/toggle")
def ma_scout_query_toggle(query_id: int,
                          user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    from fastapi import HTTPException
    q = db.get_scout_query(query_id)
    if not q:
        raise HTTPException(404, "query not found")
    new_val = 0 if q["enabled"] else 1
    db.update_scout_query(query_id, enabled=new_val)
    return {"enabled": bool(new_val)}


@router.post("/scout/queries/{query_id}/delete", status_code=204)
def ma_scout_query_delete(query_id: int,
                          user: dict = Depends(verify_init_data_dep)) -> None:
    db.delete_scout_query(query_id)


_scout_verify_lock = threading.Lock()


@router.post("/scout/verify")
def ma_scout_verify(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить Haiku-проверку типа непроверенных объявлений в фоне (не более одной сразу)."""
    from modules import scout as _scout
    if not _scout_verify_lock.acquire(blocking=False):
        return {"started": False, "running": True}

    def _run() -> None:
        try:
            _scout.verify_listings()
        except Exception:
            logger.exception("scout verify_listings упал")
        finally:
            _scout_verify_lock.release()

    threading.Thread(target=_run, daemon=True, name="scout-verify").start()
    return {"started": True, "running": True}


class ScoutCorrectBody(BaseModel):
    correct_kind: str  # 'car' | 'part' | 'other' | 'remove'
    note: str = Field("", max_length=500)


@router.post("/scout/listings/{ad_id}/correct")
def ma_scout_correct(ad_id: str, body: ScoutCorrectBody,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Операторская правка: переклассифицировать или удалить (с обучением Haiku)."""
    from fastapi import HTTPException
    actor = actor_from_user(user)
    res = db.apply_scout_correction(ad_id, body.correct_kind,
                                    note=body.note or None, created_by=actor)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "correction failed"))
    return res
