"""Бекап без ключа сервис-аккаунта — выключенная фича, а не ошибка."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

from modules import backup
import scheduler


def _cfg(value):
    return patch("modules.backup.config.google_drive_credentials_json", return_value=value)


def test_is_configured_false_when_empty():
    with _cfg(""):
        assert backup.is_configured() is False


def test_is_configured_false_on_invalid_json():
    with _cfg("{not json"):
        assert backup.is_configured() is False


def test_is_configured_false_on_oauth_client_key():
    """Клиентский OAuth-ключ вместо сервис-аккаунта — не годится."""
    with _cfg('{"type": "authorized_user", "client_id": "x"}'):
        assert backup.is_configured() is False


def test_is_configured_true_on_service_account():
    with _cfg('{"type": "service_account", "client_email": "a@b.iam.gserviceaccount.com"}'):
        assert backup.is_configured() is True


def test_run_backup_skips_when_not_configured():
    """Job не падает и не пишет ERROR — иначе hourly-мониторинг шлёт алерт."""
    with patch("scheduler.backup") as mb:
        mb.is_configured.return_value = False
        result = scheduler.run_backup()
        assert "not configured" in result
        mb.backup_and_rotate.assert_not_called()


def test_run_backup_runs_when_configured():
    with patch("scheduler.backup") as mb:
        mb.is_configured.return_value = True
        mb.backup_and_rotate.return_value = {"backup": {"name": "db.sqlite"}, "deleted_old": 2}
        result = scheduler.run_backup()
        assert "OK" in result
        mb.backup_and_rotate.assert_called_once_with(keep=14)


def test_run_backup_still_raises_on_real_failure():
    """Настроенный бекап, который упал — по-прежнему ERROR (это реальная поломка)."""
    with patch("scheduler.backup") as mb:
        mb.is_configured.return_value = True
        mb.backup_and_rotate.side_effect = RuntimeError("Drive API 500")
        try:
            scheduler.run_backup()
        except RuntimeError as e:
            assert "Drive API 500" in str(e)
        else:
            raise AssertionError("должно было пробросить исключение")
