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


# ── типы, стандартные себестоимости, комплекты, партии, «не склад», приход строкой ──

@router.get("/stock/types")
def stock_types(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Очередь «какой тип?» + справочник типов со стандартной себестоимостью."""
    return {"questions": inventory.list_type_questions(), "table": inventory.category_cost_table()}


class TypeBody(BaseModel):
    product_id: int
    category: str = Field(..., min_length=1, max_length=100)
    unit_cost_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)


@router.post("/stock/types")
def stock_set_type(body: TypeBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(body.product_id) is None:
        raise HTTPException(404, "товар не найден")
    return _call(inventory.set_type, body.product_id, body.category, unit_cost_eur=body.unit_cost_eur)


class CategoryCostBody(BaseModel):
    category: str = Field(..., min_length=1, max_length=100)
    unit_cost_eur: Optional[float] = Field(None, ge=0, lt=1_000_000)  # null = убрать стандарт


@router.put("/stock/category-costs")
def stock_category_cost(body: CategoryCostBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    _call(inv_db.set_category_cost, body.category.strip(), body.unit_cost_eur, _actor(user))
    return {"table": inventory.category_cost_table()}


class KitComponent(BaseModel):
    product_id: int
    qty: int = Field(..., ge=1, le=1000)


class KitBody(BaseModel):
    components: list[KitComponent] = Field(default_factory=list, max_length=30)  # пусто = не комплект


@router.put("/stock/products/{product_id}/kit")
def stock_kit(product_id: int, body: KitBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(product_id) is None:
        raise HTTPException(404, "товар не найден")
    comps = [(c.product_id, c.qty) for c in body.components]
    missing = [pid for pid, _ in comps if inv_db.get_product(pid) is None]
    if missing:
        raise HTTPException(404, f"товар не найден: {missing[0]}")
    return {"stock": _call(inventory.set_kit, product_id, comps)}


class BatchItem(BaseModel):
    product_id: int
    qty: int = Field(..., ge=1, le=100_000)


class BatchBody(BaseModel):
    items: list[BatchItem] = Field(..., min_length=1, max_length=50)
    total_cost_eur: Optional[float] = Field(None, ge=0, lt=10_000_000)  # null = себестоимость неизвестна
    occurred_at: Optional[str] = None
    note: Optional[str] = Field(None, max_length=500)


@router.post("/stock/batches")
def stock_batch(body: BatchBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    missing = [i.product_id for i in body.items if inv_db.get_product(i.product_id) is None]
    if missing:
        raise HTTPException(404, f"товар не найден: {missing[0]}")
    return _call(inventory.receive_batch, [(i.product_id, i.qty) for i in body.items], body.total_cost_eur,
                 occurred_at=body.occurred_at, note=body.note, created_by=_actor(user))


class NonStockBody(BaseModel):
    non_stock: bool = True


@router.post("/stock/products/{product_id}/non-stock")
def stock_non_stock(product_id: int, body: Optional[NonStockBody] = None,
                    user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    if inv_db.get_product(product_id) is None:
        raise HTTPException(404, "товар не найден")
    _call(inventory.set_non_stock, product_id, (body or NonStockBody()).non_stock)
    return {"product": _row(inv_db.get_product(product_id))}


class ReceiptParseBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


_RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "product_id": {"type": ["integer", "null"], "description": "id из списка товаров или null"},
                "title": {"type": "string"},
                "qty": {"type": "integer"},
            },
            "required": ["product_id", "title", "qty"],
            "additionalProperties": False,
        }},
        "total_cost_eur": {"type": ["number", "null"], "description": "общая сумма в евро или null"},
    },
    "required": ["items", "total_cost_eur"],
    "additionalProperties": False,
}


@router.post("/stock/receipt/parse")
def stock_receipt_parse(body: ReceiptParseBody, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Приход одной строкой («сиденья пассат 2шт, фаркоп 1, всего 150 €») → предпросмотр партии.
    Ничего не пишет: оператор подтверждает, фронт шлёт POST /stock/batches. Разбор — Haiku по списку товаров.
    items[].product_id = null — товар не распознан (создать/выбрать вручную)."""
    from modules import llm
    from modules.claude_utils import call_json, fast_model
    products = [{"id": r["id"], "title": r["title"]} for r in inv_db.list_products()][:300]
    try:
        data, _meta = call_json(
            label="приход строкой", model=fast_model(),
            system="Ты разбираешь строку прихода на склад автозапчастей. Отвечаешь строго по схеме.",
            user=("Разбери строку прихода: позиции, количество (по умолчанию 1) и общую сумму в евро, если названа. "
                  "Для каждой позиции укажи id товара из списка, если он явно тот же; иначе null. "
                  "Ничего не выдумывай.\n\nТовары:\n"
                  + "\n".join(f"{p['id']}: {p['title']}" for p in products)
                  + f"\n\nСтрока:\n{body.text.strip()}"),
            schema=_RECEIPT_SCHEMA, max_tokens=800, effort=None)
    except llm.LLMLimitError as e:
        raise HTTPException(503, str(e))
    except llm.LLMError as e:
        logger.warning("разбор прихода не удался: %s", e)
        raise HTTPException(502, f"LLM не ответил: {e}")
    known = {p["id"] for p in products}
    items = []
    for it in data.get("items", []):
        try:
            qty = int(it["qty"]) if it.get("qty") is not None else 1
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        pid = it.get("product_id")
        items.append({"product_id": pid if pid in known else None, "title": (it.get("title") or "").strip(),
                      "qty": qty})
    if not items:
        raise HTTPException(422, "в строке не нашлось позиций")
    items = items[:50]  # как лимит POST /stock/batches
    total = data.get("total_cost_eur")
    return {"items": items, "total_cost_eur": total if isinstance(total, (int, float)) and total >= 0 else None,
            "unmatched": sum(1 for i in items if i["product_id"] is None)}
