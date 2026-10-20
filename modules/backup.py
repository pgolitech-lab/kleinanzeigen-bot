# Бэкап SQLite делает НЕ процесс бота, а systemd-таймер ops: scripts/backup.sh
# (локальная ротация `sqlite3 .backup` + копия в OneDrive через rclone). Решение
# владельца 2026-10-20: Drive через сервисный аккаунт не работал (нет квоты у SA),
# код удалён.
#
# Модуль оставлен ПЕРЕХОДНОЙ заглушкой, пока scheduler.run_backup / drive_backup-джоб
# и web/app.py /backup/* не удалены их владельцами — чтобы импорт не уронил сервис.
# После этого файл удалить целиком.

import logging

logger = logging.getLogger(__name__)

_MOVED = "бэкап делает systemd-таймер (scripts/backup.sh → OneDrive), а не бот"


def is_configured() -> bool:
    """Всегда False: внутри бота бэкапа больше нет (см. шапку модуля)."""
    return False


def backup_now() -> dict:
    raise RuntimeError(_MOVED)


def backup_and_rotate(keep: int = 14) -> dict:
    raise RuntimeError(_MOVED)


def test_credentials() -> tuple[bool, str]:
    return False, _MOVED
