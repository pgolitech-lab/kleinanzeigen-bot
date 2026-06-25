"""Генератор брифа объявления через Claude.

Бриф — структурированный summary объявления для оператора и контекст для Claude
при генерации ответов клиентам. Считается единожды на ad_id, кэшируется в БД
(таблица ad_briefs). При повторном вызове используется кэш.
"""

import json
import logging
from typing import Any, Optional

import anthropic

import config
from modules.claude_utils import _calc_cost

logger = logging.getLogger(__name__)


BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brief_md": {
            "type": "string",
            "description": "Краткий бриф для оператора на русском, 3-5 предложений",
        },
        "key_facts": {
            "type": "object",
            "properties": {
                "title_short": {"type": "string"},
                "category": {"type": "string"},
                "price_eur": {"type": ["number", "null"]},
                "min_acceptable_eur": {
                    "type": ["number", "null"],
                    "description": "Минимально приемлемая цена (с учётом max_discount_percent из настроек)",
                },
                "condition": {"type": "string"},
                "key_specs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Технические характеристики/важные параметры",
                },
                "selling_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Что выгодно подчеркнуть в переговорах",
                },
                "weak_points": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Что может смутить покупателя — продумай заранее ответы",
                },
            },
            "required": ["title_short"],
            "additionalProperties": False,
        },
    },
    "required": ["brief_md", "key_facts"],
    "additionalProperties": False,
}


def generate_brief(
    ad_title: str,
    ad_url: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
) -> dict[str, Any]:
    """Сгенерировать бриф объявления.

    Возвращает dict: {brief_md, key_facts (dict), tokens_in, tokens_out, cost_usd}.
    Бросает RuntimeError если не задан API ключ или пустой ответ.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ в настройках")

    if not ad_title and not ad_description:
        raise RuntimeError("Нет данных объявления для брифа (title и description пусты)")

    discount = config.max_discount_percent()

    parts: list[str] = ["=== ОБЪЯВЛЕНИЕ ==="]
    if ad_title:
        parts.append(f"Название: {ad_title}")
    if ad_price:
        parts.append(f"Цена: {ad_price}")
    if seller_name:
        parts.append(f"Продавец: {seller_name}")
    if ad_url:
        parts.append(f"URL: {ad_url}")
    if ad_description:
        parts.append("Описание:")
        parts.append(ad_description)
    parts.append("")
    parts.append(
        f"Сделай бриф для оператора, который ведёт переговоры с покупателями.\n"
        f"Максимально допустимая скидка по политике: {discount}%.\n"
        f"\n"
        f"Поля key_facts:\n"
        f"- title_short: компактное название (1-3 слова)\n"
        f"- category: 'Auto' / 'Möbel' / 'Elektronik' / ...\n"
        f"- price_eur: число в евро (или null если не указано)\n"
        f"- min_acceptable_eur: исходная цена * (1 - {discount}/100), округли\n"
        f"- condition: одно слово/фраза о состоянии (neuwertig / gepflegt / gebraucht / ...)\n"
        f"- key_specs: 3-7 ключевых характеристик\n"
        f"- selling_points: 2-4 пункта что подчеркнуть в переговорах\n"
        f"- weak_points: 1-3 пункта что может смутить покупателя\n"
        f"\n"
        f"brief_md: 3-5 предложений на русском, краткое резюме."
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=1500,
        system=(
            "Ты помощник продавца автозапчастей и других товаров на Kleinanzeigen.de. "
            "Анализируешь объявления и делаешь брифы для оператора."
        ),
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": BRIEF_SCHEMA},
        },
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )

    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Claude вернул пустой ответ для брифа")

    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    return {
        "brief_md": data["brief_md"].strip(),
        "key_facts": data.get("key_facts") or {},
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


def format_brief_for_telegram(brief_md: str, key_facts: dict[str, Any]) -> str:
    """Компактный бриф для Telegram: только что продаём + состояние + минимум.

    Полные данные (selling_points, weak_points) хранятся в БД и используются
    Claude при генерации ответов, но оператору в карточку их не суём.
    """
    lines: list[str] = []
    title = (key_facts.get("title_short") or "").strip()
    cond = (key_facts.get("condition") or "").strip()
    if title and cond:
        lines.append(f"{title} — {cond}")
    elif title:
        lines.append(title)
    elif cond:
        lines.append(cond)

    min_p = key_facts.get("min_acceptable_eur")
    if isinstance(min_p, (int, float)) and min_p > 0:
        lines.append(f"💶 Мин. {int(min_p)} €")

    return "\n".join(lines)


def format_brief_for_claude(brief_md: str, key_facts: dict[str, Any]) -> str:
    """Развёрнутый текст брифа для подачи Claude в составе user-сообщения."""
    lines: list[str] = ["=== БРИФ ОБЪЯВЛЕНИЯ ==="]
    if brief_md:
        lines.append(brief_md)
    if not key_facts:
        return "\n".join(lines)
    if key_facts.get("title_short"):
        lines.append(f"Кратко: {key_facts['title_short']}")
    if key_facts.get("category"):
        lines.append(f"Категория: {key_facts['category']}")
    if key_facts.get("price_eur"):
        lines.append(f"Цена: {key_facts['price_eur']} €")
    if key_facts.get("min_acceptable_eur"):
        lines.append(f"Минимально приемлемая цена: {key_facts['min_acceptable_eur']} € — НЕ ОПУСКАЙСЯ ниже")
    if key_facts.get("condition"):
        lines.append(f"Состояние: {key_facts['condition']}")
    if key_facts.get("key_specs"):
        lines.append("Характеристики: " + "; ".join(key_facts["key_specs"]))
    if key_facts.get("selling_points"):
        lines.append("Сильные стороны (используй в переговорах): " + "; ".join(key_facts["selling_points"]))
    if key_facts.get("weak_points"):
        lines.append("Возможные возражения покупателя: " + "; ".join(key_facts["weak_points"]))
    return "\n".join(lines)
