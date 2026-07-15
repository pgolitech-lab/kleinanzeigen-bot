"""Autopilot config keys: message cap + shadow mode (Track B Increment 2)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def test_message_cap_default(tmp_db):
    import config
    assert config.autopilot_message_cap() == 20


def test_message_cap_override(tmp_db):
    import config
    tmp_db.set_setting("autopilot_message_cap", "5")
    assert config.autopilot_message_cap() == 5


def test_shadow_mode_default_true(tmp_db):
    import config
    assert config.autopilot_shadow_mode() is True


def test_shadow_mode_off(tmp_db):
    import config
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    assert config.autopilot_shadow_mode() is False


def test_keys_whitelisted_and_validated():
    from web import api_ma
    assert "autopilot_message_cap" in api_ma.ALLOWED_SETTING_KEYS
    assert "autopilot_shadow_mode" in api_ma.ALLOWED_SETTING_KEYS
    assert api_ma.VALIDATORS["autopilot_message_cap"]("20") is True
    assert api_ma.VALIDATORS["autopilot_message_cap"]("1001") is False
    assert api_ma.VALIDATORS["autopilot_message_cap"]("abc") is False
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("1") is True
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("0") is True
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("2") is False
