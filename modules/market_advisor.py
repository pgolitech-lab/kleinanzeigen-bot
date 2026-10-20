# «Рынок-советник» (market): ценовой советник, позиция наших объявлений в выдаче,
# подсказки перепоста с готовым текстом, советы по заметности и предупреждение о
# дублях между аккаунтами.
#
# Источники: scout_listings (публичная выдача Kleinanzeigen, собирает modules/scout.py),
# market_* (история цен и мест в выдаче — modules/db_market.py), messages / ad_briefs
# (наши объявления и письма), склад inv_* (если модуль «Склад» уже в main — товары,
# себестоимость, остаток; без него товар = группа наших объявлений с одинаковым заголовком).
#
# Перепост делает ЧЕЛОВЕК: модуль только подсказывает, когда и что выложить. Никакого
# логина и автопостинга (риск бана). На kleinanzeigen.de ходит только run_rank_check —
# публичный поиск, редко (2 раза в сутки), с паузами между страницами.
#
# Функции для api/бота (формат — в docstring каждой):
#   advise_price(...)          — цена товара относительно рынка
#   rank_report(...)           — места наших объявлений в выдаче и динамика
#   repost_suggestions(limit)  — что и когда переопубликовать + готовый текст
#   visibility_tips(...)       — как стать заметнее (слова лидеров, варианты заголовка)
#   duplicates_report()        — один товар в нескольких объявлениях / аккаунтах
#   summary_lines()            — 0–5 строк для утренней сводки бота
#   market_rank_job()          — джоб планировщика (позиции в выдаче), 2 раза в сутки
#   market_features_job()      — джоб признаков конкурентов (Haiku), раз в 6 ч

import logging
import random
import re
import statistics
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import config
from database import get_conn
from modules import db_market

logger = logging.getLogger(__name__)

BERLIN = ZoneInfo("Europe/Berlin")

# --- ПОРОГИ (подобраны по данным ideas.md, п. 1.4–1.5) ---
PAGE_SIZE = 25                 # карточек на странице выдачи Kleinanzeigen
SINK_POS = 15                  # ниже этого места (организ.) — «опускается»
QUIET_DAYS = 5                 # столько дней без писем по товару — повод перепостить
FRESH_REPOST_DAYS = 4          # объявлению меньше N дней — перепост рано
SEEN_LIVE_DAYS = 3             # видели в выдаче за N дней — объявление точно живое
PROBABLE_LIVE_DAYS = 21        # писали по нему за N дней и не «Gelöscht» — вероятно живое
COMPARABLE_DAYS = 60           # сопоставимые: активные + ушедшие за N дней
MIN_COMPARABLE = 3             # меньше — «мало данных», цифр не показываем
FAST_GONE_DAYS = 7             # исчезло из выдачи за ≤ N дней — «быстро ушло»
RANK_MIN_INTERVAL_H = 10       # не чаще одного прогона позиций за N часов
RANK_MAX_QUERIES = 12          # не больше N наших запросов за прогон
RANK_MAX_PAGES = 3
TITLE_MAX = 65                 # лимит заголовка Kleinanzeigen
DEFAULT_MARGIN_PCT = 20.0      # «минимум» = себестоимость × (1 + маржа)
REPOST_WINDOW_DAYS = (6, 0, 1, 2)   # вс, пн, вт, ср (weekday(): пн=0 … вс=6)
REPOST_WINDOW_HOURS = (16, 19)      # 16:00–19:00 по Берлину


# ══════════════════════════════════════════════════════════════════════════════
# ТЕКСТ: нормализация заголовков. Те же правила, что у склада (modules/inventory.py,
# сессия stock): если склад уже в main — берём его функции, иначе локальная копия.
# ══════════════════════════════════════════════════════════════════════════════

_CYR = str.maketrans("аеорсхук", "aeopcxyk")
_STOP = {"und", "fur", "mit", "der", "die", "das", "for", "von", "zu", "in", "an", "div", "z", "b",
         "l", "r", "ca", "bis", "ab", "vorne", "hinten", "original", "orig", "komplett", "x"}
_MODEL_WORDS = {"peugeot", "traveller", "teaveller", "expert", "citroen", "cirtroen", "spacetourer", "space",
                "tourer", "toyota", "totota", "proace", "verso", "opel", "zafira", "life", "fiat", "ulysse",
                "vivaro", "jumpy", "psa", "mistral", "scudo", "c", "tesla", "model", "ford", "mondeo",
                "renault", "vw", "multivan", "t5", "t6", "t61", "transporter", "caravelle", "vito", "v",
                "klasse", "viano", "mercedes"}
_CATEGORIES: list[tuple[str, re.Pattern]] = [
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
_SUBJECT_RE = re.compile(
    r'^\s*(?:re:\s*)?(?:nutzer-)?anfrage\s+zu\s+(?:deiner|ihrer)\s+anzeige\s*[„"«»“]?(.*?)[“"»«]?\s*$',
    re.IGNORECASE)
_STATE_RE = re.compile(r'^\s*(gelöscht|geloescht|reserviert)\s*[•·\-–:]\s*', re.IGNORECASE)
_PROMO_RE = re.compile(r'\.\.\.\s*bis zu .*schneller verkaufen\*?\s*$', re.IGNORECASE)


def _fold(s: Optional[str]) -> str:
    s = (s or "").casefold().replace("ß", "ss").translate(_CYR)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def tokens(title: Optional[str]) -> list[str]:
    s = _fold(title)
    s = re.sub(r'\b(\d)\s*-?\s*er\b', r'\1er', s)
    s = re.sub(r'(\d)\s*\+\s*(\d)', r'\1+\2', s)
    return re.findall(r'\d\+\d|[a-z0-9]+', s)


def match_key(title: Optional[str]) -> str:
    """Ключ заголовка: одинаков у перепощенных объявлений с тем же текстом."""
    return " ".join(sorted(set(t for t in tokens(title) if t not in _STOP)))


def detect_category(title: Optional[str]) -> str:
    s = " ".join(tokens(title))
    for name, rx in _CATEGORIES:
        if rx.search(s):
            return name
    return "other"


def core_tokens(title: Optional[str]) -> set[str]:
    return {t for t in tokens(title) if t not in _STOP and t not in _MODEL_WORDS}


def clean_title(raw: Optional[str]) -> tuple[str, str]:
    """messages.ad_title → (заголовок, состояние active/deleted/reserved)."""
    t = (raw or "").strip()
    state = "active"
    m = _STATE_RE.match(t)
    if m:
        state = "deleted" if m.group(1).lower().startswith("gel") else "reserved"
        t = t[m.end():].strip()
    m = _SUBJECT_RE.match(t)
    if m:
        t = m.group(1)
    t = _PROMO_RE.sub("", t)
    return " ".join(t.replace('"', " ").split()).strip(" ,-–"), state


try:  # склад в main — единые правила сопоставления
    from modules import inventory as _inv  # type: ignore
    tokens = _inv.tokens            # noqa: F811
    match_key = _inv.match_key      # noqa: F811
    detect_category = _inv.detect_category  # noqa: F811
except ImportError:  # модуля склада нет — работаем на локальной копии правил
    _inv = None


def parse_price(raw: Any) -> Optional[float]:
    """«1.299 € VB» → 1299.0; число → float; мусор/0 → None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    m = re.search(r'(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,(\d{1,2}))?', str(raw))
    if not m:
        return None
    val = float(re.sub(r'[.\s]', "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
    return val if val > 0 else None


# ══════════════════════════════════════════════════════════════════════════════
# ПРИЗНАКИ ОБЪЯВЛЕНИЯ (заголовок + описание)
# ══════════════════════════════════════════════════════════════════════════════

# Модели авто → каноническое имя (порядок: специфичное раньше общего)
_MODEL_CANON: list[tuple[re.Pattern, str]] = [
    (re.compile(r'proace\s*city'), "proace_city"),
    (re.compile(r'proace(?:\s*verso)?'), "proace_verso"),
    (re.compile(r'space\s*tourer'), "spacetourer"),
    (re.compile(r'zafira[\s-]*life'), "zafira_life"),
    (re.compile(r'traveller|teaveller'), "traveller"),
    (re.compile(r'\bexpert\b'), "expert"),
    (re.compile(r'jumpy'), "jumpy"),
    (re.compile(r'vivaro'), "vivaro"),
    (re.compile(r'\bscudo\b'), "scudo"),
    (re.compile(r'ulysse'), "ulysse"),
    (re.compile(r'multivan|\bt[56](?:\.1)?\b|caravelle|transporter'), "vw_t5_t6"),
    (re.compile(r'\bvito\b|v[\s-]*klasse|viano'), "mercedes_v"),
]

FEATURE_KEYS = ("category", "material", "vip", "seats", "condition", "models", "rails", "isofix")


def regex_features(title: Optional[str], description: Optional[str] = None) -> dict[str, Any]:
    """Признаки по regex: {category, material leder/kunstleder/stoff/None, vip, seats 1/2/3/None,
    condition neu/gebraucht/defekt/None, models [..], rails, isofix}."""
    t = " ".join(tokens(title))
    d = " ".join(tokens((description or "")[:600]))
    both = f"{t} {d}"
    cat = detect_category(title)
    if cat in ("other", "sitze"):
        c2 = detect_category(f"{title} {(description or '')[:200]}")
        if c2 not in ("other", "fahrzeug", "immobilie"):
            cat = c2
    if re.search(r'kunstleder|lederoptik|kunst leder', both):
        material = "kunstleder"
    elif re.search(r'leder|leather|nappa', both):
        material = "leder"
    elif re.search(r'\bstoff\b|stoffbezug|textil', both):
        material = "stoff"
    else:
        material = None
    seats: Optional[int] = None
    if re.search(r'\b2\+1\b|\b3er\b|dreier|3 ?sitzer|dreisitz|3 ?sitzbank', both):
        seats = 3
    elif re.search(r'\b2er\b|zweier|doppelsitz|2 ?sitzer|zweisitz|doppelbank', both):
        seats = 2
    elif cat == "einzelsitz" or re.search(r'einzelsitz|\bvip\b', both):
        seats = 1
    if re.search(r'defekt|b ware|beschadig|bastler|riss|kaputt', both):
        condition = "defekt"
    elif re.search(r'nagelneu|neuwertig|\bovp\b|fabrikneu|originalverpackt', both) or \
            re.search(r'(?<!wie )\bneu\b', t):
        condition = "neu"
    elif re.search(r'gebraucht|gebrauchsspuren|wie neu|guter zustand|gepflegt', both):
        condition = "gebraucht"
    else:
        condition = None
    models = []
    for rx, name in _MODEL_CANON:
        if rx.search(both) and name not in models:
            models.append(name)
    return {
        "category": cat, "material": material,
        "vip": bool(re.search(r'\bvip\b', both)), "seats": seats,
        "condition": condition, "models": models,
        "rails": bool(re.search(r'schiene|rails?\b|bodenplatte', both)),
        "isofix": "isofix" in both,
    }


_FEAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["ad_id", "category", "material", "vip", "seats", "condition", "models"],
        "properties": {
            "ad_id": {"type": "string"},
            "category": {"type": "string", "enum": [
                "sitzbank", "einzelsitz", "sitze", "fahrersitze", "komplettsatz", "tisch", "ahk",
                "sitzschienen", "raeder", "gurte", "leuchten", "karosserie", "dach", "ladekabel",
                "fahrzeug", "other"]},
            "material": {"type": "string", "enum": ["leder", "kunstleder", "stoff", "unknown"]},
            "vip": {"type": "boolean"},
            "seats": {"type": "integer", "description": "мест в одном изделии: 1, 2, 3; 0 = неизвестно"},
            "condition": {"type": "string", "enum": ["neu", "gebraucht", "defekt", "unknown"]},
            "models": {"type": "array", "items": {"type": "string", "enum": [
                "traveller", "spacetourer", "proace_verso", "proace_city", "zafira_life", "expert",
                "jumpy", "vivaro", "scudo", "ulysse", "vw_t5_t6", "mercedes_v", "other"]}},
        },
    }}},
}

_FEAT_SYSTEM = (
    "Ты разбираешь объявления Kleinanzeigen о б/у автозапчастях (сиденья, скамьи, столы для минивэнов). "
    "Для каждого объявления по заголовку И описанию определи: category — что продаётся; material — "
    "обивка (leder — натуральная кожа, kunstleder — кожзам, stoff — ткань, unknown); vip — VIP/Lounge-кресло; "
    "seats — сколько мест в ОДНОМ изделии (2+1 или 3er = 3, Doppelsitz = 2, Einzelsitz = 1, 0 = неизвестно); "
    "condition — neu (новое/неиспользованное), gebraucht, defekt (дефект, разрывы, B-Ware), unknown; "
    "models — для каких авто подходит (по тексту, не догадывайся). Верни по одному элементу на каждый ad_id."
)


def _llm_features(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Haiku: признаки для пачки объявлений. ad_id → признаки. Ошибку LLM бросает наверх."""
    from modules import claude_utils
    lines = []
    for it in items:
        lines.append(f"ad_id={it['ad_id']}\nЗаголовок: {it.get('title') or ''}\n"
                     f"Описание: {(it.get('description') or '')[:500]}")
    data, _meta = claude_utils.call_json(
        label="market_features", model=claude_utils.fast_model(), system=_FEAT_SYSTEM,
        user="\n\n---\n\n".join(lines), schema=_FEAT_SCHEMA, max_tokens=300 * len(items) + 200,
    )
    out: dict[str, dict[str, Any]] = {}
    for x in data.get("items") or []:
        aid = str(x.get("ad_id") or "")
        if not aid:
            continue
        out[aid] = {
            "category": x.get("category") or "other",
            "material": None if x.get("material") in (None, "unknown") else x["material"],
            "vip": bool(x.get("vip")),
            "seats": x.get("seats") if x.get("seats") in (1, 2, 3) else None,
            "condition": None if x.get("condition") in (None, "unknown") else x["condition"],
            "models": [m for m in (x.get("models") or []) if m != "other"],
        }
    return out


def features_for(listings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Признаки объявлений scout: кэш (Haiku или regex), иначе regex на лету (без записи).

    Haiku-признаки перекрывают regex по полям category/material/vip/seats/condition/models;
    rails/isofix — всегда regex.
    """
    cached = db_market.get_features_bulk([l["ad_id"] for l in listings])
    out: dict[str, dict[str, Any]] = {}
    for l in listings:
        rx = regex_features(l.get("title"), l.get("description"))
        c = cached.get(l["ad_id"])
        if c and c["src_hash"] == db_market.text_hash(l.get("title"), l.get("description")):
            # «не знаю» от Haiku (other / None / пусто) не затирает то, что нашёл regex
            rx.update({k: v for k, v in c["features"].items()
                       if k in FEATURE_KEYS and v not in (None, "other", [], 0)})
        out[l["ad_id"]] = rx
    return out


def enrich_features(limit: int = 60, use_llm: bool = True, batch: int = 10) -> dict[str, Any]:
    """Посчитать и закэшировать признаки запчастей рынка, у которых их нет или изменился текст.

    Haiku — пачками по batch; при ошибке LLM пачка сохраняется по regex (method='regex'),
    и при следующем запуске Haiku попробует снова. Возвращает {llm, regex, errors}.
    """
    db_market.ensure_schema()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ad_id, title, description FROM scout_listings "
            "WHERE COALESCE(verified_kind, kind)='part' AND COALESCE(rejected,0)=0 "
            "ORDER BY last_seen_at DESC LIMIT 2000")]
    cached = db_market.get_features_bulk([r["ad_id"] for r in rows])
    todo = []
    for r in rows:
        h = db_market.text_hash(r["title"], r["description"])
        c = cached.get(r["ad_id"])
        if c and c["src_hash"] == h and (c["method"] == "llm" or not use_llm):
            continue
        r["_hash"] = h
        todo.append(r)
        if len(todo) >= limit:
            break
    res = {"llm": 0, "regex": 0, "errors": 0, "missing": 0, "batches": 0}
    for i in range(0, len(todo), batch):
        res["batches"] += 1
        chunk = todo[i:i + batch]
        got: dict[str, dict[str, Any]] = {}
        if use_llm:
            try:
                got = _llm_features(chunk)
            except Exception as e:  # noqa: BLE001
                res["errors"] += 1
                logger.warning("market: Haiku-признаки не получены (%s) — пачка по regex", e)
        for r in chunk:
            if r["ad_id"] in got:
                db_market.set_features(r["ad_id"], got[r["ad_id"]], "llm", r["_hash"])
                res["llm"] += 1
            else:
                if use_llm and got:
                    res["missing"] += 1  # Haiku ответил, но этот ad_id пропустил
                db_market.set_features(r["ad_id"], regex_features(r["title"], r["description"]),
                                       "regex", r["_hash"])
                res["regex"] += 1
    return res


# ══════════════════════════════════════════════════════════════════════════════
# НАШИ ОБЪЯВЛЕНИЯ И ТОВАРЫ
# ══════════════════════════════════════════════════════════════════════════════

def _table_exists(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _iso_age_days(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "").split("+")[0])
    except ValueError:
        return None
    return (now - dt).total_seconds() / 86400


class _Ctx:
    """Снимок данных на один вызов: наши объявления, рынок, письма. Без кэша между вызовами."""

    def __init__(self, now: Optional[datetime] = None) -> None:
        self.now = now or datetime.utcnow()
        self.cache: dict[str, Any] = {}
        db_market.ensure_schema()
        with get_conn() as conn:
            self.has_inv = _table_exists(conn, "inv_products") and _table_exists(conn, "inv_listings")
            # Наши объявления из писем: по ad_id — последний заголовок/цена/аккаунт.
            self.msg_ads: dict[str, dict[str, Any]] = {}
            for r in conn.execute(
                "SELECT ad_id, ad_title, ad_price, ad_url, ad_description, account_id, created_at, direction, "
                "gmail_thread_id FROM messages WHERE ad_id IS NOT NULL AND ad_id != '' ORDER BY created_at"):
                a = self.msg_ads.setdefault(r["ad_id"], {
                    "ad_id": r["ad_id"], "title": "", "state": "active", "price_eur": None, "url": None,
                    "description": "", "account_id": None, "first_msg_at": r["created_at"],
                    "last_msg_at": None, "threads": set(), "inquiry_times": []})
                title, state = clean_title(r["ad_title"])
                if title:
                    a["title"] = title
                    a["state"] = state
                a["price_eur"] = parse_price(r["ad_price"]) or a["price_eur"]
                a["url"] = r["ad_url"] or a["url"]
                if r["ad_description"]:
                    a["description"] = r["ad_description"]
                a["account_id"] = r["account_id"] or a["account_id"]
                a["last_msg_at"] = r["created_at"]
                if r["direction"] == "in" and r["gmail_thread_id"] and r["gmail_thread_id"] not in a["threads"]:
                    a["threads"].add(r["gmail_thread_id"])
                    a["inquiry_times"].append(r["created_at"])
            self.briefs: dict[str, dict[str, Any]] = {}
            if _table_exists(conn, "ad_briefs"):
                cols = {c["name"] for c in conn.execute("PRAGMA table_info(ad_briefs)")}
                sel = ", ".join(c for c in ("ad_id", "ad_title", "ad_price", "ad_url", "sold_at", "min_price_eur")
                                if c in cols)
                for r in conn.execute(f"SELECT {sel} FROM ad_briefs"):
                    self.briefs[r["ad_id"]] = dict(r)
            self.inv_ads: dict[str, dict[str, Any]] = {}
            if self.has_inv:
                for r in conn.execute("SELECT * FROM inv_listings"):
                    self.inv_ads[r["ad_id"]] = dict(r)
            self.accounts: dict[int, str] = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM accounts")}
            # Рынок: все запчасти scout (включая наши — отфильтруем ниже).
            self.market: dict[str, dict[str, Any]] = {}
            for r in conn.execute(
                "SELECT ad_id, title, url, price_eur, price_raw, plz, city, description, posted_raw, "
                "first_seen_at, last_seen_at, active, deactivated_at, COALESCE(verified_kind, kind) AS ek "
                "FROM scout_listings WHERE COALESCE(rejected,0)=0"):
                self.market[r["ad_id"]] = dict(r)
        self.own_ids: set[str] = set(self.msg_ads) | set(self.briefs) | set(self.inv_ads)
        # Наши места: PLZ, где видели наши объявления в выдаче, + ручная настройка.
        # Только PLZ, не город: в большом городе (Köln, Aachen) полно конкурентов.
        self.own_plz: set[str] = set()
        self.city_account: dict[str, int] = {}
        for aid in self.own_ids:
            m = self.market.get(aid)
            if not m:
                continue
            if m["plz"]:
                self.own_plz.add(m["plz"])
            acc = (self.msg_ads.get(aid) or {}).get("account_id") or (self.inv_ads.get(aid) or {}).get("account_id")
            if acc and m["plz"]:
                self.city_account.setdefault(m["plz"], acc)
        extra = config.get("market_own_plz", "") or ""
        self.own_plz |= {p.strip() for p in extra.split(",") if p.strip()}
        self.own_titles: dict[str, str] = {}   # match_key → заголовок
        for aid in self.own_ids:
            t = self._own_title(aid)
            if t:
                self.own_titles[match_key(t)] = t
        self.own_by_location: dict[str, str] = {}   # ad_id рынка → причина «наше»
        for aid, m in self.market.items():
            if aid in self.own_ids:
                continue
            if m["plz"] and m["plz"] in self.own_plz and self._title_is_ours(m["title"]):
                self.own_by_location[aid] = "PLZ и заголовок как у нашего"
        self.serp_seen = db_market.last_serp_seen(list(self.own_ids | set(self.own_by_location)))

    def _own_title(self, ad_id: str) -> str:
        for src in (self.inv_ads, self.msg_ads):
            t = (src.get(ad_id) or {}).get("title")
            if t:
                return t
        b = self.briefs.get(ad_id) or {}
        if b.get("ad_title"):
            return clean_title(b["ad_title"])[0]
        return (self.market.get(ad_id) or {}).get("title") or ""

    def _title_is_ours(self, title: Optional[str]) -> bool:
        if not title:
            return False
        k = match_key(title)
        if k in self.own_titles:
            return True
        ct = core_tokens(title)
        cat = detect_category(title)
        for t in self.own_titles.values():
            if detect_category(t) != cat:
                continue
            co = core_tokens(t)
            if ct and co and len(ct & co) / len(ct | co) >= 0.8:
                return True
        return False

    def is_ours(self, ad_id: str) -> Optional[str]:
        """Причина, почему объявление наше, или None."""
        if ad_id in self.own_ids:
            return "наш ad_id"
        return self.own_by_location.get(ad_id)

    def last_seen(self, ad_id: str) -> Optional[str]:
        """Когда объявление последний раз видели в выдаче (scout или наши прогоны)."""
        a = self.serp_seen.get(ad_id)
        b = (self.market.get(ad_id) or {}).get("last_seen_at")
        return max([x for x in (a, b) if x], default=None)

    def liveness(self, ad_id: str) -> str:
        """seen — видели в выдаче за SEEN_LIVE_DAYS; probable — письма/склад недавно и не удалено;
        gone — удалено/давно не видно; unknown — данных нет."""
        inv = self.inv_ads.get(ad_id) or {}
        msg = self.msg_ads.get(ad_id) or {}
        if inv.get("state") == "deleted" or msg.get("state") == "deleted":
            return "gone"
        if (self.briefs.get(ad_id) or {}).get("sold_at"):
            return "gone"
        age = _iso_age_days(self.last_seen(ad_id), self.now)
        if age is not None and age <= SEEN_LIVE_DAYS:
            return "seen"
        recent = _iso_age_days(inv.get("last_seen_at") or msg.get("last_msg_at"), self.now)
        if recent is not None and recent <= PROBABLE_LIVE_DAYS:
            # Видели в выдаче давно, хотя запросы шли — скорее удалено/перепощено.
            if age is not None and age > SEEN_LIVE_DAYS * 2 and recent > SEEN_LIVE_DAYS * 2:
                return "gone"
            return "probable"
        if age is not None and age <= PROBABLE_LIVE_DAYS and (self.market.get(ad_id) or {}).get("active"):
            return "probable"
        return "unknown" if age is None and recent is None else "gone"

    def account_of(self, ad_id: str) -> Optional[int]:
        acc = (self.inv_ads.get(ad_id) or {}).get("account_id") or (self.msg_ads.get(ad_id) or {}).get("account_id")
        if acc:
            return acc
        plz = (self.market.get(ad_id) or {}).get("plz")
        return self.city_account.get(plz) if plz else None

    def ad_view(self, ad_id: str) -> dict[str, Any]:
        """Наше объявление для ответа api."""
        m = self.market.get(ad_id) or {}
        msg = self.msg_ads.get(ad_id) or {}
        inv = self.inv_ads.get(ad_id) or {}
        acc = self.account_of(ad_id)
        price = inv.get("price_eur") or msg.get("price_eur") or m.get("price_eur") \
            or parse_price((self.briefs.get(ad_id) or {}).get("ad_price"))
        first = min([x for x in (inv.get("first_seen_at"), msg.get("first_msg_at"), m.get("first_seen_at")) if x],
                    default=None)
        inq = msg.get("inquiry_times") or []
        return {
            "ad_id": ad_id, "title": self._own_title(ad_id), "price_eur": price,
            "url": inv.get("url") or msg.get("url") or m.get("url")
            or f"https://www.kleinanzeigen.de/s-anzeige/x/{ad_id}-0-0",
            "account_id": acc, "account": self.accounts.get(acc) if acc else None,
            "city": m.get("city"), "plz": m.get("plz"),
            "live": self.liveness(ad_id), "last_seen_at": self.last_seen(ad_id),
            "first_seen_at": first, "inquiries": len(inq), "last_inquiry_at": inq[-1] if inq else None,
            "description": msg.get("description") or m.get("description") or "",
            "ours_by": self.is_ours(ad_id),
        }


def _item_key(title: str) -> str:
    return "k:" + db_market.text_hash(match_key(title))


def own_items(ctx: Optional[_Ctx] = None, include_gone: bool = False) -> list[dict[str, Any]]:
    """Наши товары. Со складом — товары inv_products (+ себестоимость/остаток из product_profile),
    без склада — группы наших объявлений по одинаковому match_key.

    Элемент: {item_id ('p:<id>' | 'k:<hash>'), product_id|None, title, category, features, price_eur,
    min_price_eur, unit_cost_eur, stock_qty|None, ads: [ad_view…] (живые первыми), live_ads: int}.
    Наши объявления, найденные только в выдаче (город + заголовок), добавляются к товару с тем же ключом.
    """
    ctx = ctx or _Ctx()
    cache_key = "items_all" if include_gone else "items_live"
    if cache_key in ctx.cache:
        return ctx.cache[cache_key]
    items: dict[str, dict[str, Any]] = {}
    key_to_item: dict[str, str] = {}
    if ctx.has_inv and _inv is not None:
        try:
            profiles = _inv.list_product_profiles()
        except Exception:  # noqa: BLE001
            logger.exception("market: склад недоступен — товары по заголовкам объявлений")
            profiles = []
        for p in profiles:
            iid = f"p:{p['product_id']}"
            items[iid] = {
                "item_id": iid, "product_id": p["product_id"], "title": p["title"],
                "price_eur": p.get("list_price_eur"), "min_price_eur": p.get("min_price_eur"),
                "unit_cost_eur": p.get("unit_cost_eur"),
                "stock_qty": (p.get("stock") or {}).get("qty") if (p.get("stock") or {}).get("tracked") else None,
                "description": p.get("description") or "", "ad_ids": [l["ad_id"] for l in p.get("listings") or []],
            }
            for t in p.get("titles") or [p["title"]]:
                key_to_item.setdefault(match_key(t), iid)
    for aid in sorted(ctx.own_ids | set(ctx.own_by_location)):
        if any(aid in it["ad_ids"] for it in items.values()):
            continue
        title = ctx._own_title(aid)
        if not title:
            continue
        k = match_key(title)
        iid = key_to_item.get(k) or _item_key(title)
        key_to_item.setdefault(k, iid)
        it = items.setdefault(iid, {
            "item_id": iid, "product_id": None, "title": title, "price_eur": None, "min_price_eur": None,
            "unit_cost_eur": None, "stock_qty": None, "description": "", "ad_ids": []})
        it["ad_ids"].append(aid)
    out = []
    order = {"seen": 0, "probable": 1, "unknown": 2, "gone": 3}
    for it in items.values():
        ads = [ctx.ad_view(a) for a in it.pop("ad_ids")]
        ads.sort(key=lambda a: (order[a["live"]], -(_ts(a["last_seen_at"]) or 0)))
        live = [a for a in ads if a["live"] in ("seen", "probable")]
        if not include_gone and not live:
            continue
        lead = live[0] if live else (ads[0] if ads else None)
        it["ads"] = ads
        it["live_ads"] = len(live)
        if lead:
            it["price_eur"] = it["price_eur"] or lead["price_eur"]
            it["description"] = it["description"] or lead["description"]
            if not it["product_id"]:
                it["title"] = lead["title"] or it["title"]
        if it["min_price_eur"] is None:
            mins = [(ctx.briefs.get(a["ad_id"]) or {}).get("min_price_eur") for a in ads]
            it["min_price_eur"] = next((m for m in mins if m), None)
        it["features"] = regex_features(it["title"], it["description"])
        it["category"] = it["features"]["category"]
        out.append(it)
    out.sort(key=lambda i: (-i["live_ads"], i["title"]))
    ctx.cache[cache_key] = out
    return out


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    ta, tb = _ts(a), _ts(b)
    return (tb - ta) / 86400 if ta is not None and tb is not None else None


def _ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "").split("+")[0]).timestamp()
    except ValueError:
        return None


def _find_item(ctx: _Ctx, product_id: Optional[int] = None, ad_id: Optional[str] = None,
               item_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    items = own_items(ctx, include_gone=True)
    for it in items:
        if item_id and it["item_id"] == item_id:
            return it
        if product_id is not None and it["product_id"] == int(product_id):
            return it
        if ad_id and any(a["ad_id"] == str(ad_id) for a in it["ads"]):
            return it
    return None


# ══════════════════════════════════════════════════════════════════════════════
# ЦЕНОВОЙ СОВЕТНИК
# ══════════════════════════════════════════════════════════════════════════════

def _percentile(values: list[float], q: float) -> float:
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = (len(vs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def _round_price(p: float) -> float:
    """Цена «как у людей»: до 10 € (до 5 € ниже 100 €)."""
    step = 5 if p < 100 else 10
    return float(int(round(p / step)) * step)


def _compatible(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str]:
    """Сопоставимы ли товар a и объявление рынка b по признакам. (ok, почему нет)."""
    if a["category"] != b["category"]:
        return False, "другая категория"
    for k in ("seats",):
        if a.get(k) and b.get(k) and a[k] != b[k]:
            return False, "другое число мест"
    if bool(a.get("vip")) != bool(b.get("vip")):
        return False, "VIP / не VIP"
    ma, mb = a.get("material"), b.get("material")
    if ma and mb and (ma == "leder") != (mb == "leder"):
        return False, "кожа / не кожа"
    if (a.get("condition") == "defekt") != (b.get("condition") == "defekt"):
        return False, "дефект"
    return True, ""


def _closeness(a: dict[str, Any], b: dict[str, Any], a_title: str, b_title: str) -> float:
    """0..1 — насколько объявление похоже на товар (для сортировки «похожих»)."""
    s = 0.0
    if a.get("material") and a.get("material") == b.get("material"):
        s += 0.25
    if a.get("seats") and a.get("seats") == b.get("seats"):
        s += 0.2
    if set(a.get("models") or []) & set(b.get("models") or []):
        s += 0.25
    if (a.get("condition") == "neu") == (b.get("condition") == "neu"):
        s += 0.1
    ca, cb = core_tokens(a_title), core_tokens(b_title)
    if ca and cb:
        s += 0.2 * len(ca & cb) / len(ca | cb)
    return round(s, 3)


def _comparables(ctx: _Ctx, feats: dict[str, Any], title: str) -> list[dict[str, Any]]:
    """Сопоставимые конкуренты + сколько НАШИХ объявлений выкинуто из этого же набора."""
    cutoff = (ctx.now - timedelta(days=COMPARABLE_DAYS)).isoformat()
    if "market_feats" not in ctx.cache:
        pool = [m for m in ctx.market.values()
                if m["ek"] == "part" and m["price_eur"] and m["price_eur"] >= 20
                and (m["last_seen_at"] or "") >= cutoff]
        ctx.cache["market_pool"] = pool
        ctx.cache["market_feats"] = features_for(pool)
    pool, mf = ctx.cache["market_pool"], ctx.cache["market_feats"]
    out = []
    excluded_own = 0
    for m in pool:
        f = mf[m["ad_id"]]
        ok, _ = _compatible(feats, f)
        if not ok:
            continue
        if ctx.is_ours(m["ad_id"]):
            excluded_own += 1
            continue
        out.append({**m, "features": f, "closeness": _closeness(feats, f, title, m["title"] or "")})
    out.sort(key=lambda x: (-x["closeness"], -(_ts(x["last_seen_at"]) or 0)))
    ctx.cache["last_excluded_own"] = excluded_own
    return out


def _market_stale_days(ctx: _Ctx) -> Optional[float]:
    """Дней с последнего обновления рынка запчастей (последний last_seen_at). None — рынок пуст."""
    last = max((m["last_seen_at"] for m in ctx.market.values() if m["ek"] == "part" and m["last_seen_at"]),
               default=None)
    age = _iso_age_days(last, ctx.now)
    return round(age, 1) if age is not None else None


def advise_price(product_id: Optional[int] = None, ad_id: Optional[str] = None,
                 title: Optional[str] = None, price_eur: Optional[float] = None,
                 description: Optional[str] = None, item_id: Optional[str] = None,
                 now: Optional[datetime] = None, _ctx: Optional[_Ctx] = None) -> dict[str, Any]:
    """Цена товара относительно рынка (только чистые конкуренты: наши объявления отброшены).

    Товар задаётся product_id (склад), ad_id (наше объявление), item_id (из own_items) или
    title(+price_eur, description) — для произвольного текста.

    Возвращает:
    {
      "ok": bool, "status": "ok" | "few_data" | "not_found",
      "item": {"item_id", "product_id", "title", "price_eur", "unit_cost_eur", "min_price_eur"} | None,
      "features": {category, material, vip, seats, condition, models, …},
      "n": int — сопоставимых конкурентов (активные + ушедшие за 60 дн.), "n_active": int,
      "p25", "median", "p75": float | None (None при n < 3),
      "recommended_eur": float | None — «выставить за» (медиана; новое +10 %),
      "min_eur": float | None — «минимум» = max(себестоимость × (1 + маржа), P25),
      "min_basis": "cost" | "p25" | "cost_only" | None,
      "delta_pct": float | None — наша цена относительно медианы, %,
      "verdict": "above" | "below" | "in_market" | None,
      "verdict_ru": str — «дороже рынка на 18 %» / «в рынке» / «мало данных…»,
      "similar": [{"ad_id", "title", "price_eur", "city", "url", "active", "closeness",
                   "price_change": [{"price_eur","seen_at"}…] | None}] — до 5 самых похожих,
      "excluded_own": int — сколько наших объявлений выкинуто из рынка
    }
    """
    ctx = _ctx or _Ctx(now)
    item = None
    if product_id is not None or ad_id or item_id:
        item = _find_item(ctx, product_id=product_id, ad_id=ad_id, item_id=item_id)
        if item is None and not title:
            return {"ok": False, "status": "not_found", "item": None, "verdict_ru": "товар не найден"}
    if item:
        title = title or item["title"]
        price_eur = price_eur if price_eur is not None else item["price_eur"]
        description = description if description is not None else item["description"]
    feats = regex_features(title, description)
    comps = _comparables(ctx, feats, title or "")
    prices = [c["price_eur"] for c in comps]
    hist = db_market.price_changes([c["ad_id"] for c in comps[:5]])
    similar = [{
        "ad_id": c["ad_id"], "title": c["title"], "price_eur": c["price_eur"], "city": c["city"],
        "url": c["url"], "active": bool(c["active"]), "closeness": c["closeness"],
        "price_change": hist.get(c["ad_id"]),
    } for c in comps[:5]]
    excluded_own = ctx.cache.get("last_excluded_own", 0)
    stale = _market_stale_days(ctx)
    res: dict[str, Any] = {
        "ok": True, "status": "ok", "features": feats, "n": len(comps),
        "n_active": sum(1 for c in comps if c["active"]), "similar": similar, "excluded_own": excluded_own,
        "item": None if not item else {k: item.get(k) for k in (
            "item_id", "product_id", "title", "price_eur", "unit_cost_eur", "min_price_eur")},
        "p25": None, "median": None, "p75": None, "recommended_eur": None, "min_eur": None,
        "min_basis": None, "delta_pct": None, "verdict": None, "market_stale_days": stale,
    }
    if not item:
        res["item"] = {"item_id": None, "product_id": None, "title": title, "price_eur": price_eur,
                       "unit_cost_eur": None, "min_price_eur": None}
    margin = config.get("market_min_margin_pct", None)
    try:
        margin_pct = float(margin) if margin not in (None, "") else DEFAULT_MARGIN_PCT
    except ValueError:
        logger.warning("market: market_min_margin_pct=%r не число — беру %s", margin, DEFAULT_MARGIN_PCT)
        margin_pct = DEFAULT_MARGIN_PCT
    cost = item.get("unit_cost_eur") if item else None
    cost_floor = _round_price(cost * (1 + margin_pct / 100)) if cost else None
    if len(prices) < MIN_COMPARABLE:
        res["status"] = "few_data"
        res["verdict_ru"] = f"мало данных: сопоставимых объявлений {len(prices)} — смотрите ссылки"
        if stale is not None and stale > 3:
            res["verdict_ru"] += f" (рынок не обновлялся {int(stale)} дн. — scout стоит?)"
        if cost_floor:
            res["min_eur"], res["min_basis"] = cost_floor, "cost_only"
        return res
    p25, med, p75 = _percentile(prices, 0.25), statistics.median(prices), _percentile(prices, 0.75)
    res.update({"p25": round(p25), "median": round(med), "p75": round(p75)})
    neu_share = sum(1 for c in comps if c["features"].get("condition") == "neu") / len(comps)
    rec = med * (1.10 if feats.get("condition") == "neu" and neu_share < 0.5 else 1.0)
    res["recommended_eur"] = _round_price(rec)
    if cost_floor and cost_floor > p25:
        res["min_eur"], res["min_basis"] = cost_floor, "cost"
    else:
        res["min_eur"], res["min_basis"] = _round_price(p25), "p25"
    if res["recommended_eur"] < res["min_eur"]:
        res["recommended_eur"] = res["min_eur"]  # себестоимость с маржой выше рынка
    if price_eur:
        delta = (price_eur - med) / med * 100
        res["delta_pct"] = round(delta)
        if price_eur > p75 or price_eur > med * 1.15:
            res["verdict"], res["verdict_ru"] = "above", f"дороже рынка на {round(delta)} %"
        elif price_eur < p25:
            res["verdict"], res["verdict_ru"] = "below", f"дешевле рынка на {abs(round(delta))} %"
        else:
            res["verdict"], res["verdict_ru"] = "in_market", "в рынке"
    else:
        res["verdict_ru"] = "нашей цены нет — ориентир по рынку"
    return res


# ══════════════════════════════════════════════════════════════════════════════
# ПОЗИЦИЯ В ВЫДАЧЕ
# ══════════════════════════════════════════════════════════════════════════════

# Категория → как её ищут покупатели (первое слово поискового запроса)
_SEARCH_WORD = {
    "sitzbank": "sitzbank", "einzelsitz": "einzelsitz", "sitze": "sitze", "fahrersitze": "beifahrersitz",
    "komplettsatz": "sitze", "tisch": "tisch", "ahk": "anhängerkupplung", "sitzschienen": "sitzschiene",
    "raeder": "felgen", "gurte": "gurt", "leuchten": "rückleuchte", "dach": "dachreling",
    "ladekabel": "ladekabel",
}
_MODEL_SEARCH = {"traveller": "traveller", "spacetourer": "spacetourer", "proace_verso": "proace",
                 "proace_city": "proace city", "zafira_life": "zafira life", "expert": "expert",
                 "jumpy": "jumpy", "vivaro": "vivaro", "scudo": "scudo", "ulysse": "ulysse"}


def search_phrase(item: dict[str, Any]) -> Optional[str]:
    """Как покупатель ищет этот товар: «<что> <модель>», например «sitzbank traveller»."""
    word = _SEARCH_WORD.get(item.get("category") or "")
    if not word:
        return None
    models = [m for m in (item.get("features") or {}).get("models") or [] if m in _MODEL_SEARCH]
    return f"{word} {_MODEL_SEARCH[models[0]]}" if models else word


def _query_matches(keywords: str, title: str) -> bool:
    """Запрос относится к товару, если все его слова есть в заголовке."""
    kw = [t for t in tokens(keywords) if t not in _STOP]
    tt = set(tokens(title))
    return bool(kw) and all(t in tt for t in kw)


def sync_market_queries(ctx: Optional[_Ctx] = None, max_queries: int = RANK_MAX_QUERIES) -> dict[str, list[str]]:
    """Завести наши поисковые запросы по живым товарам. Запрос, который scout уже гоняет
    на ≥ RANK_MAX_PAGES страниц, не дублируем. Лимит max_queries — по включённым запросам.

    Возвращает {"added": [фразы], "untracked": [названия товаров без запроса]} — товары, для
    которых нет поискового слова (категория other) или не хватило лимита; их позиция — no_data.
    """
    ctx = ctx or _Ctx()
    with get_conn() as conn:
        scout_kw = {" ".join(r["keywords"].lower().split()) for r in conn.execute(
            "SELECT keywords FROM scout_queries WHERE enabled=1 AND kind='part' AND max_pages >= ?",
            (RANK_MAX_PAGES,))}
    enabled = {q["keywords"] for q in db_market.list_market_queries(only_enabled=True)}
    known = {q["keywords"] for q in db_market.list_market_queries(only_enabled=False)}
    wanted: dict[str, list[str]] = {}
    untracked: list[str] = []
    for it in own_items(ctx):
        ph = search_phrase(it)
        if not ph:
            untracked.append(it["title"])
        elif ph not in scout_kw:
            wanted.setdefault(ph, []).append(it["title"])
    added = []
    for ph, titles in sorted(wanted.items(), key=lambda x: -len(x[1])):
        if ph in known:
            continue  # уже есть (включённый или выключенный оператором — не возвращаем)
        if len(enabled) >= max_queries:
            untracked.extend(titles)
            continue
        db_market.upsert_market_query(ph, note="авто: по нашим товарам")
        enabled.add(ph)
        known.add(ph)
        added.append(ph)
    if untracked:
        logger.warning("market: позицию не отслеживаем для %d товаров (нет поискового слова или лимит "
                       "%d запросов): %s", len(untracked), max_queries, "; ".join(untracked[:10]))
    return {"added": added, "untracked": untracked}


def run_rank_check(force: bool = False, max_queries: int = RANK_MAX_QUERIES,
                   page_delay: tuple[float, float] = (4.0, 8.0)) -> dict[str, Any]:
    """Прогнать наши поисковые запросы (публичная выдача, без логина) и записать места.

    Безопасность: не чаще раза в RANK_MIN_INTERVAL_H часов (force — игнорировать), не больше
    max_queries запросов по ≤ 3 страницы, пауза 4–8 с между страницами и запросами.
    Новые для scout объявления добавляются в scout_listings (уже известные не трогаем).
    Пустая выдача и недогруженные страницы — в errors (прогон с 0 карточек не засчитывается).
    Возвращает {ran, skipped, pages, cards, errors: [..], added_queries: [..], untracked: [..]}.
    """
    from modules import scout
    import database as db
    import httpx
    res: dict[str, Any] = {"ran": 0, "skipped": None, "pages": 0, "cards": 0, "errors": [], "added_queries": []}
    db_market.ensure_schema()
    if not force:
        recent = [r for r in db_market.runs_since(
            (datetime.utcnow() - timedelta(hours=RANK_MIN_INTERVAL_H)).isoformat()) if r["source"] == "market"]
        if recent:
            res["skipped"] = f"последний прогон {recent[0]['started_at'][:16]} UTC — рано"
            return res
    try:
        sq = sync_market_queries()
        res["added_queries"], res["untracked"] = sq["added"], sq["untracked"]
    except Exception as e:  # noqa: BLE001
        logger.exception("market: не удалось обновить список запросов")
        res["errors"].append(f"sync_queries: {e}")
    queries = db_market.list_market_queries()[:max_queries]
    if not queries:
        res["skipped"] = "нет запросов"
        logger.warning("market: прогон позиций — нет ни одного нашего запроса (живых товаров нет?)")
        return res
    browser = scout._BrowserFallback()
    try:
        with httpx.Client(headers=scout._HEADERS, timeout=30.0, follow_redirects=True) as client:
            for q in queries:
                started = datetime.utcnow().isoformat()
                stats: dict[str, Any] = {"pages": 0}
                qd = {"id": q["id"], "kind": "part", "category": q["category"], "keywords": q["keywords"],
                      "max_pages": min(int(q["max_pages"] or RANK_MAX_PAGES), RANK_MAX_PAGES)}
                try:
                    rows = scout.scrape_query(client, qd, page_delay_sec=random.uniform(*page_delay),
                                              browser=browser, stats=stats)
                except Exception as e:  # noqa: BLE001
                    logger.warning("market: запрос «%s» не загрузился: %s", q["keywords"], e)
                    res["errors"].append(f"{q['keywords']}: {e}")
                    db_market.record_scrape(source="market", keywords=q["keywords"], category=q["category"],
                                            query_ref=q["id"], rows=[], pages=0, error=str(e)[:500],
                                            started_at=started)
                    continue
                if not rows:
                    # Страница «похожа на выдачу», но карточек 0: либо запрос пуст, либо сломался
                    # парсер. Прогон не засчитываем, чтобы «нас нет в выдаче» не стало ложным сигналом.
                    res["errors"].append(f"{q['keywords']}: 0 карточек")
                    db_market.record_scrape(source="market", keywords=q["keywords"], category=q["category"],
                                            query_ref=q["id"], rows=[], pages=0, error="0 карточек",
                                            started_at=started)
                    continue
                if stats.get("partial_error"):
                    res["errors"].append(f"{q['keywords']}: {stats['partial_error']}")
                _insert_new_market_listings(rows)
                db_market.record_scrape(source="market", keywords=q["keywords"], category=q["category"],
                                        query_ref=q["id"], rows=rows, pages=stats["pages"],
                                        error=stats.get("partial_error"), started_at=started)
                db_market.touch_market_query(q["id"])
                res["ran"] += 1
                res["pages"] += stats["pages"]
                res["cards"] += len(rows)
                time.sleep(random.uniform(*page_delay))
    finally:
        browser.close()
    return res


def _insert_new_market_listings(rows: list[dict[str, Any]]) -> int:
    """Новые для scout объявления — в scout_listings (только вставка: строки, которые уже
    собирает scout, не трогаем — их query_id/kind и учёт «новых» остаются за scout)."""
    import database as db
    ids = [r["ad_id"] for r in rows if r.get("ad_id")]
    if not ids:
        return 0
    with get_conn() as conn:
        ph = ",".join("?" * len(ids))
        known = {r["ad_id"] for r in conn.execute(f"SELECT ad_id FROM scout_listings WHERE ad_id IN ({ph})", ids)}
    fresh = [{**r, "query_id": None} for r in rows if r.get("ad_id") and r["ad_id"] not in known]
    if fresh:
        db.upsert_scout_listings(fresh)
    return len(fresh)


def _rank_rows(ctx: _Ctx, ad_ids: set[str], titles: list[str], days: int) -> dict[str, dict[str, Any]]:
    """По каждому запросу, относящемуся к товару: последнее место наших объявлений и динамика."""
    since = (ctx.now - timedelta(days=days)).isoformat()
    runs = [r for r in db_market.runs_since(since)
            if any(_query_matches(r["keywords"], t) for t in titles)]
    serp = db_market.serp_for_runs([r["id"] for r in runs])
    by_kw: dict[str, dict[str, Any]] = {}
    for r in runs:  # от новых к старым
        rows = serp.get(r["id"], [])
        ours = [x for x in rows if x["ad_id"] in ad_ids]
        best = min(ours, key=lambda x: x["pos"]) if ours else None
        point = {"at": r["started_at"], "page": best["page"] if best else None,
                 "pos": best["pos"] if best else None,
                 # флаг TOP распознан плохо — «органическое» место не считаем
                 "organic_pos": best["organic_pos"] if best and (r.get("top_known") or 0) >= 0.9 else None,
                 "ad_id": best["ad_id"] if best else None,
                 "searched_pages": r["pages"], "total": r["cards"]}
        e = by_kw.setdefault(r["keywords"], {"keywords": r["keywords"], "history": []})
        e["history"].append(point)
    for e in by_kw.values():
        h = e["history"]
        e["latest"] = h[0]
        e["history"] = list(reversed(h))[-20:]  # по времени, последние 20
        found = [p for p in e["history"] if p["pos"]]
        e["trend"] = None
        if len(found) >= 2:
            e["trend"] = found[-1]["pos"] - found[0]["pos"]  # > 0 — опускаемся
    return by_kw


STALE_RANK_DAYS = 2  # последнему замеру больше N дней — позиция устарела


def _rank_status(by_kw: dict[str, dict[str, Any]],
                 now: Optional[datetime] = None) -> tuple[str, Optional[dict[str, Any]]]:
    """top / page1 / sinking / missing / stale / no_data + лучшая последняя точка.

    missing — только по полному прогону (≥ RANK_MAX_PAGES страниц или выдача кончилась раньше):
    мелкий прогон scout на 1 страницу не значит «нас нет на первых страницах».
    """
    if not by_kw:
        return "no_data", None
    latest = [e["latest"] for e in by_kw.values()]
    newest = max(_ts(p["at"]) or 0 for p in latest)
    if now is not None and newest and (now.timestamp() - newest) / 86400 > STALE_RANK_DAYS:
        return "stale", None
    found = [p for p in latest if p["pos"]]
    if not found:
        complete = [p for p in latest if p["searched_pages"] >= RANK_MAX_PAGES
                    or p["total"] < p["searched_pages"] * PAGE_SIZE]
        return ("missing", None) if complete else ("no_data", None)
    best = min(found, key=lambda p: p["organic_pos"] or p["pos"])
    pos = best["organic_pos"] or best["pos"]
    if best["page"] == 1 and pos <= 10:
        return "top", best
    if best["page"] == 1 and pos <= SINK_POS:
        return "page1", best
    return "sinking", best


_RANK_RU = {"top": "в топе 1-й страницы", "page1": "на 1-й странице, внизу", "sinking": "опустилось",
            "missing": "нет на первых страницах", "no_data": "позиция ещё не измерялась",
            "stale": "замер позиции устарел — прогоны выдачи не идут"}


def rank_report(product_id: Optional[int] = None, ad_id: Optional[str] = None,
                item_id: Optional[str] = None, days: int = 14,
                now: Optional[datetime] = None, _ctx: Optional[_Ctx] = None) -> dict[str, Any]:
    """Места наших объявлений в выдаче за days дней.

    Без аргументов — по всем живым товарам; иначе по одному. Возвращает:
    {"items": [{
        "item_id", "product_id", "title",
        "status": "top" | "page1" | "sinking" | "missing" | "stale" | "no_data", "status_ru": str,
        "best": {"keywords", "page", "pos", "organic_pos", "ad_id", "at"} | None,
        "queries": [{"keywords", "latest": {at, page, pos, organic_pos, ad_id, searched_pages, total},
                     "trend": int|None (>0 — опустились на N мест), "history": [точки по времени]}],
        "days_quiet": float|None — дней с последнего письма по товару,
        "live_ads": int
    }], "checked_at": str|None — последний успешный прогон любой выдачи,
       "last_error": {"at", "keywords", "error"}|None — последний прогон с ошибкой, если он новее успешного}
    """
    ctx = _ctx or _Ctx(now)
    if product_id is not None or ad_id or item_id:
        it = _find_item(ctx, product_id=product_id, ad_id=ad_id, item_id=item_id)
        items = [it] if it else []
    else:
        items = own_items(ctx)
    out = []
    for it in items:
        ids = {a["ad_id"] for a in it["ads"]}
        titles = list({it["title"], *[a["title"] for a in it["ads"] if a["title"]]})
        by_kw = _rank_rows(ctx, ids, titles, days)
        status, best_point = _rank_status(by_kw, ctx.now)
        best = None
        if best_point:
            kw = next(k for k, e in by_kw.items() if e["latest"] is best_point)
            best = {"keywords": kw, **{k: best_point[k] for k in ("page", "pos", "organic_pos", "ad_id", "at")}}
        out.append({
            "item_id": it["item_id"], "product_id": it["product_id"], "title": it["title"],
            "status": status, "status_ru": _RANK_RU[status], "best": best,
            "queries": sorted(by_kw.values(), key=lambda e: e["keywords"]),
            "days_quiet": _days_quiet(ctx, it), "live_ads": it["live_ads"],
        })
    since = (ctx.now - timedelta(days=days)).isoformat()
    runs = db_market.runs_since(since)
    checked = runs[0]["started_at"] if runs else None
    failed = [r for r in db_market.runs_since(since, include_failed=True) if r["error"]]
    last_error = None
    if failed and (checked is None or failed[0]["started_at"] > checked):
        last_error = {"at": failed[0]["started_at"], "keywords": failed[0]["keywords"], "error": failed[0]["error"]}
    return {"items": out, "checked_at": checked, "last_error": last_error}


def _days_quiet(ctx: _Ctx, it: dict[str, Any]) -> Optional[float]:
    """Дней с последнего письма по любому объявлению товара; без писем — с появления объявления."""
    last = max([a["last_inquiry_at"] for a in it["ads"] if a["last_inquiry_at"]], default=None)
    if last is None:
        last = max([a["first_seen_at"] for a in it["ads"] if a["first_seen_at"]], default=None)
    age = _iso_age_days(last, ctx.now)
    return round(age, 1) if age is not None else None


# ══════════════════════════════════════════════════════════════════════════════
# ЗАМЕТНОСТЬ
# ══════════════════════════════════════════════════════════════════════════════

# Термины, которые сравниваем у лидеров и у нас (кроме моделей авто)
_TERMS: list[tuple[str, re.Pattern]] = [
    ("2+1", re.compile(r'\b2\+1\b')), ("3er", re.compile(r'\b3er\b')), ("Leder", re.compile(r'leder')),
    ("Original", re.compile(r'\boriginal\b|\borig\b')), ("VIP", re.compile(r'\bvip\b')),
    ("Isofix", re.compile(r'isofix')), ("Schienen", re.compile(r'schiene')),
    ("neu / neuwertig", re.compile(r'\bneu\b|neuwertig')), ("Versand", re.compile(r'versand')),
    ("Sitzbank", re.compile(r'sitzbank')), ("Sitze", re.compile(r'\bsitze\b')),
    ("Rückbank", re.compile(r'ruckbank')), ("schwarz", re.compile(r'schwarz')),
    ("Lounge", re.compile(r'lounge')), ("klappbar", re.compile(r'klappbar')),
    ("Traveller", re.compile(r'traveller')), ("Spacetourer", re.compile(r'spacetourer|space tourer')),
    ("ProAce Verso", re.compile(r'proace')), ("Zafira Life", re.compile(r'zafira')),
    ("Expert", re.compile(r'\bexpert\b')), ("Jumpy", re.compile(r'jumpy')), ("Vivaro", re.compile(r'vivaro')),
    ("Multivan T5/T6", re.compile(r'multivan|\bt[56]\b')),
]
_MODEL_TERMS = {"Traveller", "Spacetourer", "ProAce Verso", "Zafira Life", "Expert", "Jumpy", "Vivaro",
                "Multivan T5/T6"}


def _terms_in(text: Optional[str]) -> set[str]:
    s = " ".join(tokens(text))
    return {name for name, rx in _TERMS if rx.search(s)}


def _repost_pairs(ctx: _Ctx) -> set[str]:
    """ad_id рынка, которые «исчезли», но вернулись с тем же заголовком и PLZ (перепост, не продажа)."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in ctx.market.values():
        if m["ek"] != "part" or not m["title"]:
            continue
        by_key.setdefault((match_key(m["title"]), m["plz"] or ""), []).append(m)
    out = set()
    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda m: m["first_seen_at"] or "")
        for m in group[:-1]:
            out.add(m["ad_id"])
    return out


def leaders_for(ctx: _Ctx, it: dict[str, Any], days: int = 30) -> dict[str, list[dict[str, Any]]]:
    """Лидеры: (а) конкуренты выше нашего объявления в последних прогонах; (б) быстро ушедшие
    похожие объявления (исчезли за ≤ 7 дней и это не перепост)."""
    ids = {a["ad_id"] for a in it["ads"]}
    titles = list({it["title"], *[a["title"] for a in it["ads"] if a["title"]]})
    since = (ctx.now - timedelta(days=days)).isoformat()
    runs = [r for r in db_market.runs_since(since) if any(_query_matches(r["keywords"], t) for t in titles)]
    latest_by_kw: dict[str, dict[str, Any]] = {}
    for r in runs:
        latest_by_kw.setdefault(r["keywords"], r)
    serp = db_market.serp_for_runs([r["id"] for r in latest_by_kw.values()])
    above: dict[str, dict[str, Any]] = {}
    for rid, rows in serp.items():
        our_pos = min([x["pos"] for x in rows if x["ad_id"] in ids], default=None)
        limit = our_pos if our_pos else PAGE_SIZE + 1  # нас нет — лидеры = первая страница
        for x in rows:
            if x["pos"] >= limit or ctx.is_ours(x["ad_id"]) or x["ad_id"] in ids:
                continue
            m = ctx.market.get(x["ad_id"]) or {}
            if detect_category(x["title"]) != it["category"]:
                continue
            above.setdefault(x["ad_id"], {"ad_id": x["ad_id"], "title": x["title"], "price_eur": x["price_eur"],
                                          "pos": x["pos"], "city": x["city"], "url": m.get("url"),
                                          "description": m.get("description") or ""})
    reposted = _repost_pairs(ctx)
    fast = []
    cutoff = (ctx.now - timedelta(days=90)).isoformat()
    for m in ctx.market.values():
        if m["ek"] != "part" or m["active"] or not m["deactivated_at"] or ctx.is_ours(m["ad_id"]):
            continue
        if (m["deactivated_at"] or "") < cutoff or m["ad_id"] in reposted:
            continue
        life = _days_between(m["first_seen_at"], m["last_seen_at"])
        if life is None or life > FAST_GONE_DAYS:
            continue
        if detect_category(m["title"]) != it["category"]:
            continue
        fast.append({"ad_id": m["ad_id"], "title": m["title"], "price_eur": m["price_eur"], "city": m["city"],
                     "url": m["url"], "days_live": round(life, 1), "description": m["description"] or ""})
    return {"above": sorted(above.values(), key=lambda x: x["pos"]), "fast": fast[:20]}


_VIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["titles", "first_lines", "notes_ru"],
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}, "description": "3 варианта заголовка DE ≤ 65 симв."},
        "first_lines": {"type": "array", "items": {"type": "string"}, "description": "2 варианта первой строки описания DE"},
        "notes_ru": {"type": "array", "items": {"type": "string"}, "description": "2–4 коротких совета по-русски"},
    },
}
_VIS_SYSTEM = (
    "Ты помогаешь продавцу б/у автозапчастей на Kleinanzeigen.de сделать объявление заметнее в поиске. "
    "Выдача сортируется по свежести, покупатели ищут по словам заголовка. Дано: наш заголовок и начало "
    "описания, заголовки конкурентов выше нас и быстро проданных, слова, которые есть у лидеров и нет у нас. "
    "Предложи 3 заголовка на немецком (≤ 65 символов, без КАПСА и эмодзи, без выдуманных фактов: добавляй "
    "только то, что следует из нашего текста; модели авто — только если они есть в нашем тексте), 2 варианта "
    "первой строки описания (главное — что это, состояние, для каких авто) и 2–4 коротких совета по-русски."
)


def visibility_tips(product_id: Optional[int] = None, ad_id: Optional[str] = None,
                    item_id: Optional[str] = None, use_llm: bool = True,
                    now: Optional[datetime] = None, _ctx: Optional[_Ctx] = None) -> dict[str, Any]:
    """Как стать заметнее: слова лидеров, которых нет у нас, + варианты заголовка/первой строки.

    Возвращает:
    {"ok", "item": {item_id, product_id, title}, "our_terms": [..],
     "leaders": {"above": [{ad_id,title,price_eur,pos,city,url}], "fast": [{…, days_live}]},
     "missing_terms": [{"term", "share": 0..1, "is_model": bool}] — есть у ≥ 30 % лидеров (и ≥ 2), нет у нас,
     "title_len": {"ours": int, "leaders_median": int|None},
     "leader_first_lines": [str] — начало описаний лидеров (до 3),
     "suggestions": {"titles": [..], "first_lines": [..], "notes_ru": [..], "source": "llm"|"rules"|"cache"},
     "warning": str|None}
    Модели авто из missing_terms советуем добавить только если товар к ним подходит — это проверяет человек.
    """
    ctx = _ctx or _Ctx(now)
    it = _find_item(ctx, product_id=product_id, ad_id=ad_id, item_id=item_id)
    if not it:
        return {"ok": False, "warning": "товар не найден"}
    lead = leaders_for(ctx, it)
    leaders = lead["above"] + lead["fast"]
    our_text = f"{it['title']} {(it['description'] or '')[:200]}"
    ours = _terms_in(it["title"])
    ours_any = _terms_in(our_text)
    counts: dict[str, int] = {}
    for l in leaders:
        for t in _terms_in(l["title"]):
            counts[t] = counts.get(t, 0) + 1
    n = len(leaders)
    missing = []
    for t, c in sorted(counts.items(), key=lambda x: -x[1]):
        if t in ours or c < 2 or c / max(n, 1) < 0.3:
            continue
        missing.append({"term": t, "share": round(c / n, 2), "is_model": t in _MODEL_TERMS,
                        "in_our_description": t in ours_any})
    lens = [len(l["title"] or "") for l in leaders if l["title"]]
    first_lines = []
    for l in leaders:
        line = (l["description"] or "").strip().split("\n")[0][:140]
        if line and line not in first_lines:
            first_lines.append(line)
        if len(first_lines) >= 3:
            break
    res: dict[str, Any] = {
        "ok": True, "item": {"item_id": it["item_id"], "product_id": it["product_id"], "title": it["title"]},
        "our_terms": sorted(ours), "leaders": {
            "above": [{k: x[k] for k in ("ad_id", "title", "price_eur", "pos", "city", "url")} for x in lead["above"][:8]],
            "fast": [{k: x[k] for k in ("ad_id", "title", "price_eur", "days_live", "city", "url")} for x in lead["fast"][:8]]},
        "missing_terms": missing,
        "title_len": {"ours": len(it["title"]), "leaders_median": int(statistics.median(lens)) if lens else None},
        "leader_first_lines": first_lines, "warning": None,
    }
    rules = _rule_titles(it, missing)
    res["suggestions"] = {"titles": rules, "first_lines": [], "notes_ru": [], "source": "rules"}
    if not leaders:
        res["warning"] = "лидеров для сравнения нет: позиция ещё не измерялась или рынок пуст"
        return res
    if use_llm:
        key = "vis:" + db_market.text_hash(it["title"], it["description"][:300],
                                           "|".join(sorted(l["ad_id"] for l in leaders)))
        # Sonnet не чаще раза в сутки на товар: даже если лидеры сменились — отдаём последний совет
        cached = db_market.cache_get(key, max_age_days=14) or \
            db_market.cache_get(f"vis_last:{it['item_id']}", max_age_days=1)
        if cached:
            res["suggestions"] = {**cached, "source": "cache"}
            return res
        try:
            from modules import claude_utils
            user = (f"Наш заголовок: {it['title']}\nНаше описание (начало): {(it['description'] or '')[:400]}\n\n"
                    f"Лидеры выше нас:\n" + "\n".join(f"- {x['title']} ({x['price_eur']} €)" for x in lead["above"][:10])
                    + "\n\nБыстро ушли:\n" + "\n".join(f"- {x['title']}" for x in lead["fast"][:8])
                    + "\n\nНачало описаний лидеров:\n" + "\n".join(f"- {x}" for x in first_lines)
                    + "\n\nЕсть у лидеров, нет у нас: " + ", ".join(m["term"] for m in missing))
            data, _meta = claude_utils.call_json(
                label="market_visibility", model=claude_utils.smart_model(), system=_VIS_SYSTEM, user=user,
                schema=_VIS_SCHEMA, max_tokens=1200)
            data["titles"] = [t[:TITLE_MAX] for t in data.get("titles") or [] if t.strip()] or rules
            db_market.cache_set(key, "visibility", data)
            db_market.cache_set(f"vis_last:{it['item_id']}", "visibility", data)
            res["suggestions"] = {**data, "source": "llm"}
        except Exception as e:  # noqa: BLE001
            logger.warning("market: LLM-советы по заметности не получены: %s", e)
            res["warning"] = f"LLM недоступен — варианты по правилам ({e})"
    return res


def _rule_titles(it: dict[str, Any], missing: list[dict[str, Any]]) -> list[str]:
    """Варианты заголовка без LLM: наш + недостающие НЕ-модельные термины, ≤ 65 символов."""
    add = [m["term"] for m in missing if not m["is_model"] and m["term"] not in ("Versand", "neu / neuwertig")]
    base = it["title"]
    out = []
    for i in range(1, len(add) + 1):
        cand = f"{base} {' '.join(add[:i])}".strip()
        if len(cand) <= TITLE_MAX and cand not in out:
            out.append(cand)
    return out[-2:] if out else []


# ══════════════════════════════════════════════════════════════════════════════
# ДУБЛИ МЕЖДУ АККАУНТАМИ
# ══════════════════════════════════════════════════════════════════════════════

def _strip(ad: dict[str, Any]) -> dict[str, Any]:
    """ad_view без описания — для ответов api (описание длинное и там не нужно)."""
    return {k: v for k, v in ad.items() if k != "description"}


def _dup_group(ctx: _Ctx, it: dict[str, Any]) -> Optional[dict[str, Any]]:
    live = [a for a in it["ads"] if a["live"] in ("seen", "probable")]
    if len(live) < 2:
        return None
    accounts = {a["account_id"] for a in live if a["account_id"]}
    keys = [match_key(a["title"]) for a in live]
    same_text = len(keys) - len(set(keys))          # сколько объявлений повторяют уже встреченный текст
    prices = [a["price_eur"] for a in live if a["price_eur"]]
    same_price = len(prices) - len(set(prices))
    cities = {a["city"] for a in live if a["city"]}
    n = len(live)
    score = (n - 1) * 2 + max(0, len(accounts) - 1) * 3 + same_text * 2 + same_price
    reasons = [f"{n} объявлений одновременно"]
    if len(accounts) > 1:
        reasons.append(f"на {len(accounts)} аккаунтах")
    if same_text:
        reasons.append("одинаковый текст" if same_text == n - 1 else f"одинаковый текст у {same_text + 1}")
    if same_price:
        reasons.append("одинаковая цена")
    if len(cities) > 1:
        reasons.append(f"в {len(cities)} городах")
    level = "danger" if (score >= 5 or n >= 3 or (len(accounts) > 1 and same_text)) else "warn"
    # «Наше» только по PLZ+заголовку (без ad_id) — догадка: одной ею «опасно» не ставим.
    confirmed = [a for a in live if a["ours_by"] == "наш ad_id"]
    guessed = [a["ad_id"] for a in live if a["ours_by"] != "наш ad_id"]
    if guessed:
        reasons.append(f"{len(guessed)} — наши по PLZ и заголовку, проверить")
        if len(confirmed) < 2:
            level = "warn"
    # На складе несколько штук и тексты разные — несколько объявлений допустимы, но рискованно.
    stock = it.get("stock_qty")
    if stock is not None and stock >= n and not same_text and len(accounts) <= 1:
        level = "warn"
        reasons.append(f"на складе {stock} шт. — лучше одно объявление «{stock} Stück verfügbar»")
    # Оставляем одно: подтверждённое → больше писем за 14 дней → видно в выдаче → свежее.
    def keep_rank(a: dict[str, Any]) -> tuple:
        recent = 0
        for t in (ctx.msg_ads.get(a["ad_id"]) or {}).get("inquiry_times") or []:
            age = _iso_age_days(t, ctx.now)
            if age is not None and age <= 14:
                recent += 1
        return (0 if a["ours_by"] == "наш ad_id" else 1, -recent, 0 if a["live"] == "seen" else 1,
                -(_ts(a["first_seen_at"]) or 0))
    ordered = sorted(live, key=keep_rank)
    keep, remove = ordered[0], ordered[1:]
    keep, remove = _strip(keep), [_strip(r) for r in remove]
    msg = (f"{'опасно' if level == 'danger' else 'внимание'}: снимите {len(remove)} из {n} — "
           + ", ".join(f"{r['ad_id']} ({r['account'] or 'аккаунт ?'}, {r['city'] or 'город ?'})" for r in remove))
    return {
        "item_id": it["item_id"], "product_id": it["product_id"], "title": it["title"],
        "level": level, "score": score, "n_live": n, "accounts": sorted(accounts),
        "reasons_ru": reasons, "keep": keep, "remove": remove, "message_ru": msg,
        "uncertain": sorted({a["ad_id"] for a in live if a["live"] == "probable"} | set(guessed)),
        "photos_checked": False,
    }


def duplicates_report(now: Optional[datetime] = None, _ctx: Optional[_Ctx] = None) -> dict[str, Any]:
    """Один товар в нескольких живых объявлениях / аккаунтах / городах.

    Живое = видели в выдаче за 3 дня («seen») или были письма за 21 день и не «Gelöscht»
    («probable», в группе перечислены в uncertain — проверить руками). Фото не сравниваем —
    в выдаче их нет (photos_checked=False).

    Возвращает {"groups": [{item_id, product_id, title, level: "danger"|"warn", score, n_live,
                accounts: [id], reasons_ru: [..], keep: ad_view, remove: [ad_view],
                message_ru: «опасно: снимите 2 из 3 — 3537…(Аккаунт, Город), …», uncertain: [ad_id]}],
                "danger": int, "warn": int, "ads_to_remove": int}
    ad_view: {ad_id, title, price_eur, url, account_id, account, city, plz, live, last_seen_at,
              first_seen_at, inquiries, last_inquiry_at}
    """
    ctx = _ctx or _Ctx(now)
    groups = []
    for it in own_items(ctx):
        g = _dup_group(ctx, it)
        if g:
            groups.append(g)
    groups.sort(key=lambda g: (g["level"] != "danger", -g["score"]))
    return {"groups": groups, "danger": sum(1 for g in groups if g["level"] == "danger"),
            "warn": sum(1 for g in groups if g["level"] == "warn"),
            "ads_to_remove": sum(len(g["remove"]) for g in groups)}


# ══════════════════════════════════════════════════════════════════════════════
# ПЕРЕПОСТ
# ══════════════════════════════════════════════════════════════════════════════

def next_repost_window(now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """Ближайшее окно вс–ср 16–19 ч по Берлину. {"now": bool, "start", "end" (Берлин ISO), "label_ru"}."""
    now_u = now_utc or datetime.utcnow()
    if now_u.tzinfo is None:
        now_u = now_u.replace(tzinfo=ZoneInfo("UTC"))  # naive = UTC (как всё в БД)
    now_b = now_u.astimezone(BERLIN)
    h0, h1 = REPOST_WINDOW_HOURS
    for d in range(0, 8):
        day = (now_b + timedelta(days=d)).date()
        if day.weekday() not in REPOST_WINDOW_DAYS:
            continue
        start = datetime(day.year, day.month, day.day, h0, tzinfo=BERLIN)
        end = datetime(day.year, day.month, day.day, h1, tzinfo=BERLIN)
        if end <= now_b:
            continue
        inside = start <= now_b < end
        names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
        when = "сейчас" if inside else ("сегодня" if d == 0 else ("завтра" if d == 1 else names[day.weekday()]))
        return {"now": inside, "start": start.isoformat(), "end": end.isoformat(),
                "label_ru": f"{when}, {h0}–{h1} ч" if not inside else f"сейчас (до {h1}:00)"}
    return {"now": False, "start": None, "end": None, "label_ru": "—"}


def _vary_price(price: Optional[float], floor: Optional[float]) -> Optional[float]:
    """Цена для перепоста: чуть иначе текущей (−0,5…−3 %), не ниже минимума."""
    if not price:
        return None
    cands = []
    for pct in (0.005, 0.01, 0.02, 0.03):
        p = price * (1 - pct)
        for r in (_round_price(p), float(int(p / 5) * 5), float(int(p / 10) * 10 - 1 if p > 100 else int(p))):
            if r < price and (floor is None or r >= floor) and r not in cands:
                cands.append(r)
    return cands[0] if cands else price


_REPOST_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "description", "changes_ru"],
    "properties": {
        "title": {"type": "string", "description": "новый заголовок DE ≤ 65 симв."},
        "description": {"type": "string", "description": "полный текст описания DE"},
        "changes_ru": {"type": "string", "description": "что изменено, 1 строка по-русски"},
    },
}
_REPOST_SYSTEM = (
    "Ты готовишь текст для повторной публикации объявления о б/у автозапчасти на Kleinanzeigen.de. "
    "Новый текст НЕ должен быть точной копией старого (иначе площадка считает дублем): перефразируй "
    "заголовок (≤ 65 символов) и описание, поменяй порядок фактов, но НЕ меняй факты, НЕ добавляй "
    "ничего, чего нет в исходном тексте (модели авто, состояние, комплектацию, доставку). Можно добавить "
    "из списка «слова лидеров» только те, что подтверждаются исходным текстом. Немецкий, вежливо, без "
    "капса и эмодзи, без телефонов и ссылок."
)


def repost_text(it: dict[str, Any], *, price_eur: Optional[float], missing_terms: Iterable[str] = (),
                use_llm: bool = True) -> dict[str, Any]:
    """Готовый текст перепоста: {title, description, price_eur, changes_ru, source: llm|cache|rules,
    warning: str|None — показать оператору (LLM недоступен, нет описания, текст почти как старый)}."""
    title = it["title"]
    desc = it.get("description") or ""
    rules = _rule_repost(it, list(missing_terms))
    base = {"price_eur": price_eur}
    if not use_llm or not desc:
        return {**rules, **base}  # без описания LLM нечего перефразировать — warning в rules
    key = "rep:" + db_market.text_hash(title, desc[:800], ",".join(missing_terms))
    cached = db_market.cache_get(key, max_age_days=7) or \
        db_market.cache_get(f"rep_last:{it['item_id']}", max_age_days=1)
    if cached:
        return {**cached, **base, "source": "cache", "warning": None}
    try:
        from modules import claude_utils
        user = (f"Старый заголовок: {title}\nСтарое описание:\n{desc[:1500]}\n\n"
                f"Слова лидеров выдачи: {', '.join(missing_terms) or '—'}")
        data, _meta = claude_utils.call_json(
            label="market_repost", model=claude_utils.smart_model(), system=_REPOST_SYSTEM, user=user,
            schema=_REPOST_SCHEMA, max_tokens=1500, nonempty=("title", "description"))
        data["title"] = data["title"].strip()[:TITLE_MAX]
        if match_key(data["title"]) == match_key(title) and data["description"].strip() == desc.strip():
            raise ValueError("LLM вернул тот же текст")
        db_market.cache_set(key, "repost", data)
        db_market.cache_set(f"rep_last:{it['item_id']}", "repost", data)
        return {**data, **base, "source": "llm", "warning": None}
    except Exception as e:  # noqa: BLE001
        logger.warning("market: LLM-текст перепоста не получен: %s — текст по правилам", e)
        return {**rules, **base, "warning": "; ".join(x for x in (f"LLM недоступен: {e}", rules["warning"]) if x)}


_FIRST_WORD = {"sitzbank": "Sitzbank", "einzelsitz": "Einzelsitz", "sitze": "Sitze", "fahrersitze": "Sitz",
               "komplettsatz": "Sitze", "tisch": "Tisch", "ahk": "Anhängerkupplung", "sitzschienen": "Sitzschienen",
               "raeder": "Felgen", "gurte": "Gurte", "leuchten": "Leuchten", "dach": "Dachreling"}
_COND_WORD = {"neu": "neu", "gebraucht": "gebraucht", "defekt": "defekt"}


def _rule_repost(it: dict[str, Any], missing_terms: list[str]) -> dict[str, Any]:
    """Вариация без LLM: другой порядок слов заголовка + недостающие НЕ-модельные термины,
    новая первая строка. Только факты из нашего же текста — ничего не выдумываем."""
    words = it["title"].split()
    f = it.get("features") or {}
    own_terms = _terms_in(f"{it['title']} {it.get('description') or ''}")
    # добавляем только термины, которые подтверждаются нашим заголовком/описанием
    add = [t for t in missing_terms if t in own_terms and t not in _MODEL_TERMS
           and t not in ("Versand", "neu / neuwertig")]
    if len(words) > 3:
        head = next((w for w in words if detect_category(w) == it.get("category")), None)
        if head:
            words = [head] + [w for w in words if w is not head]
    title = " ".join(words + add)[:TITLE_MAX].strip()
    warnings = []
    if title == it["title"] and f.get("condition") in _COND_WORD:
        title = f"{it['title']} {_COND_WORD[f['condition']]}"[:TITLE_MAX]
    if title == it["title"] and len(words) > 1:
        title = " ".join(words[1:] + words[:1])  # хотя бы другой порядок слов
    if title == it["title"]:
        warnings.append("заголовок не удалось изменить — поправьте руками")
    parts = []
    if f.get("material") == "leder":
        parts.append("Leder")
    if f.get("seats") == 3:
        parts.append("3er")
    first = " ".join([_FIRST_WORD.get(it.get("category") or "", "")] + parts).strip()
    desc = (it.get("description") or "").strip()
    if not desc:
        warnings.append("описания нет — допишите текст перед публикацией")
        description = ""
    else:
        description = f"{first}.\n\n{desc}" if first else desc
        warnings.append("текст почти как старый — перефразируйте пару предложений")
    return {"title": title, "description": description,
            "changes_ru": "порядок слов заголовка и первая строка описания изменены (без LLM)",
            "source": "rules", "warning": "; ".join(warnings) or None}


def repost_suggestions(limit: int = 5, now: Optional[datetime] = None, with_text: bool = True,
                       use_llm: bool = True, _ctx: Optional[_Ctx] = None) -> list[dict[str, Any]]:
    """Какие объявления пора переопубликовать (делает человек) и когда.

    Сигналы: объявление опустилось (не в первых 15 местах / не найдено на первых страницах)
    и/или по товару нет писем 5+ дней. Не предлагаем: объявлению < 4 дней, товара нет на складе,
    товар продан. Если у товара уже несколько живых объявлений — сначала снять лишние
    (action="remove_duplicates"), перепост не советуем, чтобы не плодить дубли.

    Элемент: {
      "item_id", "product_id", "title", "priority": float (больше — важнее),
      "action": "repost" | "remove_duplicates",
      "reasons_ru": [..], "rank_status": str, "days_quiet": float|None,
      "old_ad": ad_view — какое объявление удалить перед публикацией нового,
      "window": {"now", "start", "end", "label_ru"} — когда публиковать (вс–ср 16–19 по Берлину),
      "text": {"title", "description", "price_eur", "changes_ru", "source"} | None,
      "price": {"current", "suggested", "advice_ru"},
      "duplicates": dup-group | None,
      "steps_ru": [..] — что сделать по шагам
    }
    """
    ctx = _ctx or _Ctx(now)
    window = next_repost_window(ctx.now)
    out = []
    for it in own_items(ctx):
        live = [a for a in it["ads"] if a["live"] in ("seen", "probable")]
        if not live:
            continue
        if it["stock_qty"] is not None and it["stock_qty"] <= 0:
            continue
        ids = {a["ad_id"] for a in it["ads"]}
        titles = list({it["title"], *[a["title"] for a in it["ads"] if a["title"]]})
        by_kw = _rank_rows(ctx, ids, titles, days=7)
        status, best = _rank_status(by_kw, ctx.now)
        quiet = _days_quiet(ctx, it)
        ages = [_iso_age_days(a["first_seen_at"], ctx.now) for a in live]
        # возраст неизвестен — считаем свежим: перепост «вслепую» не советуем
        newest_age = min(0.0 if x is None else x for x in ages)
        dup = _dup_group(ctx, it)
        reasons = []
        prio = 0.0
        if status == "sinking":
            reasons.append(f"опустилось: {best['organic_pos'] or best['pos']}-е место, стр. {best['page']}")
            prio += 3 + min(3, (best["pos"] or 0) / PAGE_SIZE)
        elif status == "missing":
            pages = max(e["latest"]["searched_pages"] for e in by_kw.values())
            reasons.append(f"нет на первых {pages} стр. выдачи")
            prio += 4
        if quiet is not None and quiet >= QUIET_DAYS:
            reasons.append(f"{int(quiet)} дн. без писем")
            prio += min(4, quiet / QUIET_DAYS * 1.5)
        if not reasons:
            continue
        if dup:
            out.append({
                "item_id": it["item_id"], "product_id": it["product_id"], "title": it["title"],
                "priority": prio + 5, "action": "remove_duplicates", "reasons_ru": reasons + dup["reasons_ru"],
                "rank_status": status, "days_quiet": quiet, "old_ad": None, "window": window, "text": None,
                "price": None, "duplicates": dup,
                "steps_ru": [dup["message_ru"], "Перепост — только после того, как останется одно объявление."]})
            continue
        if newest_age < FRESH_REPOST_DAYS:
            continue
        old = live[0]
        try:
            adv = advise_price(item_id=it["item_id"], _ctx=ctx)
        except Exception:  # noqa: BLE001
            logger.exception("market: advise_price для %s упал", it["item_id"])
            adv = {"verdict": None, "min_eur": None, "recommended_eur": None,
                   "verdict_ru": "оценка рынка не посчиталась (ошибка в логе) — цену проверьте сами"}
        floor = it["min_price_eur"] or adv.get("min_eur")
        cur = it["price_eur"]
        if adv.get("verdict") == "above" and adv.get("recommended_eur"):
            suggested = max(adv["recommended_eur"], floor or 0)
            advice = f"{adv['verdict_ru']} — предлагаю {int(suggested)} €"
        elif adv.get("verdict") == "below" and adv.get("min_eur") and cur and adv["min_eur"] > cur:
            # дешевле рынка: перепост — повод поднять до нижней границы рынка
            suggested = adv["min_eur"]
            advice = f"{adv['verdict_ru']} — можно {int(suggested)} €"
        else:
            suggested = _vary_price(cur, floor)
            advice = adv.get("verdict_ru") or ""
        text = None
        if with_text:
            try:
                tips_missing = [m["term"] for m in visibility_tips(item_id=it["item_id"], use_llm=False,
                                                                     _ctx=ctx).get("missing_terms") or []]
            except Exception:  # noqa: BLE001
                logger.exception("market: visibility_tips для %s упал", it["item_id"])
                tips_missing = []
            text = repost_text(it, price_eur=suggested, missing_terms=tips_missing, use_llm=use_llm)
        out.append({
            "item_id": it["item_id"], "product_id": it["product_id"], "title": it["title"],
            "priority": round(prio, 2), "action": "repost", "reasons_ru": reasons, "rank_status": status,
            "days_quiet": quiet, "old_ad": _strip(old),
            "window": window, "text": text,
            "price": {"current": cur, "suggested": suggested, "advice_ru": advice}, "duplicates": None,
            "steps_ru": [
                f"В окно {window['label_ru']}: удалить объявление {old['ad_id']} ({old['account'] or 'аккаунт ?'})",
                "Сразу выложить новое с текстом ниже — с ТОГО ЖЕ аккаунта, фото в другом порядке",
                "Старое не «деактивировать», а удалить — иначе это дубль",
            ]})
    out.sort(key=lambda x: -x["priority"])
    return out[:limit]


# ══════════════════════════════════════════════════════════════════════════════
# СВОДКА И ДЖОБЫ
# ══════════════════════════════════════════════════════════════════════════════

def data_health(ctx: Optional[_Ctx] = None) -> Optional[str]:
    """Почему советы могут быть пустыми: устаревшие данные. None — всё в порядке.
    Пишет WARNING в лог; вызывается в сводке и доступна api (`/market/*` → поле health)."""
    ctx = ctx or _Ctx()
    problems = []
    stale = _market_stale_days(ctx)
    if stale is not None and stale > 3:
        problems.append(f"рынок (scout) не обновлялся {int(stale)} дн.")
    if ctx.own_ids and not own_items(ctx):
        problems.append("ни одного живого нашего объявления по данным — выдача/письма устарели?")
    if db_market.list_market_queries():
        ok = db_market.runs_since((ctx.now - timedelta(days=STALE_RANK_DAYS)).isoformat())
        if not any(r["source"] == "market" for r in ok):
            problems.append(f"позиции в выдаче не измерялись больше {STALE_RANK_DAYS} дн.")
    if problems:
        msg = "; ".join(problems)
        logger.warning("market: %s", msg)
        return msg
    return None


def summary_lines(now: Optional[datetime] = None) -> list[str]:
    """0–5 строк для утренней сводки бота (HTML-экранирование — на стороне бота)."""
    lines = []
    ctx = _Ctx(now)
    note = data_health(ctx)
    if note:
        lines.append(f"Рынок: {note}")
    try:
        d = duplicates_report(_ctx=ctx)
        if d["danger"]:
            lines.append(f"Дубли: опасно — {d['danger']} товар(ов), снять {d['ads_to_remove']} объявл.")
        elif d["warn"]:
            lines.append(f"Дубли: {d['warn']} товар(ов) в 2 объявлениях — проверить")
    except Exception:  # noqa: BLE001
        logger.exception("market: дубли для сводки не посчитаны")
        lines.append("Рынок: отчёт о дублях не посчитан — см. лог")
    try:
        reps = repost_suggestions(limit=20, with_text=False, _ctx=ctx)
        rp = [r for r in reps if r["action"] == "repost"]
        if rp:
            w = rp[0]["window"]["label_ru"]
            lines.append(f"Перепост: {len(rp)} объявл. ({w}) — первым «{rp[0]['title'][:40]}»")
    except Exception:  # noqa: BLE001
        logger.exception("market: перепост для сводки не посчитан")
        lines.append("Рынок: подсказки перепоста не посчитаны — см. лог")
    return lines


def market_rank_job() -> str:
    """Джоб: позиции в выдаче. Ставить в 07:30 и 14:30 по Берлину (до вечернего окна перепоста).
    Выключатель — настройка market_rank_enabled ('1' по умолчанию)."""
    if (config.get("market_rank_enabled", "1") or "1").strip() != "1":
        return "market: позиции выключены (market_rank_enabled=0)"
    r = run_rank_check()
    if r["skipped"]:
        return f"market: пропуск — {r['skipped']}"
    if r["ran"] == 0:
        # Запросы есть, но ни один не дал карточек — блокировка или новая вёрстка.
        logger.error("market: прогон позиций без единой выдачи: %s", "; ".join(r["errors"])[:1000])
        return f"market: ни один запрос не дал выдачи, ошибок {len(r['errors'])}"
    msg = f"market: запросов {r['ran']}, страниц {r['pages']}, карточек {r['cards']}"
    if r["errors"]:
        logger.error("market: ошибки прогона позиций: %s", "; ".join(r["errors"])[:1000])
        msg += f", ошибок {len(r['errors'])}"
    return msg


def market_features_job() -> str:
    """Джоб: признаки новых объявлений рынка (Haiku, ≤ 60 за запуск). Раз в 6 ч после scout."""
    r = enrich_features()
    if r["batches"] and r["errors"] == r["batches"]:
        # Все пачки без Haiku — скорее всего слетела подписка/CLI. ERROR → hourly-монитор.
        logger.error("market: признаки — Haiku недоступен во всех %d пачках, работаем по regex", r["batches"])
    elif r["errors"] or r["missing"]:
        logger.warning("market: признаки — пачек без Haiku %d, пропущено Haiku %d", r["errors"], r["missing"])
    return (f"market: признаки llm={r['llm']} regex={r['regex']} ошибок={r['errors']} "
            f"пропусков={r['missing']}")
