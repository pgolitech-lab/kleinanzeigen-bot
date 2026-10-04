"""Юнит-тесты чистых функций разведки рынка (без сети/БД)."""
from __future__ import annotations

import pytest

from modules import scout
from modules.plz import plz_to_bundesland


# --- slugify / URL ---

@pytest.mark.parametrize("text,expected", [
    ("peugeot traveller", "peugeot-traveller"),
    ("Opel Zafira Life", "opel-zafira-life"),
    ("Citroën SpaceTourer", "citroen-spacetourer"),
    ("sitze für traveller", "sitze-fuer-traveller"),
    ("  multiple   spaces  ", "multiple-spaces"),
    ("e-Traveller (9 Sitzer)", "e-traveller-9-sitzer"),
])
def test_slugify(text, expected):
    assert scout.slugify(text) == expected


def test_build_search_url_cars():
    u = scout.build_search_url("peugeot traveller", scout.CARS_CATEGORY, 1)
    assert u == "https://www.kleinanzeigen.de/s-autos/peugeot-traveller/k0c216"


def test_build_search_url_cars_page2():
    u = scout.build_search_url("peugeot traveller", scout.CARS_CATEGORY, 2)
    assert u == "https://www.kleinanzeigen.de/s-autos/seite:2/peugeot-traveller/k0c216"


def test_build_search_url_parts():
    u = scout.build_search_url("sitze traveller", scout.PARTS_CATEGORY, 1)
    assert u == "https://www.kleinanzeigen.de/s-autoteile/sitze-traveller/k0c223"


# --- price ---

@pytest.mark.parametrize("raw,price,vb", [
    ("10.900 €", 10900.0, False),
    ("31.300 € VB", 31300.0, True),
    ("999 € VB", 999.0, True),
    ("1.234.567 €", 1234567.0, False),
    ("VB", None, True),
    ("", None, False),
    (None, None, False),
    ("Zu verschenken", None, False),
])
def test_parse_price(raw, price, vb):
    assert scout.parse_price(raw) == (price, vb)


# --- location ---

def test_parse_location_full():
    assert scout.parse_location("90449 Gebersdorf") == ("90449", "Gebersdorf")


def test_parse_location_softhyphen():
    plz, city = scout.parse_location("70180 Stuttgart-​Süd")
    assert plz == "70180"
    assert "Stuttgart" in city


def test_parse_location_no_plz():
    assert scout.parse_location("Berlin") == (None, "Berlin")


def test_parse_location_empty():
    assert scout.parse_location("") == (None, None)


# --- car tags ---

def test_parse_car_tags():
    mileage, ez, year = scout.parse_car_tags(["158.439 km", "EZ 08/2021"])
    assert mileage == 158439
    assert ez == "08/2021"
    assert year == 2021


def test_parse_car_tags_partial():
    mileage, ez, year = scout.parse_car_tags(["88.900 km"])
    assert mileage == 88900
    assert ez is None and year is None


# --- attribute heuristics ---

@pytest.mark.parametrize("text,fuel", [
    ("Peugeot Traveller BlueHDi 115", "diesel"),
    ("e-Traveller Elektro 75 kWh", "electric"),
    ("Opel Zafira Life PureTech Benzin", "petrol"),
    ("Plug-in Hybrid Van", "hybrid"),
    ("Toyota ProAce Verso", None),
    # ловушка: дизель с электро-дверью НЕ должен стать electric
    ("Peugeot Traveller BlueHDi mit elektrische Schiebetür", "diesel"),
    ("Diesel, elektrische Fensterheber", "diesel"),
    ("Peugeot Expert HDi145 EAT8, Elektro-Schiebetür", "diesel"),
])
def test_extract_fuel(text, fuel):
    assert scout.extract_fuel(text) == fuel


@pytest.mark.parametrize("text,gb", [
    ("Traveller EAT8 Automatik", "automatik"),
    ("mit Schaltgetriebe 6-Gang", "manuell"),
    ("Traveller L2", None),
    # ловушка: Klimaautomatik (климат-контроль) — это НЕ АКПП
    ("Traveller mit Klimaautomatik", None),
    ("Automatikgetriebe gepflegt", "automatik"),
])
def test_extract_gearbox(text, gb):
    assert scout.extract_gearbox(text) == gb


@pytest.mark.parametrize("text,model", [
    ("Peugeot Traveller 2.0", "traveller"),
    ("Toyota ProAce Verso 9 Sitzer", "proace_verso"),
    ("Toyota ProAce Kasten", "proace"),
    ("Citroen SpaceTourer", "spacetourer"),
    ("Opel Zafira Life", "zafira_life"),
    ("Opel Vivaro", "vivaro"),
    ("Peugeot Expert Kombi", "expert"),
    ("Fiat Ulysse", "ulysse"),
    ("VW Multivan", None),
])
def test_extract_model_family(text, model):
    assert scout.extract_model_family(text) == model


@pytest.mark.parametrize("text,ptype", [
    ("Sitzschienen Zafira Life", "rail"),
    ("Doppel Sitzbank Traveller", "bench"),
    ("3er-Sitzreihe Spacetourer", "bench"),
    ("Einzelsitz Leder", "seat"),
    ("Sitze für Traveller", "seat"),
    ("Stoßstange vorne", "other"),
])
def test_extract_part_type(text, ptype):
    assert scout.extract_part_type(text) == ptype


@pytest.mark.parametrize("text,cond", [
    ("Sitze neuwertig", "neu"),
    ("Original NEU verpackt", "neu"),
    ("Wie neu - nie benutzt", "gebraucht"),
    ("gebraucht mit Gebrauchsspuren", "gebraucht"),
    ("Sitzbank schwarz", None),
])
def test_extract_condition(text, cond):
    assert scout.extract_condition(text) == cond


@pytest.mark.parametrize("text,year", [
    ("Original Sitze MJ 2026", 2026),
    ("Baujahr 2018 Diesel", 2018),
    ("Traveller aus 2019", 2019),
    ("kein Jahr hier", None),
])
def test_extract_year_generic(text, year):
    assert scout.extract_year_generic(text) == year


# --- разбор HTML выдачи (вёрстка Kleinanzeigen с ~09.2026, Tailwind, без .aditem) ---

_SVG = '<svg viewBox="0 0 24 24"><path d="M1 1"></path></svg>'

_CAR_CARD = (
    '<article class="flex justify-between p-medium" data-adid="3502895257" '
    'data-href="/s-anzeige/peugeot-expert-traveller/3502895257-216-3434">'
    '<div><script type="application/ld+json">{"title":"Peugeot Expert Traveller Allure L2",'
    '"description":"Automatik, Diesel, 8-Sitzer, Scheckheft","@type":"ImageObject"}</script>'
    '<a href="/s-anzeige/x"><img src="x.jpg" alt="Vorschau"><div>14</div></a>'
    '<div><div>TOP</div></div></div>'
    f'<div><div><div>{_SVG}<span>14612 Falkensee</span></div>'
    f'<div>{_SVG}<span>Heute, 00:32</span></div></div>'
    '<div><h3><a href="/s-anzeige/x">Peugeot Expert Traveller Allure L2</a></h3>'
    '<p>Automatik, Diesel, 8-Sitzer, Scheckheft gepflegt, sehr guter Zustand ...</p>'
    '<div><p>26.900 € VB</p><p>28.500 €</p></div></div>'
    '<div><p><span data-dhl-promotion>121.639 km</span>'
    '<span data-dhl-promotion>EZ 02/2019</span></p></div></div></article>'
)

_PART_CARD = (
    '<article data-adid="3513270744" data-href="/s-anzeige/sitzbank/3513270744-223-1">'
    '<div><h2>3-er Leder Sitzbank Peugeot Traveller</h2>'
    '<p>Sitzbank 2+1 Leder, unbenutzt. Zustand wie neu, Abholung oder Versand.</p>'
    '<span>41564­ Kaarst</span><span>15.09.2026</span>'
    '<p>1.300 €</p><span>Versand möglich</span></div></article>'
)


def test_parse_search_html_car_card():
    html = f"<html><body><main>{_CAR_CARD}</main></body></html>"
    rows = scout.parse_search_html(html, "car", 7)
    assert len(rows) == 1
    r = rows[0]
    assert r["ad_id"] == "3502895257"
    assert r["url"] == ("https://www.kleinanzeigen.de/s-anzeige/"
                        "peugeot-expert-traveller/3502895257-216-3434")
    assert r["title"] == "Peugeot Expert Traveller Allure L2"
    assert r["price_raw"] == "26.900 € VB"       # первая цена, не зачёркнутая старая
    assert r["price_eur"] == 26900.0 and r["negotiable"] == 1
    assert (r["plz"], r["city"]) == ("14612", "Falkensee")
    assert r["posted_raw"] == "Heute, 00:32"
    assert (r["mileage_km"], r["ez_raw"], r["year"]) == (121639, "02/2019", 2019)
    assert r["fuel"] == "diesel" and r["gearbox"] == "automatik"
    assert r["query_id"] == 7 and r["shipping"] == 0


def test_parse_search_html_part_card_without_ld_json():
    rows = scout.parse_search_html(_PART_CARD, "part", 1)
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "3-er Leder Sitzbank Peugeot Traveller"   # fallback на h2
    assert r["description"].startswith("Sitzbank 2+1 Leder")
    assert (r["plz"], r["city"]) == ("41564", "Kaarst")            # soft hyphen срезан
    assert r["price_eur"] == 1300.0 and r["posted_raw"] == "15.09.2026"
    assert r["shipping"] == 1 and r["part_type"] == "bench"
    assert r["mileage_km"] is None


def test_parse_search_html_price_only_vb():
    card = _CAR_CARD.replace("<p>26.900 € VB</p><p>28.500 €</p>", "<p>VB</p>")
    r = scout.parse_search_html(card, "car", 1)[0]
    assert r["price_raw"] == "VB" and r["price_eur"] is None and r["negotiable"] == 1


def test_parse_search_html_ignores_non_ad_articles_and_garbage():
    html = "<article><p>12345 Werbung</p></article>" + _CAR_CARD + _PART_CARD + "<div><p"
    assert [r["ad_id"] for r in scout.parse_search_html(html, "car", 1)] == [
        "3502895257", "3513270744"]


def test_looks_like_results_page():
    assert scout.looks_like_results_page(_CAR_CARD)
    assert scout.looks_like_results_page(
        "<h1>Es wurden keine Gebrauchtwagen für „xyz“ in Deutschland gefunden.</h1>")
    assert not scout.looks_like_results_page("<html><body>Bitte bestätige, dass du ein Mensch bist</body></html>")
    assert not scout.looks_like_results_page("")


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code, self.text = status, text


class _Client:
    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url, _Resp(404, ""))


class _Browser:
    def __init__(self, html: str):
        self.html, self.calls = html, 0

    def get(self, url):
        self.calls += 1
        return self.html


def test_fetch_search_page_falls_back_to_browser_on_wall():
    url = "https://www.kleinanzeigen.de/s-autos/x/k0c216"
    client = _Client({url: _Resp(200, "<html>captcha</html>")})
    br = _Browser(_CAR_CARD)
    assert scout.fetch_search_page(client, url, br) == _CAR_CARD
    assert br.calls == 1


def test_fetch_search_page_raises_when_both_fail():
    url = "https://www.kleinanzeigen.de/s-autos/x/k0c216"
    client = _Client({url: _Resp(403, "denied")})
    with pytest.raises(scout.ScoutFetchError):
        scout.fetch_search_page(client, url, _Browser("<html>still blocked</html>"))
    with pytest.raises(scout.ScoutFetchError):
        scout.fetch_search_page(client, url, None)


def test_scrape_query_paginates_and_dedups():
    cards = "".join(_CAR_CARD.replace("3502895257", str(3500000000 + i)) for i in range(22))
    q = {"id": 1, "kind": "car", "category": "c216", "keywords": "peugeot traveller",
         "max_pages": 3}
    p1 = scout.build_search_url(q["keywords"], q["category"], 1)
    p2 = scout.build_search_url(q["keywords"], q["category"], 2)
    # стр.2: 1 повтор со стр.1 (TOP-объявление) + 1 новое; стр.3 → 404
    page2 = _CAR_CARD.replace("3502895257", "3500000000") + _PART_CARD
    client = _Client({p1: _Resp(200, cards), p2: _Resp(200, page2)})
    rows = scout.scrape_query(client, q, page_delay_sec=0, browser=None)
    assert len(rows) == 23
    assert len(client.calls) == 2   # <20 карточек на стр.2 → стоп без запроса стр.3


# --- PLZ → Bundesland ---

# --- DB: эффективный kind (verified_kind) + сводка по городам ---

def _mk_listing(ad_id, kind, city, bundesland, price):
    return {"ad_id": ad_id, "kind": kind, "title": f"t{ad_id}", "url": "u",
            "price_eur": price, "price_raw": None, "negotiable": 0,
            "plz": "10115", "city": city, "bundesland": bundesland,
            "year": None, "ez_raw": None, "mileage_km": None, "fuel": None,
            "gearbox": None, "model_family": None, "part_type": None,
            "condition": None, "description": "", "posted_raw": None,
            "shipping": 0, "query_id": 1}


def test_effective_kind_and_city_summary(tmp_path, monkeypatch):
    import importlib
    import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    # 3 машины (2 Berlin, 1 München), 1 запчасть (Berlin)
    db.upsert_scout_listing(_mk_listing("1", "car", "Berlin", "Berlin", 10000))
    db.upsert_scout_listing(_mk_listing("2", "car", "Berlin", "Berlin", 20000))
    db.upsert_scout_listing(_mk_listing("3", "car", "München", "Bayern", 30000))
    db.upsert_scout_listing(_mk_listing("4", "part", "Berlin", "Berlin", 100))

    # до проверки: 3 car / 1 part, 4 unverified
    c = db.scout_counts()
    assert c["cars"] == 3 and c["parts"] == 1 and c["unverified"] == 4

    # Haiku переклассифицировал машину #3 как запчасть, #2 как other
    db.set_scout_verified_kind("3", "part")
    db.set_scout_verified_kind("2", "other")
    c = db.scout_counts()
    assert c["cars"] == 1          # только #1
    assert c["parts"] == 2          # #4 + переклассифицированная #3
    assert c["other"] == 1          # #2
    assert c["unverified"] == 2     # #1, #4

    # list по эффективному виду
    cars = db.list_scout_listings(kind="car")
    assert {r["ad_id"] for r in cars} == {"1"}
    parts = db.list_scout_listings(kind="part")
    assert {r["ad_id"] for r in parts} == {"3", "4"}

    # сводка по городам (машины): только #1 Berlin
    cs = db.scout_city_summary("car")
    assert len(cs) == 1 and cs[0]["city"] == "Berlin" and cs[0]["cnt"] == 1

    # фильтр по городу
    assert len(db.list_scout_listings(kind="part", city="Berlin")) == 1


def test_scout_corrections(tmp_path, monkeypatch):
    import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "c.db")
    db.init_db()
    db.upsert_scout_listing(_mk_listing("10", "car", "Köln", "Nordrhein-Westfalen", 9000))
    db.upsert_scout_listing(_mk_listing("11", "car", "Köln", "Nordrhein-Westfalen", 50))

    # #11 — на самом деле запчасть → reclassify
    r = db.apply_scout_correction("11", "part", note="это сиденье", created_by="op")
    assert r["ok"] and "part" in r["action"]
    assert db.scout_counts()["parts"] == 1 and db.scout_counts()["cars"] == 1

    # #10 — мусор → remove (rejected)
    r = db.apply_scout_correction("10", "remove", created_by="op")
    assert r["ok"] and r["action"] == "removed"
    assert db.scout_counts()["cars"] == 0

    # повторный скрап НЕ реактивирует rejected #10
    db.upsert_scout_listing(_mk_listing("10", "car", "Köln", "Nordrhein-Westfalen", 9000))
    assert db.scout_counts()["cars"] == 0

    # правки записаны для обучения Haiku
    corr = db.recent_scout_corrections(limit=10)
    assert len(corr) == 2
    kinds = {c["correct_kind"] for c in corr}
    assert kinds == {"part", "remove"}


def test_scout_daily_stats(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    import database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "s.db")
    db.init_db()
    db.upsert_scout_listing(_mk_listing("20", "car", "Berlin", "Berlin", 10000))
    db.upsert_scout_listing(_mk_listing("21", "part", "Berlin", "Berlin", 100))

    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = "2020-01-01"
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE scout_listings SET first_seen_at = ? WHERE ad_id = '21'",
            (yesterday + "T09:00:00",),
        )

    # #20 найдена сегодня, #21 — «вчера»
    assert db.scout_daily_stats(today)["new"] == {"car": 1}
    assert db.scout_daily_stats(yesterday)["new"] == {"part": 1}
    assert db.scout_daily_stats(today)["removed"] == {}

    # деактивация проставляет deactivated_at=now → попадает в сегодняшнюю статистику
    n = db.deactivate_stale_scout_listings(
        "car", (datetime.utcnow() + timedelta(days=1)).isoformat())
    assert n == 1
    assert db.scout_daily_stats(today)["removed"] == {"car": 1}


@pytest.mark.parametrize("plz,land", [
    ("90449", "Bayern"),
    ("10707", "Berlin"),
    ("51069", "Nordrhein-Westfalen"),
    ("70180", "Baden-Württemberg"),
    ("28195", "Bremen"),
    ("01067", "Sachsen"),
    ("20095", "Hamburg"),
    ("66287", "Saarland"),
    ("", None),
    ("1", None),
    (None, None),
])
def test_plz_to_bundesland(plz, land):
    assert plz_to_bundesland(plz) == land
