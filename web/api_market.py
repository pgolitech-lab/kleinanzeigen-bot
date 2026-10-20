"""Mini App API: рынок-советник (цена относительно рынка, дубли, перепост, заметность).

Тонкие обёртки над modules/market_advisor.py (контракты ответов — в docstring его функций).
Все маршруты под /api/ma/market/*, с initData. Только чтение: перепост делает человек.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

import database as db
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


def _eur(v: Any) -> "int | None":
    return int(v) if isinstance(v, (int, float)) else None


@router.get("/threads/{thread_id}/market")
def thread_market(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Рынок под объявление треда для строки у чипов торга: «рынок 380–470 €, медиана 420».
    Мало данных — status few_data, поля цены None, text объясняет. Только чтение, без LLM."""
    hist = db.thread_history(thread_id)
    if not hist:
        raise HTTPException(404, "thread not found")
    row = hist[-1]
    ad_id = row["ad_id"] if "ad_id" in row.keys() else None
    title = row["ad_title"] if "ad_title" in row.keys() else None
    if not (ad_id or title):
        return {"status": "no_ad", "text": "У треда нет объявления — рынок не оценить",
                "p25": None, "median": None, "p75": None, "n": 0}
    res = market_advisor.advise_price(ad_id=str(ad_id) if ad_id else None, title=title)
    out = {"status": res.get("status"), "n": res.get("n", 0), "p25": _eur(res.get("p25")),
           "median": _eur(res.get("median")), "p75": _eur(res.get("p75")),
           "recommended_eur": _eur(res.get("recommended_eur")), "min_eur": _eur(res.get("min_eur")),
           "verdict_ru": res.get("verdict_ru")}
    if out["median"] is not None:
        out["text"] = f"рынок {out['p25']}–{out['p75']} €, медиана {out['median']} · {out['n']} объявл."
    else:
        out["text"] = res.get("verdict_ru") or "мало данных по рынку"
    return out
