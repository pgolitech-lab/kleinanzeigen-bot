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

import database as db
from modules import operator_lock, telegram_bot
import scheduler
from modules import claude
from modules.tg_init_data import verify_init_data_dep


router = APIRouter(prefix="/api/ma", tags=["Mini App"])


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
    }


@router.get("/pipeline")
async def ma_pipeline(user: dict = Depends(verify_init_data_dep)) -> dict[str, list]:
    """Pipeline активных тредов: разделение на red (ждут нас) / green (ждём клиента).

    Сортировка внутри секции — ASC по last_event_at (старые сверху).
    """
    rows = db.pipeline_threads()
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row["gmail_thread_id"]
        autopilot_row = db.get_thread_autopilot(thread_id)
        item = _row_to_pipeline_item(row, autopilot_row)
        if item["last_event_kind"] == "in":
            red.append(item)
        else:
            green.append(item)
    red.sort(key=lambda x: x["last_event_at"] or "")
    green.sort(key=lambda x: x["last_event_at"] or "")
    return {"red": red, "green": green}


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


@router.get("/threads/{thread_id}")
async def ma_thread(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Thread detail: header + chronological events + related-buyer block."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")

    # Header — самый свежий direction="in" (он несёт ad-инфо и buyer)
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
        "buyer_email": last_in["buyer_name"],
        "account_name": account["name"] if account is not None else None,
        "account_email": account["gmail_email"] if account is not None else None,
        "is_autopilot": is_autopilot,
    }

    events = [_event_to_api(e) for e in db.thread_events(thread_id)]

    related_matches = db.find_related_inquiries(
        last_in["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    )
    related = {
        "buyer_display_name": last_in["buyer_display_name"],
        "matches": [_related_match(r) for r in related_matches],
    }

    return {"header": header, "events": events, "related": related}


@router.get("/clients/{email}/history")
async def ma_client_history(email: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """История тредов клиента (по buyer_email)."""
    rows = db.list_threads_for_client(email)
    threads = [
        {
            "thread_id": r["thread_id"],
            "ad_title": r["ad_title"],
            "ad_id": r["ad_id"],
            "ad_price": r["ad_price"],
            "msg_count": r["msg_count"],
            "last_at": r["last_at"],
            "last_status": r["last_status"],
        }
        for r in rows
    ]
    return {"buyer_email": email, "threads": threads}


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
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped"}


@router.post("/messages/{msg_id}/sold")
async def ma_sold(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> "dict[str, Any]":
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    db.update_message(msg_id, status="skipped_sold")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped_sold"}


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
    de_text = await asyncio.to_thread(
        claude.translate_only, body.text, "ru", target_lang
    )
    ru_back = await asyncio.to_thread(
        claude.translate_only, de_text, target_lang, "ru"
    )
    db.update_message(msg_id, ru_answer=body.text, de_answer=de_text,
                      ru_translation=ru_back, status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
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
    ru_back = await asyncio.to_thread(
        claude.translate_only, body.text, source_lang, "ru"
    )
    db.update_message(msg_id, de_answer=body.text, ru_translation=ru_back,
                      status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
    return _message_review_dict(msg_id)
