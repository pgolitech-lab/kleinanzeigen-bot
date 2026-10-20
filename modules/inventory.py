# Склад: товары, объявления → товар, движения, отчёт и правила «продано / в наличии».
#
# Главная идея: «товар» живёт дольше «объявления». Владелец удаляет и перепощивает объявления
# ради поднятия, держит одно и то же на нескольких аккаунтах, продаёт по штуке из большого запаса.
# Поэтому:
#   * «Продано» в треде = −1 к остатку товара, а не «объявление продано».
#   * «Gelöscht» на объявлении ≠ «товара нет», если есть остаток или другое живое объявление.
#   * Остаток «неизвестен», пока оператор не внёс приход/инвентаризацию — из неизвестного
#     нельзя делать вывод «нет в наличии».
#
# Таблицы и SQL — modules/db_inventory.py. Здесь — нормализация заголовков, автопривязка
# (точное / нормализованное совпадение, при сомнении Haiku или вопрос оператору), синхронизация
# с messages (объявления, продажи), FIFO-отчёт.

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional

from database import get_conn, transaction
from modules import db_inventory as inv

logger = logging.getLogger(__name__)

# Порог похожести, с которого пара «новое объявление ↔ товар» считается сомнительной
# (решает Haiku или оператор). Ниже — уверенно новый товар.
DOUBT_SCORE = 0.55
# Объявление считается «живым», если его видели в письмах не раньше стольких дней назад.
ACTIVE_LISTING_DAYS = 45
# Товар «залежался», если самая старая единица на складе старше стольких дней.
STALE_DAYS = 60


# ─── заголовки ────────────────────────────────────────────────────────────────

_SUBJECT_RE = re.compile(
    r'^\s*(?:re:\s*)?(?:nutzer-)?anfrage\s+zu\s+(?:deiner|ihrer)\s+anzeige\s*[„"«»“]?(.*?)[“"»«]?\s*$',
    re.IGNORECASE,
)
_STATE_RE = re.compile(r'^\s*(gelöscht|geloescht|reserviert)\s*[•·\-–:]\s*', re.IGNORECASE)
# «3 er Sitzbank 2+1 Spacetourer ... bis zu 25 % schneller verkaufen*» — обрезанная промо-рассылка
_PROMO_RE = re.compile(r'\.\.\.\s*bis zu .*schneller verkaufen\*?\s*$', re.IGNORECASE)


@dataclass
class CleanTitle:
    title: str          # заголовок без служебных префиксов ('' если взять нечего)
    state: str          # active / deleted / reserved
    quality: int        # 3 — заголовок объявления, 2 — из темы «Nutzer-Anfrage», 1 — обрезанная промо, 0 — мусор


def clean_ad_title(raw: Optional[str], *, has_ad_data: bool = False) -> CleanTitle:
    """Вытащить название объявления из messages.ad_title.

    В ad_title бывает: настоящий заголовок (Playwright), тема письма «Nutzer-Anfrage zu deiner
    Anzeige "…"», префикс «Gelöscht • / Reserviert •», обрезанная промо «… bis zu 25 % schneller
    verkaufen*», тема рассылки («Deine wöchentliche Anzeigenübersicht»). has_ad_data — у строки есть
    цена/описание объявления, т. е. заголовок пришёл со страницы, а не из темы письма.
    """
    t = (raw or "").strip()
    if not t:
        return CleanTitle("", "active", 0)
    state = "active"
    m = _STATE_RE.match(t)
    if m:
        state = "deleted" if m.group(1).lower().startswith("gel") else "reserved"
        t = t[m.end():].strip()
    m = _SUBJECT_RE.match(t)
    if m:
        return CleanTitle(_squash(m.group(1)), state, 2)
    if _PROMO_RE.search(t):
        return CleanTitle(_squash(_PROMO_RE.sub("", t)), state, 1)
    if has_ad_data:
        return CleanTitle(_squash(t), state, 3)
    # Без цены/описания и не тема запроса — скорее всего тема рассылки Kleinanzeigen
    return CleanTitle("", state, 0)


def _squash(s: str) -> str:
    return " ".join((s or "").replace('"', " ").split()).strip(" ,-–")


_CYR = str.maketrans("аеорсхук", "aeopcxyk")


def _fold(s: str) -> str:
    """casefold + без диакритики + кириллические «двойники» латиницы (Zafirа)."""
    s = (s or "").casefold().replace("ß", "ss").translate(_CYR)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def tokens(title: str) -> list[str]:
    s = _fold(title)
    s = re.sub(r'\b(\d)\s*-?\s*er\b', r'\1er', s)      # «3 er», «3-er» → «3er»
    s = re.sub(r'(\d)\s*\+\s*(\d)', r'\1+\2', s)         # «2 + 1» → «2+1»
    return re.findall(r'\d\+\d|[a-z0-9]+', s)


_STOP = {"und", "fur", "mit", "der", "die", "das", "for", "von", "zu", "in", "an", "div", "z", "b",
         "l", "r", "ca", "bis", "ab", "vorne", "hinten", "original", "orig", "komplett", "x"}
# Модели авто — у владельца почти в каждом заголовке и для разных товаров одни и те же,
# поэтому в «ядро» похожести не идут (иначе любые две детали Traveller похожи).
_MODELS = {"peugeot", "traveller", "teaveller", "expert", "citroen", "cirtroen", "spacetourer", "space",
           "tourer", "toyota", "totota", "proace", "verso", "opel", "zafira", "life", "fiat", "ulysse",
           "vivaro", "jumpy", "psa", "mistral", "scudo", "c", "tesla", "model", "ford", "mondeo",
           "renault"}


def match_key(title: str) -> str:
    """Нормализованный ключ: одинаков у перепощенных объявлений с тем же текстом
    (другой порядок слов, лишние пробелы, Citroën/Citroen, «3-er»/«3er»)."""
    return " ".join(sorted(set(t for t in tokens(title) if t not in _STOP)))


_CATEGORIES: list[tuple[str, re.Pattern]] = [
    # Порядок важен: сначала узкие признаки («Sicherheitsgurte … Fahrer Beifahrer» — это гурты,
    # «Traveller BlueHDi – 8 Sitze» — машина, «Rückleuchten Set» — фонари, а не комплект сидений)
    ("fahrzeug", re.compile(r'bluehdi|\bkombi\b|wohnwagen|schlachtfest|\btuv\b|\b[7-9] ?sitzer\b')),
    ("immobilie", re.compile(r'garage|vermieten|lagerraum|stellplatz|\bhaus\b')),
    ("gurte", re.compile(r'gurt')),
    ("leuchten", re.compile(r'leuchte|scheinwerfer')),
    ("komplettsatz", re.compile(r'komplettsatz|sitzpaket|sitzgarnitur|\bsitz\w* (?:\w+ )?set\b|\b[6-9] ?sitze\b')),
    ("fahrersitze", re.compile(r'beifahrer|fahrersitz|doppelbank vorne')),
    ("einzelsitz", re.compile(r'einzelsitz|\bvip\b')),
    ("sitzbank", re.compile(r'sitzbank|doppelsitzbank|\b2\+1\b|\b3er\b')),
    ("sitze", re.compile(r'\b(?:leder)?sitze\b|\bsitz\b')),
    ("tisch", re.compile(r'tisch')),
    ("ahk", re.compile(r'anhangerkupplung')),
    ("sitzschienen", re.compile(r'sitzschiene|bodenplatte')),
    ("raeder", re.compile(r'felgen|reifen|radkappen|nabenkappen')),
    ("ladekabel", re.compile(r'ladekabel')),
    ("dach", re.compile(r'dachreling|dachtrager')),
    ("karosserie", re.compile(r'heckklappe|hecktur|motorhaube|seitenverkleidung')),
]


def detect_category(title: str) -> str:
    s = " ".join(tokens(title))
    for name, rx in _CATEGORIES:
        if rx.search(s):
            return name
    return "other"


def features(title: str) -> dict[str, bool]:
    s = " ".join(tokens(title))
    return {
        "defekt": bool(re.search(r'defekt|defekr|b ware|beschadig|schlachtfest', s)),
        "neu": bool(re.search(r'\bneu\b|nagelneu|neuwertig|\bovp\b', s)),
        "vip": "vip" in s.split(),
        "leder": "leder" in s,
    }


def detect_condition(title: str) -> Optional[str]:
    f = features(title)
    if f["defekt"]:
        return "defekt"
    if f["neu"]:
        return "neu"
    return None


_PRICE_RE = re.compile(r'(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,(\d{1,2}))?')


def parse_price(raw: Any) -> Optional[float]:
    """«1.299 € VB» → 1299.0, «80 €» → 80.0, число → float, пусто/мусор → None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    m = _PRICE_RE.search(str(raw))
    if not m:
        return None
    val = float(re.sub(r'[.\s]', "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
    return val if val > 0 else None


def _core(title: str) -> set[str]:
    return {t for t in tokens(title) if t not in _STOP and t not in _MODELS}


def similarity(a_title: str, a_price: Optional[float], b_title: str, b_price: Optional[float]) -> float:
    """Похожесть двух объявлений 0..1: ядро заголовка (без моделей авто) + цена.
    Разные категории или противоречие признаков (дефект/новое/VIP) — сильный штраф."""
    if detect_category(a_title) != detect_category(b_title):
        return 0.0
    ca, cb = _core(a_title), _core(b_title)
    jac = len(ca & cb) / len(ca | cb) if (ca or cb) else 0.0
    if a_price and b_price:
        price_sim = 1.0 - min(1.0, abs(a_price - b_price) / max(a_price, b_price))
    else:
        price_sim = 0.5
    score = 0.6 * jac + 0.4 * price_sim
    fa, fb = features(a_title), features(b_title)
    if any(fa[k] != fb[k] for k in ("defekt", "neu", "vip")):
        score *= 0.3
    return round(score, 3)


# ─── сопоставление ────────────────────────────────────────────────────────────

@dataclass
class IndexEntry:
    product: Any               # id товара в БД или черновик группы при начальном заполнении
    title: str
    price: Optional[float]
    key: str
    category: str


@dataclass
class Decision:
    kind: str                  # exact / doubt / new
    product: Any = None        # exact — куда привязать; doubt — кандидат
    score: float = 0.0
    reason: str = ""
    others: list = field(default_factory=list)  # exact с несколькими товарами — остальные кандидаты


def decide(title: str, price: Optional[float], index: Iterable[IndexEntry]) -> Decision:
    """Чистая функция: к какому товару относится объявление (title, price)."""
    key = match_key(title)
    entries = list(index)
    exact: list[Any] = []
    for e in entries:
        if key and e.key == key and e.product not in exact:
            exact.append(e.product)
    if exact:
        # Несколько товаров с тем же текстом — берём последний (свежие объявления в конце индекса),
        # остальные — вопрос оператору
        return Decision("exact", exact[-1], 1.0, "тот же заголовок", others=exact[:-1])
    if detect_category(title) == "other":
        return Decision("new", reason="категория не распознана — только точное совпадение")
    best: Optional[IndexEntry] = None
    best_score = 0.0
    for e in entries:
        s = similarity(title, price, e.title, e.price)
        if s > best_score:
            best, best_score = e, s
    if best is not None and best_score >= DOUBT_SCORE:
        return Decision("doubt", best.product, best_score,
                        f"похоже на «{best.title}»" + (f" ({best.price:.0f} €)" if best.price else ""))
    return Decision("new", score=best_score)


_SAME_SCHEMA = {
    "type": "object",
    "properties": {
        "same": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    },
    "required": ["same", "confidence", "reason"],
    "additionalProperties": False,
}

_SAME_SYSTEM = (
    "Ты помогаешь вести склад продавца б/у автозапчастей на Kleinanzeigen. Продавец часто удаляет "
    "объявление и публикует то же самое заново (другие слова, другой порядок моделей авто, другая "
    "цена), иногда с разных аккаунтов. Определи, описывают ли два объявления ОДИН И ТОТ ЖЕ товар "
    "(ту же позицию склада), а не просто похожие детали. Разная конфигурация (2+1 против одиночного "
    "сиденья), разная обивка, дефект против целого — разные товары. Перечень совместимых моделей "
    "(Traveller / SpaceTourer / ProAce / Zafira — одна платформа) сам по себе не различает товары. "
    "confidence=high — только если уверен. Ответ JSON по схеме, reason — одна фраза по-русски."
)


def llm_same_product(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Спросить Haiku: одно ли это товар. a/b: {title, price, description}.
    Возвращает {same, confidence, reason}. Ошибки LLM пробрасываются вызывающему."""
    from modules import claude_utils

    def fmt(x: dict[str, Any]) -> str:
        desc = (x.get("description") or "").strip().replace("\n", " ")[:600]
        price = f"{x['price']:.0f} €" if x.get("price") else "цена неизвестна"
        return f"Заголовок: {x.get('title')}\nЦена: {price}\nОписание: {desc or '—'}"

    user = f"Объявление A:\n{fmt(a)}\n\nОбъявление B:\n{fmt(b)}"
    data, _meta = claude_utils.call_json(
        label="inventory.same_product", model=claude_utils.fast_model(), system=_SAME_SYSTEM,
        user=user, schema=_SAME_SCHEMA, max_tokens=300, nonempty=("reason",),
    )
    return data


def _ad_description(ad_id: str) -> str:
    rows = inv.iter_rows(
        "SELECT ad_description FROM messages WHERE ad_id = ? AND ad_description IS NOT NULL "
        "AND ad_description != '' ORDER BY created_at DESC LIMIT 1", (str(ad_id),))
    return rows[0]["ad_description"] if rows else ""


def _db_index() -> list[IndexEntry]:
    """Индекс для сопоставления: все объявления неархивных товаров, старые → новые."""
    rows = inv.iter_rows("""
        SELECT l.ad_id, l.title, l.price_eur, l.product_id
          FROM inv_listings l JOIN inv_products p ON p.id = l.product_id
         WHERE p.archived_at IS NULL AND l.title IS NOT NULL
         ORDER BY l.last_seen_at, l.ad_id
    """)
    return [IndexEntry(r["product_id"], r["title"], r["price_eur"], match_key(r["title"]),
                       detect_category(r["title"])) for r in rows]


def _judge(title: str, price: Optional[float], ad_id: str, description: Optional[str],
           cand_product: int, judge: Callable[[dict, dict], dict]) -> Optional[dict[str, Any]]:
    """Haiku для сомнительной пары. None — LLM недоступен (решит оператор)."""
    cand = inv.iter_rows("SELECT ad_id, title, price_eur FROM inv_listings WHERE product_id = ? "
                         "ORDER BY last_seen_at DESC LIMIT 1", (int(cand_product),))
    if not cand:
        return None
    c = cand[0]
    try:
        return judge(
            {"title": title, "price": price, "description": description or _ad_description(ad_id)},
            {"title": c["title"], "price": c["price_eur"], "description": _ad_description(c["ad_id"])},
        )
    except Exception:  # noqa: BLE001 — LLM недоступен / ответ непригоден: решит оператор
        logger.warning("склад: Haiku не ответил по паре %s ↔ товар %s — вопрос оператору",
                       ad_id, cand_product, exc_info=True)
        return None


def link_listing(ad_id: str, *, title: Optional[str], price: Any = None,
                 account_id: Optional[int] = None, url: Optional[str] = None,
                 seen_at: Optional[str] = None, description: Optional[str] = None,
                 has_ad_data: Optional[bool] = None, state: Optional[str] = None,
                 use_llm: bool = False,
                 judge: Optional[Callable[[dict, dict], dict]] = None) -> dict[str, Any]:
    """Привязать объявление к товару (или обновить уже привязанное).

    Известный ad_id — обновить заголовок/цену/состояние/last_seen. Новый ad_id:
      * тот же нормализованный заголовок, что у объявления существующего товара → туда же;
      * похожее (ядро заголовка + цена, ≥ DOUBT_SCORE) → Haiku (use_llm) решает при high,
        иначе новый товар + вопрос оператору «это тот же товар?»;
      * иначе — новый товар с неизвестным остатком.
    title — сырой ad_title из письма (чистится здесь) или уже очищенный (тогда has_ad_data=True
    и state — состояние, снятое при очистке).
    Возвращает {ad_id, product_id, method, question_id?, reason}. product_id=None — заголовок
    мусорный (тема рассылки), объявление не заведено.
    """
    inv.ensure_schema()
    ad_id = str(ad_id)
    price_eur = parse_price(price)
    if has_ad_data is None:
        has_ad_data = price_eur is not None or bool(description)
    ct = clean_ad_title(title, has_ad_data=has_ad_data)
    if state and ct.quality:
        ct.state = state

    existing = inv.get_listing(ad_id)
    if existing is not None:
        inv.upsert_listing(ad_id=ad_id, product_id=existing["product_id"],
                           link_method=existing["link_method"], seen_at=seen_at, account_id=account_id,
                           title=ct.title if ct.quality >= 2 else None, price_eur=price_eur, url=url,
                           state=ct.state if ct.quality else existing["state"])
        return {"ad_id": ad_id, "product_id": existing["product_id"], "method": "known", "reason": ""}
    if not ct.title:
        return {"ad_id": ad_id, "product_id": None, "method": "skipped", "reason": "мусорный заголовок"}

    decision = decide(ct.title, price_eur, _db_index())
    verdict: Optional[dict[str, Any]] = None
    if decision.kind == "doubt" and use_llm:
        verdict = _judge(ct.title, price_eur, ad_id, description, decision.product,
                         judge or llm_same_product)

    question: Optional[tuple[int, float, str]] = None  # (suggested_product, score, reason)
    with transaction() as conn:
        # Пока думал Haiku, объявление мог завести параллельный вызов
        again = conn.execute("SELECT product_id FROM inv_listings WHERE ad_id = ?", (ad_id,)).fetchone()
        if again is not None:
            return {"ad_id": ad_id, "product_id": again["product_id"], "method": "known", "reason": ""}
        if decision.kind == "exact":
            product_id, method, reason = decision.product, "normalized", decision.reason
            if decision.others:
                question = (decision.others[-1], 1.0, "тот же заголовок у нескольких товаров")
        elif decision.kind == "doubt" and verdict and verdict.get("same") and verdict.get("confidence") == "high":
            product_id, method = decision.product, "llm"
            reason = f"Haiku: {verdict.get('reason', '')}"
        else:
            product_id = inv.create_product(
                title=ct.title, category=detect_category(ct.title), condition=detect_condition(ct.title),
                list_price_eur=price_eur, match_key=match_key(ct.title))
            method, reason = "new", "новый товар"
            if decision.kind == "doubt" and not (verdict and not verdict.get("same")
                                                and verdict.get("confidence") == "high"):
                why = decision.reason + (f"; Haiku: {verdict.get('reason')}" if verdict else "")
                question = (decision.product, decision.score, why)
        inv.upsert_listing(ad_id=ad_id, product_id=product_id, link_method=method, seen_at=seen_at,
                           account_id=account_id, title=ct.title, price_eur=price_eur, url=url,
                           state=ct.state, link_score=decision.score or None)
        qid = None
        if question is not None:
            qid = inv.add_link_question(ad_id=ad_id, product_id=product_id, suggested_product_id=question[0],
                                        score=question[1], reason=question[2])
    logger.info("склад: объявление %s → товар %s (%s) %s", ad_id, product_id, method, reason)
    return {"ad_id": ad_id, "product_id": product_id, "method": method, "question_id": qid, "reason": reason}


def answer_question(question_id: int, same: bool) -> dict[str, Any]:
    """Ответ оператора на «это тот же товар?».

    same=True — объявление переезжает к предложенному товару; если у прежнего товара не осталось
    ни объявлений, ни движений — он сливается (архивируется). same=False — вопрос закрыт.
    """
    q = inv.get_question(question_id)
    if q is None:
        raise ValueError(f"вопрос {question_id} не найден")
    if q["resolved_at"]:
        raise ValueError(f"вопрос {question_id} уже закрыт")
    if not same:
        inv.close_question(question_id, "different")
        return {"ad_id": q["ad_id"], "product_id": q["product_id"], "merged": False}
    old, new = q["product_id"], q["suggested_product_id"]
    with transaction() as conn:
        inv.relink_listing(q["ad_id"], new, "operator")
        inv.close_question(question_id, "same", conn=conn)
        left = conn.execute(
            "SELECT (SELECT COUNT(*) FROM inv_listings WHERE product_id = ?) + "
            "(SELECT COUNT(*) FROM inv_movements WHERE product_id = ?) AS n", (old, old)).fetchone()["n"]
    merged = False
    if left == 0 and old != new:
        inv.merge_products(old, new)
        merged = True
    return {"ad_id": q["ad_id"], "product_id": new, "merged": merged}


# ─── синхронизация с messages ─────────────────────────────────────────────────

def _ad_rows(since: Optional[str] = None) -> list[sqlite3.Row]:
    where = "WHERE ad_id IS NOT NULL AND ad_id != ''"
    args: list[Any] = []
    if since:
        where += " AND created_at >= ?"
        args.append(since)
    return inv.iter_rows(f"""
        SELECT ad_id, ad_title, ad_price, ad_url, account_id, created_at,
               (ad_description IS NOT NULL AND ad_description != '') AS has_desc
          FROM messages {where}
         ORDER BY created_at, id
    """, args)


@dataclass
class AdInfo:
    ad_id: str
    title: str = ""
    quality: int = 0
    state: str = "active"
    price: Optional[float] = None
    account_id: Optional[int] = None
    url: Optional[str] = None
    first_seen: str = ""
    last_seen: str = ""
    raw_titles: list = field(default_factory=list)


def collect_ads(rows: Iterable[Any]) -> list[AdInfo]:
    """Свести строки messages в объявления: лучший заголовок (страница > тема запроса > промо),
    последнее состояние и цена. Объявления только с мусорными заголовками не возвращаются.
    Порядок — по первому появлению."""
    ads: dict[str, AdInfo] = {}
    for r in rows:
        ad_id = str(r["ad_id"])
        a = ads.get(ad_id)
        if a is None:
            a = ads[ad_id] = AdInfo(ad_id, first_seen=r["created_at"] or "")
        a.last_seen = max(a.last_seen, r["created_at"] or "")
        price = parse_price(r["ad_price"])
        ct = clean_ad_title(r["ad_title"], has_ad_data=price is not None or bool(r["has_desc"]))
        if r["ad_title"] and r["ad_title"] not in a.raw_titles:
            a.raw_titles.append(r["ad_title"])
        if ct.quality and ct.quality >= a.quality:   # строки идут по времени — берём свежий
            a.title, a.quality = ct.title, ct.quality
        if ct.quality >= 2:
            a.state = ct.state
        if price is not None:
            a.price = price
        a.account_id = r["account_id"] or a.account_id
        a.url = a.url or r["ad_url"]
    return [a for a in ads.values() if a.title]


def sync_listings(*, since: Optional[str] = None, use_llm: bool = False,
                  judge: Optional[Callable[[dict, dict], dict]] = None) -> dict[str, int]:
    """Завести на складе новые объявления из messages и обновить известные (заголовок, состояние,
    last_seen). Идемпотентно — можно гонять джобом. since — только строки новее (ISO)."""
    inv.ensure_schema()
    stats = {"known": 0, "normalized": 0, "llm": 0, "new": 0, "questions": 0, "skipped": 0}
    for a in collect_ads(_ad_rows(since)):
        res = link_listing(a.ad_id, title=a.title, price=a.price, account_id=a.account_id, url=a.url,
                           seen_at=a.last_seen, has_ad_data=True, state=a.state,
                           use_llm=use_llm, judge=judge)
        # first_seen для только что заведённого объявления — по первому письму, а не по последнему
        if res["product_id"] is not None and a.first_seen:
            with get_conn() as conn:
                conn.execute("UPDATE inv_listings SET first_seen_at = MIN(first_seen_at, ?) WHERE ad_id = ?",
                             (a.first_seen, a.ad_id))
        stats[res["method"]] = stats.get(res["method"], 0) + 1
        if res.get("question_id"):
            stats["questions"] += 1
    return stats


def _sold_threads() -> list[dict[str, Any]]:
    from modules.db_sales import list_sales
    return list_sales()


def record_sale(*, price_eur: Optional[float], gmail_thread_id: Optional[str] = None,
                ad_id: Optional[str] = None, product_id: Optional[int] = None, qty: int = 1,
                occurred_at: Optional[str] = None, message_id: Optional[int] = None,
                source: str = "thread", ad_title: Optional[str] = None,
                created_by: Optional[str] = None, log_missing: bool = True) -> dict[str, Any]:
    """Продажа = −qty к остатку товара. Товар ищется: product_id → объявление ad_id →
    нормализованный заголовок ad_title. Повторный вызов для того же треда не списывает второй раз.

    source: thread / detected / manual — продажа из messages (sync_sales снимет её, если тред
    перестанет быть проданным); operator — внесена на складе вручную (не снимается).
    Возвращает {movement_id, product_id, created, qty_after, stock_tracked}; product_id=None —
    товар не найден (продажа не записана, нужно привязать вручную).
    """
    inv.ensure_schema()
    if qty <= 0:
        raise ValueError("qty продажи должно быть > 0")
    if gmail_thread_id:
        prev = inv.sale_for_thread(gmail_thread_id)
        if prev is not None:
            return {"movement_id": prev["id"], "product_id": prev["product_id"], "created": False,
                    **_stock_state(prev["product_id"])}
    pid = product_id or resolve_product(ad_id=ad_id, ad_title=ad_title)
    if pid is None:
        if log_missing:
            logger.warning("склад: продажа (тред %s, объявление %s, «%s») — товар не найден",
                           gmail_thread_id, ad_id, ad_title)
        return {"movement_id": None, "product_id": None, "created": False, "qty_after": None,
                "stock_tracked": False}
    try:
        mid = inv.add_movement(product_id=pid, kind="sale", qty=-int(qty), unit_price_eur=price_eur,
                               occurred_at=occurred_at, source=source, gmail_thread_id=gmail_thread_id,
                               message_id=message_id, ad_id=ad_id, created_by=created_by)
    except sqlite3.IntegrityError:
        # Параллельная запись той же продажи (уникальный индекс по треду)
        prev = inv.sale_for_thread(gmail_thread_id or "")
        if prev is None:
            raise
        return {"movement_id": prev["id"], "product_id": prev["product_id"], "created": False,
                **_stock_state(prev["product_id"])}
    state = _stock_state(pid)
    if state["stock_tracked"] and state["qty_after"] < 0:
        logger.warning("склад: остаток товара %s ушёл в минус (%s) — не внесён приход?", pid, state["qty_after"])
    return {"movement_id": mid, "product_id": pid, "created": True, **state}


def _stock_state(product_id: int) -> dict[str, Any]:
    p = inv.get_product(product_id)
    return {"qty_after": inv.stock_qty(product_id), "stock_tracked": bool(p and p["stock_tracked"])}


def resolve_product(*, ad_id: Optional[str] = None, ad_title: Optional[str] = None) -> Optional[int]:
    """Товар по объявлению; если объявления нет на складе — по точному нормализованному заголовку."""
    if ad_id:
        l = inv.get_listing(str(ad_id))
        if l is not None:
            return _follow_merge(l["product_id"])
    if ad_title:
        ct = clean_ad_title(ad_title, has_ad_data=True)
        if ct.title:
            d = decide(ct.title, None, _db_index())
            if d.kind == "exact":
                return d.product
    return None


def _follow_merge(product_id: int) -> int:
    seen = set()
    p = inv.get_product(product_id)
    while p is not None and p["merged_into"] and p["id"] not in seen:
        seen.add(p["id"])
        p = inv.get_product(p["merged_into"])
    return p["id"] if p is not None else product_id


def sync_sales() -> dict[str, Any]:
    """Перенести продажи из messages (status='skipped_sold' с ценой — «Продано» в MA, найденные,
    ручные) в движения склада; отменённые продажи (тред больше не skipped_sold) — void.
    Идемпотентно. Возвращает счётчики и список продаж, для которых товар не найден."""
    inv.ensure_schema()
    sold = _sold_threads()
    detected = {r["gmail_thread_id"] for r in inv.iter_rows(
        "SELECT gmail_thread_id FROM detected_sales WHERE applied_at IS NOT NULL")}
    have = inv.thread_ids_with_sale()
    stats: dict[str, Any] = {"recorded": 0, "already": 0, "voided": 0, "unmatched": []}
    sold_ids = set()
    for s in sold:
        tid = s["thread_id"]
        if not tid:
            continue
        sold_ids.add(tid)
        if tid in have:
            stats["already"] += 1
            continue
        source = "manual" if tid.startswith("manual-") else "detected" if tid in detected else "thread"
        res = record_sale(price_eur=s["sold_price_eur"], gmail_thread_id=tid, ad_id=s["ad_id"],
                          ad_title=s["ad_title"], occurred_at=s["sold_at"], source=source,
                          log_missing=False)
        if res["product_id"] is None:
            stats["unmatched"].append({"thread_id": tid, "ad_id": s["ad_id"], "ad_title": s["ad_title"],
                                       "price": s["sold_price_eur"], "sold_at": s["sold_at"]})
        elif res["created"]:
            stats["recorded"] += 1
    # Продажу откатили (тред снова открыт / статус сменили) — движение больше не считаем.
    # Только для источников, которые сами пришли из messages; source='operator' (продажа,
    # внесённая на складе руками) не трогаем.
    for r in inv.iter_rows("SELECT id, gmail_thread_id FROM inv_movements WHERE kind = 'sale' "
                           "AND voided_at IS NULL AND source IN ('thread', 'detected', 'manual') "
                           "AND gmail_thread_id IS NOT NULL"):
        if r["gmail_thread_id"] not in sold_ids:
            if inv.void_movement(r["id"], "тред больше не помечен проданным"):
                stats["voided"] += 1
                logger.info("склад: продажа треда %s отменена — движение %s void", r["gmail_thread_id"], r["id"])
    return stats


_last_unmatched: set[str] = set()


def sync_all(*, use_llm: bool = False) -> dict[str, Any]:
    """Джоб склада: объявления + продажи. Для scheduler (раз в 30 мин).
    Продажи без товара логируются WARNING только когда их список изменился (не каждые 30 мин)."""
    global _last_unmatched
    listings = sync_listings(use_llm=use_llm)
    sales = sync_sales()
    ids = {str(u["thread_id"]) for u in sales["unmatched"]}
    if ids - _last_unmatched:
        logger.warning("склад: %d продаж без товара (привязать на складе): %s", len(ids),
                       ", ".join(sorted(ids)[:5]))
    _last_unmatched = ids
    return {"listings": listings, "sales": {**sales, "unmatched": len(sales["unmatched"])}}


# ─── операции оператора ───────────────────────────────────────────────────────

def receive(product_id: int, qty: int, unit_cost_eur: Optional[float] = None, *,
            occurred_at: Optional[str] = None, note: Optional[str] = None,
            created_by: Optional[str] = None) -> int:
    """Приход: купили qty шт. по unit_cost_eur за штуку."""
    return inv.add_movement(product_id=product_id, kind="receipt", qty=qty, unit_cost_eur=unit_cost_eur,
                            occurred_at=occurred_at, note=note, created_by=created_by)


def write_off(product_id: int, qty: int, *, note: Optional[str] = None,
              created_by: Optional[str] = None) -> int:
    """Списание (брак, отдали, потеряли): −qty без выручки."""
    return inv.add_movement(product_id=product_id, kind="writeoff", qty=-abs(int(qty)), note=note,
                            created_by=created_by)


def set_stock(product_id: int, qty: int, *, note: Optional[str] = None,
              created_by: Optional[str] = None) -> Optional[int]:
    """Инвентаризация: «сейчас на складе qty шт.» — корректировка на разницу, учёт включается.
    Возвращает id движения или None, если остаток уже такой и учёт уже включён."""
    if qty < 0:
        raise ValueError("остаток не может быть отрицательным")
    with transaction() as conn:
        cur = conn.execute("SELECT COALESCE(SUM(qty), 0) AS q FROM inv_movements "
                           "WHERE product_id = ? AND voided_at IS NULL", (int(product_id),)).fetchone()["q"]
        delta = int(qty) - int(cur)
        if delta == 0:
            p = conn.execute("SELECT stock_tracked FROM inv_products WHERE id = ?", (int(product_id),)).fetchone()
            if p is None:
                raise ValueError(f"товар {product_id} не найден")
            if not p["stock_tracked"]:
                conn.execute("UPDATE inv_products SET stock_tracked = 1, updated_at = ? WHERE id = ?",
                             (datetime.utcnow().isoformat(), int(product_id)))
            return None
        return inv.add_movement(product_id=product_id, kind="adjust", qty=delta,
                                note=note or f"инвентаризация: {qty} шт.", created_by=created_by, conn=conn)


# ─── правила для других модулей ───────────────────────────────────────────────

def ad_context(ad_id: Optional[str], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Что склад знает о товаре объявления.

    stock: in_stock (учёт ведётся, остаток > 0) / out_of_stock (учёт ведётся, остаток ≤ 0) /
    unknown (объявление не на складе или остаток не внесён — НЕ значит «нет в наличии»).
    has_other_active_listing — у товара есть другое объявление, которое видели недавно и без
    «Gelöscht» (перепостили).
    """
    out: dict[str, Any] = {"known": False, "product_id": None, "product_title": None, "stock": "unknown",
                           "qty": None, "list_price_eur": None, "min_price_eur": None,
                           "has_other_active_listing": False}
    if not ad_id:
        return out
    inv.ensure_schema()
    l = inv.get_listing(str(ad_id))
    if l is None:
        return out
    pid = _follow_merge(l["product_id"])
    p = inv.get_product(pid)
    if p is None:
        return out
    qty = inv.stock_qty(pid)
    cutoff = ((now or datetime.utcnow()) - timedelta(days=ACTIVE_LISTING_DAYS)).isoformat()
    other = inv.iter_rows("SELECT 1 FROM inv_listings WHERE product_id = ? AND ad_id != ? AND state = 'active' "
                          "AND last_seen_at >= ? LIMIT 1", (pid, str(ad_id), cutoff))
    out.update(known=True, product_id=pid, product_title=p["title"],
               stock=("in_stock" if qty > 0 else "out_of_stock") if p["stock_tracked"] else "unknown",
               qty=qty if p["stock_tracked"] else None, list_price_eur=p["list_price_eur"],
               min_price_eur=p["min_price_eur"], has_other_active_listing=bool(other))
    return out


def ad_context_safe(ad_id: Optional[str]) -> dict[str, Any]:
    """ad_context без исключений: при ошибке БД — «неизвестно» (безопасно: «продано» не скажем)
    и ERROR в лог (его подхватит monitor_errors)."""
    try:
        return ad_context(ad_id)
    except Exception:  # noqa: BLE001
        logger.exception("склад: ad_context(%s) упал — считаю остаток неизвестным", ad_id)
        return ad_context(None)


def effective_ad_state(ad_id: Optional[str], raw_state: str) -> str:
    """Состояние объявления для промпта LLM с учётом склада.

    raw_state — что видно по самому объявлению: '' / 'deleted' (Gelöscht) / 'reserved' /
    'sold' (старая отметка ad_briefs.sold_at). Результат:
      'out_of_stock' — учёт ведётся и остаток 0: не обещать наличие;
      ''             — товар есть (остаток > 0) или его перепостили: Gelöscht/sold не упоминать;
      raw_state      — склад ничего не знает: прежнее поведение, кроме 'sold' (отметка объявления,
                       а не товара) — она превращается в 'deleted' без слова «продано».
    """
    ctx = ad_context_safe(ad_id)
    if ctx["stock"] == "out_of_stock":
        return "out_of_stock"
    if ctx["stock"] == "in_stock":
        return ""
    if raw_state in ("deleted", "sold") and ctx["has_other_active_listing"]:
        return ""
    if raw_state == "sold":
        return "deleted"
    return raw_state or ""


def prompt_note(ad_id: Optional[str]) -> str:
    """Строка о наличии для системного промпта LLM ('' — сказать нечего)."""
    ctx = ad_context_safe(ad_id)
    if ctx["stock"] == "in_stock":
        return (f"Склад: товар в наличии ({ctx['qty']} шт.). Не говори, что он продан или его нет, "
                f"даже если объявление удалено или зарезервировано.")
    if ctx["stock"] == "out_of_stock":
        return ("Склад: по учёту товара сейчас нет (остаток 0). Не обещай наличие и сроки — "
                "скажи, что уточнишь, и не подтверждай продажу.")
    if ctx["has_other_active_listing"]:
        return "Склад: товар есть в другом актуальном объявлении продавца — не говори, что он продан."
    return ""


# ─── отчёт ────────────────────────────────────────────────────────────────────

def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=None) if d.tzinfo else d


def _fifo(movements: list[Any], default_cost: Optional[float]) -> tuple[list[list], list[dict]]:
    """Прогнать движения товара по FIFO. Возвращает (остаток партиями [дата, шт, цена_ед],
    продажи [{movement, occurred, qty, revenue, cogs, days}]). cogs/days = None, если не известны
    (нет себестоимости или продали больше, чем было внесено)."""
    lots: deque[list] = deque()
    sales: list[dict] = []
    for m in movements:
        when = _dt(m["occurred_at"])
        q = int(m["qty"])
        if q > 0:
            cost = m["unit_cost_eur"] if m["kind"] == "receipt" and m["unit_cost_eur"] is not None else default_cost
            lots.append([when, q, cost])
            continue
        need = -q
        cogs: Optional[float] = 0.0
        days_sum, days_n = 0.0, 0
        while need and lots:
            lot = lots[0]
            take = min(need, lot[1])
            if cogs is not None:
                cogs = None if lot[2] is None else cogs + take * lot[2]
            if when and lot[0]:
                days_sum += take * max(0.0, (when - lot[0]).total_seconds() / 86400)
                days_n += take
            lot[1] -= take
            need -= take
            if lot[1] == 0:
                lots.popleft()
        if need:   # продали больше, чем внесено приходом — себестоимость по оценке товара
            cogs = None if (cogs is None or default_cost is None) else cogs + need * default_cost
        if m["kind"] == "sale":
            price = m["unit_price_eur"]
            sales.append({
                "movement_id": m["id"], "occurred_at": m["occurred_at"], "qty": -q,
                "revenue": None if price is None else price * -q,
                "cogs": cogs, "days": (days_sum / days_n) if days_n and not need else None,
                "gmail_thread_id": m["gmail_thread_id"],
            })
    return list(lots), sales


def report(period_from: Optional[str] = None, period_to: Optional[str] = None, *,
           now: Optional[datetime] = None, stale_days: int = STALE_DAYS) -> dict[str, Any]:
    """Отчёт склада.

    По товару: остаток (None — не внесён), себестоимость остатка (FIFO по приходам, иначе оценка
    unit_cost_eur), ожидаемая выручка и прибыль по цене объявления и по минимальной, продажи за
    период (шт., выручка, себестоимость, прибыль), средний срок до продажи, возраст старейшей
    единицы / сколько висит объявление. Итоги — в totals. Неизвестные суммы — None, а не 0,
    с подсчётом «без себестоимости», чтобы не рисовать ложную прибыль.
    """
    inv.ensure_schema()
    now = now or datetime.utcnow()
    pf, pt = _dt(period_from), _dt(period_to)
    products = inv.list_products(include_archived=True)
    moves: dict[int, list] = {}
    for m in inv.list_movements():
        moves.setdefault(m["product_id"], []).append(m)

    items: list[dict[str, Any]] = []
    T: dict[str, Any] = {"products": 0, "tracked": 0, "qty": 0, "stock_cost": 0.0, "units_without_cost": 0,
                         "expected_revenue_list": 0.0, "expected_revenue_min": 0.0,
                         "units_without_list_price": 0, "units_without_min_price": 0,
                         "sales": 0, "sold_qty": 0, "revenue": 0.0, "cogs": 0.0, "profit": 0.0,
                         "sales_without_cost": 0, "sales_without_price": 0, "negative_stock": 0,
                         "stale": 0, "untracked_listed": 0, "open_questions": len(inv.list_open_questions())}
    days_all: list[tuple[float, int]] = []

    for p in products:
        mv = moves.get(p["id"], [])
        if p["archived_at"] and not mv:
            continue
        lots, sales = _fifo(mv, p["unit_cost_eur"])
        tracked = bool(p["stock_tracked"])
        qty = int(p["qty"]) if tracked else None
        on_hand = max(0, qty or 0)
        stock_cost: Optional[float] = None
        no_cost = 0
        oldest_days: Optional[float] = None
        if tracked and on_hand:
            lot_units = sum(l[1] for l in lots)
            stock_cost = sum(l[1] * l[2] for l in lots if l[2] is not None)
            no_cost = sum(l[1] for l in lots if l[2] is None) + max(0, on_hand - lot_units)
            ages = [(now - l[0]).total_seconds() / 86400 for l in lots if l[0]]
            oldest_days = round(max(ages), 1) if ages else None
        listed = _dt(p["first_listed_at"])
        listed_days = round((now - listed).total_seconds() / 86400, 1) if listed else None

        exp_list = on_hand * p["list_price_eur"] if on_hand and p["list_price_eur"] else None
        exp_min = on_hand * p["min_price_eur"] if on_hand and p["min_price_eur"] else None
        full_cost = stock_cost if (on_hand and no_cost == 0) else None

        in_period = [s for s in sales if (not pf or (_dt(s["occurred_at"]) or now) >= pf)
                     and (not pt or (_dt(s["occurred_at"]) or now) < pt)]
        p_rev = sum(s["revenue"] for s in in_period if s["revenue"] is not None)
        p_cogs = sum(s["cogs"] for s in in_period if s["cogs"] is not None)
        p_profit = sum(s["revenue"] - s["cogs"] for s in in_period
                       if s["revenue"] is not None and s["cogs"] is not None)
        with_cost = [s for s in in_period if s["revenue"] is not None and s["cogs"] is not None]
        known_days = [(s["days"], s["qty"]) for s in in_period if s["days"] is not None]
        days_all.extend(known_days)

        item = {
            "product_id": p["id"], "title": p["title"], "category": p["category"], "condition": p["condition"],
            "archived": bool(p["archived_at"]), "stock_tracked": tracked, "qty": qty,
            "unit_cost_eur": p["unit_cost_eur"], "list_price_eur": p["list_price_eur"],
            "min_price_eur": p["min_price_eur"], "listings": p["listings"],
            "stock_cost_eur": _r(stock_cost) if on_hand else None, "units_without_cost": no_cost,
            "expected_revenue_list": _r(exp_list), "expected_revenue_min": _r(exp_min),
            "expected_profit_list": _r(exp_list - full_cost) if exp_list is not None and full_cost is not None else None,
            "expected_profit_min": _r(exp_min - full_cost) if exp_min is not None and full_cost is not None else None,
            "sold_qty": sum(s["qty"] for s in in_period), "revenue": _r(p_rev), "cogs": _r(p_cogs),
            # Прибыль только по продажам с известной себестоимостью; ни одной — None, а не 0
            "profit": _r(p_profit) if with_cost else None,
            "sales_without_cost": sum(1 for s in in_period if s["cogs"] is None),
            "avg_days_to_sell": _r(_wavg(known_days)), "oldest_unit_days": oldest_days,
            "listed_days": listed_days,
            "stale": bool(tracked and on_hand and oldest_days is not None and oldest_days > stale_days),
            "negative_stock": bool(tracked and qty is not None and qty < 0),
        }
        items.append(item)

        T["products"] += 0 if p["archived_at"] else 1
        T["tracked"] += 1 if tracked else 0
        T["qty"] += on_hand
        T["stock_cost"] += stock_cost or 0.0
        T["units_without_cost"] += no_cost
        T["expected_revenue_list"] += exp_list or 0.0
        T["expected_revenue_min"] += exp_min or 0.0
        T["units_without_list_price"] += on_hand if not p["list_price_eur"] else 0
        T["units_without_min_price"] += on_hand if not p["min_price_eur"] else 0
        T["sales"] += len(in_period)
        T["sold_qty"] += item["sold_qty"]
        T["revenue"] += p_rev
        T["cogs"] += p_cogs
        T["profit"] += p_profit
        T["sales_without_cost"] += item["sales_without_cost"]
        T["sales_without_price"] += sum(1 for s in in_period if s["revenue"] is None)
        T["negative_stock"] += 1 if item["negative_stock"] else 0
        T["stale"] += 1 if item["stale"] else 0
        T["untracked_listed"] += 1 if (not tracked and not p["archived_at"]) else 0

    for k in ("stock_cost", "expected_revenue_list", "expected_revenue_min", "revenue", "cogs", "profit"):
        T[k] = _r(T[k])
    if T["sales"] and T["sales_without_cost"] == T["sales"]:
        T["profit"] = None
    if not T["tracked"]:
        # Остатки ещё не внесены — денежные суммы склада неизвестны, а не нулевые
        T["stock_cost"] = T["expected_revenue_list"] = T["expected_revenue_min"] = None
    T["unlinked_sales"] = len(unlinked_sales())
    # Ожидаемая прибыль по всему складу — только если себестоимость известна у всех единиц
    T["expected_profit_list"] = _r(T["expected_revenue_list"] - T["stock_cost"]) \
        if T["tracked"] and T["units_without_cost"] == 0 and T["units_without_list_price"] == 0 else None
    T["expected_profit_min"] = _r(T["expected_revenue_min"] - T["stock_cost"]) \
        if T["tracked"] and T["units_without_cost"] == 0 and T["units_without_min_price"] == 0 else None
    T["avg_days_to_sell"] = _r(_wavg(days_all))
    items.sort(key=lambda i: (i["archived"], not i["stale"], -(i["qty"] or 0), i["title"]))
    return {"period_from": period_from, "period_to": period_to, "generated_at": now.isoformat(),
            "totals": T, "products": items}


def _wavg(pairs: list[tuple[float, int]]) -> Optional[float]:
    n = sum(q for _, q in pairs)
    return sum(d * q for d, q in pairs) / n if n else None


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _eur(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:,.0f} €".replace(",", " ")


def unlinked_sales() -> list[dict[str, Any]]:
    """Продажи из messages, которые не удалось списать ни с одного товара (без записи в БД)."""
    have = inv.thread_ids_with_sale()
    return [s for s in _sold_threads() if s["thread_id"] and s["thread_id"] not in have]


def summary_text(rep: Optional[dict[str, Any]] = None) -> str:
    """Короткая сводка склада для ежедневной сводки / уведомления (plain text, RU)."""
    rep = rep or report()
    t = rep["totals"]
    if t["tracked"]:
        lines = [f"Склад: {t['qty']} шт. в учёте ({t['tracked']} из {t['products']} товаров), "
                 f"себестоимость {_eur(t['stock_cost'])}"
                 + (f" (+{t['units_without_cost']} шт. без цены закупки)" if t["units_without_cost"] else "")]
        lines.append(f"Ожидаемая выручка: {_eur(t['expected_revenue_list'])} по объявлениям, "
                     f"{_eur(t['expected_revenue_min'])} по минимуму")
    else:
        lines = [f"Склад: остатки не внесены ({t['products']} товаров из объявлений)"]
    if t["sold_qty"]:
        if t["profit"] is None:
            prof = "неизвестна (нет себестоимости)"
        else:
            prof = _eur(t["profit"]) + (f" (без {t['sales_without_cost']} продаж без себестоимости)"
                                        if t["sales_without_cost"] else "")
        lines.append(f"Продано: {t['sold_qty']} шт. на {_eur(t['revenue'])}, прибыль {prof}")
    if t["stale"]:
        lines.append(f"Залежалось > {STALE_DAYS} дн.: {t['stale']} товаров")
    if t["negative_stock"]:
        lines.append(f"Остаток в минусе: {t['negative_stock']} товаров — внести приход")
    if t["open_questions"]:
        lines.append(f"Вопросов «тот же товар?»: {t['open_questions']}")
    if t["unlinked_sales"]:
        lines.append(f"Продаж без товара: {t['unlinked_sales']} — привязать на складе")
    return "\n".join(lines)


# ─── начальное заполнение по истории ──────────────────────────────────────────

@dataclass
class SeedGroup:
    gid: int
    ads: list = field(default_factory=list)   # AdInfo, по времени первого появления

    @property
    def latest(self) -> "AdInfo":
        return max(self.ads, key=lambda a: a.last_seen)

    @property
    def title(self) -> str:
        return self.latest.title


def plan_seed(ads: list[AdInfo], *, judge: Optional[Callable[[dict, dict], dict]] = None) -> dict[str, Any]:
    """Сгруппировать объявления в товары без записи в БД (для dry-run и apply_seed).

    Тот же алгоритм, что у живой автопривязки: точное нормализованное совпадение — в группу;
    похожее — Haiku (если передан judge) при уверенном «да», иначе отдельная группа + сомнение
    для оператора. Возвращает {groups: [SeedGroup], doubtful: [{ad, gid, suggested_gid, score, reason}]}.
    """
    groups: list[SeedGroup] = []
    index: list[IndexEntry] = []
    doubtful: list[dict[str, Any]] = []
    for a in sorted(ads, key=lambda x: (x.first_seen, x.ad_id)):
        d = decide(a.title, a.price, index)
        target: Optional[SeedGroup] = None
        doubt: Optional[dict[str, Any]] = None
        if d.kind == "exact":
            target = d.product
        elif d.kind == "doubt":
            verdict = None
            if judge is not None:
                c = d.product.latest
                try:
                    verdict = judge({"title": a.title, "price": a.price, "description": ""},
                                    {"title": c.title, "price": c.price, "description": ""})
                except Exception:  # noqa: BLE001 — без Haiku решит оператор
                    logger.warning("склад/seed: Haiku не ответил по %s", a.ad_id, exc_info=True)
            if verdict and verdict.get("same") and verdict.get("confidence") == "high":
                target = d.product
            elif not (verdict and not verdict.get("same") and verdict.get("confidence") == "high"):
                doubt = {"suggested": d.product, "score": d.score,
                         "reason": d.reason + (f"; Haiku: {verdict.get('reason')}" if verdict else "")}
        if target is None:
            target = SeedGroup(len(groups) + 1)
            groups.append(target)
        target.ads.append(a)
        index.append(IndexEntry(target, a.title, a.price, match_key(a.title), detect_category(a.title)))
        if doubt:
            doubtful.append({"ad": a, "gid": target.gid, "suggested_gid": doubt["suggested"].gid,
                             "score": doubt["score"], "reason": doubt["reason"]})
    return {"groups": groups, "doubtful": doubtful}


def apply_seed(plan: dict[str, Any]) -> dict[str, int]:
    """Записать план в БД одной транзакцией. Объявления, уже заведённые на складе, пропускаются
    (повторный запуск безопасен). Сомнительные пары → вопросы оператору."""
    inv.ensure_schema()
    known = inv.known_ad_ids()
    gid2pid: dict[int, int] = {}
    stats = {"products": 0, "listings": 0, "questions": 0, "skipped_known": 0}
    with transaction():
        for g in plan["groups"]:
            fresh = [a for a in g.ads if a.ad_id not in known]
            stats["skipped_known"] += len(g.ads) - len(fresh)
            if not fresh:
                continue
            lt = g.latest
            pid = inv.create_product(title=lt.title, category=detect_category(lt.title),
                                     condition=detect_condition(lt.title), list_price_eur=lt.price,
                                     match_key=match_key(lt.title))
            gid2pid[g.gid] = pid
            stats["products"] += 1
            for a in fresh:
                inv.upsert_listing(ad_id=a.ad_id, product_id=pid, link_method="seed", seen_at=a.last_seen,
                                   account_id=a.account_id, title=a.title, price_eur=a.price, url=a.url,
                                   state=a.state)
                with get_conn() as conn:
                    conn.execute("UPDATE inv_listings SET first_seen_at = ? WHERE ad_id = ?",
                                 (a.first_seen or a.last_seen, a.ad_id))
                stats["listings"] += 1
        for d in plan["doubtful"]:
            pid, spid = gid2pid.get(d["gid"]), gid2pid.get(d["suggested_gid"])
            if pid and spid and pid != spid:
                if inv.add_link_question(ad_id=d["ad"].ad_id, product_id=pid, suggested_product_id=spid,
                                         score=d["score"], reason=d["reason"]):
                    stats["questions"] += 1
    return stats


def seed_ads_from_messages() -> list[AdInfo]:
    """Объявления из всей истории messages (для scripts/inventory_seed.py)."""
    return collect_ads(_ad_rows())
