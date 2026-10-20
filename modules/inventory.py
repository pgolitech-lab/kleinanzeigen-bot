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

    llm_error = decision.kind == "doubt" and use_llm and verdict is None

    question: Optional[tuple[int, float, str]] = None  # (suggested_product, score, reason)
    with transaction() as conn:
        # Пока думал Haiku, объявление мог завести параллельный вызов
        again = conn.execute("SELECT product_id FROM inv_listings WHERE ad_id = ?", (ad_id,)).fetchone()
        if again is not None:
            return {"ad_id": ad_id, "product_id": again["product_id"], "method": "known", "reason": ""}
        if decision.kind != "exact":
            # Параллельный вызов мог только что завести товар с тем же заголовком — перепроверка
            # под блокировкой записи, чтобы не наплодить дублей
            fresh = decide(ct.title, price_eur, _db_index())
            if fresh.kind == "exact":
                decision = fresh
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
        if method != "new" and price_eur is not None:
            # Перепост с новой ценой — цена товара = цена самого свежего объявления
            newest = conn.execute("SELECT ad_id FROM inv_listings WHERE product_id = ? "
                                  "ORDER BY last_seen_at DESC LIMIT 1", (product_id,)).fetchone()
            if newest is not None and newest["ad_id"] == ad_id:
                inv.update_product(product_id, list_price_eur=price_eur)
        qid = None
        if question is not None:
            qid = inv.add_link_question(ad_id=ad_id, product_id=product_id, suggested_product_id=question[0],
                                        score=question[1], reason=question[2])
    logger.info("склад: объявление %s → товар %s (%s) %s", ad_id, product_id, method, reason)
    return {"ad_id": ad_id, "product_id": product_id, "method": method, "question_id": qid, "reason": reason,
            "llm_error": llm_error}


def answer_question(question_id: int, same: bool) -> dict[str, Any]:
    """Ответ оператора на «это тот же товар?».

    same=True — объявление и его движения (продажи по этому ad_id) переезжают к предложенному
    товару; если у прежнего товара не осталось ни объявлений, ни движений — он сливается
    (архивируется). Всё одной транзакцией. same=False — вопрос закрыт.
    """
    with transaction() as conn:
        q = conn.execute("SELECT * FROM inv_link_questions WHERE id = ?", (int(question_id),)).fetchone()
        if q is None:
            raise ValueError(f"вопрос {question_id} не найден")
        if q["resolved_at"]:
            raise ValueError(f"вопрос {question_id} уже закрыт")
        if not same:
            inv.close_question(question_id, "different", conn=conn)
            return {"ad_id": q["ad_id"], "product_id": q["product_id"], "merged": False}
        cur = conn.execute("SELECT product_id FROM inv_listings WHERE ad_id = ?", (q["ad_id"],)).fetchone()
        if cur is None:
            raise ValueError(f"объявление {q['ad_id']} не найдено на складе")
        old = cur["product_id"]
        new = _follow_merge(q["suggested_product_id"])
        target = conn.execute("SELECT archived_at FROM inv_products WHERE id = ?", (new,)).fetchone()
        if target is None or target["archived_at"]:
            raise ValueError(f"товар {new} в архиве — сначала верните его из архива")
        inv.relink_listing(q["ad_id"], new, "operator")
        conn.execute("UPDATE inv_movements SET product_id = ? WHERE product_id = ? AND ad_id = ?",
                     (new, old, q["ad_id"]))
        if not inv.close_question(question_id, "same", conn=conn):
            raise ValueError(f"вопрос {question_id} уже закрыт")
        left = conn.execute(
            "SELECT (SELECT COUNT(*) FROM inv_listings WHERE product_id = ?) + "
            "(SELECT COUNT(*) FROM inv_movements WHERE product_id = ?) AS n", (old, old)).fetchone()["n"]
        merged = False
        if left == 0 and old != new:
            inv.merge_products(old, new)   # вложенная транзакция = SAVEPOINT
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
    stats = {"known": 0, "normalized": 0, "llm": 0, "new": 0, "questions": 0, "skipped": 0,
             "llm_errors": 0, "errors": 0}
    for a in collect_ads(_ad_rows(since)):
        # Одно «отравленное» объявление не должно останавливать остальные
        try:
            res = link_listing(a.ad_id, title=a.title, price=a.price, account_id=a.account_id, url=a.url,
                               seen_at=a.last_seen, has_ad_data=True, state=a.state,
                               use_llm=use_llm, judge=judge)
            # first_seen для только что заведённого объявления — по первому письму, а не по последнему
            if res["product_id"] is not None and a.first_seen:
                with get_conn() as conn:
                    conn.execute("UPDATE inv_listings SET first_seen_at = MIN(first_seen_at, ?) WHERE ad_id = ?",
                                 (a.first_seen, a.ad_id))
        except Exception:  # noqa: BLE001
            logger.exception("склад: объявление %s («%s») не обработано", a.ad_id, a.title)
            stats["errors"] += 1
            continue
        stats[res["method"]] = stats.get(res["method"], 0) + 1
        if res.get("question_id"):
            stats["questions"] += 1
        if res.get("llm_error"):
            stats["llm_errors"] += 1
    return stats


def _sold_threads() -> list[dict[str, Any]]:
    from modules.db_sales import list_sales
    return list_sales()


def record_sale(*, price_eur: Optional[float], gmail_thread_id: Optional[str] = None,
                ad_id: Optional[str] = None, product_id: Optional[int] = None, qty: int = 1,
                occurred_at: Optional[str] = None, message_id: Optional[int] = None,
                source: str = "operator", ad_title: Optional[str] = None,
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


def _thread_still_sold(thread_id: str) -> bool:
    rows = inv.iter_rows("SELECT 1 FROM messages WHERE gmail_thread_id = ? AND status = 'skipped_sold' "
                         "AND sold_price_eur IS NOT NULL LIMIT 1", (thread_id,))
    return bool(rows)


def sync_sales() -> dict[str, Any]:
    """Перенести продажи из messages (status='skipped_sold' с ценой — «Продано» в MA, найденные,
    ручные) в движения склада; обновить цену, если оператор её поправил; отменённые продажи
    (тред больше не skipped_sold с ценой) — void. Идемпотентно.

    Треды skipped_sold вовсе без цены — это «закрыт, потому что продали другому» (mark_thread_sold
    с close_other_threads_for_ad) или старая отметка без цены: продажей не считаются, их число —
    в stats["unpriced"].
    """
    inv.ensure_schema()
    sold = _sold_threads()
    detected = {r["gmail_thread_id"] for r in inv.iter_rows(
        "SELECT gmail_thread_id FROM detected_sales WHERE applied_at IS NOT NULL")}
    stats: dict[str, Any] = {"recorded": 0, "already": 0, "repriced": 0, "voided": 0, "errors": 0,
                             "unmatched": [], "unpriced": 0}
    for s in sold:
        tid = s["thread_id"]
        if not tid:
            continue
        try:
            prev = inv.sale_for_thread(tid)
            if prev is not None:
                stats["already"] += 1
                price = s["sold_price_eur"]
                if price is not None and prev["unit_price_eur"] != price and prev["qty"] == -1:
                    inv.update_sale_price(prev["id"], float(price))
                    stats["repriced"] += 1
                    logger.info("склад: цена продажи треда %s исправлена %s → %s", tid, prev["unit_price_eur"], price)
                continue
            source = "manual" if tid.startswith("manual-") else "detected" if tid in detected else "thread"
            res = record_sale(price_eur=s["sold_price_eur"], gmail_thread_id=tid, ad_id=s["ad_id"],
                              ad_title=s["ad_title"], occurred_at=s["sold_at"], source=source,
                              log_missing=False)
        except Exception:  # noqa: BLE001
            logger.exception("склад: продажа треда %s не перенесена", tid)
            stats["errors"] += 1
            continue
        if res["product_id"] is None:
            stats["unmatched"].append({"thread_id": tid, "ad_id": s["ad_id"], "ad_title": s["ad_title"],
                                       "price": s["sold_price_eur"], "sold_at": s["sold_at"]})
        elif res["created"]:
            stats["recorded"] += 1
    # Продажу откатили (тред снова открыт / статус сменили) — движение больше не считаем.
    # Проверка по messages в момент отмены (а не по снимку выше): «Продано» могли отметить
    # прямо во время прохода. Только источники из messages; source='operator' не трогаем.
    for r in inv.iter_rows("SELECT id, gmail_thread_id FROM inv_movements WHERE kind = 'sale' "
                           "AND voided_at IS NULL AND source IN ('thread', 'detected', 'manual') "
                           "AND gmail_thread_id IS NOT NULL"):
        try:
            if not _thread_still_sold(r["gmail_thread_id"]) and \
                    inv.void_movement(r["id"], "тред больше не помечен проданным"):
                stats["voided"] += 1
                logger.warning("склад: продажа треда %s отменена (тред больше не «продано») — "
                               "остаток +1, движение %s", r["gmail_thread_id"], r["id"])
        except Exception:  # noqa: BLE001
            logger.exception("склад: отмена продажи треда %s не удалась", r["gmail_thread_id"])
            stats["errors"] += 1
    stats["unpriced"] = inv.iter_rows("""
        SELECT COUNT(*) AS n FROM (SELECT gmail_thread_id FROM messages WHERE status = 'skipped_sold'
                                    AND gmail_thread_id IS NOT NULL GROUP BY gmail_thread_id
                                   HAVING MAX(sold_price_eur) IS NULL)""")[0]["n"]
    return stats


_last_unmatched: set[str] = set()


def sync_all(*, use_llm: bool = False) -> dict[str, Any]:
    """Джоб склада: объявления + продажи. Для scheduler (раз в 30 мин). Каждая половина
    изолирована: падение объявлений не мешает продажам. Итог — INFO; ошибки — ERROR (их
    подхватит monitor_errors). Продажи без товара — WARNING, когда их список изменился."""
    global _last_unmatched
    out: dict[str, Any] = {}
    try:
        out["listings"] = sync_listings(use_llm=use_llm)
    except Exception:  # noqa: BLE001
        logger.exception("склад: sync_listings упал целиком")
        out["listings"] = {"errors": 1}
    try:
        sales = sync_sales()
    except Exception:  # noqa: BLE001
        logger.exception("склад: sync_sales упал целиком")
        sales = {"errors": 1, "unmatched": []}
    ids = {str(u["thread_id"]) for u in sales["unmatched"]}
    if ids - _last_unmatched:
        logger.warning("склад: %d продаж без товара (привязать на складе): %s", len(ids),
                       ", ".join(sorted(ids)[:5]))
    _last_unmatched = ids
    out["sales"] = {**sales, "unmatched": len(sales["unmatched"])}
    errors = out["listings"].get("errors", 0) + sales.get("errors", 0)
    llm_err = out["listings"].get("llm_errors", 0)
    logger.info("склад: sync %s", out)
    if errors:
        logger.error("склад: sync завершён с ошибками: %d (см. выше)", errors)
    if llm_err and llm_err == out["listings"].get("questions", 0):
        logger.error("склад: Haiku не ответил ни по одной сомнительной паре (%d) — вход в подписку?", llm_err)
    return out


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
    """Инвентаризация: «сейчас на складе qty шт.» — корректировка на разницу с остатком с начала
    учёта. У товара без учёта она же начинает учёт (продажи из истории до неё не считаются).
    Возвращает id движения или None, если остаток уже такой."""
    if qty < 0:
        raise ValueError("остаток не может быть отрицательным")
    # Оператор считает склад уже без только что проданной штуки — перенести свежие «Продано»
    # до корректировки, иначе джоб запишет их после неё и спишет второй раз
    try:
        sync_sales()
    except Exception:  # noqa: BLE001
        logger.exception("склад: sync_sales перед инвентаризацией товара %s упал", product_id)
    with transaction() as conn:
        p = conn.execute("SELECT stock_tracked FROM inv_products WHERE id = ?", (int(product_id),)).fetchone()
        if p is None:
            raise ValueError(f"товар {product_id} не найден")
        started = inv.tracked_since(product_id, conn=conn) is not None
        delta = int(qty) - (inv.stock_qty(product_id, conn=conn) if started else 0)
        if delta == 0 and started:
            return None
        # delta == 0 у товара без учёта («0 шт.») — всё равно движение: оно отмечает начало учёта
        return inv.add_movement(product_id=product_id, kind="adjust", qty=delta,
                                note=note or f"инвентаризация: {qty} шт.", created_by=created_by, conn=conn)


# ─── правила для других модулей ───────────────────────────────────────────────

def _empty_context() -> dict[str, Any]:
    return {"known": False, "product_id": None, "product_title": None, "stock": "unknown",
            "qty": None, "negative_stock": False, "list_price_eur": None, "min_price_eur": None,
            "has_other_active_listing": False, "error": False}


def ad_context(ad_id: Optional[str], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Что склад знает о товаре объявления.

    stock: in_stock (учёт ведётся, остаток > 0) / out_of_stock (учёт ведётся, остаток ровно 0) /
    unknown (объявление не на складе, остаток не внесён или ушёл в минус — НЕ значит «нет в
    наличии»). negative_stock — учёт ведётся, а остаток < 0: оператору проверить приход.
    has_other_active_listing — у товара есть другое объявление без «Gelöscht», по которому были
    письма за последние ACTIVE_LISTING_DAYS дней (перепостили).
    error — склад не ответил (см. ad_context_safe).
    """
    out = _empty_context()
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
    tracked = bool(p["stock_tracked"])
    qty = inv.stock_qty(pid)
    cutoff = ((now or datetime.utcnow()) - timedelta(days=ACTIVE_LISTING_DAYS)).isoformat()
    other = inv.iter_rows("SELECT 1 FROM inv_listings WHERE product_id = ? AND ad_id != ? AND state = 'active' "
                          "AND last_seen_at >= ? LIMIT 1", (pid, str(ad_id), cutoff))
    if not tracked or qty < 0:
        stock = "unknown"
    else:
        stock = "in_stock" if qty > 0 else "out_of_stock"
    out.update(known=True, product_id=pid, product_title=p["title"], stock=stock,
               qty=qty if tracked else None, negative_stock=tracked and qty < 0,
               list_price_eur=p["list_price_eur"], min_price_eur=p["min_price_eur"],
               has_other_active_listing=bool(other))
    return out


def ad_context_safe(ad_id: Optional[str]) -> dict[str, Any]:
    """ad_context без исключений: при ошибке БД — «неизвестно» с error=True и ERROR в лог
    (его подхватит monitor_errors). Вызывающий не должен делать выводов из сбойного ответа."""
    try:
        return ad_context(ad_id)
    except Exception:  # noqa: BLE001
        logger.exception("склад: ad_context(%s) упал — считаю остаток неизвестным", ad_id)
        out = _empty_context()
        out["error"] = True
        return out


def effective_ad_state(ad_id: Optional[str], raw_state: str) -> str:
    """Состояние объявления для промпта LLM с учётом склада.

    raw_state — что видно по самому объявлению: '' / 'deleted' (Gelöscht) / 'reserved' /
    'sold' (старая отметка ad_briefs.sold_at). Результат:
      'out_of_stock' — учёт ведётся и остаток 0: не обещать наличие;
      'reserved'     — объявление зарезервировано, а в наличии ≤ 1 шт. (её, видимо, и держат);
      ''             — товар есть или его перепостили: Gelöscht/sold не упоминать;
                       так же при сбое склада — ничего не утверждать;
      raw_state      — склад ничего не знает: прежнее поведение, кроме 'sold' (отметка объявления,
                       а не товара) — она превращается в 'deleted' без слова «продано».
    """
    ctx = ad_context_safe(ad_id)
    if ctx["error"]:
        return ""
    if ctx["stock"] == "out_of_stock":
        return "out_of_stock"
    if ctx["stock"] == "in_stock":
        return "reserved" if raw_state == "reserved" and (ctx["qty"] or 0) <= 1 else ""
    if raw_state in ("deleted", "sold") and ctx["has_other_active_listing"]:
        return ""
    if raw_state == "sold":
        return "deleted"
    return raw_state or ""


def prompt_note(ad_id: Optional[str]) -> str:
    """Строка о наличии для системного промпта LLM ('' — сказать нечего, в т. ч. при сбое склада)."""
    ctx = ad_context_safe(ad_id)
    if ctx["error"]:
        return ""
    if ctx["stock"] == "in_stock":
        return (f"Склад: товар в наличии ({ctx['qty']} шт.). Не говори, что он продан или его нет, "
                f"даже если объявление удалено.")
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
        return datetime.fromisoformat(inv.norm_ts(s))
    except ValueError:
        logger.warning("склад: нераспознанная дата %r", s)
        return None


def _fifo(movements: list[Any], default_cost: Optional[float],
          since: Optional[str]) -> tuple[list[dict], list[dict]]:
    """Прогнать движения товара по FIFO.

    since — начало учёта (inv.tracked_since). Движения раньше него — история до учёта: продажи
    берут себестоимость по оценке товара (default_cost) и в партии не залезают.
    Возвращает (партии, продажи):
      партия  {movement_id, date, qty_in, cost, left, sold, revenue} — sold/revenue: сколько штук
              партии продано и на какую выручку (для окупаемости), left — осталось;
      продажа {movement_id, occurred_at, qty, revenue, cogs, days, pre_tracking, gmail_thread_id}.
    cogs/days = None, если не известны (нет себестоимости, продали больше, чем было внесено).
    """
    lots: list[dict] = []
    queue: deque[dict] = deque()
    sales: list[dict] = []
    for m in movements:
        when = _dt(m["occurred_at"])
        q = int(m["qty"])
        pre = since is None or (m["occurred_at"] or "") < since
        if q > 0 and not pre:
            cost = m["unit_cost_eur"] if m["kind"] == "receipt" and m["unit_cost_eur"] is not None else default_cost
            lot = {"movement_id": m["id"], "kind": m["kind"], "date": when, "occurred_at": m["occurred_at"],
                   "qty_in": q, "cost": cost, "left": q, "sold": 0, "revenue": 0.0, "unpriced_sold": 0}
            lots.append(lot)
            queue.append(lot)
            continue
        if q >= 0:
            continue
        need = -q
        unit_price = m["unit_price_eur"] if m["kind"] == "sale" else None
        cogs: Optional[float] = 0.0
        days_sum, days_n = 0.0, 0
        while need and queue and not pre:
            lot = queue[0]
            take = min(need, lot["left"])
            if cogs is not None:
                cogs = None if lot["cost"] is None else cogs + take * lot["cost"]
            if when and lot["date"]:
                days_sum += take * max(0.0, (when - lot["date"]).total_seconds() / 86400)
                days_n += take
            if m["kind"] == "sale":
                lot["sold"] += take
                if unit_price is None:
                    lot["unpriced_sold"] += take
                else:
                    lot["revenue"] += take * unit_price
            lot["left"] -= take
            need -= take
            if lot["left"] == 0:
                queue.popleft()
        if need:   # до начала учёта или больше, чем внесено, — себестоимость по оценке товара
            cogs = None if (cogs is None or default_cost is None) else cogs + need * default_cost
        if m["kind"] == "sale":
            sales.append({
                "movement_id": m["id"], "occurred_at": m["occurred_at"], "qty": -q,
                "revenue": None if unit_price is None else unit_price * -q,
                "cogs": cogs, "days": (days_sum / days_n) if days_n and not need else None,
                "pre_tracking": pre, "gmail_thread_id": m["gmail_thread_id"],
            })
    return lots, sales


def _in_period(iso: Optional[str], pf: Optional[datetime], pt: Optional[datetime], now: datetime) -> bool:
    d = _dt(iso)
    if d is None:
        # Без распознаваемой даты в период не попадает (иначе «прилипала» бы к любому периоду)
        return pf is None and pt is None
    return (pf is None or d >= pf) and (pt is None or d < pt)


def _product_moves() -> dict[int, list]:
    moves: dict[int, list] = {}
    for m in inv.list_movements():
        moves.setdefault(m["product_id"], []).append(m)
    return moves


def report(period_from: Optional[str] = None, period_to: Optional[str] = None, *,
           now: Optional[datetime] = None, stale_days: int = STALE_DAYS) -> dict[str, Any]:
    """Отчёт склада.

    По товару: остаток (None — не внесён), себестоимость остатка (FIFO по приходам, иначе оценка
    unit_cost_eur), ожидаемая выручка и прибыль по цене объявления и по минимальной, продажи за
    период (шт., выручка, себестоимость, прибыль), средний срок до продажи, возраст старейшей
    единицы / сколько висит объявление. Итоги — в totals. Неизвестные суммы — None, а не 0,
    с подсчётом «без себестоимости / без цены», чтобы не рисовать ложную прибыль.
    """
    inv.ensure_schema()
    now = now or datetime.utcnow()
    pf, pt = _dt(period_from), _dt(period_to)
    products = inv.list_products(include_archived=True)
    moves = _product_moves()

    items: list[dict[str, Any]] = []
    T: dict[str, Any] = {"products": 0, "tracked": 0, "qty": 0, "stock_cost": 0.0, "units_without_cost": 0,
                         "expected_revenue_list": 0.0, "expected_revenue_min": 0.0,
                         "units_without_list_price": 0, "units_without_min_price": 0,
                         "sales": 0, "sold_qty": 0, "revenue": 0.0, "cogs": 0.0, "profit": 0.0,
                         "sales_without_cost": 0, "sales_without_price": 0, "sales_with_profit": 0,
                         "negative_stock": 0, "stale": 0, "untracked_listed": 0,
                         "open_questions": len(inv.list_open_questions())}
    days_all: list[tuple[float, int]] = []

    for p in products:
        mv = moves.get(p["id"], [])
        if p["archived_at"] and not mv:
            continue
        since = inv.tracked_since(p["id"])
        lots, sales = _fifo(mv, p["unit_cost_eur"], since)
        tracked = bool(p["stock_tracked"])
        qty = int(p["qty"]) if tracked else None
        on_hand = max(0, qty or 0)
        stock_cost: Optional[float] = None
        no_cost = 0
        oldest_days: Optional[float] = None
        if tracked and on_hand:
            open_lots = [l for l in lots if l["left"] > 0]
            costed = [l for l in open_lots if l["cost"] is not None]
            no_cost = sum(l["left"] for l in open_lots if l["cost"] is None)
            stock_cost = sum(l["left"] * l["cost"] for l in costed) if costed else None
            ages = [(now - l["date"]).total_seconds() / 86400 for l in open_lots if l["date"]]
            oldest_days = round(max(ages), 1) if ages else None
        listed = _dt(p["first_listed_at"])
        listed_days = round((now - listed).total_seconds() / 86400, 1) if listed else None

        exp_list = on_hand * p["list_price_eur"] if on_hand and p["list_price_eur"] else None
        exp_min = on_hand * p["min_price_eur"] if on_hand and p["min_price_eur"] else None
        full_cost = stock_cost if (on_hand and no_cost == 0) else None

        in_period = [x for x in sales if _in_period(x["occurred_at"], pf, pt, now)]
        priced = [x for x in in_period if x["revenue"] is not None]
        costed_sales = [x for x in in_period if x["cogs"] is not None]
        with_profit = [x for x in priced if x["cogs"] is not None]
        p_rev = sum(x["revenue"] for x in priced)
        p_cogs = sum(x["cogs"] for x in costed_sales)
        p_profit = sum(x["revenue"] - x["cogs"] for x in with_profit)
        known_days = [(x["days"], x["qty"]) for x in in_period if x["days"] is not None]
        days_all.extend(known_days)

        item = {
            "product_id": p["id"], "title": p["title"], "category": p["category"], "condition": p["condition"],
            "archived": bool(p["archived_at"]), "stock_tracked": tracked, "tracked_since": since, "qty": qty,
            "unit_cost_eur": p["unit_cost_eur"], "list_price_eur": p["list_price_eur"],
            "min_price_eur": p["min_price_eur"], "listings": p["listings"],
            "stock_cost_eur": _r(stock_cost), "units_without_cost": no_cost,
            "expected_revenue_list": _r(exp_list), "expected_revenue_min": _r(exp_min),
            "expected_profit_list": _r(exp_list - full_cost) if exp_list is not None and full_cost is not None else None,
            "expected_profit_min": _r(exp_min - full_cost) if exp_min is not None and full_cost is not None else None,
            "sold_qty": sum(x["qty"] for x in in_period),
            # Неизвестное — None, а не 0: продажи были, но без цены / без себестоимости
            "revenue": _r(p_rev) if priced or not in_period else None,
            "cogs": _r(p_cogs) if costed_sales or not in_period else None,
            "profit": _r(p_profit) if with_profit else (0.0 if not in_period else None),
            "sales_without_cost": sum(1 for x in in_period if x["cogs"] is None),
            "sales_without_price": len(in_period) - len(priced),
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
        T["sales_with_profit"] += len(with_profit)
        T["sales_without_cost"] += item["sales_without_cost"]
        T["sales_without_price"] += item["sales_without_price"]
        T["negative_stock"] += 1 if item["negative_stock"] else 0
        T["stale"] += 1 if item["stale"] else 0
        T["untracked_listed"] += 1 if (not tracked and not p["archived_at"]) else 0

    for k in ("stock_cost", "expected_revenue_list", "expected_revenue_min", "revenue", "cogs", "profit"):
        T[k] = _r(T[k])
    if T["sales"] and not T["sales_with_profit"]:
        T["profit"] = None
    if T["sales"] and T["sales_without_price"] == T["sales"]:
        T["revenue"] = None
    if T["sales"] and T["sales_without_cost"] == T["sales"]:
        T["cogs"] = None
    if not T["tracked"]:
        # Остатки ещё не внесены — денежные суммы склада неизвестны, а не нулевые
        T["stock_cost"] = T["expected_revenue_list"] = T["expected_revenue_min"] = None
    # Ожидаемая прибыль по всему складу — только если себестоимость и цены известны у всех единиц
    T["expected_profit_list"] = _r(T["expected_revenue_list"] - T["stock_cost"]) \
        if T["tracked"] and T["units_without_cost"] == 0 and T["units_without_list_price"] == 0 else None
    T["expected_profit_min"] = _r(T["expected_revenue_min"] - T["stock_cost"]) \
        if T["tracked"] and T["units_without_cost"] == 0 and T["units_without_min_price"] == 0 else None
    T["avg_days_to_sell"] = _r(_wavg(days_all))
    T["unlinked_sales"] = len(unlinked_sales())
    items.sort(key=lambda i: (i["archived"], not i["stale"], -(i["qty"] or 0), i["title"]))
    return {"period_from": period_from, "period_to": period_to, "generated_at": now.isoformat(),
            "totals": T, "products": items}


def monthly_report(year: int, month: int, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Отчёт прибыли за месяц (даты UTC; граница месяца — 1-е число 00:00).

    Возвращает:
      invested       — вложено: сумма приходов месяца (шт. × цена закупки; без цены — в invested_unknown_qty)
      revenue        — выручка продаж месяца (без цены — sales_without_price)
      cogs           — себестоимость проданного по FIFO-партиям (до начала учёта — по оценке товара)
      profit         — revenue − cogs по продажам, где известно и то и другое (None — ни одной такой)
      stock_end      — остаток на конец месяца: шт., по себестоимости, по цене объявления, по минимуму
      lots           — партии (приходы), купленные до конца месяца, с окупаемостью на конец месяца:
                       {product_id, title, movement_id, date, qty_in, unit_cost, invested, sold,
                        left, revenue, payback: 'paid' | 'not_yet' | 'unknown', payback_pct}
      products       — по товару: продано шт., выручка, себестоимость, прибыль за месяц
    Остаток «на конец месяца» считается по движениям до границы (поздние не учитываются); его
    оценка по цене объявления / минимуму — по ТЕКУЩИМ ценам товара (истории цен нет), единицы без
    цены — в units_without_list_price / units_without_min_price.
    """
    if not (1 <= int(month) <= 12):
        raise ValueError("месяц 1..12")
    start = datetime(int(year), int(month), 1)
    end = datetime(int(year) + (month == 12), 1 if month == 12 else int(month) + 1, 1)
    s_iso, e_iso = start.isoformat(), end.isoformat()
    inv.ensure_schema()
    products = {p["id"]: p for p in inv.list_products(include_archived=True)}
    moves = _product_moves()
    out: dict[str, Any] = {"year": int(year), "month": int(month), "from": s_iso, "to": e_iso,
                           "invested": 0.0, "invested_unknown_qty": 0, "received_qty": 0,
                           "revenue": 0.0, "cogs": 0.0, "profit": 0.0, "sales": 0, "sold_qty": 0,
                           "sales_without_price": 0, "sales_without_cost": 0, "sales_with_profit": 0,
                           "stock_end": {"qty": 0, "cost": 0.0, "units_without_cost": 0,
                                         "list_value": 0.0, "min_value": 0.0, "tracked_products": 0,
                                         "units_without_list_price": 0, "units_without_min_price": 0},
                           "lots": [], "products": []}
    for pid, mv_all in moves.items():
        p = products.get(pid)
        if p is None:
            continue
        mv = [m for m in mv_all if (m["occurred_at"] or "") < e_iso]
        if not mv:
            continue
        since = min((m["occurred_at"] for m in mv if m["kind"] in ("receipt", "adjust")), default=None)
        lots, sales = _fifo(mv, p["unit_cost_eur"], since)
        month_sales = [x for x in sales if s_iso <= (x["occurred_at"] or "") < e_iso]
        priced = [x for x in month_sales if x["revenue"] is not None]
        with_profit = [x for x in priced if x["cogs"] is not None]
        rev = sum(x["revenue"] for x in priced)
        cogs = sum(x["cogs"] for x in month_sales if x["cogs"] is not None)
        prof = sum(x["revenue"] - x["cogs"] for x in with_profit)
        if month_sales:
            out["products"].append({
                "product_id": pid, "title": p["title"], "sold_qty": sum(x["qty"] for x in month_sales),
                "revenue": _r(rev) if priced else None,
                "cogs": _r(cogs) if any(x["cogs"] is not None for x in month_sales) else None,
                "profit": _r(prof) if with_profit else None,
                "sales_without_price": len(month_sales) - len(priced),
                "sales_without_cost": sum(1 for x in month_sales if x["cogs"] is None)})
        out["sales"] += len(month_sales)
        out["sold_qty"] += sum(x["qty"] for x in month_sales)
        out["revenue"] += rev
        out["cogs"] += cogs
        out["profit"] += prof
        out["sales_with_profit"] += len(with_profit)
        out["sales_without_price"] += len(month_sales) - len(priced)
        out["sales_without_cost"] += sum(1 for x in month_sales if x["cogs"] is None)

        for l in lots:
            if l["kind"] != "receipt":
                continue
            in_month = s_iso <= (l["occurred_at"] or "") < e_iso
            invested = l["qty_in"] * l["cost"] if l["cost"] is not None else None
            if in_month:
                out["received_qty"] += l["qty_in"]
                if invested is None:
                    out["invested_unknown_qty"] += l["qty_in"]
                else:
                    out["invested"] += invested
            if invested is None or l["unpriced_sold"]:
                payback, pct = "unknown", None
            else:
                pct = round(100 * l["revenue"] / invested, 1) if invested else None
                payback = "paid" if l["revenue"] >= invested else "not_yet"
            out["lots"].append({
                "product_id": pid, "title": p["title"], "movement_id": l["movement_id"],
                "date": l["occurred_at"], "qty_in": l["qty_in"], "unit_cost": l["cost"],
                "invested": _r(invested), "sold": l["sold"], "left": l["left"], "revenue": _r(l["revenue"]),
                "payback": payback, "payback_pct": pct, "bought_this_month": in_month})

        if since is not None and p["stock_tracked"]:
            qty_end = sum(int(m["qty"]) for m in mv if m["voided_at"] is None and (m["occurred_at"] or "") >= since)
            se = out["stock_end"]
            se["tracked_products"] += 1
            if qty_end > 0:
                se["qty"] += qty_end
                for l in lots:
                    if l["left"]:
                        if l["cost"] is None:
                            se["units_without_cost"] += l["left"]
                        else:
                            se["cost"] += l["left"] * l["cost"]
                # Цены — текущие цены товара (истории цен нет)
                if p["list_price_eur"]:
                    se["list_value"] += qty_end * p["list_price_eur"]
                else:
                    se["units_without_list_price"] += qty_end
                if p["min_price_eur"]:
                    se["min_value"] += qty_end * p["min_price_eur"]
                else:
                    se["units_without_min_price"] += qty_end
    for k in ("invested", "revenue", "cogs", "profit"):
        out[k] = _r(out[k])
    if out["sales"] and not out["sales_with_profit"]:
        out["profit"] = None
    if out["sales"] and out["sales_without_price"] == out["sales"]:
        out["revenue"] = None
    if out["sales"] and out["sales_without_cost"] == out["sales"]:
        out["cogs"] = None
    se = out["stock_end"]
    for k in ("cost", "list_value", "min_value"):
        se[k] = _r(se[k]) if se["tracked_products"] else None
    out["lots"].sort(key=lambda l: (l["date"] or "", l["movement_id"]))
    out["products"].sort(key=lambda x: -(x["revenue"] or 0))
    return out


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
        no_price = (f" (+{t['units_without_list_price']} шт. без цены)"
                    if t["units_without_list_price"] else "")
        no_min = (f" (+{t['units_without_min_price']} шт. без минимума)"
                  if t["units_without_min_price"] else "")
        lines.append(f"Ожидаемая выручка: {_eur(t['expected_revenue_list'])} по объявлениям{no_price}, "
                     f"{_eur(t['expected_revenue_min'])} по минимуму{no_min}")
    else:
        lines = [f"Склад: остатки не внесены ({t['products']} товаров из объявлений)"]
    if t["sold_qty"]:
        if t["profit"] is None:
            prof = "неизвестна (нет себестоимости)"
        else:
            prof = _eur(t["profit"]) + (f" (по {t['sales_with_profit']} из {t['sales']} продаж)"
                                        if t["sales_with_profit"] < t["sales"] else "")
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
    """Записать план в БД одной транзакцией. Объявления, уже заведённые на складе, пропускаются;
    новые объявления группы, часть которой уже на складе, идут к товару известной части (без
    дублей товаров). Сомнительные пары → вопросы оператору."""
    inv.ensure_schema()
    gid2pid: dict[int, int] = {}
    stats = {"products": 0, "listings": 0, "questions": 0, "skipped_known": 0,
             "joined_existing": 0, "doubts_skipped": 0}
    with transaction():
        for g in plan["groups"]:
            known = [inv.get_listing(a.ad_id) for a in g.ads]
            known_pids = [_follow_merge(l["product_id"]) for l in known if l is not None]
            fresh = [a for a, l in zip(g.ads, known) if l is None]
            stats["skipped_known"] += len(g.ads) - len(fresh)
            if known_pids:
                gid2pid[g.gid] = known_pids[-1]
                for other in sorted(set(known_pids) - {known_pids[-1]}):
                    # Объявления одной группы уже на разных товарах — спросить, не слить ли
                    ad = next(a for a, l in zip(g.ads, known)
                              if l is not None and _follow_merge(l["product_id"]) == other)
                    if inv.add_link_question(ad_id=ad.ad_id, product_id=other, suggested_product_id=known_pids[-1],
                                             score=1.0, reason="тот же заголовок, но уже разные товары"):
                        stats["questions"] += 1
            if not fresh:
                continue
            if known_pids:
                pid = known_pids[-1]
                stats["joined_existing"] += len(fresh)
            else:
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
            if not (pid and spid) or pid == spid:
                stats["doubts_skipped"] += 1   # обе стороны уже один товар / нечего спрашивать
                continue
            if inv.add_link_question(ad_id=d["ad"].ad_id, product_id=pid, suggested_product_id=spid,
                                     score=d["score"], reason=d["reason"]):
                stats["questions"] += 1
            else:
                stats["doubts_skipped"] += 1   # такой вопрос уже открыт
    return stats


def seed_ads_from_messages() -> list[AdInfo]:
    """Объявления из всей истории messages (для scripts/inventory_seed.py)."""
    return collect_ads(_ad_rows())


# ─── профиль товара (стабильный контракт для kab-market) ──────────────────────

PROFILE_VERSION = 1


def product_profile(product_id: int) -> Optional[dict[str, Any]]:
    """Товар → всё, что нужно для сравнения с рынком. Стабильный контракт (PROFILE_VERSION):
    новые ключи могут добавляться, существующие не переименовываются и не меняют смысл.

    {
      "version": 1, "product_id", "title", "category", "condition" (neu/gebraucht/defekt/None),
      "archived": bool, "match_key": str, "core_tokens": [str] (заголовок без моделей авто и стоп-слов),
      "models": [str] (упомянутые модели/марки авто: traveller, proace, zafira, …),
      "features": {"defekt", "neu", "vip", "leder": bool},
      "list_price_eur", "min_price_eur": float|None,
      "unit_cost_eur": float|None — средняя себестоимость единицы на складе (FIFO-партии),
                       иначе оценка товара; "unit_cost_source": "lots" | "estimate" | None,
      "stock": {"tracked": bool, "qty": int|None},
      "listings": [{"ad_id", "title", "price_eur", "url", "state", "account_id",
                    "first_seen_at", "last_seen_at"}] — от свежих к старым,
      "titles": [str] — все разные заголовки объявлений,
      "description": str — самое свежее описание объявления из писем ('' если нет)
    }
    None — товара нет. Слитый товар (merged_into) разворачивается в итоговый.
    """
    inv.ensure_schema()
    p = inv.get_product(_follow_merge(int(product_id)))
    if p is None:
        return None
    pid = p["id"]
    listings = sorted(inv.list_listings(pid), key=lambda l: l["last_seen_at"] or "", reverse=True)
    titles: list[str] = []
    for t in [p["title"]] + [l["title"] for l in listings]:
        if t and t not in titles:
            titles.append(t)
    all_tokens = set()
    for t in titles:
        all_tokens |= set(tokens(t))
    since = inv.tracked_since(pid)
    lots, _ = _fifo(inv.list_movements(pid), p["unit_cost_eur"], since)
    open_lots = [l for l in lots if l["left"] > 0 and l["cost"] is not None]
    if open_lots:
        units = sum(l["left"] for l in open_lots)
        unit_cost, cost_src = round(sum(l["left"] * l["cost"] for l in open_lots) / units, 2), "lots"
    elif p["unit_cost_eur"] is not None:
        unit_cost, cost_src = p["unit_cost_eur"], "estimate"
    else:
        unit_cost, cost_src = None, None
    tracked = bool(p["stock_tracked"])
    desc = ""
    for l in listings:
        desc = _ad_description(l["ad_id"])
        if desc:
            break
    return {
        "version": PROFILE_VERSION, "product_id": pid, "title": p["title"], "category": p["category"],
        "condition": p["condition"], "archived": bool(p["archived_at"]), "match_key": p["match_key"] or match_key(p["title"]),
        "core_tokens": sorted(_core(" ".join(titles))), "models": sorted(all_tokens & _MODELS),
        "features": features(" ".join(titles)),
        "list_price_eur": p["list_price_eur"], "min_price_eur": p["min_price_eur"],
        "unit_cost_eur": unit_cost, "unit_cost_source": cost_src,
        "stock": {"tracked": tracked, "qty": inv.stock_qty(pid) if tracked else None},
        "listings": [{k: l[k] for k in ("ad_id", "title", "price_eur", "url", "state", "account_id",
                                        "first_seen_at", "last_seen_at")} for l in listings],
        "titles": titles, "description": desc or "",
    }


def list_product_profiles(include_archived: bool = False) -> list[dict[str, Any]]:
    """product_profile для всех (неархивных) товаров."""
    out = []
    for p in inv.list_products(include_archived=include_archived):
        prof = product_profile(p["id"])
        if prof is not None and (include_archived or not prof["archived"]):
            out.append(prof)
    return out


# ─── подсказка цены продажи ───────────────────────────────────────────────────

_TEXT_PRICE_RE = re.compile(r'(\d{1,3}(?:[.\s]\d{3})+|\d{2,6})\s*(?:,-|,00)?\s*(?:€|eur\b|euro\b)', re.IGNORECASE)


_NOT_ITEM_PRICE_RE = re.compile(r'versand|porto|liefer|shipping|доставк|kaution', re.IGNORECASE)


def _prices_in(text: Optional[str]) -> list[float]:
    """Цены в тексте письма: без процитированной истории («> …») и без доставки/залога."""
    lines = [ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">")]
    body = "\n".join(lines)
    out = []
    for m in _TEXT_PRICE_RE.finditer(body):
        if _NOT_ITEM_PRICE_RE.search(body[max(0, m.start() - 25):m.start()]):
            continue
        v = float(re.sub(r'[.\s]', "", m.group(1)))
        if v > 0:
            out.append(v)
    return out


def suggest_sale_price(gmail_thread_id: str) -> dict[str, Any]:
    """Подсказка цены для «Продано» (цена обязательна — оператору показать предзаполненной).

    Кандидаты по убыванию доверия:
      negotiation_our / negotiation_buyer — negotiation_state треда (наша / покупателя последняя цена);
      deal_brief  — negotiated_price_eur из самого свежего deal_brief_json треда;
      our_text    — последняя цена в нашем последнем отправленном ответе;
      buyer_text  — последняя цена в последнем письме покупателя;
      ad_price    — цена объявления (из письма, иначе товара на складе).
    Возвращает {"price_eur": float|None, "source": str|None, "candidates": [{"source", "price_eur"}]}.
    """
    import json
    cands: list[dict[str, Any]] = []

    def add(src: str, val: Any) -> None:
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        if v > 0 and not any(c["source"] == src for c in cands):
            cands.append({"source": src, "price_eur": round(v, 2)})

    tid = str(gmail_thread_id)
    ns = inv.iter_rows("SELECT our_last_offer_eur, buyer_last_offer_eur FROM negotiation_state "
                       "WHERE gmail_thread_id = ?", (tid,))
    if ns:
        add("negotiation_our", ns[0]["our_last_offer_eur"])
        add("negotiation_buyer", ns[0]["buyer_last_offer_eur"])
    for r in inv.iter_rows("SELECT deal_brief_json FROM messages WHERE gmail_thread_id = ? "
                           "AND deal_brief_json IS NOT NULL ORDER BY created_at DESC, id DESC", (tid,)):
        try:
            brief = json.loads(r["deal_brief_json"] or "{}")
        except (TypeError, ValueError):
            logger.warning("склад: битый deal_brief_json в треде %s", tid)
            continue
        if isinstance(brief, dict) and brief.get("negotiated_price_eur"):
            add("deal_brief", brief["negotiated_price_eur"])
            break
    # sent_debug (redirect на отладочный ящик) клиент не видел — не считаем
    ours = inv.iter_rows("SELECT de_answer FROM messages WHERE gmail_thread_id = ? AND status IN ('sent', 'sent_external') "
                         "AND de_answer IS NOT NULL ORDER BY COALESCE(sent_at, created_at) DESC, id DESC LIMIT 1",
                         (tid,))
    if ours and _prices_in(ours[0]["de_answer"]):
        add("our_text", _prices_in(ours[0]["de_answer"])[-1])
    theirs = inv.iter_rows("SELECT de_client FROM messages WHERE gmail_thread_id = ? AND direction = 'in' "
                           "AND de_client IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1", (tid,))
    if theirs and _prices_in(theirs[0]["de_client"]):
        add("buyer_text", _prices_in(theirs[0]["de_client"])[-1])
    ad = inv.iter_rows("SELECT ad_id, ad_price FROM messages WHERE gmail_thread_id = ? AND ad_id IS NOT NULL "
                       "ORDER BY created_at DESC, id DESC LIMIT 1", (tid,))
    if ad:
        add("ad_price", parse_price(ad[0]["ad_price"]) or ad_context_safe(ad[0]["ad_id"])["list_price_eur"])
    best = cands[0] if cands else None
    return {"price_eur": best["price_eur"] if best else None, "source": best["source"] if best else None,
            "candidates": cands}
