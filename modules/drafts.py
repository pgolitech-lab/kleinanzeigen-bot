# Генерация и регенерация черновиков ответов.
# Выделено из scheduler.py. Публичный API:
#   generate_draft_for_msg, drain_deferred_thread, drain_all_deferred
#   regenerate_draft, regenerate_draft_with_price, regenerate_draft_with_instruction

import json
import logging
from typing import Any, Optional

import config
import database as db
from modules import ad_brief, claude, telegram_bot

logger = logging.getLogger(__name__)

def generate_draft_for_msg(message_id: int) -> dict[str, Any]:
    """Sonnet-генерация ответа для уже сохранённой row (триггер «🤖 Предложить ответ»).

    Откладывается из `_process_incoming` чтобы оператор сначала видел тред целиком
    и решал вручную нужен ли draft (vs. ⏳ Ждать / 🏁 Завершить / 🚀 Автопилот).
    Делает то же что раньше делалось upfront в _process_incoming для manual flow:
      — собирает history / brief / lessons
      — вызывает claude.generate_reply (RU + DE + ru_translation + deal_brief)
      — генерит cheap Haiku-summary (если есть история)
      — обновляет row, status='pending'
    Возвращает {kind: 'generated' | 'error', message, cost_usd}.
    """
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"msg #{message_id} не найден"}
    if msg["status"] in ("sent", "sent_debug", "skipped", "skipped_sold", "not_sent_disabled"):
        return {"kind": "error",
                "message": f"msg #{message_id} в финальном состоянии ({msg['status']}), draft не нужен"}

    account = db.get_account(msg["account_id"])
    if not account:
        return {"kind": "error", "message": f"msg #{message_id}: аккаунт удалён"}

    thread_id = msg["gmail_thread_id"] or ""
    body = msg["de_client"] or ""
    ad_title = msg["ad_title"] or ""
    ad_price = msg["ad_price"] or ""
    ad_description = msg["ad_description"] or ""
    seller_name = msg["seller_name"] or ""
    ad_id_val = msg["ad_id"] if "ad_id" in msg.keys() else None

    # История (без текущего incoming)
    history = [
        h for h in claude.history_for(thread_id)
        if h.get("de_client") != body
    ]

    # Бриф объявления
    brief_text = ""
    if ad_id_val:
        cached = db.get_ad_brief(ad_id_val)
        if cached:
            try:
                key_facts = json.loads(cached["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                key_facts = {}
            brief_text = ad_brief.format_brief_for_claude(cached["brief_md"], key_facts)

    lessons_rows = db.find_relevant_lessons(
        ad_id=ad_id_val, account_id=account["id"], limit=5,
    )
    lessons_payload = [dict(r) for r in lessons_rows]

    try:
        reply = claude.generate_reply(
            de_client_text=body,
            ad_title=ad_title,
            ad_price=ad_price,
            ad_description=ad_description,
            seller_name=seller_name,
            history=history,
            brief_text=brief_text,
            lessons=lessons_payload,
        )
    except Exception as e:
        logger.exception("generate_draft Sonnet упал для msg=%s: %s", message_id, e)
        return {"kind": "error", "message": f"Sonnet упал: {e}"}

    extra_cost = 0.0
    summary_ru = None
    if history:
        full_history = [
            dict(r) for r in db.thread_history(thread_id)
            if r["id"] != message_id and not _row_get(r, "is_auto_ack")
        ]
        if full_history:
            try:
                sm = claude.summarize_thread(full_history)
                summary_ru = sm["summary_ru"]
                extra_cost += sm["cost_usd"]
            except Exception:
                logger.exception("summarize_thread fail msg=%s", message_id)

    deal_brief_json = (
        json.dumps(reply["deal_brief"], ensure_ascii=False) if reply.get("deal_brief") else None
    )

    prior_cost = msg["cost_usd"] or 0.0
    db.update_message(
        message_id,
        ru_answer=reply["ru_answer"],
        de_answer=reply["de_answer"],
        ru_translation=reply.get("ru_translation"),
        client_lang=reply.get("client_lang") or msg["client_lang"],
        tokens_in=(msg["tokens_in"] or 0) + (reply.get("tokens_in") or 0),
        tokens_out=(msg["tokens_out"] or 0) + (reply.get("tokens_out") or 0),
        cost_usd=prior_cost + (reply.get("cost_usd", 0.0) or 0.0) + extra_cost,
        history_summary_ru=summary_ru,
        deal_brief_json=deal_brief_json,
        status="pending",
    )
    total_cost = (reply.get("cost_usd", 0.0) or 0.0) + extra_cost
    logger.info(
        "Draft сгенерён для msg=%s (cost=$%.4f, status='pending')",
        message_id, total_cost,
    )
    return {"kind": "generated", "cost_usd": total_cost,
            "message": f"✅ Draft готов (стоило ${total_cost:.4f})"}



def _regen_context(msg: Any) -> tuple[list, str, list]:
    """Собрать контекст для регенерации: history (без текущего), brief_text, lessons.
    Используется во всех `regenerate_draft_*` обёртках.
    """
    thread_id = msg["gmail_thread_id"] or ""
    history = claude.history_for(thread_id) if thread_id else []
    history = [h for h in history if h.get("de_client") != msg["de_client"]]

    brief_text = ""
    ad_id_val = msg["ad_id"] if "ad_id" in msg.keys() else None
    if ad_id_val:
        bf = db.get_ad_brief(ad_id_val)
        if bf:
            try:
                kf = json.loads(bf["key_facts_json"] or "{}")
            except json.JSONDecodeError:
                kf = {}
            brief_text = ad_brief.format_brief_for_claude(bf["brief_md"], kf)

    lessons = [dict(r) for r in db.find_relevant_lessons(
        ad_id=ad_id_val, account_id=msg["account_id"], limit=5,
    )]
    return history, brief_text, lessons


def _save_regen_result(message_id: int, msg: Any, result: dict[str, Any]) -> None:
    """Сохранить результат регенерации в БД (накапливая cost/tokens + deal_brief)."""
    prior_in = msg["tokens_in"] or 0
    prior_out = msg["tokens_out"] or 0
    prior_cost = msg["cost_usd"] or 0.0
    fields: dict[str, Any] = dict(
        ru_answer=result["ru_answer"],
        de_answer=result["client_answer"],
        ru_translation=result.get("ru_translation"),
        status="edited",
        tokens_in=prior_in + result["tokens_in"],
        tokens_out=prior_out + result["tokens_out"],
        cost_usd=prior_cost + result["cost_usd"],
    )
    if result.get("deal_brief"):
        fields["deal_brief_json"] = json.dumps(result["deal_brief"], ensure_ascii=False)
    db.update_message(message_id, **fields)


def regenerate_draft(message_id: int, strategy: str) -> dict[str, Any]:
    """Перегенерировать драфт для msg_id под preset-стратегию (fest/harsh/friend/short/regen)."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_strategy(
            msg, strategy=strategy,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft упал msg=%s strategy=%s", message_id, strategy)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"🔄 Драфт перегенерирован ({strategy})", "result": result}


def regenerate_draft_with_price(message_id: int, price_eur: float) -> dict[str, Any]:
    """Перегенерировать драфт под конкретную цену от оператора."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_price(
            msg, price_eur=price_eur,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_price упал msg=%s price=%s", message_id, price_eur)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"💸 Драфт перегенерирован под цену {price_eur}€", "result": result}


def regenerate_draft_with_instruction(message_id: int, instruction: str) -> dict[str, Any]:
    """Перегенерировать драфт по операторской свободной инструкции."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    history, brief_text, lessons = _regen_context(msg)
    try:
        result = claude.regenerate_with_instruction(
            msg, instruction=instruction,
            brief_text=brief_text, history=history, lessons=lessons,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_instruction упал msg=%s", message_id)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"📝 Драфт перегенерирован по инструкции", "result": result}


