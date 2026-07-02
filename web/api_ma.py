"""Mini App API router.

Все endpoint'ы под префиксом /api/ma/. Тонкие обёртки над database.* и
existing scheduler.* helpers — никакой бизнес-логики.

Все endpoint'ы требуют валидной Telegram initData через verify_init_data_dep.
"""
from __future__ import annotations
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

import json
import re

import database as db
import config
from modules import operator_lock, telegram_bot
import scheduler
from modules import claude
from modules import claude_scout
from modules import scout_runner
from modules.tg_init_data import verify_init_data_dep


router = APIRouter(prefix="/api/ma", tags=["Mini App"])

_CLOSED_STATUSES = {"skipped", "skipped_sold", "archived"}
_ALLOWED_TAGS = {"Серьёзный", "Торгуется", "Тянет время", "Мошенник"}


@router.get("/health")
async def ma_health(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Health endpoint для Mini App. Возвращает идентичность оператора."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }


def _row_to_pipeline_item(row: Any, autopilot_row: Any) -> dict[str, Any]:
    """Конвертация row из db.pipeline_threads + autopilot lookup в API-форму."""
    is_autopilot = False
    if autopilot_row is not None:
        try:
            is_autopilot = bool(autopilot_row["active"])
        except (KeyError, IndexError):
            is_autopilot = False
    return {
        "thread_id": row["gmail_thread_id"],
        "msg_id": row["id"],
        "ad_title": row["ad_title"],
        "ad_price": row["ad_price"],
        "ad_url": row["ad_url"] if "ad_url" in row.keys() else None,
        "buyer_display_name": row["buyer_display_name"],
        "deal_brief_json": row["deal_brief_json"] if "deal_brief_json" in row.keys() else None,
        "last_event_at": row["last_event_at"],
        "last_event_kind": row["last_event_kind"],
        "pending_drafts_count": row["pending_drafts_count"],
        "is_autopilot": is_autopilot,
        "account_id": row["account_id"] if "account_id" in row.keys() else None,
        "ru_client": row["ru_client"] if "ru_client" in row.keys() else None,
        "is_pinned": bool(row["is_pinned"]) if "is_pinned" in row.keys() else False,
        "operator_unread": bool(row["operator_unread"]) if "operator_unread" in row.keys() else False,
    }


@router.get("/pipeline")
async def ma_pipeline(user: dict = Depends(verify_init_data_dep)) -> dict[str, list]:
    """Pipeline активных тредов: разделение на red (ждут нас) / green (ждём клиента).

    Сортировка внутри секции — DESC по last_event_at (новые сверху).
    """
    rows = db.pipeline_threads()
    pinned: list[dict[str, Any]] = []
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row["gmail_thread_id"]
        autopilot_row = db.get_thread_autopilot(thread_id)
        item = _row_to_pipeline_item(row, autopilot_row)
        if item["is_pinned"]:
            pinned.append(item)
        elif item["last_event_kind"] == "in":
            red.append(item)
        else:
            green.append(item)
    pinned.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    red.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    green.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    accounts = [{"id": a["id"], "name": a["name"]} for a in db.list_accounts()]
    return {"pinned": pinned, "red": red, "green": green, "accounts": accounts}


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
    }


def _related_match(row: Any) -> dict[str, Any]:
    return {
        "thread_id": row["gmail_thread_id"],
        "ad_title": row["ad_title"] if "ad_title" in row.keys() else None,
        "ad_price": row["ad_price"] if "ad_price" in row.keys() else None,
        "last_at": (row["sent_at"] if "sent_at" in row.keys() else None) or (
            row["created_at"] if "created_at" in row.keys() else None),
    }


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
        "account_name": account["name"] if account is not None else None,
        "account_email": account["gmail_email"] if account is not None else None,
        "is_autopilot": is_autopilot,
    }
    events = [_event_to_api(e) for e in db.thread_events(thread_id)]
    related_matches = db.find_related_inquiries(
        last_in["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if last_in["buyer_display_name"] else []
    related = {
        "buyer_display_name": last_in["buyer_display_name"],
        "matches": [_related_match(r) for r in related_matches],
    }
    return {"header": header, "events": events, "related": related}


class BulkActionBody(BaseModel):
    thread_ids: list[str] = Field(..., min_length=1)
    action: str  # "pin" | "unpin" | "read" | "unread" | "close"


@router.post("/threads/bulk-action")
async def ma_bulk_action(
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


@router.get("/threads/{thread_id}")
async def ma_thread(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _thread_dict(thread_id)


@router.get("/clients/{email}/history")
async def ma_client_history(email: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Профиль клиента: треды + deal_brief + теги + агрегаты."""
    rows = db.list_threads_for_client(email)

    # display_name и total_cost — один коннект
    display_name = email
    total_cost_usd = 0.0
    with db.get_conn() as conn:
        dn_row = conn.execute(
            "SELECT buyer_display_name FROM messages "
            "WHERE buyer_name = ? AND buyer_display_name IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if dn_row:
            display_name = dn_row["buyer_display_name"]
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
    }




class ClientProfilePayload(BaseModel):
    tags: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=4000)


@router.post("/clients/{email}/profile")
async def ma_client_profile_save(
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
    related_matches = db.find_related_inquiries(
        row["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if row["buyer_display_name"] else []
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
        "related": {
            "buyer_display_name": row["buyer_display_name"],
            "matches": [_related_match(r) for r in related_matches],
        },
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
async def ma_message_review(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Полный review payload."""
    return _message_review_dict(msg_id)


@router.get("/messages/{msg_id}/lock")
async def ma_message_lock_state(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
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
async def ma_lock_acquire(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict:
    """Acquire lock on review card. 409 if held by someone else."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign_holder = telegram_bot._check_lock(msg_id, actor)
    if foreign_holder is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "holder": foreign_holder,
                "remaining_min": operator_lock.remaining_min(msg_id),
            },
        )
    telegram_bot._acquire_lock(msg_id, actor)
    return {
        "holder": actor,
        "remaining_min": operator_lock.remaining_min(msg_id),
    }


@router.post("/messages/{msg_id}/lock/release", status_code=204)
async def ma_lock_release(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> None:
    """Release lock. Permissive — does not check holder."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    telegram_bot._release_lock(msg_id)
    return None


def _check_actor_holds(msg_id: int, actor: str) -> "str | None":
    return telegram_bot._check_lock(msg_id, actor)


def _ensure_lock(msg_id: int, actor: str) -> None:
    telegram_bot._acquire_lock(msg_id, actor)


@router.post("/messages/{msg_id}/send")
async def ma_send(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(scheduler.send_one, msg_id)
    if result.get("kind") == "error":
        raise HTTPException(500, result.get("message", "send failed"))

    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    telegram_bot._release_lock(msg_id)

    fresh = db.get_message(msg_id)
    return {"ok": True, "status": fresh["status"] if fresh else "sent"}


@router.post("/messages/{msg_id}/skip")
async def ma_skip(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    db.update_message(msg_id, status="skipped")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped"}


class SoldBody(BaseModel):
    price_eur: float = Field(..., ge=0, lt=1000000)
    close_other_threads_for_ad: bool = False


@router.post("/messages/{msg_id}/sold")
async def ma_sold(msg_id: int, body: SoldBody,
                  user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    result = db.mark_thread_sold(
        msg_id,
        sold_price_eur=body.price_eur,
        close_other_threads_for_ad=body.close_other_threads_for_ad,
    )
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    telegram_bot._release_lock(msg_id)
    return {
        "ok": True,
        "status": "skipped_sold",
        "sold_price_eur": body.price_eur,
        "closed_other_threads": result["closed_other_threads"],
    }


def _apply_regenerate_result(msg_id: int, result: "dict[str, Any]") -> None:
    """Записать результат claude.regenerate_* в db и перевести status в 'edited'."""
    fields: "dict[str, Any]" = {"status": "edited"}
    if "ru_answer" in result:
        fields["ru_answer"] = result["ru_answer"]
    if "client_answer" in result:
        fields["de_answer"] = result["client_answer"]
    if "ru_translation" in result:
        fields["ru_translation"] = result["ru_translation"]
    deal: "dict[str, Any]" = {}
    for src_key in ("deal_summary_ru", "expected_next", "negotiated_price_eur", "client_assessment"):
        if src_key in result:
            target_key = "summary_ru" if src_key == "deal_summary_ru" else src_key
            deal[target_key] = result[src_key]
    if deal:
        fields["deal_brief_json"] = json.dumps(deal, ensure_ascii=False)
    db.update_message(msg_id, **fields)


@router.post("/messages/{msg_id}/regenerate")
async def ma_regenerate(msg_id: int, body: RegenerateBody,
                        user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Регенерировать draft под strategy. Intermediate — lock keeps."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_strategy, row, body.strategy
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()

    return _message_review_dict(msg_id)


class EditTextBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/messages/{msg_id}/edit-ru")
async def ma_edit_ru(msg_id: int, body: EditTextBody,
                     user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Operator edits RU answer. Forward+back translate."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    target_lang = row["client_lang"] if "client_lang" in row.keys() else "de"
    de_res = await asyncio.to_thread(
        claude.translate_only, body.text, target_lang=target_lang, source_lang="ru"
    )
    de_text = de_res.get("translation", "") if isinstance(de_res, dict) else str(de_res)
    ru_res = await asyncio.to_thread(
        claude.translate_only, de_text, target_lang="ru", source_lang=target_lang
    )
    ru_back = ru_res.get("translation", "") if isinstance(ru_res, dict) else str(ru_res)
    db.update_message(msg_id, ru_answer=body.text, de_answer=de_text,
                      ru_translation=ru_back, status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/edit-de")
async def ma_edit_de(msg_id: int, body: EditTextBody,
                     user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    """Operator edits DE answer directly. Back-translate for verification."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    source_lang = row["client_lang"] if "client_lang" in row.keys() else "de"
    ru_res = await asyncio.to_thread(
        claude.translate_only, body.text, target_lang="ru", source_lang=source_lang
    )
    ru_back = ru_res.get("translation", "") if isinstance(ru_res, dict) else str(ru_res)
    db.update_message(msg_id, de_answer=body.text, ru_translation=ru_back,
                      status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    return _message_review_dict(msg_id)


class PriceBody(BaseModel):
    eur: float = Field(..., gt=0, lt=100000)


class InstructionBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


@router.post("/messages/{msg_id}/price")
async def ma_price(msg_id: int, body: PriceBody,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать с конкретной ценой от оператора."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_price, row, body.eur
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/instruction")
async def ma_instruction(msg_id: int, body: InstructionBody,
                          user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать со свободной инструкцией."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_instruction, row, body.text
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
    return _message_review_dict(msg_id)


class ComposeBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


class ComposeSendBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000, description="ru_text из ответа /compose-preview")
    final_text: str = Field(..., min_length=1, max_length=4000, description="итоговый текст клиенту — из превью, возможно отредактированный оператором")
    target_lang: str = Field(..., min_length=2, max_length=5, description="target_lang из ответа /compose-preview")


@router.post("/threads/{thread_id}/compose")
async def ma_compose(thread_id: str, body: ComposeSendBody,
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

    import asyncio
    result = await asyncio.to_thread(
        scheduler.send_manual_compose, source_msg_id, body.text, body.final_text, body.target_lang
    )
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

    # Broadcast обновление текстов мини-карточек у закрытых drafts
    for mid in closed_drafts:
        try:
            telegram_bot.broadcast_after_external_action(mid)
        except Exception:
            pass
    telegram_bot.refresh_pipeline_for_active_chats()

    return {
        "ok": True,
        "sent_msg_id": result.get("message_id"),
        "closed_drafts": closed_drafts,
        "thread_id": thread_id,
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
async def ma_autopilot_start(thread_id: str, body: AutopilotStartBody,
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
        try:
            telegram_bot.send_autopilot_start_notification(msg_id, body.floor_eur, actor)
        except Exception:
            pass  # best-effort

    telegram_bot.refresh_pipeline_for_active_chats()

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
        return [], "", []


@router.post("/threads/{thread_id}/autopilot/preview")
async def ma_autopilot_preview(thread_id: str, body: AutopilotPreviewBody,
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

    import asyncio

    def _get_field(row, key):
        try:
            return row[key] if key in row.keys() else ""
        except Exception:
            return ""

    try:
        result = await asyncio.to_thread(
            claude.generate_autopilot_reply,
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
            "deal_brief": {
                "summary_ru": result.get("deal_summary_ru", ""),
                "expected_next": result.get("expected_next", ""),
                "negotiated_price_eur": result.get("negotiated_price_eur"),
                "client_assessment": result.get("client_assessment", ""),
            },
            "should_stop": result.get("should_stop", False),
            "stop_reason": result.get("stop_reason"),
            "used_web_search": result.get("used_web_search", False),
        }
    }


@router.post("/threads/{thread_id}/autopilot/stop")
async def ma_autopilot_stop(thread_id: str,
                             user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Остановить автопилот (manual stop)."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    db.stop_thread_autopilot(thread_id, "manual")
    telegram_bot.refresh_pipeline_for_active_chats()
    return _thread_dict(thread_id)


@router.post("/threads/{thread_id}/wait")
async def ma_thread_wait(thread_id: str,
                          user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    actor = actor_from_user(user)
    db.mark_thread_waiting(thread_id, marked_by=actor)
    telegram_bot.refresh_pipeline_for_active_chats()
    return _thread_dict(thread_id)


@router.post("/threads/{thread_id}/suggest-reply")
async def ma_suggest_reply(thread_id: str,
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

    msg_id = latest_in["id"]
    import asyncio
    result = await asyncio.to_thread(scheduler.regenerate_draft, msg_id, "regen")
    if result.get("kind") == "error":
        raise HTTPException(500, result.get("message", "regenerate failed"))

    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot.refresh_pipeline_for_active_chats()
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
async def ma_sales(
    period: str = "all",
    from_: "str | None" = None,
    to_: "str | None" = None,
    account_id: "int | None" = None,
    q: "str | None" = None,
    group_by: str = "month",
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Все продажи (in-one-pot из всех аккаунтов) + сводка + breakdown по периоду."""
    period = period if period in {"all", "week", "month", "year", "custom"} else "all"
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

@router.get("/detected-sales")
async def ma_detected_sales(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Список pending detections для review-экрана."""
    rows = db.list_pending_detections()
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
async def ma_detection_apply(detection_id: int, body: DetectionApplyBody,
                              user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Применить detection — записывает в messages + close_thread."""
    try:
        applied = db.apply_detection_to_messages(detection_id, sold_price_eur=body.price_eur)
    except ValueError as e:
        raise HTTPException(400, str(e))
    telegram_bot.refresh_pipeline_for_active_chats()
    return {"ok": True, **applied}


@router.post("/detected-sales/{detection_id}/reject", status_code=204)
async def ma_detection_reject(detection_id: int,
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
async def ma_sales_manual(body: ManualSaleBody,
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
    "telegram_authorized", "telegram_operator_dm_ids",
    "max_discount_percent",
    "claude_model", "system_prompt",
    "chat_font_em", "chat_padding_v_rem", "chat_padding_h_rem",
    "chat_max_width_pct", "chat_radius_rem", "chat_row_gap_rem",
    "chat_meta_font_em", "chat_secondary_font_em",
    "api_balance_snapshot_usd", "api_balance_snapshot_at",
    "anthropic_api_key", "telegram_bot_token",
    "google_drive_credentials_json", "google_drive_folder_id", "backup_interval_hours",
    "web_port", "web_host",
}

SENSITIVE_KEYS = {
    "anthropic_api_key", "telegram_bot_token",
    "google_drive_credentials_json",
}

VALIDATORS = {
    "send_mode": lambda v: v in {"disabled", "redirect", "production"},
    "polling_paused": lambda v: v in {"0", "1"},
    "reminders_enabled": lambda v: v in {"0", "1"},
    "gmail_poll_interval_sec": lambda v: v.isdigit() and 10 <= int(v) <= 3600,
    "inquiry_max_age_days": lambda v: v.isdigit() and 1 <= int(v) <= 365,
    "max_discount_percent": lambda v: v.replace(".", "", 1).isdigit() and 0 <= float(v) <= 100,
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
async def ma_settings_get(user: dict = Depends(verify_init_data_dep)) -> dict[str, str]:
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
async def ma_settings_post(body: SettingPostBody,
                           user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Установить одно значение. Whitelist + per-key validator."""
    if body.key not in ALLOWED_SETTING_KEYS:
        raise HTTPException(400, f"key '{body.key}' is not in whitelist")
    validator = VALIDATORS.get(body.key)
    if validator and not validator(body.value):
        raise HTTPException(400, f"invalid value for {body.key}")
    db.set_setting(body.key, body.value)
    return {"ok": True, "key": body.key, "value": body.value}


@router.get("/dashboard")
async def ma_dashboard(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сводный дашборд: pipeline-счётчики, сегодня, баланс API, продажи 7/30д."""
    from datetime import datetime, timedelta

    # --- pipeline ---
    rows = db.pipeline_threads()
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
        new_today = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE direction='in' "
            "AND date(created_at)=date('now')"
        ).fetchone()["c"]
        sent_today = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE status IN ('sent','sent_debug') "
            "AND date(COALESCE(sent_at, created_at))=date('now')"
        ).fetchone()["c"]
        sold_today = conn.execute(
            "SELECT COUNT(*) AS c FROM ad_briefs WHERE sold_at IS NOT NULL "
            "AND date(sold_at)=date('now')"
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
    def _sales_window(days: int) -> dict[str, Any]:
        frm = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            srows = db.list_sales(period_from=frm, period_to=None, account_id=None, query=None)
        except Exception:
            srows = []
        prices = []
        for r in srows:
            v = r.get("sold_price_eur") if hasattr(r, "get") else r["sold_price_eur"]
            if v is not None:
                prices.append(float(v))
        return {"count": len(srows), "total_eur": round(sum(prices), 2) if prices else 0.0}

    return {
        "pipeline": {"red": red, "green": green, "drafts": drafts,
                     "autopilot_active": ap_active},
        "today": {"new": new_today, "sent": sent_today, "sold": sold_today},
        "api_balance": {"snapshot_usd": snapshot_usd, "remaining_usd": remaining,
                        "burn_per_day_usd": burn_per_day, "days_remaining": days_remaining},
        "sales_7d": _sales_window(7),
        "sales_30d": _sales_window(30),
    }


@router.get("/accounts")
async def ma_accounts(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Список аккаунтов (для фильтра инбокса и бейджей)."""
    return {"accounts": [
        {"id": a["id"], "name": a["name"], "active": bool(a["is_active"])}
        for a in db.list_accounts()
    ]}


@router.get("/clients")
async def ma_clients(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
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
async def ma_compose_preview(thread_id: str, body: ComposeBody,
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
    import asyncio
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
        tr = await asyncio.to_thread(claude.translate_only, ru_text,
                                     target_lang=target_lang, source_lang="ru", context=ad_ctx)
        translated = tr.get("translation", "") if isinstance(tr, dict) else ""

    if target_lang == "ru":
        back_ru = translated
    else:
        back = await asyncio.to_thread(claude.translate_only, translated,
                                       target_lang="ru", source_lang=target_lang)
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


def _scout_listing_dict(r: Any) -> dict[str, Any]:
    return {
        "ad_id": r["ad_id"], "kind": r["kind"], "title": r["title"], "url": r["url"],
        "price_eur": r["price_eur"], "negotiable": bool(r["negotiable"]),
        "plz": r["plz"], "city": r["city"], "bundesland": r["bundesland"],
        "year": r["year"], "mileage_km": r["mileage_km"], "fuel": r["fuel"],
        "gearbox": r["gearbox"], "model_family": r["model_family"],
        "part_type": r["part_type"], "condition": r["condition"],
        "posted_raw": r["posted_raw"],
    }


def _scout_region_dict(r: Any) -> dict[str, Any]:
    return {
        "bundesland": r["bundesland"], "cnt": r["cnt"],
        "min_price": r["min_price"], "avg_price": r["avg_price"],
        "max_price": r["max_price"],
    }


@router.get("/scout/overview")
async def ma_scout_overview(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сводка разведки: счётчики, статус прогона, запросы, регионы."""
    st = scout_runner.status()
    return {
        "counts": db.scout_counts(),
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
async def ma_scout_listings(kind: str = "car",
                            user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Активные объявления разведки одного вида (car|part). Фильтрация — на клиенте."""
    kind = "part" if kind == "part" else "car"
    rows = db.list_scout_listings(kind=kind)
    return {"kind": kind, "listings": [_scout_listing_dict(r) for r in rows]}


@router.get("/scout/status")
async def ma_scout_status(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Статус фонового прогона (для поллинга)."""
    st = scout_runner.status()
    return {"running": st["running"], "started_at": st["started_at"],
            "summary": st["summary"], "counts": db.scout_counts()}


class ScoutRunBody(BaseModel):
    query_id: int | None = None


@router.post("/scout/run")
async def ma_scout_run(body: ScoutRunBody,
                       user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить разведку в фоне (все enabled, либо один query_id)."""
    ids = [body.query_id] if body.query_id else None
    started = scout_runner.start(ids)
    return {"started": started, "running": scout_runner.is_running()}


class ScoutGenerateBody(BaseModel):
    extra: str = Field("", max_length=2000)


@router.post("/scout/generate")
async def ma_scout_generate(body: ScoutGenerateBody,
                            user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Сгенерить запросы через LLM и добавить новые (без дублей)."""
    import asyncio
    existing = [q["keywords"] for q in db.list_scout_queries()]
    result = await asyncio.to_thread(
        claude_scout.generate_scout_queries, existing_keywords=existing,
        extra_instruction=body.extra.strip(),
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
async def ma_scout_query_add(body: ScoutQueryBody,
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
async def ma_scout_query_update(query_id: int, body: ScoutQueryUpdateBody,
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
async def ma_scout_query_toggle(query_id: int,
                                user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    from fastapi import HTTPException
    q = db.get_scout_query(query_id)
    if not q:
        raise HTTPException(404, "query not found")
    new_val = 0 if q["enabled"] else 1
    db.update_scout_query(query_id, enabled=new_val)
    return {"enabled": bool(new_val)}


@router.post("/scout/queries/{query_id}/delete", status_code=204)
async def ma_scout_query_delete(query_id: int,
                                user: dict = Depends(verify_init_data_dep)) -> None:
    db.delete_scout_query(query_id)


@router.post("/scout/verify")
async def ma_scout_verify(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить Haiku-проверку типа непроверенных объявлений в фоне."""
    import threading
    from modules import scout as _scout
    threading.Thread(target=_scout.verify_listings, daemon=True).start()
    return {"started": True}


class ScoutCorrectBody(BaseModel):
    correct_kind: str  # 'car' | 'part' | 'other' | 'remove'
    note: str = Field("", max_length=500)


@router.post("/scout/listings/{ad_id}/correct")
async def ma_scout_correct(ad_id: str, body: ScoutCorrectBody,
                           user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Операторская правка: переклассифицировать или удалить (с обучением Haiku)."""
    from fastapi import HTTPException
    actor = actor_from_user(user)
    res = db.apply_scout_correction(ad_id, body.correct_kind,
                                    note=body.note or None, created_by=actor)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "correction failed"))
    return res
