"""Бэкап вынесен из бота в systemd-таймер ops; modules/backup.py — переходная заглушка."""
from __future__ import annotations

import pytest

from modules import backup


def test_backup_not_in_bot():
    assert backup.is_configured() is False
    ok, msg = backup.test_credentials()
    assert ok is False and "scripts/backup.sh" in msg


def test_manual_backup_fails_loudly():
    with pytest.raises(RuntimeError):
        backup.backup_now()
    with pytest.raises(RuntimeError):
        backup.backup_and_rotate()


def test_no_google_drive_settings_in_config():
    import config
    assert "google_drive_credentials_json" not in config.DEFAULTS
    assert not hasattr(config, "google_drive_credentials_json")
