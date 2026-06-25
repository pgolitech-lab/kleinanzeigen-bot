# Market Scout: LLM-функции для разведки рынка Kleinanzeigen.
# Выделено из modules/claude.py. Публичный API:
#   generate_scout_queries  — генерация поисковых запросов (Sonnet)
#   classify_scout_listings — классификация объявлений car/part/other (Haiku)

import json
from typing import Any, Optional

import anthropic

import config
from modules.claude_utils import _calc_cost

# --- MARKET SCOUT: генерация поисковых запросов ---

_SCOUT_QUERIES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["car", "part"]},
                    "label": {"type": "string", "description": "короткое имя запроса по-русски"},
                    "keywords": {
                        "type": "string",
                        "description": "поисковая фраза НА НЕМЕЦКОМ как её вводят на Kleinanzeigen",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["c216", "c223"],
                        "description": "c216=Autos (для kind=car), c223=Autoteile (для kind=part)",
                    },
                    "max_pages": {"type": "integer", "description": "1..10, глубина пагинации"},
                },
                "required": ["kind", "label", "keywords", "category", "max_pages"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}

_SCOUT_SYSTEM = (
    "Ты эксперт по немецкому рынку б/у минивэнов и автозапчастей на Kleinanzeigen.de. "
    "Ты отлично знаешь платформу EMP2/PSA (бывшие 'K0' вэны) и все её модели-близнецы: "
    "Peugeot Traveller, Peugeot Expert, Citroën SpaceTourer, Citroën Jumpy, "
    "Opel Zafira Life, Opel Vivaro(-e), Toyota ProAce, Toyota ProAce Verso, "
    "Fiat Scudo, Fiat Ulysse, а также электрические версии "
    "(e-Traveller, ë-SpaceTourer, Zafira-e Life, ProAce Electric, e-Expert, e-Ulysse). "
    "Твоя задача — придумать набор немецких поисковых запросов, чтобы поймать МАКСИМУМ "
    "релевантных объявлений по всей Германии."
)


def generate_scout_queries(
    existing_keywords: Optional[list[str]] = None,
    extra_instruction: str = "",
) -> dict[str, Any]:
    """LLM генерит поисковые запросы для разведки рынка (машины + запчасти).

    existing_keywords — уже имеющиеся фразы (чтобы не дублировать).
    extra_instruction — доп. указание оператора (RU), напр. «добавь больше по электричкам».
    Возвращает {queries: [{kind,label,keywords,category,max_pages}], tokens_in, tokens_out, cost_usd}.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API key")

    existing = existing_keywords or []
    existing_block = (
        "Уже есть такие запросы (НЕ дублируй их дословно, можешь дополнять варианты):\n"
        + "\n".join(f"- {k}" for k in existing) + "\n\n"
    ) if existing else ""

    user_msg = (
        f"{existing_block}"
        "Сгенерируй поисковые запросы для Kleinanzeigen.de двух видов:\n\n"
        "1) kind='car', category='c216' — целые машины. Покрой ВСЕ модели платформы и их "
        "написания (включая отдельные запросы по электричкам). Примеры фраз: "
        "'peugeot traveller', 'peugeot expert kombi', 'citroen spacetourer', "
        "'opel zafira life', 'toyota proace verso', 'e-traveller', 'opel vivaro 9 sitzer'.\n\n"
        "2) kind='part', category='c223' — запчасти, особенно: сиденья (Sitze), "
        "скамейки/ряды сидений (Sitzbank, Sitzreihe, Doppelbank), направляющие рельсы "
        "сидений (Sitzschienen, Sitz Schienen). Примеры: 'sitze peugeot traveller', "
        "'sitzbank zafira life', 'sitzschienen spacetourer', 'sitzreihe proace'.\n\n"
        "Требования:\n"
        "- keywords строго на НЕМЕЦКОМ, короткие (2-4 слова), как реально вводят в поиск\n"
        "- НЕ добавляй город/регион в keywords — ищем по всей Германии\n"
        "- max_pages: 5 для широких запросов (машины), 3 для узких (запчасти)\n"
        "- дай 18-30 запросов суммарно, разнообразных по моделям и написаниям\n"
        + (f"\nДоп. указание оператора: {extra_instruction}\n" if extra_instruction.strip() else "")
        + "\nВерни поле queries (массив)."
    )

    client = anthropic.Anthropic(api_key=api_key)
    model = config.claude_model()
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=_SCOUT_SYSTEM,
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _SCOUT_QUERIES_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Пустой ответ generate_scout_queries")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(model, response.usage)

    queries: list[dict[str, Any]] = []
    for q in data.get("queries", []):
        kind = "part" if q.get("kind") == "part" else "car"
        category = q.get("category") or ("c223" if kind == "part" else "c216")
        if category not in ("c216", "c223"):
            category = "c223" if kind == "part" else "c216"
        kw = (q.get("keywords") or "").strip()
        if not kw:
            continue
        try:
            mp = int(q.get("max_pages") or 5)
        except (ValueError, TypeError):
            mp = 5
        queries.append({
            "kind": kind,
            "label": (q.get("label") or kw).strip(),
            "keywords": kw,
            "category": category,
            "max_pages": max(1, min(10, mp)),
        })

    return {
        "queries": queries,
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


# --- MARKET SCOUT: проверка типа объявления (машина / запчасть / другое) ---

_SCOUT_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "ad_id из входа (как есть)"},
                    "type": {
                        "type": "string",
                        "enum": ["car", "part", "other"],
                        "description": "car=целый автомобиль; part=запчасть/аксессуар; other=иное",
                    },
                },
                "required": ["id", "type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


def classify_scout_listings(
    items: list[dict[str, Any]],
    examples: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Haiku-проверка пачки объявлений: целый автомобиль / запчасть / другое.

    items: [{ad_id, title, description}].
    examples: операторские правки [{title, description, was_kind, correct_kind}] —
      few-shot для in-context обучения (учится на корректировках оператора).
    Возвращает {map: {ad_id: 'car'|'part'|'other'}, tokens_in, tokens_out, cost_usd}.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API key")
    if not items:
        return {"map": {}, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

    examples_block = ""
    if examples:
        ex_lines = []
        for e in examples[:30]:
            t = (e.get("title") or "").replace("\n", " ")[:90]
            ck = e.get("correct_kind")
            if ck == "remove":
                ck = "other"
            ex_lines.append(f"- «{t}» → {ck}")
        if ex_lines:
            examples_block = (
                "\nОператор ранее ПОПРАВИЛ классификацию таких объявлений "
                "(учитывай эти примеры как эталон):\n" + "\n".join(ex_lines) + "\n")

    lines = []
    for it in items:
        title = (it.get("title") or "").replace("\n", " ")[:140]
        desc = (it.get("description") or "").replace("\n", " ")[:200]
        lines.append(f"[{it.get('ad_id')}] {title} — {desc}")
    listing_block = "\n".join(lines)

    user_msg = (
        "Классифицируй каждое объявление с немецкого Kleinanzeigen по типу:\n"
        "- car = ЦЕЛЫЙ автомобиль/минивэн на продажу (даже аварийный, на запчасти, без ТО)\n"
        "- part = отдельная ЗАПЧАСТЬ или аксессуар (сиденье, скамейка, рельсы, дверь, "
        "двигатель, фара, коврики, шины, и т.п.)\n"
        "- other = всё прочее (услуга, прокат/аренда, реклама, не относится к авто/запчастям)\n\n"
        "Подсказка: '9 Sitzer'/'8 Sitze' в названии машины — это число мест, это CAR, не part.\n"
        "Цена целого вэна обычно тысячи евро; запчасть обычно дешевле.\n"
        "ВНИМАНИЕ: 'SUCHE'/'Suche'/'gesucht' = это ПОКУПАТЕЛЬ ищет (не продажа) → other; "
        "'mieten'/'Vermietung'/'Miete' = аренда → other.\n"
        f"{examples_block}\n"
        f"Объявления (формат [id] заголовок — описание):\n{listing_block}\n\n"
        "Верни results: массив {id, type} для КАЖДОГО id."
    )

    client = anthropic.Anthropic(api_key=api_key)
    model = "claude-haiku-4-5"
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        system=("Ты классификатор объявлений авторынка. Точно отличаешь целый "
                "автомобиль от отдельной запчасти."),
        output_config={
            "format": {"type": "json_schema", "schema": _SCOUT_CLASSIFY_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Пустой ответ classify_scout_listings")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(model, response.usage)

    out_map: dict[str, str] = {}
    for r in data.get("results", []):
        rid = str(r.get("id") or "").strip()
        rtype = r.get("type")
        if rid and rtype in ("car", "part", "other"):
            out_map[rid] = rtype
    return {"map": out_map, "tokens_in": in_t, "tokens_out": out_t, "cost_usd": cost}
