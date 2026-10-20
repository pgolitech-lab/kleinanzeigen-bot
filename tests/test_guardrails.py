"""Pure deterministic guardrails (Track B Increment 2). No side effects."""
from __future__ import annotations

import pytest

from modules import guardrails as g


# --- reconcile_floor ---
def test_reconcile_floor_takes_max():
    assert g.reconcile_floor(1000.0, 1200.0) == 1200.0
    assert g.reconcile_floor(1400.0, 1200.0) == 1400.0

def test_reconcile_floor_handles_none_and_zero():
    assert g.reconcile_floor(None, 900.0) == 900.0
    assert g.reconcile_floor(900.0, None) == 900.0
    assert g.reconcile_floor(None, None) is None
    assert g.reconcile_floor(0, 0) is None


# --- floor_violation ---
def test_floor_violation():
    assert g.floor_violation(999.0, 1000.0) is True
    assert g.floor_violation(1000.0, 1000.0) is False
    assert g.floor_violation(1500.0, 1000.0) is False
    assert g.floor_violation(None, 1000.0) is False
    assert g.floor_violation(999.0, None) is False


# --- outgoing_language_ok ---
def test_language_ru_requires_cyrillic():
    assert g.outgoing_language_ok("Здравствуйте, цена окончательная.", "ru") is True
    assert g.outgoing_language_ok("Hello there", "ru") is False

def test_language_non_ru_rejects_cyrillic():
    assert g.outgoing_language_ok("Здравствуйте", "de") is False
    assert g.outgoing_language_ok("Hallo, der Preis ist fest.", "de") is True
    assert g.outgoing_language_ok("Bonjour, c'est possible.", "fr") is True

def test_language_empty_is_not_ok():
    assert g.outgoing_language_ok("", "de") is False
    assert g.outgoing_language_ok("   ", "ru") is False


# --- extract_json_object ---
def test_extract_plain_json():
    assert g.extract_json_object(['{"a": 1, "b": "x"}']) == {"a": 1, "b": "x"}

def test_extract_json_with_trailing_narrative():
    block = 'Here is the result: {"a": 1} — hope that helps!'
    assert g.extract_json_object([block]) == {"a": 1}

def test_extract_prefers_a_valid_json_block_from_the_end():
    blocks = ['searching the web...', '{"final": true, "price": 1500}']
    assert g.extract_json_object(blocks) == {"final": True, "price": 1500}

def test_extract_skips_nonjson_last_block():
    blocks = ['{"answer": "ok"}', 'no json here']
    assert g.extract_json_object(blocks) == {"answer": "ok"}

def test_extract_nested_braces_and_strings():
    block = '{"msg": "he said {hi}", "d": {"x": 1}}'
    assert g.extract_json_object([block]) == {"msg": "he said {hi}", "d": {"x": 1}}

def test_extract_raises_when_no_json():
    with pytest.raises(RuntimeError):
        g.extract_json_object(['nothing', 'still nothing'])
    with pytest.raises(RuntimeError):
        g.extract_json_object([])

def test_extract_prefers_last_object_within_one_block():
    # Draft price then a corrected final price inside ONE text block —
    # must return the FINAL object, not the stale draft.
    block = 'draft: {"price": 900, "note": "stale"} final: {"price": 1500, "final": true}'
    assert g.extract_json_object([block]) == {"price": 1500, "final": True}


# --- language_verdict: любые языки, не только ru ---
import pytest as _pytest


@_pytest.mark.parametrize("text,lang", [
    ("Hallo, die Sitzbank ist noch da. Viele Grüße", "de"),
    ("Hi, yes it is still available. Thanks", "en"),
    ("Здравствуйте, да, сиденье ещё есть.", "ru"),
    ("Добрий день, так, сидіння ще є. Дякую", "uk"),
    ("Здравейте, да, седалката още я има. Благодаря", "bg"),
    ("Merhaba, evet hâlâ satılık. Teşekkürler", "tr"),
    ("Dzień dobry, tak, siedzenie jest nadal dostępne. Dziękuję", "pl"),
])
def test_language_verdict_ok(text, lang):
    assert g.language_verdict(text, lang) == "ok"


@_pytest.mark.parametrize("text,lang", [
    ("Здравствуйте, да, сиденье ещё есть.", "de"),        # кириллица немцу
    ("Hallo, die Sitzbank ist noch da. Viele Grüße", "ru"),
    ("Здравствуйте, да, сиденье ещё есть, можно забрать.", "uk"),  # русский украинцу
    ("Hi, yes it is still available, thanks for asking", "de"),   # английский немцу
    ("Hallo, die Sitzbank ist noch da und gerne mit Lieferung", "pl"),
    ("Dzień dobry, tak, jest nadal dostępne. Dziękuję", "tr"),
])
def test_language_verdict_mismatch(text, lang):
    assert g.language_verdict(text, lang) == "mismatch"


def test_outgoing_language_ok_checks_script_only():
    assert g.outgoing_language_ok("Так, ще є.", "uk") is True
    assert g.outgoing_language_ok("Так, ще є.", "ru") is True   # одна письменность — решает LLM-слой
    assert g.outgoing_language_ok("Здравствуйте", "tr") is False
    assert g.outgoing_language_ok("Hallo", "bg") is False


def test_language_verdict_short_is_unsure_or_ok():
    assert g.language_verdict("Ok, 1200 €", "de") in ("ok", "unsure")
    assert g.language_verdict("1200", "tr") == "ok"


def test_language_llm_fallback_used_only_when_unsure():
    from unittest.mock import patch
    with patch("modules.claude.detect_text_lang", return_value={"lang": "de"}) as m:
        assert g.outgoing_language_ok_llm("Passt", "de") is True
        m.assert_called_once()
    with patch("modules.claude.detect_text_lang", side_effect=RuntimeError("down")):
        assert g.outgoing_language_ok_llm("Passt", "de") is False
    with patch("modules.claude.detect_text_lang") as m:
        assert g.outgoing_language_ok_llm("Здравствуйте, сиденье ещё есть.", "de") is False
        m.assert_not_called()
