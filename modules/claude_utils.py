# Утилиты LLM: модели, подсчёт стоимости и единая функция вызова с JSON-схемой.
# Импортируется из modules/claude.py, modules/ad_brief.py, modules/claude_scout.py.

import json
import logging
from typing import Any, Optional

import config
from modules import llm

logger = logging.getLogger(__name__)

# Быстрая/дешёвая модель: классификатор, переводы, auto-ack, детектор языка.
# Если в config появится геттер `claude_fast_model` (модуль db) — берём его.
FAST_MODEL_DEFAULT = "claude-haiku-4-5"


def smart_model() -> str:
    """Основная модель (черновики, автопилот, бриф) — из настроек."""
    return config.claude_model()


def fast_model() -> str:
    getter = getattr(config, "claude_fast_model", None)
    if callable(getter):
        try:
            m = getter()
            if m:
                return m
        except Exception:  # noqa: BLE001
            logger.warning("config.claude_fast_model() упал — беру %s", FAST_MODEL_DEFAULT, exc_info=True)
    return FAST_MODEL_DEFAULT


# Цены за миллион токенов в USD (на 2026-05-01). Через подписку cost = 0,
# таблица нужна только если когда-нибудь вернёмся на API.
PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30},
    "claude-sonnet-4-7": {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30},
    "claude-opus-4-6":   {"in": 15.0, "out": 75.0, "cache_w": 18.75, "cache_r": 1.50},
    "claude-opus-4-7":   {"in": 15.0, "out": 75.0, "cache_w": 18.75, "cache_r": 1.50},
    "claude-haiku-4-5":  {"in": 1.0, "out": 5.0, "cache_w": 1.25, "cache_r": 0.10},
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0, "cache_w": 1.25, "cache_r": 0.10},
}
_FALLBACK_PRICE = {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30}


def _calc_cost(model: str, usage: Any) -> tuple[int, int, float]:
    """Подсчёт стоимости USD по usage. Возвращает (input_tokens, output_tokens, cost_usd)."""
    p = PRICING.get(model, _FALLBACK_PRICE)
    if getattr(usage, "subscription", False):
        # Вызов через подписку Claude (modules/llm.py) — денег не списывает
        p = {"in": 0.0, "out": 0.0, "cache_w": 0.0, "cache_r": 0.0}
    in_t = getattr(usage, "input_tokens", 0) or 0
    out_t = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        in_t * p["in"] + out_t * p["out"]
        + cw * p["cache_w"] + cr * p["cache_r"]
    ) / 1_000_000
    return in_t, out_t, cost


class LLMResponseError(RuntimeError):
    """LLM ответил, но ответ непригоден: пустой, обрезан, не JSON, нет обязательных полей."""


def _is_fast(model: str) -> bool:
    return "haiku" in (model or "").lower()


def call_json(
    *,
    label: str,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    max_tokens: int,
    effort: Optional[str] = "low",
    web_search: bool = False,
    nonempty: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Один вызов LLM со структурированным ответом по JSON-схеме.

    Возвращает (data, meta), meta = {tokens_in, tokens_out, cost_usd, used_web_search, model}.
    Бросает llm.LLMError (CLI/подписка недоступны) или LLMResponseError (ответ непригоден)
    с понятным текстом, где есть `label` вызова.

    nonempty — строковые поля, которые обязаны быть непустыми (иначе пустой текст
    уйдёт клиенту). Наличие всех `schema.required` проверяется всегда.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
    # Haiku не поддерживает effort/thinking — не передаём
    if not _is_fast(model):
        kwargs["thinking"] = {"type": "disabled"}
        if effort:
            output_config["effort"] = effort
    kwargs["output_config"] = output_config
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    response = llm.Client().messages.create(**kwargs)

    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        raise LLMResponseError(f"{label}: ответ обрезан по лимиту {max_tokens} токенов")
    if stop == "refusal":
        raise LLMResponseError(f"{label}: модель отказалась отвечать")

    # С web_search первые text-блоки могут быть нарративом — берём последний
    texts = [b.text for b in response.content if getattr(b, "type", "") == "text" and b.text]
    if not texts:
        raise LLMResponseError(f"{label}: пустой ответ модели")
    raw = texts[-1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        from modules.guardrails import extract_json_object
        try:
            data = extract_json_object(texts)
        except RuntimeError:
            raise LLMResponseError(f"{label}: ответ не JSON: {raw[:300]!r}") from None
    if not isinstance(data, dict):
        raise LLMResponseError(f"{label}: ожидался JSON-объект, пришло {type(data).__name__}")

    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise LLMResponseError(f"{label}: в ответе нет полей {', '.join(missing)}")
    empty = [k for k in nonempty if not isinstance(data.get(k), str) or not data[k].strip()]
    if empty:
        raise LLMResponseError(f"{label}: пустые поля {', '.join(empty)}")

    in_t, out_t, cost = _calc_cost(model, response.usage)
    used_web = any(getattr(b, "type", "") in ("server_tool_use", "tool_use") for b in response.content)
    return data, {
        "tokens_in": in_t, "tokens_out": out_t, "cost_usd": cost,
        "used_web_search": used_web, "model": model,
    }
