# Генерация и регенерация черновиков ответов (ручной режим, по кнопкам оператора).
# Публичный API (реэкспорт в scheduler):
#   generate_draft_for_msg — первый полноценный черновик (объявление, бриф, история)
#   regenerate_draft, regenerate_draft_with_price, regenerate_draft_with_instruction
#   _regen_context, _save_regen_result

import json
import logging
from typing import Any

import database as db
from modules import claude

logger = logging.getLogger(__name__)

_FINAL_STATUSES = ("sent", "sent_debug", "skipped", "skipped_sold", "not_sent_disabled", "sending")


def _final_msg(message_id: int, status: str) -> str:
    return (f"❌ Письмо #{message_id} уже обработано ({status}) — черновик не перезаписываю. "
            "Для нового сообщения клиенту используйте «✉️ Написать».")


def _regen_context(msg: Any) -> tuple[list, str, list]:
    """Контекст генерации по сохранённому письму: (история до письма, бриф, уроки).

    Уроки — всегда [] (таблица lessons не наполняется); элемент кортежа оставлен
    ради совместимости с api_ma (превью автопилота).
    """
    history, brief_text = claude.draft_context(msg)
    return history, brief_text, []


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
    if msg["status"] in _FINAL_STATUSES:
        return {"kind": "error", "message": _final_msg(message_id, msg["status"])}
    history, brief_text = claude.draft_context(msg)
    try:
        result = claude.generate_draft(
            msg["de_client"] or "",
            ad_title=msg["ad_title"] or "",
            ad_price=msg["ad_price"] or "",
            ad_description=msg["ad_description"] or "",
            seller_name=msg["seller_name"] or "",
            history=history, brief_text=brief_text,
            client_lang=msg["client_lang"] or None,
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
    if msg["status"] in _FINAL_STATUSES:
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
