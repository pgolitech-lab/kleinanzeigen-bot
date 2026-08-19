# Разведка рынка Kleinanzeigen: скрапинг страниц поиска (Playwright sync API).
#
# Ищет машины платформы Peugeot Traveller (EMP2/PSA: Traveller/Expert,
# Citroën SpaceTourer/Jumpy, Opel Zafira Life/Vivaro, Toyota ProAce(Verso),
# Fiat Scudo/Ulysse, вкл. электрички) и запчасти (сиденья, скамейки, рельсы).
#
# Поисковые запросы (scout_queries) генерит LLM (modules/claude.generate_scout_queries),
# редактирует оператор в /scout. Результаты пишутся в scout_listings (дедуп по ad_id).
#
# Парсинг строится на разведанной DOM-структуре:
#   article.aditem[data-adid][data-href]
#     .aditem-main--middle--price-shipping--price  → цена ('10.900 € VB')
#     .aditem-main--top--left                       → 'PLZ Город'
#     .aditem-main--bottom .simpletag               → теги ('158.439 km', 'EZ 08/2021', 'Versand möglich')
#     .aditem-main--top--right                      → дата ('Heute, 15:07' / '14.06.2026')
#     script[application/ld+json]                    → {title, description}

import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Optional

from playwright.sync_api import Page, sync_playwright

import config
import database as db
from modules import claude_scout
from modules.plz import plz_to_bundesland

logger = logging.getLogger(__name__)

BASE = "https://www.kleinanzeigen.de"
CARS_CATEGORY = "c216"      # Autos
PARTS_CATEGORY = "c223"     # Autoteile & Reifen

# category → секция в URL
_SECTION: dict[str, str] = {
    CARS_CATEGORY: "s-autos",
    PARTS_CATEGORY: "s-autoteile",
}

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                         "Ä": "ae", "Ö": "oe", "Ü": "ue"})


# --- URL ---

def slugify(text: str) -> str:
    """Превратить поисковую фразу в slug Kleinanzeigen ('peugeot traveller' → 'peugeot-traveller')."""
    s = (text or "").strip().lower().translate(_UMLAUT)
    # снять оставшуюся диакритику (ë→e, é→e, ç→c, …) — умляуты уже стали ae/oe/ue
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def build_search_url(keywords: str, category: str, page: int = 1) -> str:
    """URL страницы поиска. k0 = радиус по всей Германии."""
    section = _SECTION.get(category, "s")
    slug = slugify(keywords)
    if page > 1:
        return f"{BASE}/{section}/seite:{page}/{slug}/k0{category}"
    return f"{BASE}/{section}/{slug}/k0{category}"


# --- ПАРСЕРЫ ПОЛЕЙ ---

def parse_price(raw: Optional[str]) -> tuple[Optional[float], bool]:
    """'10.900 € VB' → (10900.0, True). Возвращает (цена_eur|None, торг_возможен)."""
    if not raw:
        return None, False
    negotiable = bool(re.search(r"\bV(?:B|HB)\b", raw, re.IGNORECASE))
    # убрать разделители тысяч (точка), взять число перед €
    m = re.search(r"(\d[\d.]*)\s*€", raw)
    if not m:
        return None, negotiable
    num = m.group(1).replace(".", "")
    try:
        return float(num), negotiable
    except ValueError:
        return None, negotiable


def parse_location(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'90449 Gebersdorf' → ('90449', 'Gebersdorf'). Без PLZ → (None, city|None)."""
    if not raw:
        return None, None
    # убрать невидимые символы (soft hyphen ­, zero-width ​)
    txt = raw.replace("­", "").replace("​", "").strip()
    m = re.match(r"^(\d{5})\s+(.*)$", txt)
    if m:
        return m.group(1), m.group(2).strip() or None
    return None, (txt or None)


def parse_car_tags(tags: list[str]) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """Теги машины → (пробег_км, ez_raw 'MM/YYYY', год). '158.439 km', 'EZ 08/2021'."""
    mileage: Optional[int] = None
    ez_raw: Optional[str] = None
    year: Optional[int] = None
    for t in tags:
        t = (t or "").strip()
        m_km = re.search(r"([\d.]+)\s*km", t, re.IGNORECASE)
        if m_km:
            try:
                mileage = int(m_km.group(1).replace(".", ""))
            except ValueError:
                pass
        m_ez = re.search(r"EZ\s*(\d{1,2}/(\d{4}))", t, re.IGNORECASE)
        if m_ez:
            ez_raw = m_ez.group(1)
            year = int(m_ez.group(2))
    return mileage, ez_raw, year


# --- ЭВРИСТИКИ АТРИБУТОВ (по title + description) ---

def extract_fuel(text: str) -> Optional[str]:
    t = (text or "").lower()
    # 1) Определяющие коды двигателя в тексте — самый надёжный сигнал. Проверяем
    #    ПЕРВЫМИ: HDi-вэн с 'elektrische Schiebetür' в описании — это дизель.
    if re.search(r"\b(bluehdi|blue hdi|hdi|tdi|cdi|dci)\d*", t) or re.search(r"\bdiesel\b", t):
        return "diesel"
    if re.search(r"\b(puretech|tsi|tfsi)\d*", t) or re.search(r"\bbenzin\b", t):
        return "petrol"
    # 2) Электро — только сильные сигналы (без широкого 'elektro'/'elektrisch',
    #    которые часто относятся к опциям: 'elektrische Fensterheber/Schiebetür').
    if re.search(
        r"(e-traveller|e-expert|e-ulysse|e-spacetourer|ë-|zafira-e|vivaro-e|"
        r"proace electric|elektroauto|elektrofahrz|elektromotor|elektroantrieb|"
        r"vollelektr|batterieelektr|\bbev\b|\d{2,3}\s*kwh)", t):
        return "electric"
    if re.search(r"\b(phev|plug-in|plugin|hybrid)\b", t):
        return "hybrid"
    return None


def extract_gearbox(text: str) -> Optional[str]:
    t = (text or "").lower()
    # Сильные сигналы АКПП. 'automatik' голым НЕ берём (часто 'Klimaautomatik').
    if re.search(r"\b(eat\s?8|eat\s?6|dsg|automatikgetriebe|wandlerautomat|vollautomatik)\b", t):
        return "automatik"
    if re.search(r"(?<!klima)(?<!klima-)\bautomatik\b", t):
        return "automatik"
    # МКПП — по 'schaltgetriebe'/'handschalt'/'…-gang schaltgetriebe'.
    if re.search(r"\b(schaltgetriebe|handschalt|handschaltung|manuelle\s+schaltung)\b", t):
        return "manuell"
    return None


# модель-семейство: порядок важен (специфичное раньше общего)
_MODEL_PATTERNS: list[tuple[str, str]] = [
    (r"proace\s*verso", "proace_verso"),
    (r"proace", "proace"),
    (r"spacetourer|space tourer", "spacetourer"),
    (r"zafira[\s-]*life", "zafira_life"),
    (r"zafira[\s-]*tourer", "zafira_tourer"),
    (r"vivaro", "vivaro"),
    (r"traveller", "traveller"),
    (r"\bexpert\b", "expert"),
    (r"jumpy", "jumpy"),
    (r"\bscudo\b", "scudo"),
    (r"ulysse", "ulysse"),
    (r"\bdispatch\b", "expert"),
]


def extract_model_family(text: str) -> Optional[str]:
    t = (text or "").lower()
    for pat, name in _MODEL_PATTERNS:
        if re.search(pat, t):
            return name
    return None


# тип запчасти: rail/bench проверяем раньше seat (содержат 'sitz')
def extract_part_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"sitzschien|schiene|laufschien|gleitschien|\brail", t):
        return "rail"
    if re.search(r"sitzbank|doppelbank|dreierbank|\bbank\b|sitzreihe|3er[\s-]*sitz|2er[\s-]*sitz", t):
        return "bench"
    if re.search(r"sitz|sessel", t):
        return "seat"
    return "other"


def extract_condition(text: str) -> Optional[str]:
    t = (text or "").lower()
    if re.search(r"neuwertig|fabrikneu|nagelneu|originalverpackt|\bovp\b", t):
        return "neu"
    # 'neu' но НЕ 'wie neu' (последнее = б/у в отличном состоянии)
    if re.search(r"(?<!wie )\bneu\b", t):
        return "neu"
    if re.search(r"gebraucht|gebrauchsspuren|benutzt|defekt|bastler|wie neu", t):
        return "gebraucht"
    return None


def extract_year_generic(text: str) -> Optional[int]:
    """Год для запчасти: 'MJ 2026', 'Modelljahr 2019', 'Baujahr 2018', 'BJ 2017', либо 19xx/20xx."""
    t = text or ""
    m = re.search(r"(?:mj|modelljahr|baujahr|bj|ez)\.?\s*(\d{2}/)?(\d{4})", t, re.IGNORECASE)
    if m:
        y = int(m.group(2))
        if 1990 <= y <= 2035:
            return y
    m2 = re.search(r"\b(19[9]\d|20[0-3]\d)\b", t)
    if m2:
        return int(m2.group(1))
    return None


# --- СКРАПИНГ ---

def _accept_cookies(page: Page) -> None:
    for sel in ("#gdpr-banner-accept", "button:has-text('Alle akzeptieren')",
                "button:has-text('Akzeptieren')"):
        try:
            b = page.query_selector(sel)
            if b:
                b.click(timeout=2000)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _item_text(item: Any, selector: str) -> str:
    try:
        e = item.query_selector(selector)
        return (e.inner_text() or "").strip() if e else ""
    except Exception:
        return ""


def _parse_item(item: Any, kind: str, query_id: int) -> Optional[dict[str, Any]]:
    """Распарсить одну article.aditem в dict-строку scout_listings."""
    ad_id = (item.get_attribute("data-adid") or "").strip()
    if not ad_id:
        return None
    href = (item.get_attribute("data-href") or "").strip()
    url = (BASE + href) if href.startswith("/") else (href or None)

    # title + description из JSON-LD (надёжнее DOM)
    title = ""
    description = ""
    try:
        ld = item.query_selector("script[type='application/ld+json']")
        if ld:
            data = json.loads(ld.inner_text())
            title = (data.get("title") or "").strip()
            description = (data.get("description") or "").strip()
    except Exception:
        pass
    if not title:
        title = _item_text(item, "h2 a, .text-module-begin a, .ellipsis")

    price_raw = _item_text(item, ".aditem-main--middle--price-shipping--price, .aditem-main--middle--price")
    price_eur, negotiable = parse_price(price_raw)
    plz, city = parse_location(_item_text(item, ".aditem-main--top--left"))
    bundesland = plz_to_bundesland(plz)
    posted_raw = _item_text(item, ".aditem-main--top--right") or None

    tags: list[str] = []
    try:
        for e in item.query_selector_all(".aditem-main--bottom .simpletag"):
            txt = (e.inner_text() or "").strip()
            if txt:
                tags.append(txt)
    except Exception:
        pass
    shipping = 1 if any("versand" in t.lower() for t in tags) else 0

    blob = f"{title}\n{description}"
    row: dict[str, Any] = {
        "ad_id": ad_id,
        "kind": kind,
        "title": title or None,
        "url": url,
        "price_eur": price_eur,
        "price_raw": price_raw or None,
        "negotiable": 1 if negotiable else 0,
        "plz": plz,
        "city": city,
        "bundesland": bundesland,
        "description": (description[:600] or None),
        "posted_raw": posted_raw,
        "shipping": shipping,
        "query_id": query_id,
        "model_family": extract_model_family(blob),
        "fuel": None, "gearbox": None, "year": None, "ez_raw": None,
        "mileage_km": None, "part_type": None, "condition": None,
    }

    if kind == "car":
        mileage, ez_raw, year = parse_car_tags(tags)
        row["mileage_km"] = mileage
        row["ez_raw"] = ez_raw
        row["year"] = year or extract_year_generic(blob)
        row["fuel"] = extract_fuel(blob)
        row["gearbox"] = extract_gearbox(blob)
    else:  # part
        row["part_type"] = extract_part_type(blob)
        row["condition"] = extract_condition(blob)
        row["year"] = extract_year_generic(blob)
        row["fuel"] = extract_fuel(blob)  # электро-запчасти бывают релевантны

    return row


def scrape_query(
    page: Page,
    query: Any,
    page_delay_sec: float = 1.5,
) -> list[dict[str, Any]]:
    """Проскрапить один запрос постранично. Возвращает список dict-строк (с дедупом по ad_id)."""
    kind = query["kind"]
    category = query["category"]
    keywords = query["keywords"]
    qid = query["id"]
    max_pages = max(1, int(query["max_pages"] or 1))

    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []

    for pnum in range(1, max_pages + 1):
        url = build_search_url(keywords, category, pnum)
        try:
            page.goto(url, timeout=40000, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("scout: goto fail q=%s p=%s: %s", qid, pnum, e)
            break
        if pnum == 1:
            _accept_cookies(page)
        try:
            page.wait_for_selector("article.aditem", timeout=12000)
        except Exception:
            # нет результатов на этой странице — конец
            break
        items = page.query_selector_all("article.aditem")
        if not items:
            break
        new_on_page = 0
        for it in items:
            try:
                row = _parse_item(it, kind, qid)
            except Exception as e:
                logger.debug("scout: item parse fail: %s", e)
                continue
            if not row:
                continue
            aid = row["ad_id"]
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            out.append(row)
            new_on_page += 1
        # если страница вернула < 20 карточек — последняя страница
        if len(items) < 20 or new_on_page == 0:
            break
        if pnum < max_pages:
            time.sleep(page_delay_sec)

    return out


def run_scout(
    query_ids: Optional[list[int]] = None,
    full_run: bool = False,
    page_delay_sec: float = 1.5,
    verify: bool = True,
) -> dict[str, Any]:
    """Прогнать разведку.

    query_ids=None → все enabled запросы (это «полный прогон»).
    full_run=True (или query_ids=None) → после прогона объявления, которых НЕ
      видели дольше scout_stale_days дней, помечаются active=0 (сняты/проданы).
      Деактивация по ВОЗРАСТУ (не «не виден в этом прогоне») + пропуск kind с 0
      результатов — чтобы упавший/заблокированный прогон не обнулял базу.
    verify=True → после скрапинга прогнать Haiku-проверку типа новых объявлений.

    Возвращает сводку: {ran, cars_new, parts_new, total_seen, errors, by_query, ...}.
    """
    if query_ids is None:
        queries = db.list_scout_queries(only_enabled=True)
        full_run = True
    else:
        queries = [q for q in (db.get_scout_query(i) for i in query_ids) if q]

    summary: dict[str, Any] = {
        "ran": 0, "cars_new": 0, "parts_new": 0, "total_seen": 0,
        "errors": [], "by_query": [], "kinds": set(), "seen_by_kind": {},
        "deactivated": 0, "verify": None, "new_listings": [],
    }
    if not queries:
        return summary

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for q in queries:
                # Свежий context на КАЖДЫЙ запрос: Kleinanzeigen отдаёт cookie-wall
                # без карточек (0 результатов) начиная со 2-й навигации в одной и той
                # же browser-сессии — похоже на анти-бот эвристику по паттерну
                # навигации, не на rate-limit по времени (не спасает и больший delay).
                # Новый context = "чистая" сессия для сайта на каждый поисковый запрос.
                ctx = browser.new_context(
                    user_agent=_UA, locale="de-DE",
                    viewport={"width": 1366, "height": 1000},
                )
                try:
                    page = ctx.new_page()
                    try:
                        rows = scrape_query(page, q, page_delay_sec=page_delay_sec)
                    except Exception as e:
                        logger.exception("scout: query %s failed", q["id"])
                        summary["errors"].append(f"q{q['id']} ({q['keywords']}): {e}")
                        continue
                finally:
                    ctx.close()
                new_count = 0
                for row in rows:
                    is_new = db.upsert_scout_listing(row)
                    if is_new:
                        new_count += 1
                        if row["kind"] == "car":
                            summary["cars_new"] += 1
                        else:
                            summary["parts_new"] += 1
                        summary["new_listings"].append({
                            "ad_id": row["ad_id"], "kind": row["kind"], "title": row["title"],
                            "price_raw": row["price_raw"], "price_eur": row["price_eur"],
                            "url": row["url"],
                        })
                summary["total_seen"] += len(rows)
                summary["ran"] += 1
                summary["kinds"].add(q["kind"])
                summary["seen_by_kind"][q["kind"]] = (
                    summary["seen_by_kind"].get(q["kind"], 0) + len(rows))
                db.update_scout_query(
                    q["id"],
                    last_run_at=datetime.utcnow().isoformat(),
                    last_count=len(rows),
                )
                summary["by_query"].append({
                    "id": q["id"], "keywords": q["keywords"],
                    "kind": q["kind"], "found": len(rows), "new": new_count,
                })
                time.sleep(page_delay_sec)
        finally:
            browser.close()

    # Деактивировать устаревшие — ПО ВОЗРАСТУ (last_seen старше scout_stale_days),
    # и только для kind, по которому реально что-то нашли в этот раз (иначе блокировка
    # сайта → 0 результатов → не трогаем базу).
    if full_run:
        cutoff = (datetime.utcnow() - timedelta(days=config.scout_stale_days())).isoformat()
        for kind in summary["kinds"]:
            if summary["seen_by_kind"].get(kind, 0) <= 0:
                logger.warning("scout: kind=%s дал 0 результатов — деактивацию пропускаем", kind)
                continue
            try:
                n = db.deactivate_stale_scout_listings(kind, cutoff)
                summary["deactivated"] += n
                if n:
                    logger.info("scout: deactivated %d stale %s listings (>%dd)",
                                n, kind, config.scout_stale_days())
            except Exception:
                logger.exception("scout: deactivate stale fail")

    # Haiku-проверка типа новых/непроверенных объявлений
    if verify:
        try:
            summary["verify"] = verify_listings()
        except Exception as e:  # noqa: BLE001
            logger.exception("scout: verify_listings fail")
            summary["errors"].append(f"verify: {e}")

        # Пересчитать cars_new/parts_new/new_listings по РЕЗУЛЬТАТУ проверки, а не
        # по сырому kind поискового запроса — иначе ложные совпадения (напр. Citroën
        # C4 Picasso/Spacetourer под запрос 'citroen spacetourer', которые Haiku
        # верно отбраковывает в 'other') всё равно лезут в дайджест как «новая машина».
        try:
            eff = db.get_scout_effective_kinds(
                [it["ad_id"] for it in summary["new_listings"]]
            )
            filtered = []
            cars_new = parts_new = 0
            for it in summary["new_listings"]:
                k = eff.get(it["ad_id"], it["kind"])
                if k not in ("car", "part"):
                    continue
                it["kind"] = k
                filtered.append(it)
                if k == "car":
                    cars_new += 1
                else:
                    parts_new += 1
            summary["new_listings"] = filtered
            summary["cars_new"] = cars_new
            summary["parts_new"] = parts_new
        except Exception:
            logger.exception("scout: post-verify digest filter fail")

    summary["kinds"] = sorted(summary["kinds"])
    return summary


def verify_listings(batch_size: int = 25, max_items: int = 1000) -> dict[str, Any]:
    """Haiku-проверка типа (car/part/other) для непроверенных активных объявлений.

    Батчами по batch_size. Возвращает {checked, changed, by_type, cost_usd}.
    changed — сколько объявлений Haiku переклассифицировала относительно kind-источника.
    """
    rows = db.list_unverified_scout_listings(limit=max_items)
    result = {"checked": 0, "changed": 0, "by_type": {"car": 0, "part": 0, "other": 0},
              "cost_usd": 0.0}
    if not rows:
        return result
    # few-shot: учим Haiku на операторских правках
    examples = [dict(c) for c in db.recent_scout_corrections(limit=30)]
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        items = [{"ad_id": r["ad_id"], "title": r["title"], "description": r["description"]}
                 for r in batch]
        try:
            res = claude_scout.classify_scout_listings(items, examples=examples)
        except Exception:
            logger.exception("scout: classify batch fail")
            continue
        result["cost_usd"] += res.get("cost_usd", 0.0)
        vmap = res["map"]
        for r in batch:
            v = vmap.get(str(r["ad_id"]))
            if not v:
                continue
            db.set_scout_verified_kind(r["ad_id"], v)
            result["checked"] += 1
            result["by_type"][v] = result["by_type"].get(v, 0) + 1
            if v != r["kind"]:
                result["changed"] += 1
        time.sleep(0.2)
    logger.info("scout: verify checked=%d changed=%d by=%s cost=$%.4f",
                result["checked"], result["changed"], result["by_type"], result["cost_usd"])
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    ids = [int(x) for x in sys.argv[1:]] or None
    print(json.dumps(run_scout(ids), ensure_ascii=False, indent=2))
