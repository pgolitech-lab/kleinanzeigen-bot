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
