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
