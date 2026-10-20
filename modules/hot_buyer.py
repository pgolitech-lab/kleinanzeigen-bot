"""«Горячий покупатель»: письмо, где клиент уже решил купить или договаривается о самовывозе/оплате.

Около 8 % входящих («ich nehme», «heute abholen», «komme morgen», «überweise»,
согласие на цену). От первого письма до продажи — медиана 1 день, решают быстрые,
поэтому такое письмо идёт оператору сразу с пометкой 🔥 и наверх.

Сначала бесплатная эвристика по словам (de/en/pl/ru): 'hot' / 'no' / 'unsure'.
Haiku — только при 'unsure' (на реальных письмах это меньшинство). Ошибка или
лимит LLM — не горячее (пометка — подсказка оператору, не блокировка).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_F = re.IGNORECASE | re.UNICODE


def _rx(*parts: str) -> "re.Pattern[str]":
    return re.compile("|".join(f"(?:{p})" for p in parts), _F)


# Уверенные признаки: решение купить / согласие на цену / забор и оплата.
_STRONG = _rx(
    # de — «ich nehme», «nehme ich», «würde es (gerne) nehmen/kaufen»
    r"\bich\s+nehme\b", r"\bnehme\s+(?:ich|es|sie|ihn|den|die|das)\b",
    r"\b(?:würden?|möchten?|will|wollen)\s+(?:\w+\s+){0,5}?(?:gerne?\s+)?(?:kaufen|nehmen)\b(?![^.?!\n]*\?)",
    r"\bgerne?\s+(?:kaufen|nehmen)\b(?![^.?!\n]*\?)", r"\bkaufe\s+(?:ich|es|sie|ihn|den|die|das)\b",
    r"\b(?:einverstanden|abgemacht|deal)\b",
    r"\b\d{2,5}\s*(?:€|eur|euro)\s+(?:ist|sind|wäre|wären)\s+(?:für\s+mich\s+)?(?:ok|okay|in\s+ordnung|gut)\b",
    r"\b(?:preis|angebot)\s+(?:ist|passt)\s+(?:für\s+mich\s+)?(?:ok|okay|in\s+ordnung|gut)\b",
    r"\b(?:heute|morgen|gleich|sofort)\s+(?:\w+\s+){0,3}?(?:abholen|vorbeikommen)\b",
    r"\bwann\s+(?:kann|könnte|darf)\s+ich\s+(?:\w+\s+){0,4}?(?:abholen|vorbeikommen|kommen)\b",
    r"\b(?:komme|käme|bin)\s+(?:dann\s+)?(?:heute|morgen|gleich)\b",
    r"\b(?:fahre|komme)\s+(?:\w+\s+){0,4}?(?:heute|morgen)\s+(?:\w+\s+){0,4}?(?:los|vorbei)\b",
    r"\bich\s+überweise\b", r"\bgeld\s+(?:habe\s+ich\s+)?(?:gerade\s+)?(?:gesendet|überwiesen|geschickt)\b",
    # en
    r"\bi(?:'ll| will)\s+take\s+(?:it|them)\b", r"\bi\s+(?:want|would\s+like)\s+to\s+buy\b",
    r"\bpick\s+(?:it|them)\s+up\s+(?:today|tomorrow)\b", r"\bi(?:'ll| will)\s+(?:transfer|pay)\b",
    # pl
    r"\bbior[ęe]\b", r"\bkupi[ęe]\b", r"\bodbior[ęe]\b", r"\bprzelew\w*\b",
    # ru
    r"\bберу\b", r"\bкуплю\b", r"\bзаберу\b", r"\bпереведу\b", r"\bдоговорились\b",
)

# Похоже на покупку, но неоднозначно — решает Haiku.
_MEDIUM = _rx(
    r"\babhol\w*\b", r"\bvorbeikommen\b", r"\b(?:heute|morgen)\b[^.?!\n]{0,40}\b\d{1,2}(?::\d{2})?\s*uhr\b",
    r"\b(?:bar|barzahlung|paypal|überweisung)\b", r"\bkaufen\b",
    r"\bpick\s*up\b", r"\btake\s+it\b",
)

# Не горячее, даже если есть слова выше: вопросы «можно ли только часть»,
# отказ/отложенное решение, типичные мошеннические сценарии.
_COOL = _rx(
    r"\b(?:nur|einzeln)\b[^.?!\n]{0,60}\?", r"\bmöglich\b[^.?!\n]{0,60}\?",
    r"\b(?:nächste[nr]?\s+woche|überleg\w*|muss\s+(?:noch\s+)?schauen|melde\s+mich)\b",
    r"\b(?:kein\s+interesse|zu\s+teuer|schon\s+gekauft|anderweitig)\b",
    r"\bpaypal\s+teilt\b", r"\bkonto\s+(?:wurde\s+)?gesperrt\b", r"\bspedition\b", r"\bkurier\b",
)


# Отрицание рядом с сильным признаком («nehme ich leider nicht», «nicht einverstanden»).
_NEG = re.compile(r"\b(?:nicht|kein\w*|leider|not|don't|nie|не)\b", _F)
# Слова-согласия, которые в вопросе ничего не значат («Ist das ein guter Deal?»).
_QUESTIONABLE = re.compile(r"einverstanden|abgemacht|deal|przelew|\d\s*(?:€|eur|euro)", _F)


def _sentence_around(t: str, m: "re.Match[str]") -> str:
    start = max(t.rfind(ch, 0, m.start()) for ch in ".!?\n") + 1
    ends = [i for i in (t.find(ch, m.end()) for ch in ".!?\n") if i != -1]
    end = min(ends) + 1 if ends else len(t)
    return t[start:end]


def heuristic(text: str) -> tuple[str, str]:
    """('hot'|'no'|'unsure', причина). Чистая функция, без сети."""
    t = (text or "").strip()
    if not t:
        return "no", "пусто"
    cool = _COOL.search(t)
    strong = _STRONG.search(t)
    if strong:
        sentence = _sentence_around(t, strong)
        if _NEG.search(sentence):
            return "unsure", f"{strong.group(0)} / отрицание"
        if _QUESTIONABLE.search(strong.group(0)) and sentence.rstrip().endswith("?"):
            return "unsure", f"{strong.group(0)} / вопрос"
    if strong and not cool:
        return "hot", strong.group(0)
    if strong and cool:
        return "unsure", f"{strong.group(0)} / {cool.group(0)}"
    if cool:
        return "no", cool.group(0)
    medium = _MEDIUM.search(t)
    if medium:
        return "unsure", medium.group(0)
    return "no", ""


_HOT_SCHEMA = {
    "type": "object",
    "properties": {
        "hot": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["hot", "reason"],
    "additionalProperties": False,
}


def _ask_haiku(text: str) -> tuple[bool, str]:
    from modules import llm
    client = llm.Client()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=120,
        system="Ты оцениваешь письма покупателей на Kleinanzeigen. Отвечаешь строго по схеме.",
        # Haiku не поддерживает effort/thinking — не передавать
        output_config={"format": {"type": "json_schema", "schema": _HOT_SCHEMA}},
        messages=[{"role": "user", "content": (
            "Покупатель уже РЕШИЛ купить или договаривается о конкретном самовывозе/оплате/времени "
            "встречи? Простые вопросы о товаре, торг без согласия, «а можно только часть?» — НЕ горячий. "
            "Подозрение на мошенничество (оплата через ссылку, курьер, «PayPal сообщает») — НЕ горячий.\n"
            "reason — 3–6 слов по-русски.\n\n"
            f"Письмо:\n{text[:1500]}"
        )}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(raw)
    return bool(data.get("hot")), str(data.get("reason") or "")[:120]


def classify(text: str, use_llm: bool = True) -> tuple[bool, str]:
    """(горячий?, причина). Эвристика; при 'unsure' — Haiku (если use_llm)."""
    verdict, why = heuristic(text)
    if verdict != "unsure":
        return verdict == "hot", why
    if not use_llm:
        return False, f"неясно: {why}"
    try:
        hot, reason = _ask_haiku(text)
        return hot, f"{reason} (Haiku)"
    except Exception as e:
        # Пометка — подсказка; без вердикта считаем не горячим, письмо обрабатывается как обычно.
        logger.warning("hot_buyer: Haiku не ответил (%s) — считаю не горячим", e)
        return False, f"неясно: {why}"


def is_hot_row(row: Any) -> bool:
    """Горячее ли письмо по сохранённой пометке (колонка is_hot, если её уже добавила db)."""
    try:
        return bool(row["is_hot"])
    except (KeyError, IndexError, TypeError):
        return False


def hot_reason_text(reason: Optional[str]) -> str:
    return f"🔥 горячий покупатель: {reason}" if reason else "🔥 горячий покупатель"
