"""Mini App API: склад (товары, движения, вопросы «тот же товар?», отчёты).

Тонкие обёртки над modules/inventory.py и modules/db_inventory.py (логика — там, docs/optimization/stock.md).
Все маршруты под /api/ma/stock/* и /api/ma/threads/{id}/sale-price, с initData. ValueError склада → 400.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from modules import db_inventory as inv_db
from modules import inventory
from modules.tg_init_data import verify_init_data_dep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ma", tags=["Mini App — склад"])


def _actor(user: dict) -> str:
    from web.api_ma import actor_from_user
    return actor_from_user(user)


def _row(r: Any) -> Optional[dict[str, Any]]:
    return dict(r) if r is not None else None


def _call(fn, *args, **kwargs):
    """Ошибки предметной области склада (нет товара, qty ≤ 0, неизвестное поле) — 400 с текстом."""
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── отчёты ──

@router.get("/stock/report")
def stock_report(from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                 user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _call(inventory.report, from_, to)


@router.get("/stock/month")
def stock_month(year: int, month: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if not 1 <= month <= 12:
        raise HTTPException(400, "month: 1..12")
    return _call(inventory.monthly_report, year, month)


@router.get("/threads/{thread_id}/sale-price")
def thread_sale_price(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Подсказка цены для «Продано» (цена обязательна — фронт предзаполняет её)."""
    return _call(inventory.suggest_sale_price, thread_id)


# ── товары ──

class ProductBody(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    category: Optional[str] = Field(None, max_length=100)
    condition: Optional[str] = Field(None, max_length=50)
    unit_cost_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)
    list_price_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)
    min_price_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)
    note: Optional[str] = Field(None, max_length=2000)


@router.get("/stock/products")
def stock_products(include_archived: bool = False,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    items = [dict(r) for r in inv_db.list_products(include_archived=include_archived)]
    return {"items": items, "count": len(items)}


@router.get("/stock/products/{product_id}")
def stock_product(product_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    p = inv_db.get_product(product_id)
    if p is None:
        raise HTTPException(404, "товар не найден")
    listings = [dict(r) for r in inv_db.list_listings(product_id)]
    # Состояние склада по любому живому объявлению товара (остаток, цены) — для шапки карточки
    ctx = inventory.ad_context_safe(listings[0]["ad_id"]) if listings else None
    return {"product": dict(p), "listings": listings,
            "movements": [dict(r) for r in inv_db.list_movements(product_id)], "context": ctx}


@router.post("/stock/products")
def stock_product_create(body: ProductBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if not (body.title or "").strip():
        raise HTTPException(400, "название товара обязательно")
    fields = {k: v for k, v in body.model_dump().items() if v is not None and k != "title"}
    pid = _call(inv_db.create_product, title=body.title.strip(), **fields)
    return {"id": pid, "product": _row(inv_db.get_product(pid))}


@router.patch("/stock/products/{product_id}")
def stock_product_update(product_id: int, body: ProductBody,
                         user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(product_id) is None:
        raise HTTPException(404, "товар не найден")
    fields = body.model_dump(exclude_unset=True)  # переданный null = очистить поле
    if not fields:
        raise HTTPException(400, "нечего менять")
    if "title" in fields:
        if not (fields["title"] or "").strip():
            raise HTTPException(400, "название товара не может быть пустым")
        fields["title"] = fields["title"].strip()
        from modules.inventory import match_key
        fields["match_key"] = match_key(fields["title"])  # иначе автопривязка искала бы по старому названию
    _call(inv_db.update_product, product_id, **fields)
    return {"product": _row(inv_db.get_product(product_id))}


class QtyBody(BaseModel):
    qty: int = Field(..., ge=0, le=100_000)
    unit_cost_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)  # только приход
    occurred_at: Optional[str] = None  # только приход
    note: Optional[str] = Field(None, max_length=500)


def _qty_positive(body: QtyBody) -> None:
    if body.qty <= 0:
        raise HTTPException(400, "количество должно быть больше 0")


@router.post("/stock/products/{product_id}/receipt")
def stock_receipt(product_id: int, body: QtyBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    _qty_positive(body)
    mid = _call(inventory.receive, product_id, body.qty, body.unit_cost_eur,
                occurred_at=body.occurred_at, note=body.note, created_by=_actor(user))
    return {"movement_id": mid, "qty": inv_db.stock_qty(product_id)}


@router.post("/stock/products/{product_id}/writeoff")
def stock_writeoff(product_id: int, body: QtyBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    _qty_positive(body)
    mid = _call(inventory.write_off, product_id, body.qty, note=body.note, created_by=_actor(user))
    return {"movement_id": mid, "qty": inv_db.stock_qty(product_id)}


@router.post("/stock/products/{product_id}/count")
def stock_count(product_id: int, body: QtyBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Инвентаризация: «сейчас на складе qty шт.» (0 — допустимо)."""
    mid = _call(inventory.set_stock, product_id, body.qty, note=body.note, created_by=_actor(user))
    return {"movement_id": mid, "qty": inv_db.stock_qty(product_id)}


class ArchiveBody(BaseModel):
    archived: bool = True


@router.post("/stock/products/{product_id}/archive")
def stock_archive(product_id: int, body: Optional[ArchiveBody] = None,
                  user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    _call(inv_db.archive_product, product_id, (body or ArchiveBody()).archived)
    return {"product": _row(inv_db.get_product(product_id))}


class MergeBody(BaseModel):
    src_id: int
    dst_id: int


@router.post("/stock/merge")
def stock_merge(body: MergeBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    res = _call(inv_db.merge_products, body.src_id, body.dst_id)
    logger.info("склад: товар %s слит в %s (%s)", body.src_id, body.dst_id, _actor(user))
    return {"ok": True, **res}


class RelinkBody(BaseModel):
    product_id: int


@router.post("/stock/listings/{ad_id}/relink")
def stock_relink(ad_id: str, body: RelinkBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(body.product_id) is None:
        raise HTTPException(404, "товар не найден")
    _call(inv_db.relink_listing, ad_id, body.product_id)
    return {"listing": _row(inv_db.get_listing(ad_id))}


# ── вопросы «тот же товар?» и продажи без товара ──

@router.get("/stock/questions")
def stock_questions(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    items = [dict(r) for r in inv_db.list_open_questions()]
    return {"items": items, "count": len(items)}


class AnswerBody(BaseModel):
    same: bool


@router.post("/stock/questions/{question_id}")
def stock_answer(question_id: int, body: AnswerBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _call(inventory.answer_question, question_id, body.same)


@router.get("/stock/unlinked-sales")
def stock_unlinked_sales(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    items = inventory.unlinked_sales()
    return {"items": items, "count": len(items)}


class LinkSaleBody(BaseModel):
    product_id: int
    price_eur: float = Field(..., gt=0, lt=1_000_000)
    qty: int = Field(1, ge=1, le=1000)


@router.post("/stock/unlinked-sales/{thread_id}")
def stock_link_sale(thread_id: str, body: LinkSaleBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(body.product_id) is None:
        raise HTTPException(404, "товар не найден")
    return _call(inventory.record_sale, product_id=body.product_id, gmail_thread_id=thread_id,
                 price_eur=body.price_eur, qty=body.qty, source="thread", created_by=_actor(user))
