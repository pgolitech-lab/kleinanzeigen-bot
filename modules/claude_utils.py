# Утилиты Claude API: таблица цен и подсчёт стоимости запроса.
# Импортируется из modules/claude.py и modules/claude_scout.py.

from typing import Any

# Цены за миллион токенов в USD (на 2026-05-01).
# Источник: docs.anthropic.com/en/docs/about-claude/models — обнови при смене.
PRICING: dict[str, dict[str, float]] = {
    # Sonnet 4.x
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30},
    "claude-sonnet-4-7": {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30},
    # Opus 4.x
    "claude-opus-4-6":   {"in": 15.0, "out": 75.0, "cache_w": 18.75, "cache_r": 1.50},
    "claude-opus-4-7":   {"in": 15.0, "out": 75.0, "cache_w": 18.75, "cache_r": 1.50},
    # Haiku 4.x
    "claude-haiku-4-5":  {"in": 1.0, "out": 5.0, "cache_w": 1.25, "cache_r": 0.10},
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0, "cache_w": 1.25, "cache_r": 0.10},
}
_FALLBACK_PRICE = {"in": 3.0, "out": 15.0, "cache_w": 3.75, "cache_r": 0.30}


def _calc_cost(model: str, usage: Any) -> tuple[int, int, float]:
    """Подсчёт стоимости USD по usage из Anthropic SDK.
    Возвращает (input_tokens, output_tokens, cost_usd)."""
    p = PRICING.get(model, _FALLBACK_PRICE)
    in_t = getattr(usage, "input_tokens", 0) or 0
    out_t = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        in_t * p["in"] + out_t * p["out"]
        + cw * p["cache_w"] + cr * p["cache_r"]
    ) / 1_000_000
    return in_t, out_t, cost
