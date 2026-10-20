# LLM-функции переговоров: черновики, регенерация, автопилот, переводы, классификатор.
# Все вызовы — через claude_utils.call_json (подписка Claude, modules/llm.py, JSON-схема).
# Основная модель (config.claude_model) — черновики и автопилот; быстрая (Haiku) —
# переводы, классификатор, auto-ack, детектор языка.

import logging
import re
from typing import Any, Optional

import database as db
from modules.claude_utils import (  # noqa: F401
    PRICING, _FALLBACK_PRICE, _calc_cost, LLMResponseError, call_json, fast_model, smart_model,
)

logger = logging.getLogger(__name__)


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


def is_empty_directive(operator_text: str) -> bool:
    """True если оператор набрал языковую директиву («на немецком:», «in english:»)
    и не дописал само сообщение после неё.

    detect_lang_override() в этом случае молча игнорирует директиву и возвращает
    исходный текст целиком («на немецком:») — что затем уходит в переводчик как
    обычное сообщение и провоцирует LLM на бессмысленный ответ (см. живую проверку
    2026-07-02). Здесь — явная проверка ДО перевода, чтобы вернуть понятную ошибку.
    """
    text = operator_text or ""
    for p in _LANG_DIRECTIVE_PATTERNS:
        m = p.match(text)
        if not m:
            continue
        if m.group(1).lower() not in _LANG_NAME_TO_CODE:
            continue
        if not (m.group(2) or "").strip():
            return True
    return False




# ─────────────────────────── История треда для LLM ───────────────────────────

# Наши реплики попадают в историю только если реально ушли клиенту
# (sent_debug — режим redirect: для бота это тоже часть переписки).
SENT_STATUSES = ("sent", "sent_debug")
HISTORY_MAX_CHARS = 6000      # весь блок истории
HISTORY_MSG_MAX_CHARS = 1200  # одна реплика


def _rget(row: Any, key: str, default: Any = None) -> Any:
    """Поле из sqlite3.Row / dict, без KeyError на старых схемах."""
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


def history_for(
    gmail_thread_id: str,
    exclude_id: Optional[int] = None,
    before: Optional[str] = None,
) -> list[dict[str, Any]]:
    """История треда из БД (строки, по хронологии) для промпта.

    exclude_id — id текущего письма (отсекаем по id, а не по тексту);
    before — created_at текущего письма: более поздние ВХОДЯЩИЕ не берём
    (иначе при регенерации старого черновика в «историю» попадут будущие письма).
    Строка in хранит И вопрос клиента (de_client), И наш ответ (de_answer) —
    разворачивает их `format_history`.
    """
    if not gmail_thread_id:
        return []
    return rows_to_history(db.thread_history(gmail_thread_id), exclude_id=exclude_id, before=before)


def rows_to_history(
    rows: list[Any],
    exclude_id: Optional[int] = None,
    before: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Строки messages → формат истории для format_history (см. history_for)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        # Отсекаем только БОЛЕЕ ПОЗДНИЕ письма клиента. Наши out-строки (auto-ack,
        # compose) создаются с utcnow() позже Date-заголовка текущего письма —
        # их терять нельзя, иначе модель снова поздоровается.
        if before and r["direction"] == "in" and (_rget(r, "created_at") or "") > before:
            continue
        out.append({
            "id": r["id"],
            "direction": r["direction"],
            "de_client": r["de_client"],
            "de_answer": r["de_answer"],
            "status": _rget(r, "status"),
            "is_auto_ack": bool(_rget(r, "is_auto_ack")),
            "created_at": _rget(r, "created_at") or "",
            "sent_at": _rget(r, "sent_at") or "",
            # для памяти о клиенте (автопилот получает только историю)
            "gmail_thread_id": _rget(r, "gmail_thread_id"),
            "account_id": _rget(r, "account_id"),
            "ad_id": _rget(r, "ad_id"),
            "buyer_name": _rget(r, "buyer_name"),
            "buyer_display_name": _rget(r, "buyer_display_name"),
        })
    return out


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def format_history(
    history: Optional[list[dict[str, Any]]],
    max_chars: int = HISTORY_MAX_CHARS,
    with_dates: bool = False,
) -> tuple[str, bool]:
    """Развернуть строки истории в реплики «[Клиент]/[Продавец]» по времени.

    Возвращает (текст блока или "", есть_ли_наша_реплика). Наши ответы — только
    отправленные. При превышении max_chars отбрасываются самые старые реплики.
    """
    if not history:
        return "", False
    turns: list[tuple[str, int, int, str]] = []  # (ts, id, порядок, строка)
    for h in history:
        hid = h.get("id") or 0
        status = h.get("status")
        if h.get("direction") == "in" and h.get("de_client"):
            turns.append((h.get("created_at") or "", hid, 0,
                          f"[Клиент]: {_clip(h['de_client'], HISTORY_MSG_MAX_CHARS)}"))
        # sent_at — тоже признак отправки: после «Продано» статус строки меняется на
        # skipped_sold, а ответ уже ушёл. Без status (старые вызывающие) — отправленным.
        sent = (status in SENT_STATUSES or bool(h.get("sent_at"))) if status is not None else True
        if h.get("de_answer") and sent:
            who = "[Продавец, авто-приветствие]" if h.get("is_auto_ack") else "[Продавец]"
            turns.append((h.get("sent_at") or h.get("created_at") or "", hid, 1,
                          f"{who}: {_clip(h['de_answer'], HISTORY_MSG_MAX_CHARS)}"))
    if not turns:
        return "", False
    turns.sort(key=lambda t: (t[0], t[1], t[2]))
    if with_dates:
        lines = [f"[{t[0][:16].replace('T', ' ')}] {t[3]}" if t[0] else t[3] for t in turns]
    else:
        lines = [t[3] for t in turns]
    has_ours = any(t[2] == 1 for t in turns)

    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > max_chars and kept:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    if len(kept) < len(lines):
        kept.insert(0, f"(… ещё {len(lines) - len(kept)} более ранних реплик опущено)")
    return "\n".join(kept), has_ours


# ─────────────────────────── Сборка промпта ───────────────────────────

def _context_parts(
    *,
    ad_title: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    memory_text: str = "",
    lessons_text: str = "",
    ad_state: str = "",
) -> list[str]:
    """Общий контекст генераций: бриф, объявление, память о клиенте, уроки, история.

    ad_state — 'deleted' / 'reserved' / 'sold' (из `ad_state_for`); пусто — по ad_title."""
    parts: list[str] = []
    if brief_text:
        parts += [brief_text, ""]

    # Объявление помечено Gelöscht / Reserviert — чтобы модель не обещала товар
    from modules.parser import detect_ad_state
    state = ad_state or detect_ad_state(ad_title or "")
    if state == "sold":
        parts += ["⚠️ ТОВАР УЖЕ ПРОДАН (оператор отметил продажу по этому объявлению).",
                  "Тактично сообщи клиенту, что товар продан. НЕ обещай его и не торгуйся.", ""]
    elif state == "deleted":
        parts += ["⚠️ ОБЪЯВЛЕНИЕ УДАЛЕНО ПРОДАВЦОМ. Товар уже не продаётся (возможно продан другому или снят).",
                  "В ответе тактично объясни клиенту что объявление снято. НЕ обещай продать.", ""]
    elif state == "reserved":
        parts += ["🔒 ОБЪЯВЛЕНИЕ ЗАРЕЗЕРВИРОВАНО. Мы уже договорились с другим покупателем.",
                  "Сообщи клиенту что товар зарезервирован, можно занять очередь если первая сделка сорвётся.", ""]

    if ad_title or ad_price or ad_description:
        parts.append("=== ОБЪЯВЛЕНИЕ ===")
        if ad_title:
            parts.append(f"Название: {ad_title}")
        if ad_price:
            parts.append(f"Цена: {ad_price}")
        if seller_name:
            parts.append(f"Имя продавца: {seller_name}")
        if ad_description:
            parts += ["Описание:", _clip(ad_description, 4000)]
        parts.append("")

    if memory_text:
        parts += [memory_text, ""]
    if lessons_text:
        parts += [lessons_text, ""]

    hist_text, has_ours = format_history(history)
    if hist_text:
        parts += ["=== ИСТОРИЯ ПЕРЕПИСКИ (от старых к новым) ===", hist_text, ""]
        if has_ours:
            parts += [
                "ВАЖНО: в истории уже есть наши реплики (возможно — авто-приветствие). "
                "НЕ повторяй приветствие («Hallo», «Guten Tag», «Hi» и т.п.), не благодари за сообщение ещё раз "
                "и не повторяй уже сказанное. Переходи сразу к сути.",
                "",
            ]
    return parts


def _fields_instruction(client_lang: Optional[str], need_ru_client: bool) -> str:
    """Описание полей ответа (общая часть схем черновика/автопилота)."""
    if client_lang:
        lang_name, _ = lang_display(client_lang)
        lang_line = (f"client_lang — «{client_lang}» (язык клиента, {lang_name}); "
                     "если сообщение клиента явно на другом языке — укажи его ISO-код.")
    else:
        lang_line = ("client_lang — двухбуквенный ISO-код языка сообщения клиента "
                     "(de, en, ru, tr, …); по умолчанию de.")
    lines = [lang_line]
    if need_ru_client:
        lines.append("ru_client — точный перевод сообщения клиента на русский (если уже на русском — как есть).")
    lines += [
        "ru_answer — ответ на русском от лица продавца (для оператора).",
        "client_answer — этот же ответ НА ЯЗЫКЕ КЛИЕНТА (client_lang), естественный, готовый к отправке.",
        "ru_translation — ТОЧНЫЙ обратный перевод client_answer на русский (буквальный, без отсебятины). "
        "Если client_answer на русском — как есть.",
        "deal_summary_ru — резюме переговоров на русском (1-2 предложения, ≤120 символов).",
        "expected_next — что ждём дальше (≤40 символов): «ответа на 1500€», «согласия на встречу» и т.п.",
        "negotiated_price_eur — последняя ОЗВУЧЕННАЯ нами цена в торгах (число EUR); "
        "если торга не было — цена объявления; null если непонятно.",
        "client_assessment — коротко о клиенте на русском: серьёзный / торгуется / лоу-баллер / спрашивает / горячий …",
    ]
    return "\n".join(lines)


_DEAL_PROPS: dict[str, Any] = {
    "deal_summary_ru": {"type": "string"},
    "expected_next": {"type": "string"},
    "negotiated_price_eur": {"type": ["number", "null"]},
    "client_assessment": {"type": "string"},
}
_ANSWER_PROPS: dict[str, Any] = {
    "client_lang": {"type": "string", "description": "ISO 639-1 код языка клиента"},
    "ru_answer": {"type": "string", "description": "Ответ на русском (для оператора)"},
    "client_answer": {"type": "string", "description": "Тот же ответ на языке клиента, готовый к отправке"},
    "ru_translation": {"type": "string", "description": "Точный обратный перевод client_answer на русский"},
    **_DEAL_PROPS,
}

# Черновик и регенерации (ручной режим)
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": dict(_ANSWER_PROPS),
    "required": list(_ANSWER_PROPS),
    "additionalProperties": False,
}

# Автопилот: + перевод письма клиента (incoming его не делает) + стоп-детекция
_AUTOPILOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_ANSWER_PROPS,
        "ru_client": {"type": "string", "description": "Перевод сообщения клиента на русский"},
        "should_stop": {"type": "boolean", "description": "true если автопилот должен остановиться"},
        "stop_reason": {"type": "string",
                        "description": "ready_to_buy / wants_contact / threat / '' если не стоп"},
        "client_pressing_below_floor": {"type": "boolean",
                                        "description": "true если клиент давит ниже floor_eur"},
    },
    "required": [*_ANSWER_PROPS, "ru_client", "should_stop", "stop_reason", "client_pressing_below_floor"],
    "additionalProperties": False,
}


def _answer_result(data: dict[str, Any], meta: dict[str, Any], fallback_lang: str = "de") -> dict[str, Any]:
    """Нормализованный результат генерации ответа.

    Ключи для всех потребителей сразу: `de_answer` (историческое имя колонки,
    читают incoming/drafts) и `client_answer` (читает api_ma); бриф — и вложенным
    `deal_brief`, и плоскими ключами deal_summary_ru/... (их читает api_ma).
    """
    deal = {
        "summary_ru": (data.get("deal_summary_ru") or "").strip(),
        "expected_next": (data.get("expected_next") or "").strip(),
        "negotiated_price_eur": data.get("negotiated_price_eur"),
        "client_assessment": (data.get("client_assessment") or "").strip(),
    }
    answer = data["client_answer"].strip()
    return {
        "client_lang": ((data.get("client_lang") or "").strip().lower() or fallback_lang),
        "ru_answer": data["ru_answer"].strip(),
        "client_answer": answer,
        "de_answer": answer,
        "ru_translation": (data.get("ru_translation") or "").strip(),
        "deal_brief": deal,
        "deal_summary_ru": deal["summary_ru"],
        "expected_next": deal["expected_next"],
        "negotiated_price_eur": deal["negotiated_price_eur"],
        "client_assessment": deal["client_assessment"],
        "tokens_in": meta["tokens_in"],
        "tokens_out": meta["tokens_out"],
        "cost_usd": meta["cost_usd"],
    }


def _system_prompt() -> str:
    import config
    return config.system_prompt()


# ─────────────────────────── Черновик ответа ───────────────────────────

def generate_draft(
    de_client_text: str,
    *,
    ad_title: str = "",
    ad_price: str = "",
    ad_description: str = "",
    seller_name: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    brief_text: str = "",
    client_lang: Optional[str] = None,
    task: str = "",
    current_draft: str = "",
    memory_text: str = "",
    lessons_text: str = "",
    ad_state: str = "",
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Ответ клиенту с полным контекстом (объявление, бриф, история).

    task — доп. задача (стратегия / цена / инструкция оператора); пусто — обычный
    первый черновик. current_draft — текущий черновик, если задача его переделать.
    memory_text / lessons_text — блоки из `extra_context` (память о покупателе, уроки).
    Возвращает `_answer_result`.
    """
    parts = _context_parts(
        ad_title=ad_title, ad_price=ad_price, ad_description=ad_description,
        seller_name=seller_name, brief_text=brief_text, history=history,
        memory_text=memory_text, lessons_text=lessons_text, ad_state=ad_state,
    )
    parts += ["=== НОВОЕ СООБЩЕНИЕ КЛИЕНТА (на него отвечаем) ===", (de_client_text or "").strip(), ""]
    if current_draft:
        parts += ["=== ТЕКУЩИЙ ЧЕРНОВИК (переработай его) ===", current_draft.strip(), ""]
    if task:
        parts += ["=== ЗАДАЧА ===", task.strip(), ""]
    parts += [_fields_instruction(client_lang, need_ru_client=False), "", GERMAN_CLOSING_RULE]

    data, meta = call_json(
        label="черновик ответа",
        model=smart_model(), system=_system_prompt(), user="\n".join(parts),
        schema=DRAFT_SCHEMA, max_tokens=max_tokens,
        nonempty=("client_lang", "ru_answer", "client_answer", "ru_translation"),
    )
    return _answer_result(data, meta, fallback_lang=client_lang or "de")


# ─────────────────────────── Автопилот ───────────────────────────

def _autopilot_addendum(floor_eur: float, last_our_price_eur: Optional[float]) -> str:
    text = (
        "=== РЕЖИМ АВТОПИЛОТА ===\n"
        "Ты ведёшь переговоры самостоятельно — оператор не проверяет твой ответ. Цель — продать товар.\n"
        f"HARD FLOOR: НЕ ОПУСКАТЬ цену ниже {floor_eur}€. "
    )
    if last_our_price_eur and last_our_price_eur > 0:
        text += f"Также НЕ опускать ниже того что уже сказали клиенту в этом треде ({last_our_price_eur}€).\n"
    else:
        text += "В этом треде мы ещё не озвучивали свою цену.\n"
    text += (
        "\nОБЯЗАТЕЛЬНО останавливай автопилот (should_stop=true) когда:\n"
        "- клиент готов купить / просит банк-реквизиты / адрес для встречи / детали закрытия → "
        "stop_reason=\"ready_to_buy\" или \"wants_contact\"\n"
        "- клиент пишет угрозы / агрессивные / оскорбительные фразы → stop_reason=\"threat\". В client_answer "
        "ответь «Wir betrachten das als Drohung. Bitte unterlassen Sie weitere Nachrichten dieser Art.» или "
        "эквивалент на ЯЗЫКЕ КЛИЕНТА. Это единственный stop_reason где client_answer ВСЁ ЕЩЁ отправляется; "
        "при остальных стоп-причинах пиши в client_answer нейтральный ответ (оператор решит руками).\n"
        "\nВ остальных случаях продолжай переговоры:\n"
        "- Просит то чего нет (другая комплектация, цвет, размер) → вежливо откажи: «нет в наличии, доступно только X»\n"
        "- Просит фото/видео которых у нас нет → «не под рукой, могу позже отправить / приезжайте посмотреть лично»\n"
        "- Технический вопрос — отвечай из своих знаний и описания. web_search — ТОЛЬКО для очень специфичного "
        "вопроса (совместимость с конкретной моделью/годом), когда без него ответить нельзя; не больше одного поиска.\n"
        "- НЕ ВРИ про конкретные числа, которых не знаешь: «уточню при встрече» или «в описании указано X».\n"
        "- Клиент давит ниже floor → откажи «Это окончательная цена», client_pressing_below_floor=true\n"
        "\nСтиль: настойчивый продавец, дружелюбный но твёрдый, держит фокус на закрытии сделки. 2-4 предложения."
    )
    return text


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
    client_memory: Optional[str] = None,
) -> dict[str, Any]:
    """Ответ автопилота: floor-aware, с web_search, со стоп-детекцией.

    lessons — строки lessons (как из db.find_relevant_lessons); client_memory — блок
    памяти о покупателе; если None — строится по последней строке истории (там есть
    buyer_name/аккаунт/тред), чтобы вызывающим не нужно было ничего менять.
    Возвращает `_answer_result` + ru_client / should_stop / stop_reason /
    client_pressing_below_floor / used_web_search.
    """
    from modules import llm_memory
    if client_memory is None:
        anchor = next((h for h in reversed(history or []) if h.get("buyer_name")), None)
        client_memory = _safe_memory(anchor) if anchor else ""
    parts = _context_parts(
        ad_title=ad_title, ad_price=ad_price, ad_description=ad_description,
        seller_name=seller_name, brief_text=brief_text, history=history,
        memory_text=client_memory, lessons_text=llm_memory.format_lessons(lessons),
    )
    parts += [
        "=== НОВОЕ СООБЩЕНИЕ КЛИЕНТА (на него отвечаем) ===", (de_client_text or "").strip(), "",
        _fields_instruction(None, need_ru_client=True), "", GERMAN_CLOSING_RULE, "",
        _autopilot_addendum(floor_eur, last_our_price_eur),
    ]
    data, meta = call_json(
        label="автопилот",
        model=smart_model(), system=_system_prompt(), user="\n".join(parts),
        schema=_AUTOPILOT_SCHEMA, max_tokens=max_tokens, web_search=True,
        # Автопилот отправляет без ревью — пустой ответ клиенту недопустим
        nonempty=("client_lang", "ru_client", "ru_answer", "client_answer", "ru_translation"),
    )
    result = _answer_result(data, meta)
    result.update({
        "ru_client": data["ru_client"].strip(),
        "should_stop": bool(data.get("should_stop")),
        "stop_reason": (data.get("stop_reason") or "").strip(),
        "client_pressing_below_floor": bool(data.get("client_pressing_below_floor")),
        "used_web_search": meta["used_web_search"],
    })
    return result


# ─────────────────────────── Переводы (быстрая модель) ───────────────────────────

_TRANSLATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"translation": {"type": "string"}},
    "required": ["translation"],
    "additionalProperties": False,
}

_TRANSLATE_BACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translation": {"type": "string", "description": "Перевод на целевой язык"},
        "back_translation_ru": {"type": "string",
                                "description": "Буквальный обратный перевод translation на русский"},
    },
    "required": ["translation", "back_translation_ru"],
    "additionalProperties": False,
}

_CLOSING_CLAUSE_DE = (
    " Если в исходном тексте нет прощальной формулы — обязательно добавь её в конце перевода "
    "(«MfG», «Viele Grüße», «Mit freundlichen Grüßen» или другая стандартная форма, на твой выбор)."
)


# Без этой рамки быстрая модель переводит «Geht 1200 bar?» как «Выдерживает 1200 бар?»
_TRADE_FRAME = (
    " Тексты — переписка продавца и покупателя на Kleinanzeigen.de (торг, цена, оплата, "
    "доставка, встреча): «bar» = наличными, «VB» = торг уместен, «Abholung» = самовывоз."
)


def _glossary(context: Optional[str]) -> str:
    """Товарный глоссарий: без него «4 сиденья» → «4 Plätze» (места) вместо «4 Sitze»."""
    if not (context and context.strip()):
        return _TRADE_FRAME
    return _TRADE_FRAME + (
        f"\n\nКонтекст переписки: продажа — «{context.strip()[:300]}»."
        "\nГлоссарий (соблюдай ТОЧНО при переводе на немецкий):"
        "\n- сиденье / сиденья → Sitz / Sitze (НЕ «Platz/Plätze»)"
        "\n- лавка / диван (в авто) → Sitzbank"
        "\n- одиночное сиденье → Einzelsitz"
        "\n- складной столик → Klapptisch"
        "\n- крепления / направляющие → Schienen / Befestigung"
        "\n- состояние / б/у / новое → Zustand / gebraucht / neu"
    )


def _lang_pair(direction: str, target_lang: Optional[str], source_lang: Optional[str]) -> tuple[str, str, str]:
    """(название исходного, название целевого, ISO целевого)."""
    if target_lang:
        dst = lang_display(target_lang)[0]
        src = lang_display(source_lang)[0] if source_lang else "исходного языка (определи сам)"
        return src, dst, target_lang.lower()
    if direction == "ru_to_de":
        return "русского", "немецкий", "de"
    return "немецкого", "русский", "ru"


def _check_translation(label: str, src: str, translation: str, target: str,
                       source_lang: Optional[str]) -> None:
    """Перевод не должен быть эхом исходника и должен быть на целевом языке.

    Ловит случай «модель вернула исходный русский текст» до того, как он уйдёт клиенту.
    """
    from modules.guardrails import language_verdict
    same_lang = source_lang and source_lang.lower() == target
    if not same_lang and len(src.strip()) > 20 and translation.strip() == src.strip():
        raise LLMResponseError(f"{label}: модель вернула исходный текст без перевода")
    if language_verdict(translation, target) == "mismatch":
        raise LLMResponseError(f"{label}: результат не на языке «{target}»")


def translate_only(
    text: str,
    direction: str = "ru_to_de",
    target_lang: Optional[str] = None,
    source_lang: Optional[str] = None,
    context: Optional[str] = None,
) -> dict[str, Any]:
    """Перевод без генерации ответа (быстрая модель).

    target_lang / source_lang — ISO-коды (приоритетнее legacy direction 'ru_to_de' / 'de_to_ru').
    Если source_lang не задан при заданном target_lang — модель определяет язык сама.
    context — название объявления для глоссария.
    Возвращает {translation, target_lang, tokens_in, tokens_out, cost_usd}.
    """
    src, dst, dst_code = _lang_pair(direction, target_lang, source_lang)
    closing = _CLOSING_CLAUSE_DE if dst_code == "de" else ""
    data, meta = call_json(
        label="перевод",
        model=fast_model(),
        system=(f"Ты профессиональный переводчик с {src} на {dst}. "
                "Переводишь точно и естественно, без отсебятины, пояснений и вариантов. Переводишь ВЕСЬ текст, "
                "ничего не отвечаешь на его содержание." + closing + _glossary(context)),
        user=(text or "").strip(),
        schema=_TRANSLATE_SCHEMA, max_tokens=1500, nonempty=("translation",),
    )
    _check_translation("перевод", text or "", data["translation"], dst_code, source_lang)
    return {
        "translation": data["translation"].strip(),
        "target_lang": dst_code,
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


def translate_with_back(
    text: str,
    target_lang: str,
    source_lang: Optional[str] = "ru",
    context: Optional[str] = None,
) -> dict[str, Any]:
    """Перевод на язык клиента + буквальный обратный перевод на русский ОДНИМ вызовом.

    Для edit-ru / compose-preview (раньше — два вызова translate_only подряд).
    Если target_lang='ru' — back_translation = сам перевод.
    Возвращает {translation, back_translation, target_lang, tokens_in, tokens_out, cost_usd}.
    """
    target = (target_lang or "de").lower()
    if target == "ru":
        tr = translate_only(text, target_lang="ru", source_lang=source_lang, context=context)
        tr["back_translation"] = tr["translation"]
        return tr
    src, dst, _ = _lang_pair("", target, source_lang)
    closing = _CLOSING_CLAUSE_DE if target == "de" else ""
    data, meta = call_json(
        label="перевод + обратный перевод",
        model=fast_model(),
        system=(f"Ты профессиональный переводчик с {src} на {dst}. "
                "translation — точный естественный перевод ВСЕГО текста, без отсебятины, "
                "ничего не отвечаешь на его содержание." + closing +
                " back_translation_ru — буквальный обратный перевод translation на русский: "
                "оператор по нему проверяет, что уйдёт клиенту." + _glossary(context)),
        user=(text or "").strip(),
        schema=_TRANSLATE_BACK_SCHEMA, max_tokens=2500,
        nonempty=("translation", "back_translation_ru"),
    )
    _check_translation("перевод + обратный перевод", text or "", data["translation"], target, source_lang)
    return {
        "translation": data["translation"].strip(),
        "back_translation": data["back_translation_ru"].strip(),
        "target_lang": target,
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


# ─────────────────────────── Регенерация черновика ───────────────────────────

# Письма в этих статусах уже ушли/закрыты — их черновик не перегенерируем
FINAL_STATUSES = ("sent", "sent_debug", "skipped", "skipped_sold", "not_sent_disabled", "sending")


def is_final(msg_row: Any) -> bool:
    """Письмо уже ушло/закрыто: финальный статус ИЛИ наш SMTP Message-ID записан
    (статус мог смениться позже — например, на edited после старой регенерации)."""
    return (_rget(msg_row, "status") in FINAL_STATUSES
            or bool((_rget(msg_row, "sent_message_id") or "").strip()))

# Пресеты, которые принимает api (/messages/{id}/regenerate, VALID_STRATEGIES)
QUICK_STRATEGIES: dict[str, str] = {
    "fest": "Цена ОКОНЧАТЕЛЬНАЯ. Никаких уступок. Вежливо но твёрдо отказать в скидке. 1-2 предложения.",
}

TWEAK_INSTRUCTIONS: dict[str, str] = {
    "regen": "Сгенерируй ДРУГОЙ вариант ответа с тем же смыслом — другая структура и слова.",
    "harsh": "Перепиши ЖЁСТЧЕ — без сюсюканий, твёрдо, по делу. Не агрессивно, но без лишней любезности.",
    "friend": "Перепиши ДРУЖЕЛЮБНЕЕ — теплее, человечнее, может с лёгкой эмпатией.",
    "short": "Сократи в 2 раза. Только суть.",
}


def draft_context(msg_row: Any) -> tuple[list[dict[str, Any]], str]:
    """Контекст для генерации по сохранённому письму: (история до письма, текст брифа)."""
    from modules import ad_brief
    thread_id = _rget(msg_row, "gmail_thread_id") or ""
    history = history_for(thread_id, exclude_id=_rget(msg_row, "id"),
                          before=_rget(msg_row, "created_at") or None)
    return history, ad_brief.brief_text_for(_rget(msg_row, "ad_id"))


def ad_state_for(msg_row: Any) -> str:
    """Статус объявления для промпта: 'sold' (ad_briefs.sold_at), 'deleted'/'reserved'
    (префикс в САМОМ СВЕЖЕМ заголовке треда — у старой строки он мог быть ещё без
    «Gelöscht»), иначе ''. Ошибка БД → '' с записью в лог."""
    from modules.parser import detect_ad_state
    try:
        ad_id = _rget(msg_row, "ad_id")
        if ad_id:
            bf = db.get_ad_brief(ad_id)
            if bf is not None and _rget(bf, "sold_at"):
                return "sold"
        title = _rget(msg_row, "ad_title") or ""
        tid = _rget(msg_row, "gmail_thread_id")
        if tid:
            titles = [_rget(r, "ad_title") for r in db.thread_history(tid) if _rget(r, "ad_title")]
            title = titles[-1] if titles else title
        state = detect_ad_state(title)
        return state if state in ("deleted", "reserved") else ""
    except Exception:  # noqa: BLE001
        logger.exception("статус объявления не определён для msg=%s", _rget(msg_row, "id"))
        return ""


def _safe_memory(msg_row: Any) -> str:
    """Память о покупателе; ошибка не валит генерацию ответа (логируется с traceback)."""
    from modules import llm_memory
    try:
        return llm_memory.client_memory_text(msg_row)
    except Exception:  # noqa: BLE001
        logger.exception("память о клиенте не собрана для msg=%s — генерирую без неё", _rget(msg_row, "id"))
        return ""


def extra_context(msg_row: Any) -> tuple[str, str]:
    """(memory_text, lessons_text) для письма: память о покупателе + релевантные уроки.

    Вспомогательный контекст: при ошибке БД — пустая строка и запись в лог с traceback,
    сам ответ всё равно генерируется.
    """
    from modules import llm_memory
    memory = _safe_memory(msg_row)
    try:
        lessons = llm_memory.format_lessons(llm_memory.lessons_for(msg_row))
    except Exception:  # noqa: BLE001
        logger.exception("уроки не загружены для msg=%s — генерирую без них", _rget(msg_row, "id"))
        lessons = ""
    return memory, lessons


def _regenerate_core(
    msg_row: Any,
    instruction: str,
    *,
    include_current_draft: bool,
    brief_text: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Регенерация с полным контекстом. Если вызывающий не передал history/brief
    (api_ma зовёт regenerate_with_* только с row) — подгружаем сами."""
    status = _rget(msg_row, "status")
    if is_final(msg_row):
        # api зовёт regenerate_with_* напрямую — защищаем отправленные письма здесь
        raise RuntimeError(
            f"Письмо #{_rget(msg_row, 'id')} уже обработано ({status}) — черновик не перезаписываю. "
            "Для нового сообщения клиенту используйте «✉️ Написать»."
        )
    if history is None or brief_text is None:
        auto_hist, auto_brief = draft_context(msg_row)
        history = auto_hist if history is None else history
        brief_text = auto_brief if brief_text is None else brief_text
    current = ""
    if include_current_draft:
        current = _rget(msg_row, "ru_answer") or _rget(msg_row, "de_answer") or ""
    memory_text, lessons_text = extra_context(msg_row)
    ad_state = ad_state_for(msg_row)
    return generate_draft(
        _rget(msg_row, "de_client") or "",
        ad_title=_rget(msg_row, "ad_title") or "",
        ad_price=_rget(msg_row, "ad_price") or "",
        ad_description=_rget(msg_row, "ad_description") or "",
        seller_name=_rget(msg_row, "seller_name") or "",
        history=history, brief_text=brief_text,
        client_lang=_rget(msg_row, "client_lang") or _rget(msg_row, "answer_lang") or None,
        task=instruction, current_draft=current,
        memory_text=memory_text, lessons_text=lessons_text, ad_state=ad_state,
    )


def regenerate_with_strategy(
    msg_row: Any,
    strategy: str,
    brief_text: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,  # noqa: ARG001 — совместимость (уроки подбираются сами)
) -> dict[str, Any]:
    """Перегенерить драфт под пресет (fest / harsh / friend / short / regen)."""
    instruction = QUICK_STRATEGIES.get(strategy) or TWEAK_INSTRUCTIONS.get(strategy)
    if not instruction:
        raise RuntimeError(f"Неизвестная стратегия: {strategy}")
    return _regenerate_core(
        msg_row, instruction,
        include_current_draft=strategy in TWEAK_INSTRUCTIONS,
        brief_text=brief_text, history=history,
    )


def regenerate_with_price(
    msg_row: Any,
    price_eur: float,
    brief_text: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,  # noqa: ARG001 — совместимость (уроки подбираются сами)
) -> dict[str, Any]:
    """Перегенерить драфт с конкретной ценой от оператора."""
    price_str = f"{int(price_eur)}" if float(price_eur).is_integer() else f"{price_eur:.2f}"
    instruction = (
        f"Согласись на цену ровно {price_str}€. Преподнеси как лучшее предложение, "
        f"кратко обоснуй (например: «это моё последнее предложение», «учитывая состояние», "
        f"«ради быстрой сделки»). 1-2 предложения. НЕ объясняй математику скидки."
    )
    return _regenerate_core(msg_row, instruction, include_current_draft=False,
                            brief_text=brief_text, history=history)


def regenerate_with_instruction(
    msg_row: Any,
    instruction: str,
    brief_text: Optional[str] = None,
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,  # noqa: ARG001 — совместимость (уроки подбираются сами)
) -> dict[str, Any]:
    """Перегенерить драфт по свободной инструкции оператора."""
    full_instruction = (
        f"Инструкция от оператора: {instruction.strip()}\n"
        f"Следуй этой инструкции. Если она противоречит общим правилам (требует обещать невыполнимое "
        f"или сильно нарушает политику) — следуй здравому смыслу и подсвети конфликт в ru_answer."
    )
    return _regenerate_core(msg_row, full_instruction, include_current_draft=True,
                            brief_text=brief_text, history=history)


# ─────────────────────────── Уроки ───────────────────────────

_LESSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "has_lesson": {"type": "boolean",
                       "description": "false, если правка — мелочь (опечатка, порядок слов) и учить нечему"},
        "situation_ru": {"type": "string", "description": "Ситуация клиента одной фразой на русском"},
        "lesson_ru": {"type": "string",
                      "description": "Обобщённый урок, 1-2 предложения: что делать / чего не делать в таких случаях"},
    },
    "required": ["has_lesson", "situation_ru", "lesson_ru"],
    "additionalProperties": False,
}


def extract_operator_lesson(
    *,
    client_message: str,
    llm_draft: str,
    sent_text: str,
    llm_draft_ru: str = "",
    sent_text_ru: str = "",
    ad_title: str = "",
) -> dict[str, Any]:
    """Сформулировать урок по паре «черновик LLM → что оператор реально отправил».

    Возвращает {lesson_ru ('' если учить нечему), situation_ru, tokens_in, tokens_out, cost_usd}.
    Запись в БД — llm_memory.learn_from_sent.
    """
    user = (
        f"Объявление: {ad_title or '—'}\n\n"
        f"=== СООБЩЕНИЕ КЛИЕНТА ===\n{_clip(client_message, 1500)}\n\n"
        f"=== ЧЕРНОВИК БОТА ===\n{_clip(llm_draft, 1500)}\n"
        + (f"(по-русски: {_clip(llm_draft_ru, 1000)})\n" if llm_draft_ru else "")
        + f"\n=== ОПЕРАТОР ОТПРАВИЛ ВМЕСТО ЭТОГО ===\n{_clip(sent_text, 1500)}\n"
        + (f"(по-русски: {_clip(sent_text_ru, 1000)})\n" if sent_text_ru else "")
        + "\nЧто оператор исправил по существу (цена, тон, факты, тактика, длина, лишние обещания)? "
          "Сформулируй обобщённый урок для будущих ответов бота — не пересказ этого письма, а правило. "
          "Если отличия косметические — has_lesson=false и пустой lesson_ru."
    )
    data, meta = call_json(
        label="урок из правки оператора",
        model=smart_model(),
        system="Ты наставник продавца на Kleinanzeigen.de. Извлекаешь уроки из правок оператора.",
        user=user, schema=_LESSON_SCHEMA, max_tokens=600,
    )
    lesson = (data.get("lesson_ru") or "").strip() if data.get("has_lesson") else ""
    return {
        "lesson_ru": lesson,
        "situation_ru": (data.get("situation_ru") or "").strip(),
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


# ─────────────────────────── Классификатор / детекторы (быстрая модель) ───────────────────────────

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
    """Классификация письма: настоящий buyer-inquiry vs системная рассылка.

    Возвращает {is_inquiry, reason, tokens_in, tokens_out, cost_usd}.
    """
    data, meta = call_json(
        label="классификатор писем",
        model=fast_model(),
        system=(
            "Ты помощник продавца на немецком сайте Kleinanzeigen.de. "
            "Классифицируешь входящие письма: реальный buyer-inquiry или системная рассылка."
        ),
        user=(
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
        ),
        schema=_INQUIRY_CLASSIFY_SCHEMA, max_tokens=200,
    )
    return {
        "is_inquiry": bool(data["is_inquiry"]),
        "reason": (data.get("reason") or "").strip(),
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


_LANG_DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lang": {
            "type": "string",
            "description": "ISO 639-1 код основного языка текста, или 'und' если текст языково-нейтрален",
        },
    },
    "required": ["lang"],
    "additionalProperties": False,
}


def detect_text_lang(text: str) -> dict[str, Any]:
    """Основной язык текста — финальный языковой guard send-флоу MA.

    Возвращает {lang, tokens_in, tokens_out, cost_usd}; lang='und' — языково-нейтральный
    текст (числа, «Ok», emoji).
    """
    data, meta = call_json(
        label="детектор языка",
        model=fast_model(),
        system="Ты определяешь язык текста. Отвечаешь строго по схеме.",
        user=(
            "Определи ОСНОВНОЙ язык этого сообщения (ISO 639-1: de/en/ru/tr/fr/...).\n"
            "Если текст языково-нейтрален (только числа, цены, имена, 'Ok', emoji) — верни 'und'.\n"
            "Вежливая подпись на другом языке (MfG и т.п.) не меняет основной язык.\n\n"
            f"Текст:\n{(text or '')[:2000]}"
        ),
        schema=_LANG_DETECT_SCHEMA, max_tokens=100, nonempty=("lang",),
    )
    return {
        "lang": data["lang"].strip().lower(),
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


_AUTO_ACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_lang": {"type": "string", "description": "ISO 639-1 код языка письма клиента"},
        "ack_text": {
            "type": "string",
            "description": "Короткий автоответ-заглушка на языке клиента, 1-2 предложения",
        },
        "ack_text_ru": {
            "type": "string",
            "description": "Точный перевод ack_text на русский (для оператора)",
        },
    },
    "required": ["client_lang", "ack_text", "ack_text_ru"],
    "additionalProperties": False,
}


def generate_auto_ack(
    buyer_display_name: str,
    body: str,
    hour_local: int,
    excuse_hint: str,
) -> dict[str, Any]:
    """Короткое приветствие-заглушка (накрутка метрики «отвечает быстро»).

    Сразу с RU-зеркалом (`ack_text_ru`) — отдельный вызов перевода не нужен.
    Возвращает {client_lang, ack_text, ack_text_ru, tokens_in, tokens_out, cost_usd}.
    """
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
        f"Поля: client_lang (ISO 639-1), ack_text, ack_text_ru — точный перевод ack_text на русский."
    )
    data, meta = call_json(
        label="auto-ack",
        model=fast_model(),
        system=(
            "Ты вежливый продавец на немецком сайте Kleinanzeigen.de. "
            "Пишешь короткое приветствие-заглушку покупателю когда не можешь ответить подробно сразу."
        ),
        user=user_msg, schema=_AUTO_ACK_SCHEMA, max_tokens=400, nonempty=("client_lang", "ack_text"),
    )
    lang = data["client_lang"].strip().lower()
    ack = data["ack_text"].strip()
    ack_ru = (data.get("ack_text_ru") or "").strip() or (ack if lang == "ru" else "")
    if not ack_ru:
        raise LLMResponseError("auto-ack: пустое RU-зеркало ack_text_ru")
    return {
        "client_lang": lang,
        "ack_text": ack,
        "ack_text_ru": ack_ru,
        # Совместимость: incoming читает translation из отдельного translate_only
        "translation": ack_ru,
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
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
    """Определить язык + перевести на русский (входящие письма → ru_client).

    Пустой текст → {lang:'?', translation_ru:''} без вызова. Ошибка LLM — исключение
    (вызывающие ловят и оставляют ru_client пустым, а не подставляют оригинал).
    """
    if not text or not text.strip():
        return {"lang": "?", "translation_ru": "", "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    data, meta = call_json(
        label="перевод входящего на русский",
        model=fast_model(),
        system=("Ты переводчик. Определи язык исходного текста и сделай точный перевод на русский. "
                "Переводи ВЕСЬ текст, ничего не отвечай на его содержание. Только перевод — без пояснений, "
                "вариантов и скобок." + _TRADE_FRAME),
        user=f"Текст:\n«{text[:3000]}»",
        schema=_DETECT_TRANSLATE_SCHEMA, max_tokens=1500, nonempty=("lang", "translation_ru"),
    )
    return {
        "lang": (data.get("lang") or "?").lower().strip(),
        "translation_ru": (data.get("translation_ru") or "").strip(),
        "tokens_in": meta["tokens_in"], "tokens_out": meta["tokens_out"], "cost_usd": meta["cost_usd"],
    }


# re-export для обратной совместимости
from modules.claude_scout import (  # noqa: F401,E402
    generate_scout_queries,
    classify_scout_listings,
)
