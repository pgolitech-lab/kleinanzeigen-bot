# Парсинг страницы объявления на Kleinanzeigen.de через Playwright (sync API).
# Из публичной страницы вытягивает: title, price, description, seller_name.

import re
from typing import Any, Optional

from playwright.sync_api import Page, sync_playwright

# Регулярка для поиска ссылки на объявление в тексте письма
KLEINANZEIGEN_URL_RE = re.compile(
    r'https?://(?:www\.)?kleinanzeigen\.de/s-anzeige/[^\s<>"\'\)]+',
    re.IGNORECASE,
)

# Регулярка для извлечения числового ID объявления (10 цифр) —
# Kleinanzeigen в text/plain пишет вид «Anfrage zu Ihrer Anzeige gesendet: 3390278104:».
KLEINANZEIGEN_AD_ID_RE = re.compile(r'\b(\d{10})\b')


def extract_url(*sources: Optional[str]) -> Optional[str]:
    """Найти первую ссылку на объявление Kleinanzeigen.
    Принимает несколько кусков текста — обычно (plain_body, html_body).
    """
    for src in sources:
        if not src:
            continue
        m = KLEINANZEIGEN_URL_RE.search(src)
        if m:
            return m.group(0)
    return None


def extract_ad_id(*sources: Optional[str]) -> Optional[str]:
    """Извлечь числовой ID объявления Kleinanzeigen из текста."""
    for src in sources:
        if not src:
            continue
        m = KLEINANZEIGEN_AD_ID_RE.search(src)
        if m:
            return m.group(1)
    return None


def url_from_ad_id(ad_id: str) -> str:
    """Сконструировать URL объявления по ID. Kleinanzeigen редиректит на полный URL."""
    return f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-0-0"


# Шаблонные заголовки Kleinanzeigen-уведомлений, которые надо срезать из тела письма.
# Каждый паттерн применяется один раз с начала текста (re.MULTILINE не нужен).
_NOISE_HEADER_PATTERNS: list[re.Pattern] = [
    # «Ein Interessent hat eine Anfrage zu Ihrer Anzeige gesendet: 3390278104:»
    re.compile(
        r'\AEin\s+Interessent\s+hat\s+eine?\s+(?:Anfrage|Frage|Nachricht)\s+'
        r'zu\s+Ihrer\s+Anzeige(?:\s+gesendet)?:?\s*\d{6,}:?\s*\n+',
        re.IGNORECASE,
    ),
    # «Sie haben eine neue Nachricht erhalten:»
    re.compile(r'\ASie\s+haben\s+eine\s+neue\s+Nachricht.*?:\s*\n+', re.IGNORECASE),
    # «Neue Nachricht zu Ihrer Anzeige <id>:»
    re.compile(r'\ANeue\s+Nachricht\s+zu\s+Ihrer\s+Anzeige\s*:?\s*\d*:?\s*\n+', re.IGNORECASE),
    # «<Имя> über Kleinanzeigen replied to your ad <id>:» (для replies в треде)
    re.compile(
        r'\A.{0,80}?(?:über|via|ueber)\s+Kleinanzeigen\s+replied\s+to\s+your\s+ad\s*\d{6,}\s*:?\s*\n+',
        re.IGNORECASE,
    ),
    # «Reply from <name>:» / «Antwort von <name>:» в начале
    re.compile(r'\A(?:Reply|Antwort)\s+(?:from|von)\s+[^\n:]+:\s*\n+', re.IGNORECASE),
]

# Шаблонные футеры/disclaimer Kleinanzeigen — обычно «не отвечайте на это письмо…»
_NOISE_FOOTER_PATTERNS: list[re.Pattern] = [
    re.compile(
        r'\n+(?:Bitte\s+(?:antworten\s+Sie\s+)?nicht\s+direkt|Antworten\s+Sie\s+nicht).*\Z',
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'\n+--\s*\nDiese\s+Nachricht.*\Z',
        re.IGNORECASE | re.DOTALL,
    ),
]


# Шаблоны Subject для системных писем Kleinanzeigen, которые НЕ являются inquiries
# и должны игнорироваться (saved-search alerts, истечение объявления, отзывы и т.п.).
JUNK_SUBJECT_PATTERNS: list[re.Pattern] = [
    re.compile(r'Neue\s+Treffer\s+zu\s+(?:deiner|Ihrer)\s+Suche', re.IGNORECASE),
    re.compile(r'Deine\s+Anzeige\s+(?:wurde\s+verlängert|wird\s+verlängert|läuft|ist\s+abgelaufen)', re.IGNORECASE),
    re.compile(r'Anzeige\s+(?:läuft\s+ab|automatisch\s+deaktiviert|verlängert)', re.IGNORECASE),
    re.compile(r'Bewertung\s+(?:erhalten|abgegeben|abgeben)', re.IGNORECASE),
    re.compile(r'Bestätige\s+deine?\s+E-?Mail', re.IGNORECASE),
    re.compile(r'(?:Kleinanzeigen\s+)?(?:Tipp|Newsletter|News\s+aus)', re.IGNORECASE),
    re.compile(r'Pro-Vorteile|Pro\s+(?:Account|Werden|Mitglied)', re.IGNORECASE),
    re.compile(r'Sicherheits-?(?:warnung|hinweis)', re.IGNORECASE),
    re.compile(r'(?:Willkommen|Welcome)\s+(?:bei|zu)\s+Kleinanzeigen', re.IGNORECASE),
    re.compile(r'Neue\s+Anzeigen?\s+von\s+', re.IGNORECASE),  # follow-seller alert
    re.compile(r'\bhat\s+eine\s+neue\s+Anzeige\s+aufgegeben\b', re.IGNORECASE),
    re.compile(r'\bDu\s+folgst\s+jetzt\b', re.IGNORECASE),
    re.compile(r'Anzeige\s+erfolgreich', re.IGNORECASE),
    re.compile(r'Preis(?:senkung|änderung|anpassung|gesenkt)', re.IGNORECASE),
]


def detect_ad_state(ad_title: Optional[str]) -> str:
    """Определить состояние объявления по заголовку который вернул Playwright.

    Kleinanzeigen рендерит префиксы «Gelöscht •» / «Reserviert •» прямо в title.
    Возвращает: 'deleted' / 'reserved' / 'active' / '' (если title не известен).
    """
    if not ad_title:
        return ""
    t = ad_title.strip().lower()
    if t.startswith("gelöscht") or t.startswith("geloescht"):
        return "deleted"
    if t.startswith("reserviert"):
        return "reserved"
    return "active"


def is_junk_subject(subject: str) -> bool:
    """Проверка: это системное письмо Kleinanzeigen которое не является inquiry."""
    if not subject:
        return False
    return any(p.search(subject) for p in JUNK_SUBJECT_PATTERNS)


# Whitelist: настоящий buyer-inquiry от Kleinanzeigen ВСЕГДА содержит «Anfrage»
# в Subject. «Anfrage zu Ihrer Anzeige…», «Nutzer-Anfrage zu deiner Anzeige…»,
# «Re: Anfrage…». Всё остальное — system noise (alerts, lifecycle, follow-seller).
INQUIRY_SUBJECT_PATTERNS: list[re.Pattern] = [
    re.compile(r'\bAnfrage\b', re.IGNORECASE),
]


def is_real_inquiry_subject(subject: str) -> bool:
    """Whitelist: subject содержит признаки реального buyer-inquiry."""
    if not subject:
        return False
    return any(p.search(subject) for p in INQUIRY_SUBJECT_PATTERNS)


# Маркеры автоматических/системных писем Kleinanzeigen в ТЕЛЕ (не в subject).
# Используются чтобы системка, залетевшая в активный тред, не прошла через
# classifier-bypass follow-up'ов (Bug 7).
SYSTEM_BODY_PATTERNS: list[re.Pattern] = [
    re.compile(r'automatisch\s+(?:generierte?|erstellte?|versendete?|erzeugte?)\s+(?:E-?Mail|Nachricht)', re.IGNORECASE),
    re.compile(r'automatisch\s+generiert', re.IGNORECASE),
    re.compile(r'Diese\s+(?:E-?Mail|Nachricht)\s+wurde\s+automatisch', re.IGNORECASE),
    re.compile(r'Bitte\s+(?:antworten\s+Sie\s+)?nicht\s+(?:direkt\s+)?auf\s+diese\s+E-?Mail', re.IGNORECASE),
    re.compile(r'Antworten\s+Sie\s+nicht\s+auf\s+diese', re.IGNORECASE),
    re.compile(r'\bno-?reply\b', re.IGNORECASE),
]


def is_system_message_body(body: str) -> bool:
    """Похоже ли тело письма на автоматическое/системное сообщение Kleinanzeigen.

    Дешёвая эвристика по маркерам («automatisch generierte E-Mail», «bitte nicht
    auf diese E-Mail antworten»). Применяется в classifier-bypass follow-up'ов,
    чтобы системка не прошла в Claude/оператора без проверки Haiku.
    """
    if not body:
        return False
    return any(p.search(body) for p in SYSTEM_BODY_PATTERNS)


def clean_email_body(text: str) -> str:
    """Срезать шаблонные заголовки/футеры Kleinanzeigen + лишние отступы и пустые строки.

    Идемпотентна: повторный вызов на уже-очищенном тексте даёт тот же результат.
    Если паттерны не сработали — возвращает исходный текст trimmed.
    """
    if not text:
        return ""
    cleaned = text
    for p in _NOISE_HEADER_PATTERNS:
        new = p.sub('', cleaned, count=1)
        if new != cleaned:
            cleaned = new
            break
    for p in _NOISE_FOOTER_PATTERNS:
        cleaned = p.sub('', cleaned)

    # Удаляем общий лидирующий отступ непустых строк (Kleinanzeigen часто
    # вставляет «    Hallo...» с 4 пробелами).
    lines = [l.rstrip() for l in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        min_indent = min(
            (len(l) - len(l.lstrip(' ')) for l in lines if l.strip()),
            default=0,
        )
        if min_indent > 0:
            lines = [l[min_indent:] if len(l) >= min_indent else l for l in lines]

    return re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()


def _text(page: Page, selector: str) -> str:
    """Прочитать inner_text элемента по селектору. Пустая строка если нет."""
    try:
        el = page.query_selector(selector)
        if not el:
            return ""
        return (el.inner_text() or "").strip()
    except Exception:
        return ""


def _accept_cookies(page: Page) -> None:
    """Закрыть CMP-баннер cookies, если он появился. Молча игнорирует если нет."""
    selectors = [
        "#gdpr-banner-accept",
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _extract_seller_name(page: Page) -> str:
    """Достать имя продавца. Сначала из JS (contactName), потом fallback на DOM."""

    # Попытка 1: переменная contactName в window / viewadModel
    try:
        name = page.evaluate("""() => {
            try {
                if (typeof contactName !== 'undefined' && contactName) return contactName;
                if (window.contactName) return window.contactName;
                if (window.viewadModel && window.viewadModel.contactName) return window.viewadModel.contactName;
                if (window.GAFunctions && window.GAFunctions.contactName) return window.GAFunctions.contactName;
            } catch (e) {}
            return null;
        }""")
        if name and isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass

    # Попытка 2: регуляркой по HTML страницы (var contactName = "..." / "contactName":"...")
    try:
        html = page.content()
        for pattern in (
            r'contactName\s*[:=]\s*["\']([^"\']+)["\']',
            r'"contactName"\s*:\s*"([^"]+)"',
        ):
            m = re.search(pattern, html)
            if m:
                return m.group(1).strip()
    except Exception:
        pass

    # Попытка 3: DOM-блок контакта продавца
    for sel in (
        "#viewad-contact .text-body-regular-strong",
        "#viewad-contact a[href*='/s-bestandsliste']",
        "#viewad-contact a[href*='/profile']",
        ".userprofile-vip a",
    ):
        text = _text(page, sel)
        if text:
            return text

    return ""


def parse_ad(
    url: str,
    headless: bool = True,
    timeout_ms: int = 30000,
    user_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Открыть страницу объявления и вернуть данные.

    Возвращает dict: url, title, price, description, seller_name.
    Если какое-то поле не удалось получить — оно будет пустой строкой.
    """
    result: dict[str, Any] = {
        "url": url,
        "title": "",
        "price": "",
        "description": "",
        "seller_name": "",
    }

    default_ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(
                user_agent=user_agent or default_ua,
                locale="de-DE",
                viewport={"width": 1366, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

            # Закрыть cookie-баннер если есть (не критично)
            _accept_cookies(page)

            # Дождаться появления заголовка — страница может рендериться async
            try:
                page.wait_for_selector("#viewad-title", timeout=10000)
            except Exception:
                pass

            result["title"] = _text(page, "#viewad-title")
            result["price"] = _text(page, "#viewad-price")
            result["description"] = _text(page, "#viewad-description-text")
            result["seller_name"] = _extract_seller_name(page)

        finally:
            browser.close()

    return result


if __name__ == "__main__":
    # Ручной тест: python modules/parser.py <url>
    import json
    import sys
    if len(sys.argv) < 2:
        print("Использование: python modules/parser.py <url объявления>")
        sys.exit(1)
    data = parse_ad(sys.argv[1])
    print(json.dumps(data, ensure_ascii=False, indent=2))
