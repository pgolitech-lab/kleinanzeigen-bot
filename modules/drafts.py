# Генерация и регенерация черновиков ответов (ручной режим, по кнопкам оператора).
# Публичный API (реэкспорт в scheduler):
#   generate_draft_for_msg — первый полноценный черновик (объявление, бриф, история)
#   regenerate_draft, regenerate_draft_with_price, regenerate_draft_with_instruction
#   _regen_context, _save_regen_result

import json
import logging
from typing import Any

import database as db
from modules import claude, llm_memory

logger = logging.getLogger(__name__)

def _final_msg(message_id: int, status: str) -> str:
    return (f"❌ Письмо #{message_id} уже обработано ({status}) — черновик не перезаписываю. "
            "Для нового сообщения клиенту используйте «✉️ Написать».")


def _regen_context(msg: Any) -> tuple[list, str, list]:
    """Контекст генерации по сохранённому письму: (история до письма, бриф, уроки).

    Уроки — строки lessons (api_ma передаёт их в превью автопилота).
    """
    history, brief_text = claude.draft_context(msg)
    try:
        lessons = llm_memory.lessons_for(msg)
    except Exception:  # noqa: BLE001 — уроки вспомогательные
        logger.exception("уроки не загружены для msg=%s", msg["id"])
        lessons = []
    return history, brief_text, lessons


def _save_regen_result(message_id: int, msg: Any, result: dict[str, Any], *,
                       first_draft: bool = False) -> None:
    """Сохранить результат генерации в БД (накапливая cost/tokens + deal_brief)."""
    fields: dict[str, Any] = dict(
        ru_answer=result["ru_answer"],
        de_answer=result["client_answer"],
        ru_translation=result.get("ru_translation"),
        status="edited",
        tokens_in=(msg["tokens_in"] or 0) + (result.get("tokens_in") or 0),
        tokens_out=(msg["tokens_out"] or 0) + (result.get("tokens_out") or 0),
        cost_usd=(msg["cost_usd"] or 0.0) + (result.get("cost_usd") or 0.0),
    )
    if first_draft and not msg["client_lang"] and result.get("client_lang"):
        fields["client_lang"] = result["client_lang"]
    fields.update(llm_memory.llm_draft_fields(result["client_answer"], result["ru_answer"]))
    if result.get("deal_brief"):
        fields["deal_brief_json"] = json.dumps(result["deal_brief"], ensure_ascii=False)
    db.update_message(message_id, **fields)


def generate_draft_for_msg(message_id: int) -> dict[str, Any]:
    """Первый черновик для сохранённого письма (кнопка «🤖 Предложить»).

    Полный контекст: объявление из row, бриф из кэша, история треда до письма
    (вопросы клиента и наши отправленные ответы).
    Возвращает {kind: 'generated' | 'error', message, result?}.
    """
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    if claude.is_final(msg):
        return {"kind": "error", "message": _final_msg(message_id, msg["status"])}
    history, brief_text = claude.draft_context(msg)
    memory_text, lessons_text = claude.extra_context(msg)
    try:
        result = claude.generate_draft(
            msg["de_client"] or "",
            ad_title=msg["ad_title"] or "",
            ad_price=msg["ad_price"] or "",
            ad_description=msg["ad_description"] or "",
            seller_name=msg["seller_name"] or "",
            history=history, brief_text=brief_text,
            client_lang=msg["client_lang"] or None,
            memory_text=memory_text, lessons_text=lessons_text,
            ad_state=claude.ad_state_for(msg),
        )
    except Exception as e:
        logger.exception("generate_draft_for_msg упал msg=%s", message_id)
        return {"kind": "error", "message": f"❌ Генерация черновика упала: {e}"}
    _save_regen_result(message_id, msg, result, first_draft=True)
    logger.info("Черновик сгенерирован для msg=%s (история: %d строк, бриф: %s)",
                message_id, len(history), "да" if brief_text else "нет")
    return {"kind": "generated", "message": "✅ Черновик готов", "result": result}


def regenerate_draft(message_id: int, strategy: str) -> dict[str, Any]:
    """Перегенерировать драфт под пресет (fest/harsh/friend/short/regen).

    «regen» без существующего черновика = первый черновик (так «🤖 Предложить»
    приходит из api: suggest-reply зовёт regenerate_draft(msg, 'regen')).
    """
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    if claude.is_final(msg):
        return {"kind": "error", "message": _final_msg(message_id, msg["status"])}
    if strategy == "regen" and not (msg["de_answer"] or "").strip():
        return generate_draft_for_msg(message_id)
    history, brief_text, _ = _regen_context(msg)
    try:
        result = claude.regenerate_with_strategy(
            msg, strategy=strategy, brief_text=brief_text, history=history,
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
    if claude.is_final(msg):
        return {"kind": "error", "message": _final_msg(message_id, msg["status"])}
    history, brief_text, _ = _regen_context(msg)
    try:
        result = claude.regenerate_with_price(
            msg, price_eur=price_eur, brief_text=brief_text, history=history,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_price упал msg=%s price=%s", message_id, price_eur)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": f"💸 Драфт перегенерирован под цену {price_eur}€", "result": result}


def regenerate_draft_with_instruction(message_id: int, instruction: str) -> dict[str, Any]:
    """Перегенерировать драфт по свободной инструкции оператора."""
    msg = db.get_message(message_id)
    if not msg:
        return {"kind": "error", "message": f"❌ msg={message_id} не найдено"}
    if claude.is_final(msg):
        return {"kind": "error", "message": _final_msg(message_id, msg["status"])}
    history, brief_text, _ = _regen_context(msg)
    try:
        result = claude.regenerate_with_instruction(
            msg, instruction=instruction, brief_text=brief_text, history=history,
        )
    except Exception as e:
        logger.exception("regenerate_draft_with_instruction упал msg=%s", message_id)
        return {"kind": "error", "message": f"❌ Перегенерация упала: {e}"}
    _save_regen_result(message_id, msg, result)
    return {"kind": "regenerated", "message": "📝 Драфт перегенерирован по инструкции", "result": result}


# ─────────────── Подсказки оператору (ничего не пишут в БД и не отправляют) ───────────────

def _latest_incoming(thread_id: str) -> Any:
    rows = db.thread_history(thread_id)
    return next((r for r in reversed(rows) if r["direction"] == "in"), None), rows


def variants(msg_id: int, tones: "list[str] | None" = None) -> dict[str, Any]:
    """Черновики под тона (hold / minus5 / floor / decline) одним вызовом LLM.

    Цены считает код, пол не нарушается (варианты ниже пола отбрасываются в `dropped`).
    Возвращает claude.generate_variants(...). Ошибки: LookupError (нет письма),
    claude.SuggestError (ValueError: письмо уже обработано, тоны неприменимы),
    llm.LLMError/LLMLimitError/LLMResponseError — текст пригоден для оператора.
    """
    msg = db.get_message(msg_id)
    if msg is None:
        raise LookupError(f"письмо #{msg_id} не найдено")
    return claude.generate_variants(msg, tones)


def quick_reply(msg_id: int, kind: str, account_settings: "dict[str, Any] | None" = None) -> dict[str, Any]:
    """Типовой короткий ответ (available / last_price / delivery / pickup / payment) на языке
    покупателя + RU-зеркало (Haiku). account_settings — факты продавца: delivery / pickup / payment.
    Возвращает claude.generate_quick_reply(...); claude.SuggestError — не хватает факта/цены."""
    msg = db.get_message(msg_id)
    if msg is None:
        raise LookupError(f"письмо #{msg_id} не найдено")
    return claude.generate_quick_reply(msg, kind, account_settings)


def followup_ping(thread_id: str) -> dict[str, Any]:
    """Напоминание клиенту, который молчит после нашего ответа.

    Возвращает claude.generate_followup_ping(...) + days_silent, anchor_msg_id.
    claude.SuggestError (ValueError) — последним писал клиент или тред закрыт.
    """
    from datetime import datetime
    anchor, rows = _latest_incoming(thread_id)
    if anchor is None:
        raise LookupError(f"в треде {thread_id} нет входящих писем")
    if claude.is_final(anchor) and anchor["status"] in ("skipped", "skipped_sold"):
        raise claude.SuggestError("тред закрыт оператором — напоминание не нужно")
    # События — только РЕАЛЬНО отправленное нами (черновик pending клиент не видел)
    events: list[tuple[str, str]] = []
    for r in rows:
        if r["direction"] == "in" and r["de_client"]:
            events.append((r["created_at"] or "", "in"))
        if r["de_answer"] and (r["status"] in claude.SENT_STATUSES or r["sent_at"]):
            events.append((r["sent_at"] or r["created_at"] or "", "out"))
    events.sort()
    if not events or events[-1][1] != "out":
        raise claude.SuggestError("последним писал клиент — нужен ответ, а не напоминание")
    try:
        last = datetime.fromisoformat(events[-1][0][:19])
        days = max(0.0, (datetime.utcnow() - last).total_seconds() / 86400)
    except ValueError:
        logger.warning("followup_ping %s: не разобрал дату %r — считаю 1 день", thread_id, events[-1][0])
        days = 1.0
    lang = anchor["client_lang"] or next((r["client_lang"] for r in reversed(rows) if r["client_lang"]), None) or "de"
    res = claude.generate_followup_ping(anchor, days_silent=days, client_lang=lang)
    res.update(days_silent=round(days, 1), anchor_msg_id=anchor["id"])
    return res
