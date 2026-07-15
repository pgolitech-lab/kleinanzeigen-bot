# Детерминированные guardrails для негоциации — «код владеет числами».
# Чистые функции без побочных эффектов и без обращений к БД/сети. Вешаются на
# авто-отправку автопилота в Инкременте 3; здесь — только сами примитивы + тесты.

import json
import re
from typing import Any, Optional


def reconcile_floor(
    ad_min_eur: Optional[float],
    operator_floor_eur: Optional[float],
) -> Optional[float]:
    """Единый эффективный пол = максимум из ad_brief-минимума и операторского
    floor (оператор может только УЖЕСТОЧИТЬ, не опустить ниже ad-min).
    None, если ни одно значение не является положительным числом."""
    vals = [float(v) for v in (ad_min_eur, operator_floor_eur)
            if v is not None and float(v) > 0]
    return max(vals) if vals else None


def floor_violation(
    proposed_eur: Optional[float],
    floor_eur: Optional[float],
) -> bool:
    """True, если предложенная цена ниже пола (нарушение). Если любая из величин
    None — сравнивать нечего, нарушения нет."""
    if proposed_eur is None or floor_eur is None:
        return False
    return float(proposed_eur) < float(floor_eur)


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def outgoing_language_ok(text: str, client_lang: Optional[str]) -> bool:
    """Алфавитная санити-проверка исходящего клиенту текста.

    Ловит катастрофу — когда вместо переведённого client_answer уходит русский
    черновик ru_answer (или наоборот). Кириллица допустима ТОЛЬКО при
    client_lang='ru'; для не-ru языков кириллица = провал, а для 'ru' —
    наоборот требуем кириллицу. Пустой текст → провал (нечего слать)."""
    if not text or not text.strip():
        return False
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    lang = (client_lang or "").strip().lower()
    if lang == "ru":
        return has_cyrillic
    return not has_cyrillic


def extract_json_object(text_blocks: list[str]) -> dict[str, Any]:
    """Устойчиво достать JSON-объект из текстовых блоков ответа модели.

    С web_search модель может вернуть блоки вида [нарратив, tool, JSON, хвост],
    и внутри ОДНОГО блока может быть несколько объектов (черновик + финал).
    Предпочитаем ПОСЛЕДНИЙ валидный dict в порядке чтения (финальный ответ):
    идём по всем блокам и всем сбалансированным {...} слева-направо, пробуем
    json.loads, запоминаем последний успешный dict. RuntimeError, если ни один
    блок не содержит валидного JSON-объекта."""
    last_valid: Optional[dict[str, Any]] = None
    for block in text_blocks:
        if not block:
            continue
        for candidate in _iter_json_objects(block):
            try:
                data = json.loads(candidate)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                last_valid = data
    if last_valid is None:
        raise RuntimeError("no JSON object found in model response")
    return last_valid


def _iter_json_objects(text: str):
    """Генератор: все top-level сбалансированные {...}-подстроки в порядке чтения."""
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return
        end = _balanced_end(text, start)
        if end is None:
            idx = start + 1
            continue
        yield text[start:end + 1]
        idx = end + 1


def _balanced_end(text: str, start: int) -> Optional[int]:
    """Индекс закрывающей }, балансирующей { в позиции start; None если не
    сбалансировано. Корректно учитывает вложенность и строковые литералы с
    экранированием."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None
