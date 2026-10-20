"""Mini App API: рынок-советник (цена относительно рынка, дубли, перепост, заметность).

Тонкие обёртки над modules/market_advisor.py (контракты ответов — в docstring его функций).
Все маршруты под /api/ma/market/*, с initData. Только чтение: перепост делает человек.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from modules import market_advisor
from modules.tg_init_data import verify_init_data_dep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ma", tags=["Mini App — рынок"])


def _item(res: dict[str, Any]) -> dict[str, Any]:
    """Советник вернул not_found — это 404, а не пустой 200."""
    if res.get("status") == "not_found" or (res.get("ok") is False and not res.get("item")):
        raise HTTPException(404, "товар не найден")
    return res


@router.get("/market/advice/{product_id}")
def market_advice(product_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _item(market_advisor.advise_price(product_id=product_id))


@router.get("/market/duplicates")
def market_duplicates(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return market_advisor.duplicates_report()


@router.get("/market/repost")
def market_repost(limit: int = Query(5, ge=1, le=20), with_text: bool = True,
                  user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    items = market_advisor.repost_suggestions(limit=limit, with_text=with_text)
    return {"items": items, "count": len(items)}


@router.get("/market/tips/{product_id}")
def market_tips(product_id: int, use_llm: bool = True,
                user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _item(market_advisor.visibility_tips(product_id=product_id, use_llm=use_llm))
