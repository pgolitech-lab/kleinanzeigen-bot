# Детерминированные guardrails для негоциации — «код владеет числами».
# Чистые функции без побочных эффектов и без обращений к БД/сети; исключение —
# outgoing_language_ok_llm (добирает Haiku, когда эвристика языка не уверена).

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


_CYRILLIC_RE = re.compile(r"[а-яёіїєґўђјљњћџ]", re.IGNORECASE)
_SCRIPT_RES: dict[str, "re.Pattern[str]"] = {
    "cyrillic": _CYRILLIC_RE,
    "latin": re.compile(r"[a-zà-ɏ]", re.IGNORECASE),
    "arabic": re.compile(r"[؀-ۿ]"),
    "greek": re.compile(r"[α-ωά-ώ]", re.IGNORECASE),
    "hebrew": re.compile(r"[א-ת]"),
    "cjk": re.compile(r"[぀-ヿ一-鿿가-힯]"),
}
_LANG_SCRIPT: dict[str, str] = {
    **{c: "cyrillic" for c in ("ru", "uk", "bg", "sr", "mk", "be", "kk")},
    **{c: "arabic" for c in ("ar", "fa", "ur")},
    "el": "greek", "he": "hebrew", "zh": "cjk", "ja": "cjk", "ko": "cjk",
}

# Частые служебные слова: по ним отличаем языки с одной письменностью.
# Порог — минимум 2 совпадения, чтобы имя/подпись («MfG», «Müller») не решали за весь текст.
_STOPWORDS: dict[str, set[str]] = {
    "de": {"und", "ist", "ich", "nicht", "sie", "das", "die", "der", "mit", "für", "noch",
           "gerne", "wir", "haben", "ein", "eine", "auch", "bitte", "danke", "kann", "wäre", "es"},
    "en": {"the", "and", "is", "you", "it", "for", "with", "not", "are", "can", "have",
           "still", "available", "thanks", "please", "would", "we", "this", "of", "to"},
    "tr": {"ve", "bir", "bu", "için", "ile", "çok", "mi", "mı", "evet", "hayır", "teşekkürler",
           "merhaba", "var", "yok", "fiyat", "ne", "ben", "siz", "olarak"},
    "pl": {"jest", "nie", "się", "i", "na", "że", "dla", "czy", "jak", "dzień", "dobry",
           "dziękuję", "proszę", "tak", "cena", "mam", "pan", "pani", "do"},
    "fr": {"le", "la", "les", "est", "et", "pour", "avec", "pas", "vous", "je", "bonjour",
           "merci", "oui", "une", "des", "du", "encore"},
    "es": {"el", "la", "es", "y", "para", "con", "no", "usted", "hola", "gracias", "sí",
           "una", "los", "las", "todavía", "precio"},
    "it": {"il", "è", "e", "per", "con", "non", "sono", "ciao", "grazie", "una", "ancora",
           "prezzo", "buongiorno", "della"},
    "nl": {"het", "een", "en", "is", "niet", "voor", "met", "nog", "ik", "u", "bedankt", "graag", "ja"},
    "ru": {"и", "не", "в", "на", "что", "это", "да", "есть", "вы", "я", "спасибо",
           "здравствуйте", "цена", "ещё", "еще", "можно", "мы", "по", "для", "уже"},
    "uk": {"і", "що", "це", "так", "є", "ви", "дякую", "добрий", "день", "ціна", "ще",
           "можна", "ми", "для", "вже", "будь", "ласка"},
    "bg": {"и", "не", "е", "на", "че", "това", "да", "има", "вие", "аз", "благодаря",
           "здравейте", "цена", "още", "може", "ние", "за", "вече"},
}
# Буквы, которых нет в «соседях» по письменности: однозначный сигнал языка
_MARKERS: dict[str, "re.Pattern[str]"] = {
    "uk": re.compile(r"[іїєґ]", re.IGNORECASE),
    "ru": re.compile(r"[ыэё]", re.IGNORECASE),
    # без IGNORECASE: «ı» при свёртке регистра совпадает с обычной «i»
    "tr": re.compile(r"[ğışĞŞİ]"),
    "pl": re.compile(r"[ąęłńśźż]", re.IGNORECASE),
    "de": re.compile(r"[äöüß]", re.IGNORECASE),
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _script_of(text: str) -> Optional[str]:
    """Преобладающая письменность текста (по числу букв) или None, если букв нет."""
    counts = {name: len(rx.findall(text)) for name, rx in _SCRIPT_RES.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def language_verdict(text: str, client_lang: Optional[str]) -> str:
    """Дешёвая эвристика: подходит ли язык текста языку клиента.

    'ok' — уверенно совпадает; 'mismatch' — уверенно другой язык (другая письменность
    или явные слова/буквы другого языка при нуле признаков языка клиента);
    'unsure' — эвристика не решает (короткий текст, нет известных слов) → нужна LLM-проверка.
    Пустой текст → 'mismatch' (нечего слать).
    """
    if not text or not text.strip():
        return "mismatch"
    lang = (client_lang or "de").strip().lower()
    script = _script_of(text)
    if script is None:
        return "ok"  # только цифры/знаки — языково-нейтрально
    if script != _LANG_SCRIPT.get(lang, "latin"):
        return "mismatch"

    words = [w.lower() for w in _WORD_RE.findall(text)]
    scores = {code: sum(1 for w in words if w in sw) for code, sw in _STOPWORDS.items()
              if _LANG_SCRIPT.get(code, "latin") == script}
    for code, rx in _MARKERS.items():
        if code in scores and rx.search(text):
            scores[code] += 2
    own = scores.get(lang, 0)
    rival_code, rival = max(((c, v) for c, v in scores.items() if c != lang),
                            key=lambda cv: cv[1], default=(None, 0))
    if lang not in scores:
        # Язык без словаря (fr-подобные редкие, sr, …): уверены только при сильном сопернике
        return "mismatch" if rival >= 4 else "unsure"
    if own >= 2 and own >= rival:
        return "ok"
    if rival >= 2 and own == 0:
        return "mismatch"
    return "unsure"


def outgoing_language_ok(text: str, client_lang: Optional[str]) -> bool:
    """Чистая (без LLM) проверка ПИСЬМЕННОСТИ исходящего: кириллица — только клиентам
    на кириллических языках (ru/uk/bg/…), латиница — латинским и т.д. Пустой текст → False.
    Различение языков одной письменности (de/en, ru/uk) — `language_verdict` /
    `outgoing_language_ok_llm`."""
    if not text or not text.strip():
        return False
    script = _script_of(text)
    if script is None:
        return True  # только цифры/знаки
    return script == _LANG_SCRIPT.get((client_lang or "de").strip().lower(), "latin")


def outgoing_language_ok_llm(text: str, client_lang: Optional[str]) -> bool:
    """Эвристика + Haiku (claude.detect_text_lang), если эвристика не уверена.

    Fail-closed: ошибка LLM → False (автоотправка не должна уйти вслепую; вызывающий
    отдаёт письмо оператору). Языково-нейтральный текст ('und') — ок.
    """
    verdict = language_verdict(text, client_lang)
    if verdict != "unsure":
        return verdict == "ok"
    from modules import claude
    try:
        detected = claude.detect_text_lang(text)["lang"]
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("language guard: detect_text_lang упал — считаю несовпадением")
        return False
    return detected in ("und", (client_lang or "de").strip().lower())


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
