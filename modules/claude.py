# Anthropic Claude API. Один запрос → перевод DE→RU + ответ RU + перевод RU→DE.
# Через structured outputs (output_config.format с json_schema) — гарантированный JSON.

import json
import re
from typing import Any, Optional

import anthropic

import config
import database as db


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


# Правило для всех немецких исходящих: ОБЯЗАТЕЛЬНО прощание в конце.
# Применяется к любым нашим текстам (Sonnet draft, quick-action, tweak,
# follow-up ping, auto-ack, ручной перевод оператора). Вариант — на выбор Claude.
GERMAN_CLOSING_RULE = (
    "Если итоговый текст на НЕМЕЦКОМ языке — ОБЯЗАТЕЛЬНО завершай его прощальной "
    "формулой («MfG», «Viele Grüße», «Mit freundlichen Grüßen», «Beste Grüße» — "
    "вариант на твой выбор), даже если ответ короткий. Если ответ на другом языке — "
    "следуй естественным нормам того языка, специальное прощание не обязательно."
)


# Простая база языков, на которых обычно пишут на Kleinanzeigen.
# code → (русское название, флаг для Telegram)
LANG_INFO: dict[str, tuple[str, str]] = {
    "de": ("немецкий", "🇩🇪"),
    "en": ("английский", "🇬🇧"),
    "ru": ("русский", "🇷🇺"),
    "uk": ("украинский", "🇺🇦"),
    "tr": ("турецкий", "🇹🇷"),
    "ar": ("арабский", "🇸🇦"),
    "fr": ("французский", "🇫🇷"),
    "es": ("испанский", "🇪🇸"),
    "it": ("итальянский", "🇮🇹"),
    "pl": ("польский", "🇵🇱"),
    "nl": ("голландский", "🇳🇱"),
    "ro": ("румынский", "🇷🇴"),
    "pt": ("португальский", "🇵🇹"),
    "zh": ("китайский", "🇨🇳"),
}


def lang_display(code: Optional[str]) -> tuple[str, str]:
    """code → (название, флаг). Неизвестный код возвращает (code или '?', '🌐')."""
    if not code:
        return ("?", "🌐")
    info = LANG_INFO.get(code.lower())
    if info:
        return info
    return (code, "🌐")


# Словарь название-языка → ISO-код для парсинга директивы оператора
_LANG_NAME_TO_CODE: dict[str, str] = {}
for _code, (_name, _flag) in LANG_INFO.items():
    _LANG_NAME_TO_CODE[_name] = _code  # «немецкий» → de
# Падежи / альтернативы
_LANG_NAME_TO_CODE.update({
    "немецком": "de", "англ": "en", "английском": "en",
    "русском": "ru", "украинском": "uk", "турецком": "tr",
    "французском": "fr", "испанском": "es", "итальянском": "it",
    "польском": "pl", "арабском": "ar", "голландском": "nl",
    "румынском": "ro", "португальском": "pt", "китайском": "zh",
    # Английские названия (на случай если оператор пишет латиницей)
    "german": "de", "english": "en", "russian": "ru", "ukrainian": "uk",
    "turkish": "tr", "french": "fr", "spanish": "es", "italian": "it",
    "polish": "pl", "arabic": "ar", "dutch": "nl", "romanian": "ro",
    "portuguese": "pt", "chinese": "zh",
})

# Шаблоны директивы языка в начале текста оператора.
# Ловим: «переведи на X:», «на X:», «translate to X:», «in X:», «X: ...».
_LANG_DIRECTIVE_PATTERNS = [
    re.compile(r'^\s*(?:переведи\s+)?на\s+([а-яёa-z]+)[\s:,\-—]+(.*)', re.IGNORECASE | re.DOTALL),
    re.compile(r'^\s*translate\s+(?:to|into)\s+([a-z]+)[\s:,\-—]+(.*)', re.IGNORECASE | re.DOTALL),
    re.compile(r'^\s*in\s+([a-z]+)[\s:,\-—]+(.*)', re.IGNORECASE | re.DOTALL),
]


def detect_lang_override(operator_text: str) -> tuple[Optional[str], str]:
    """Распознать директиву «переведи на X: ...» в начале текста оператора.

    Возвращает (lang_code, stripped_text). Если не распознано — (None, original_text).
    Скрипт устойчив к падежам и регистру; «переведи на немецкий: текст» → ('de', 'текст').
    """
    text = operator_text or ""
    for p in _LANG_DIRECTIVE_PATTERNS:
        m = p.match(text)
        if not m:
            continue
        lang_word = m.group(1).lower()
        rest = (m.group(2) or "").strip()
        if not rest:
            continue  # директива без текста — игнорим
        code = _LANG_NAME_TO_CODE.get(lang_word)
        if code:
            return code, rest
    return None, text


def _build_user_message(
    de_client_text: str,
    ad_title: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    brief_text: str = "",
    lessons: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Собрать пользовательское сообщение: бриф + объявление + уроки + история + новое письмо."""
    parts: list[str] = []

    if brief_text:
        parts.append(brief_text)
        parts.append("")

    # Если объявление помечено Gelöscht / Reserviert — поясняем Claude чтобы
    # он не делал вид что товар ещё доступен.
    from modules.parser import detect_ad_state
    state = detect_ad_state(ad_title)
    if state == "deleted":
        parts.append("⚠️ ОБЪЯВЛЕНИЕ УДАЛЕНО ПРОДАВЦОМ. Товар уже не продаётся (возможно продан другому или снят).")
        parts.append("В ответе тактично объясни клиенту что объявление снято. НЕ обещай продать.")
        parts.append("")
    elif state == "reserved":
        parts.append("🔒 ОБЪЯВЛЕНИЕ ЗАРЕЗЕРВИРОВАНО. Мы уже договорились с другим покупателем.")
        parts.append("Сообщи клиенту что товар зарезервирован, можно занять очередь если первая сделка сорвётся.")
        parts.append("")

    if ad_title or ad_price or ad_description:
        parts.append("=== ОБЪЯВЛЕНИЕ (raw) ===")
        if ad_title:
            parts.append(f"Название: {ad_title}")
        if ad_price:
            parts.append(f"Цена: {ad_price}")
        if seller_name:
            parts.append(f"Имя продавца: {seller_name}")
        if ad_description:
            parts.append("Описание:")
            parts.append(ad_description)
        parts.append("")

    if lessons:
        parts.append("=== УРОКИ ОТ ОПЕРАТОРА (как НЕ надо vs как ПРАВИЛЬНО) ===")
        parts.append("Ниже примеры твоих прошлых черновиков и того как оператор их переписал. Учись из них стилю, тону и логике переговоров.")
        for i, ls in enumerate(lessons, 1):
            sit = (ls.get("client_situation_ru") or "")[:300]
            bad = (ls.get("bad_draft_ru") or "")[:500]
            good = (ls.get("good_answer_ru") or "")[:500]
            if not (bad and good):
                continue
            parts.append(f"--- Урок #{i} ---")
            if sit:
                parts.append(f"[Клиент написал]: {sit}")
            parts.append(f"[Бот предложил — НЕ ПОВТОРЯЙ ТАК]: {bad}")
            parts.append(f"[Оператор переписал — следуй этому стилю]: {good}")
        parts.append("")

    if history:
        parts.append("=== ИСТОРИЯ ПЕРЕПИСКИ (от старых к новым) ===")
        for h in history:
            role = "Клиент" if h.get("direction") == "in" else "Продавец"
            text = h.get("de_client") or h.get("de_answer") or ""
            if text:
                parts.append(f"[{role}]: {text}")
        parts.append("")
        # Если в истории уже есть наш turn (вкл. auto-ack-приветствие) — не дублируй greeting.
        if any(h.get("direction") == "out" for h in history):
            parts.append(
                "ВАЖНО: в истории выше уже есть твой предыдущий turn (возможно — авто-приветствие). "
                "НЕ повторяй приветствие («Hallo», «Guten Tag», «Hi» и т.п.) и не благодари за сообщение ещё раз. "
                "Переходи сразу к сути ответа по существу вопроса."
            )
            parts.append("")

    parts.append("=== НОВОЕ СООБЩЕНИЕ КЛИЕНТА ===")
    parts.append(de_client_text.strip())
    parts.append("")
    parts.append("Определи язык сообщения и сделай:")
    parts.append("1) client_lang — двухбуквенный ISO-код языка сообщения клиента (de, en, ru, tr, fr, es, it, pl, nl, uk, ar, …). По умолчанию de если уверенно не определяется.")
    parts.append("2) ru_client — точный перевод сообщения клиента на русский. Если оно уже на русском — продублируй как есть.")
    parts.append("3) ru_answer — твой ответ на русском от лица продавца (в твоём стиле, по системному промпту).")
    parts.append("4) client_answer — этот же ответ НА ЯЗЫКЕ КЛИЕНТА (тот что ты определил в client_lang). Естественный, грамотный, не буквальный перевод.")
    parts.append("4b) ru_translation — ОБРАТНЫЙ ТОЧНЫЙ ПЕРЕВОД client_answer на русский (буквальный, без отсебятины). Оператор видит его чтобы убедиться что отправит именно то что хочет. Если client_answer на русском — продублируй как есть.")
    parts.append("5) deal_summary_ru — краткое резюме переговоров на РУССКОМ (1-2 предложения, ≤120 символов): где сейчас торг, что договорились/спорим. Если переговоры только начинаются — «начинается, клиент спрашивает X».")
    parts.append("6) expected_next — что ждём дальше (1 короткая фраза, ≤40 символов): «ответа клиента на 1500€», «согласия на встречу», «уточнения совместимости», и т.п.")
    parts.append("7) negotiated_price_eur — последняя ОЗВУЧЕННАЯ цена в торгах в евро (число). Если клиент дал низкий контр-оффер 1300 а мы держим 1550 — пиши последний УПОМЯНУТЫЙ нами оффер. Если торгов не было — исходную цену объявления. null если непонятно.")
    parts.append("8) client_assessment — одно слово / короткая фраза: «серьёзный», «торгуется», «лоу-баллер», «спрашивает», «спам», «горячий», «холодный», и т.п. На русском.")
    parts.append("")
    parts.append(GERMAN_CLOSING_RULE)

    return "\n".join(parts)


# JSON-схема ответа Claude
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_lang": {
            "type": "string",
            "description": "ISO 639-1 код языка клиента (de, en, ru, tr, fr, ...)",
        },
        "ru_client": {
            "type": "string",
            "description": "Перевод сообщения клиента на русский язык",
        },
        "ru_answer": {
            "type": "string",
            "description": "Черновик ответа на русском от лица продавца",
        },
        "client_answer": {
            "type": "string",
            "description": "Тот же ответ на языке клиента, готовый к отправке",
        },
        "ru_translation": {
            "type": "string",
            "description": "Точный обратный перевод client_answer на русский для верификации оператором",
        },
        # Динамический бриф сделки — для отображения в pipeline-карточке.
        # Все 4 поля required, но Claude может писать «—» если нечего сказать.
        "deal_summary_ru": {
            "type": "string",
            "description": "Краткое резюме переговоров на русском, 1-2 предложения, ≤120 chars",
        },
        "expected_next": {
            "type": "string",
            "description": "Что ждём дальше (1 короткая фраза на русском, ≤40 chars)",
        },
        "negotiated_price_eur": {
            "type": ["number", "null"],
            "description": "Последняя озвученная цена в торгах в EUR. null если непонятно.",
        },
        "client_assessment": {
            "type": "string",
            "description": "Одно слово/фраза о клиенте на русском (серьёзный/торгуется/лоу-баллер/...)",
        },
    },
    "required": [
        "client_lang", "ru_client", "ru_answer", "client_answer", "ru_translation",
        "deal_summary_ru", "expected_next", "negotiated_price_eur", "client_assessment",
    ],
    "additionalProperties": False,
}


def generate_reply(
    de_client_text: str,
    ad_title: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    brief_text: str = "",
    lessons: Optional[list[dict[str, Any]]] = None,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Перевод DE→RU + черновик ответа на RU + перевод RU→DE одним запросом.

    Возвращает dict: {ru_client, ru_answer, de_answer}.
    Бросает RuntimeError если не задан API ключ или Claude вернул мусор.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ в настройках")

    client = anthropic.Anthropic(api_key=api_key)

    user_text = _build_user_message(
        de_client_text=de_client_text,
        ad_title=ad_title,
        ad_price=ad_price,
        ad_description=ad_description,
        seller_name=seller_name,
        history=history,
        brief_text=brief_text,
        lessons=lessons,
    )

    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=max_tokens,
        system=config.system_prompt(),
        # Для перевода + короткого ответа thinking не нужен; чат-нагрузка → low effort
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
        },
        messages=[{"role": "user", "content": user_text}],
    )

    # output_config.format гарантирует, что первый text-блок содержит валидный JSON
    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("Claude вернул пустой ответ")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude вернул невалидный JSON: {e}\n{text[:500]}")

    # Проверим что все четыре поля на месте
    for key in ("client_lang", "ru_client", "ru_answer", "client_answer"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise RuntimeError(f"В ответе Claude отсутствует поле '{key}'")

    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    # `de_answer` — историческое имя поля БД (когда поддерживался только немецкий).
    # Сейчас оно хранит ответ на языке клиента (любом).
    deal_brief = {
        "summary_ru": (data.get("deal_summary_ru") or "").strip(),
        "expected_next": (data.get("expected_next") or "").strip(),
        "negotiated_price_eur": data.get("negotiated_price_eur"),
        "client_assessment": (data.get("client_assessment") or "").strip(),
    }
    return {
        "client_lang": data["client_lang"].strip().lower(),
        "ru_client": data["ru_client"].strip(),
        "ru_answer": data["ru_answer"].strip(),
        "de_answer": data["client_answer"].strip(),
        "ru_translation": (data.get("ru_translation") or "").strip(),
        "deal_brief": deal_brief,
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


_AUTOPILOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_lang": {"type": "string"},
        "ru_client": {"type": "string"},
        "ru_answer": {"type": "string"},
        "client_answer": {"type": "string"},
        "ru_translation": {"type": "string"},
        "deal_summary_ru": {"type": "string"},
        "expected_next": {"type": "string"},
        "negotiated_price_eur": {"type": ["number", "null"]},
        "client_assessment": {"type": "string"},
        "should_stop": {
            "type": "boolean",
            "description": "true если автопилот должен остановиться после этого ответа",
        },
        "stop_reason": {
            "type": "string",
            "description": "Почему стоп: ready_to_buy / wants_contact / threat / '' если не стоп",
        },
        "client_pressing_below_floor": {
            "type": "boolean",
            "description": "true если клиент давит ниже floor_eur — мы отказали в client_answer",
        },
    },
    "required": [
        "client_lang", "ru_client", "ru_answer", "client_answer", "ru_translation",
        "deal_summary_ru", "expected_next", "negotiated_price_eur", "client_assessment",
        "should_stop", "stop_reason", "client_pressing_below_floor",
    ],
    "additionalProperties": False,
}


def generate_autopilot_reply(
    de_client_text: str,
    ad_title: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    brief_text: str = "",
    lessons: Optional[list[dict[str, Any]]] = None,
    floor_eur: float = 0,
    last_our_price_eur: Optional[float] = None,
    max_tokens: int = 2500,
) -> dict[str, Any]:
    """Sonnet auto-reply для автопилота: floor-aware, с web_search tool, со stop-detection.

    Возвращает dict с полями generate_reply + should_stop / stop_reason /
    client_pressing_below_floor / used_web_search.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ")

    user_text = _build_user_message(
        de_client_text=de_client_text,
        ad_title=ad_title, ad_price=ad_price, ad_description=ad_description,
        seller_name=seller_name, history=history,
        brief_text=brief_text, lessons=lessons,
    )

    autopilot_addendum = (
        "\n\n=== РЕЖИМ АВТОПИЛОТА ===\n"
        "Ты ведёшь переговоры самостоятельно — оператор не проверяет твой ответ. Цель — продать товар.\n"
        f"HARD FLOOR: НЕ ОПУСКАТЬ цену ниже {floor_eur}€. "
    )
    if last_our_price_eur and last_our_price_eur > 0:
        autopilot_addendum += (
            f"Также НЕ опускать ниже того что уже сказали клиенту в этом треде ({last_our_price_eur}€).\n"
        )
    else:
        autopilot_addendum += "В этом треде мы ещё не озвучивали свою цену.\n"
    autopilot_addendum += (
        "\nОБЯЗАТЕЛЬНО останавливай автопилот (should_stop=true) когда:\n"
        "- клиент готов купить / просит банк-реквизиты / адрес для встречи / детали закрытия → stop_reason=\"ready_to_buy\" или \"wants_contact\"\n"
        "- клиент пишет угрозы / агрессивные / оскорбительные фразы → stop_reason=\"threat\". В client_answer ответь «Wir betrachten das als Drohung. Bitte unterlassen Sie weitere Nachrichten dieser Art.» или эквивалент на ЯЗЫКЕ КЛИЕНТА. Это единственный stop_reason где мы ВСЁ ЕЩЁ отправляем client_answer (предупреждение); остальные stop-причины → пишем что-то нейтральное в client_answer (всё равно отправится если оператор решит руками).\n"
        "\nВ остальных случаях продолжай переговоры:\n"
        "- Просит то чего нет (другая комплектация, цвет, размер) → вежливо откажи: «нет в наличии, доступно только X»\n"
        "- Просит фото/видео которых у нас нет → «не под рукой, могу позже отправить / приезжайте посмотреть лично»\n"
        "- Технический вопрос — отвечай из своих знаний. Используй web_search ТОЛЬКО если действительно специфичный вопрос (точный VIN, конкретная совместимость с редкой моделью) и ты не уверен. Мы платим за поиск.\n"
        "- НЕ ВРИ про конкретные spec-числа которых не знаешь. Лучше: «уточню при встрече» или «в описании указано X, точные характеристики могу проверить».\n"
        "- Если клиент давит ниже floor → откажи «Это окончательная цена», установи client_pressing_below_floor=true\n"
        "\nстиль автопилота: настойчивый продавец, дружелюбный но твёрдый, не теряющий фокус на закрытии сделки. Краткий — 2-4 предложения."
    )

    full_user = user_text + autopilot_addendum

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=max_tokens,
        system=config.system_prompt(),
        thinking={"type": "disabled"},
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _AUTOPILOT_SCHEMA},
        },
        messages=[{"role": "user", "content": full_user}],
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("Claude вернул пустой ответ (autopilot)")

    # Детект использовал ли web_search
    used_web_search = any(
        getattr(b, "type", "") in ("server_tool_use", "tool_use") for b in response.content
    )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Autopilot reply невалидный JSON: {e}\n{text[:500]}")

    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    deal_brief = {
        "summary_ru": (data.get("deal_summary_ru") or "").strip(),
        "expected_next": (data.get("expected_next") or "").strip(),
        "negotiated_price_eur": data.get("negotiated_price_eur"),
        "client_assessment": (data.get("client_assessment") or "").strip(),
    }
    return {
        "client_lang": data["client_lang"].strip().lower(),
        "ru_client": data["ru_client"].strip(),
        "ru_answer": data["ru_answer"].strip(),
        "de_answer": data["client_answer"].strip(),
        "ru_translation": (data.get("ru_translation") or "").strip(),
        "deal_brief": deal_brief,
        "should_stop": bool(data.get("should_stop")),
        "stop_reason": (data.get("stop_reason") or "").strip(),
        "client_pressing_below_floor": bool(data.get("client_pressing_below_floor")),
        "used_web_search": used_web_search,
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


def translate_only(
    text: str,
    direction: str = "ru_to_de",
    target_lang: Optional[str] = None,
    source_lang: Optional[str] = None,
    context: Optional[str] = None,
) -> dict[str, Any]:
    """Перевод без генерации ответа.

    target_lang: ISO-код целевого языка (de/en/tr/...) — приоритетнее direction.
    source_lang: ISO-код исходного — для точности промпта (напр., back-translate из DE/FR/EN в RU).
    direction: legacy 'ru_to_de' / 'de_to_ru' (используется если target/source не заданы).
    context: опциональная контекстная подсказка — название/описание объявления,
        чтобы переводчик корректно выбирал терминологию (Sitze vs Plätze для сидений).
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ в настройках")

    if target_lang and source_lang:
        target_name, _ = lang_display(target_lang)
        source_name, _ = lang_display(source_lang)
        src, dst = source_name, target_name
    elif target_lang:
        target_name, _flag = lang_display(target_lang)
        src, dst = "русского", target_name
    elif direction == "ru_to_de":
        src, dst = "русского", "немецкий"
    else:
        src, dst = "немецкого", "русский"
    client = anthropic.Anthropic(api_key=api_key)

    schema = {
        "type": "object",
        "properties": {"translation": {"type": "string"}},
        "required": ["translation"],
        "additionalProperties": False,
    }

    # Если переводим на немецкий — добавляем правило про обязательное прощание.
    target_is_de = (target_lang == "de") if target_lang else (direction == "ru_to_de")
    closing_clause = (
        " Если в исходном тексте нет прощальной формулы — обязательно "
        "добавь её в конце перевода («MfG», «Viele Grüße», «Mit freundlichen Grüßen» "
        "или другая стандартная форма, на твой выбор)."
        if target_is_de else ""
    )

    # Контекст-блок для глоссария: даёт переводчику товарную семантику.
    # Без него «4 сиденья» переводится как «4 Plätze» (места); с контекстом — «4 Sitze».
    context_clause = ""
    if context and context.strip():
        context_clause = (
            f"\n\nКонтекст переписки: продажа автомобильной комплектации — «{context.strip()}»."
            "\nГлоссарий (соблюдай ТОЧНО при переводе на немецкий):"
            "\n- сиденье / сиденья → Sitz / Sitze (НЕ «Platz/Plätze»)"
            "\n- лавка / диван (в авто) → Sitzbank"
            "\n- одиночное сиденье → Einzelsitz"
            "\n- складной столик → Klapptisch"
            "\n- крепления / направляющие → Schienen / Befestigung"
            "\n- состояние / б/у / новое → Zustand / gebraucht / neu"
        )

    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=1500,
        system=(
            f"Ты профессиональный переводчик с {src} на {dst}. "
            f"Переводишь точно и естественно, без отсебятины." + closing_clause + context_clause
        ),
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": text.strip()}],
    )

    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Claude вернул пустой ответ")
    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    return {
        "translation": json.loads(raw)["translation"].strip(),
        "target_lang": target_lang or ("de" if direction == "ru_to_de" else "ru"),
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


# --- Quick actions: переписать драфт под конкретную стратегию ---
QUICK_STRATEGIES: dict[str, str] = {
    "fest": "Цена ОКОНЧАТЕЛЬНАЯ. Никаких уступок. Вежливо но твёрдо отказать. 1-2 предложения.",
    "minus5": "Согласись на скидку ровно 5% от исходной цены. Преподнеси как лучшее предложение, не больше. 1-2 предложения.",
    "minus10": "Согласись на максимальную скидку (10% от исходной цены — это политика). 1-2 предложения, подчеркни что это последнее предложение.",
    "ask": "Не отвечай напрямую — задай уточняющий вопрос (готовность купить сегодня? самовывоз? и т.п.). 1 предложение-вопрос.",
    "meet": "Предложи встретиться/посмотреть товар вживую без обсуждения цены до этого. Кратко.",
}

TWEAK_INSTRUCTIONS: dict[str, str] = {
    "regen": "Сгенерируй СОВЕРШЕННО ДРУГУЮ формулировку с тем же смыслом — другая структура и слова.",
    "harsh": "Перепиши ЖЁСТЧЕ — без сюсюканий, твёрдо, по делу. Не агрессивно, но без лишней любезности.",
    "friend": "Перепиши ДРУЖЕЛЮБНЕЕ — теплее, человечнее, может с лёгкой эмпатией.",
    "short": "Сократи в 2 раза. Только суть.",
}


_QUICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ru_answer": {"type": "string"},
        "client_answer": {"type": "string"},
        "ru_translation": {
            "type": "string",
            "description": "Точный обратный перевод client_answer на русский (буквальный, для верификации)",
        },
        "deal_summary_ru": {"type": "string"},
        "expected_next": {"type": "string"},
        "negotiated_price_eur": {"type": ["number", "null"]},
        "client_assessment": {"type": "string"},
    },
    "required": [
        "ru_answer", "client_answer", "ru_translation",
        "deal_summary_ru", "expected_next", "negotiated_price_eur", "client_assessment",
    ],
    "additionalProperties": False,
}


def _regenerate_core(
    msg_row: Any,
    instruction: str,
    *,
    include_current_draft: bool,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Общий пайплайн регенерации: build prompt → Claude → parse.

    instruction: задача для Claude (preset / price / operator-instruction).
    include_current_draft: для tweak/instruction — True (Claude видит текущий драфт);
                          для price/strategy-quick — False (фреш генерация).
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ в настройках")

    client_lang = msg_row["client_lang"] or msg_row["answer_lang"] or "de"
    lang_name, _ = lang_display(client_lang)

    parts: list[str] = []
    if brief_text:
        parts.append(brief_text)
        parts.append("")
    if lessons:
        parts.append("=== УРОКИ ОТ ОПЕРАТОРА (стиль) ===")
        for i, ls in enumerate(lessons[:3], 1):
            good = (ls.get("good_answer_ru") or "").strip()
            if good:
                parts.append(f"--- Стиль #{i}: {good[:300]}")
        parts.append("")
    if history:
        parts.append("=== ИСТОРИЯ ===")
        for h in history:
            role = "Клиент" if h.get("direction") == "in" else "Продавец"
            text = h.get("de_client") or h.get("de_answer") or ""
            if text:
                parts.append(f"[{role}]: {text[:300]}")
        parts.append("")
    parts.append("=== ПОСЛЕДНЕЕ СООБЩЕНИЕ КЛИЕНТА ===")
    parts.append(msg_row["de_client"] or "")
    parts.append("")
    if include_current_draft:
        prior = msg_row["ru_answer"] or msg_row["de_answer"] or ""
        if prior:
            parts.append("=== ТЕКУЩИЙ ДРАФТ (нужно переработать) ===")
            parts.append(prior)
            parts.append("")
    parts.append("=== ЗАДАЧА ===")
    parts.append(instruction)
    parts.append("")
    parts.append(
        f"ru_answer — на русском (для оператора, как «инструкция»/идея).\n"
        f"client_answer — на ЯЗЫКЕ КЛИЕНТА ({lang_name}, ISO {client_lang}). Готов к отправке.\n"
        f"ru_translation — ТОЧНЫЙ обратный перевод client_answer на русский (буквальный, без отсебятины) для верификации оператором.\n"
        f"deal_summary_ru — краткое резюме переговоров на русском (1-2 предл, ≤120 chars).\n"
        f"expected_next — что ждём дальше (короткая фраза, ≤40 chars).\n"
        f"negotiated_price_eur — последняя озвученная цена в торгах (число EUR) или null.\n"
        f"client_assessment — короткая оценка клиента на русском (серьёзный/торгуется/...)."
    )
    parts.append("")
    parts.append(GERMAN_CLOSING_RULE)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=900,
        system=config.system_prompt(),
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _QUICK_SCHEMA},
        },
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Claude вернул пустой ответ")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    deal_brief = {
        "summary_ru": (data.get("deal_summary_ru") or "").strip(),
        "expected_next": (data.get("expected_next") or "").strip(),
        "negotiated_price_eur": data.get("negotiated_price_eur"),
        "client_assessment": (data.get("client_assessment") or "").strip(),
    }
    return {
        "ru_answer": data["ru_answer"].strip(),
        "client_answer": data["client_answer"].strip(),
        "ru_translation": (data.get("ru_translation") or "").strip(),
        "deal_brief": deal_brief,
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


def regenerate_with_strategy(
    msg_row: Any,
    strategy: str,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Перегенерить драфт под preset-стратегию (fest / harsh / friend / short / regen)."""
    instruction = QUICK_STRATEGIES.get(strategy) or TWEAK_INSTRUCTIONS.get(strategy)
    if not instruction:
        raise RuntimeError(f"Неизвестная стратегия: {strategy}")
    is_tweak = strategy in TWEAK_INSTRUCTIONS
    return _regenerate_core(
        msg_row, instruction,
        include_current_draft=is_tweak,
        brief_text=brief_text, history=history, lessons=lessons,
    )


def regenerate_with_price(
    msg_row: Any,
    price_eur: float,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Перегенерить драфт с конкретной ценой от оператора."""
    # Округляем целое если без копеек
    price_str = f"{int(price_eur)}" if float(price_eur).is_integer() else f"{price_eur:.2f}"
    instruction = (
        f"Согласись на цену ровно {price_str}€. Преподнеси как лучшее предложение, "
        f"кратко обоснуй (например: «это моё последнее предложение», «учитывая состояние», "
        f"«ради быстрой сделки»). 1-2 предложения. НЕ объясняй математику скидки."
    )
    return _regenerate_core(
        msg_row, instruction,
        include_current_draft=False,
        brief_text=brief_text, history=history, lessons=lessons,
    )


def regenerate_with_instruction(
    msg_row: Any,
    instruction: str,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Перегенерить драфт по операторской свободной инструкции."""
    full_instruction = (
        f"Инструкция от оператора: {instruction.strip()}\n"
        f"Следуй этой инструкции при перегенерации ответа. Если инструкция противоречит "
        f"общим правилам (например, требует обещать невыполнимое или сильно нарушает политику) — "
        f"следуй здравому смыслу и подсветь конфликт в ru_answer."
    )
    return _regenerate_core(
        msg_row, full_instruction,
        include_current_draft=True,
        brief_text=brief_text, history=history, lessons=lessons,
    )


_PING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ru_text": {"type": "string", "description": "Follow-up на русском (для оператора)"},
        "client_text": {"type": "string", "description": "Тот же follow-up на языке клиента (для отправки)"},
    },
    "required": ["ru_text", "client_text"],
    "additionalProperties": False,
}


def generate_followup_ping(
    history: list[dict[str, Any]],
    client_lang: str,
    days_silent: int,
    brief_text: str = "",
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Сгенерить вежливый follow-up пинг клиенту, который молчит N дней.

    Возвращает {ru_text, client_text, target_lang, tokens_in, tokens_out, cost_usd}.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ в настройках")

    lang_name, _flag = lang_display(client_lang or "de")
    parts: list[str] = []
    if brief_text:
        parts.append(brief_text)
        parts.append("")
    if lessons:
        parts.append("=== УРОКИ ОТ ОПЕРАТОРА (стиль) ===")
        for i, ls in enumerate(lessons[:3], 1):
            good = (ls.get("good_answer_ru") or "").strip()
            if good:
                parts.append(f"--- Стиль #{i}: {good[:300]}")
        parts.append("")
    if history:
        parts.append("=== ИСТОРИЯ ПЕРЕПИСКИ ===")
        for h in history:
            role = "Клиент" if h.get("direction") == "in" else "Продавец"
            text = h.get("de_client") or h.get("de_answer") or ""
            if text:
                parts.append(f"[{role}]: {text}")
        parts.append("")
    parts.append(
        f"Клиент молчит {days_silent} дн(я) после последнего твоего сообщения.\n"
        f"Напиши вежливый ненавязчивый follow-up:\n"
        f"- 1-2 коротких предложения\n"
        f"- Без давления и спам-фраз\n"
        f"- Можно мягко спросить «остаётесь ли заинтересованы?», «нужны ли уточнения?»\n"
        f"- НЕ повторяй уже сказанное в прошлых сообщениях\n"
        f"- НЕ сбавляй цену сам — только если клиент уже торговался\n"
        f"\n"
        f"ru_text — на русском (для оператора).\n"
        f"client_text — на ЯЗЫКЕ КЛИЕНТА ({lang_name}, ISO {client_lang}). Естественно и грамотно."
    )
    parts.append("")
    parts.append(GERMAN_CLOSING_RULE)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=600,
        system=config.system_prompt(),
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _PING_SCHEMA},
        },
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Claude вернул пустой ответ для follow-up ping")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    return {
        "ru_text": data["ru_text"].strip(),
        "client_text": data["client_text"].strip(),
        "target_lang": (client_lang or "de").lower(),
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary_ru": {"type": "string", "description": "1-2 предложения на русском"},
    },
    "required": ["summary_ru"],
    "additionalProperties": False,
}


def summarize_thread(history: list[dict[str, Any]]) -> dict[str, Any]:
    """Краткое резюме переписки на русском для оператора.

    history: rows из db.thread_history (исключая текущий msg).
    Возвращает {summary_ru, tokens_in, tokens_out, cost_usd}.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API ключ")

    parts: list[str] = ["=== ПЕРЕПИСКА ==="]
    for h in history:
        if h.get("de_client"):
            sender = h.get("buyer_display_name") or h.get("buyer_name") or "Клиент"
            text = h.get("ru_client") or h.get("de_client") or ""
            parts.append(f"[{sender}]: {text[:300]}")
        if h.get("de_answer") and h.get("status") in ("sent", "sent_debug", "edited", "approved"):
            text = h.get("ru_answer") or h.get("de_answer") or ""
            parts.append(f"[Продавец]: {text[:300]}")
    parts.append("")
    parts.append(
        "Сделай краткое резюме переписки на русском для оператора (1-2 предложения, ≤200 символов).\n"
        "Что обсуждали, на чём встали, кто на каких условиях стоит. Без воды и приветствий.\n"
        "summary_ru — только сам текст резюме."
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=300,
        system="Ты помощник продавца. Кратко резюмируешь переписку с покупателями.",
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA},
        },
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Пустой ответ от Claude для summary")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(config.claude_model(), response.usage)
    return {
        "summary_ru": data["summary_ru"].strip(),
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


_INQUIRY_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_inquiry": {
            "type": "boolean",
            "description": "true ТОЛЬКО если реальный buyer-inquiry на конкретное наше объявление",
        },
        "reason": {"type": "string", "description": "одно короткое предложение объяснения"},
    },
    "required": ["is_inquiry", "reason"],
    "additionalProperties": False,
}


def classify_email_is_inquiry(
    subject: str, from_name: str, from_email: str, body: str,
) -> dict[str, Any]:
    """Классификация письма дешёвой моделью (Haiku): настоящий buyer-inquiry vs системная рассылка.

    Возвращает {is_inquiry, reason, tokens_in, tokens_out, cost_usd}.
    Используется в `_process_incoming` как финальный gate перед дорогими операциями.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API key")

    client = anthropic.Anthropic(api_key=api_key)
    classifier_model = "claude-haiku-4-5"
    response = client.messages.create(
        model=classifier_model,
        max_tokens=200,
        system=(
            "Ты помощник продавца на немецком сайте Kleinanzeigen.de. "
            "Классифицируешь входящие письма: реальный buyer-inquiry или системная рассылка."
        ),
        # Haiku не поддерживает effort/thinking — убрали
        output_config={
            "format": {"type": "json_schema", "schema": _INQUIRY_CLASSIFY_SCHEMA},
        },
        messages=[{"role": "user", "content": (
            "Письмо:\n"
            f"From-name: {from_name}\n"
            f"From-email: {from_email}\n"
            f"Subject: {subject}\n\n"
            f"Body (обрезано):\n{(body or '')[:1000]}\n\n"
            "is_inquiry=true ТОЛЬКО если это реальный покупатель пишет про КОНКРЕТНОЕ моё объявление: "
            "задаёт вопрос про товар, торгуется, хочет купить, договаривается о деталях.\n"
            "is_inquiry=false для: saved-search alerts («Neue Treffer zu deiner Suche»), "
            "follow-seller уведомлений («Neue Anzeigen von», «hat eine neue Anzeige aufgegeben»), "
            "follow-ad alerts (price change, ad updated), "
            "lifecycle (объявление продлено/истекает/удалено), promo/newsletter, "
            "security warnings, payment confirmations, любых системных сообщений от Kleinanzeigen.\n"
            "При любых сомнениях — false."
        )}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Пустой ответ classifier")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(classifier_model, response.usage)
    return {
        "is_inquiry": bool(data["is_inquiry"]),
        "reason": data.get("reason", "").strip(),
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


_AUTO_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_lang": {"type": "string", "description": "ISO 639-1 код языка письма клиента"},
        "ack_text": {
            "type": "string",
            "description": "Короткий автоответ-заглушка на языке клиента, 1-2 предложения",
        },
    },
    "required": ["client_lang", "ack_text"],
    "additionalProperties": False,
}


def generate_auto_ack(
    buyer_display_name: str,
    body: str,
    hour_local: int,
    excuse_hint: str,
) -> dict[str, Any]:
    """Сгенерить короткое приветствие-заглушку для накрутки метрики Kleinanzeigen.

    Использует Haiku — быстро (~2с) и дёшево (~$0.0006).
    buyer_display_name: «Hans Müller» или "" если неизвестно.
    body: тело письма клиента — Haiku сама детектит язык.
    hour_local: 0..23, текущий час в Europe/Berlin (для Guten Morgen/Tag/Abend).
    excuse_hint: повод почему не отвечаю сейчас (RU), Haiku переведёт.
    Возвращает {client_lang, ack_text, tokens_in, tokens_out, cost_usd}.
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API key")

    name_part = buyer_display_name.strip() if buyer_display_name else ""
    name_line = f"Имя: {name_part}" if name_part else "Имя: не указано"

    user_msg = (
        f"Покупатель прислал:\n"
        f"\"{(body or '')[:500]}\"\n\n"
        f"{name_line}\n"
        f"Время в Берлине: {hour_local:02d}:00\n\n"
        f"Сгенерируй очень короткий (1–2 предложения) автоответ-заглушку:\n"
        f"- Поздоровайся с учётом времени суток (Guten Morgen/Tag/Abend / аналог на нужном языке)\n"
        f"- Обратись по имени, если оно реальное (не email/служебный текст); "
        f"иначе обычное \"Hallo!\" или аналог\n"
        f"- Скажи кратко повод почему не можешь сразу: \"{excuse_hint}\"\n"
        f"- Пообещай ответить подробно скоро (в течение часа / сегодня)\n"
        f"- БЕЗ конкретики по товару — это заглушка\n"
        f"- На том же языке, что написал покупатель (определи язык)\n"
        f"- Если ответ на немецком — ОБЯЗАТЕЛЬНО заверши прощальной формулой "
        f"(«MfG», «Viele Grüße», «Mit freundlichen Grüßen» — на твой выбор). "
        f"На других языках — следуй естественным нормам того языка.\n\n"
        f"Поля: client_lang (ISO 639-1), ack_text."
    )

    client = anthropic.Anthropic(api_key=api_key)
    classifier_model = "claude-haiku-4-5"
    response = client.messages.create(
        model=classifier_model,
        max_tokens=300,
        system=(
            "Ты вежливый продавец на немецком сайте Kleinanzeigen.de. "
            "Пишешь короткое приветствие-заглушку покупателю когда не можешь ответить подробно сразу."
        ),
        # Haiku не поддерживает effort/thinking — не передаём.
        output_config={
            "format": {"type": "json_schema", "schema": _AUTO_ACK_SCHEMA},
        },
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        raise RuntimeError("Пустой ответ generate_auto_ack")
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(classifier_model, response.usage)
    return {
        "client_lang": (data.get("client_lang") or "de").strip().lower(),
        "ack_text": data["ack_text"].strip(),
        "tokens_in": in_t,
        "tokens_out": out_t,
        "cost_usd": cost,
    }


_DETECT_TRANSLATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lang": {"type": "string", "description": "ISO 639-1 код языка исходного"},
        "translation_ru": {"type": "string", "description": "Точный перевод на русский"},
    },
    "required": ["lang", "translation_ru"],
    "additionalProperties": False,
}


def detect_and_translate_to_ru(text: str) -> dict[str, Any]:
    """Haiku: определить язык + перевести на русский.

    Используется для archived rows у которых ru_client пуст (был backfill без Sonnet).
    Дешёво (~$0.001 на короткий текст).
    """
    api_key = config.anthropic_api_key()
    if not api_key:
        raise RuntimeError("Не задан Anthropic API key")
    if not text or not text.strip():
        return {"lang": "?", "translation_ru": "", "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    client = anthropic.Anthropic(api_key=api_key)
    classifier_model = "claude-haiku-4-5"
    response = client.messages.create(
        model=classifier_model,
        max_tokens=800,
        system="Ты переводчик. Определи язык исходного текста и сделай точный перевод на русский.",
        output_config={
            "format": {"type": "json_schema", "schema": _DETECT_TRANSLATE_SCHEMA},
        },
        messages=[{"role": "user", "content": f"Текст:\n«{text[:1500]}»"}],
    )
    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        return {"lang": "?", "translation_ru": text[:200], "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    data = json.loads(raw)
    in_t, out_t, cost = _calc_cost(classifier_model, response.usage)
    return {
        "lang": (data.get("lang") or "?").lower().strip(),
        "translation_ru": (data.get("translation_ru") or "").strip(),
        "cost_usd": cost,
        "tokens_in": in_t,
        "tokens_out": out_t,
    }


def history_for(gmail_thread_id: str) -> list[dict[str, Any]]:
    """Подгрузить историю треда из БД в формате для generate_reply()."""
    rows = db.thread_history(gmail_thread_id)
    return [
        {
            "direction": r["direction"],
            "de_client": r["de_client"],
            "de_answer": r["de_answer"],
        }
        for r in rows
    ]


def test_api_key(api_key: str) -> tuple[bool, str]:
    """Проверка API ключа дешёвым запросом на Haiku. Для веб-морды."""
    if not api_key:
        return False, "API ключ пустой"
    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, "API ключ валиден"
    except anthropic.AuthenticationError:
        return False, "Невалидный API ключ"
    except anthropic.APIError as e:
        return False, f"Ошибка API: {e}"
    except Exception as e:
        return False, f"Ошибка: {e}"


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
