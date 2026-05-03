# Бекап SQLite в Google Drive через сервисный аккаунт.
# Сценарий настройки:
#   1) GCP project → enable Drive API → create service account → download JSON key
#   2) Расшарить целевую папку Google Drive на email сервисного аккаунта (роль Editor)
#   3) JSON ключ и folder_id положить в настройки через веб-морду

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import config
import database as db

logger = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
BACKUP_PREFIX = "kleinanzeigen_"
BACKUP_MIME = "application/x-sqlite3"


# --- Авторизация ---

def _get_drive_service() -> Any:
    """Создать клиент Drive API из credentials JSON в настройках."""
    creds_json = config.google_drive_credentials_json()
    if not creds_json:
        raise RuntimeError("Не задан Google Drive credentials JSON в настройках")
    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Невалидный JSON в google_drive_credentials_json: {e}")

    creds = service_account.Credentials.from_service_account_info(
        info, scopes=DRIVE_SCOPES
    )
    # cache_discovery=False — не пишет .cache в /home (мешает в systemd)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# --- Атомарный снэпшот SQLite ---

def _create_snapshot() -> Path:
    """Создать атомарную копию БД через sqlite3 backup API.

    Безопасно при работающих транзакциях (учитывает WAL).
    Возвращает путь к временному файлу — caller должен удалить его после загрузки.
    """
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix=BACKUP_PREFIX)
    os.close(fd)
    tmp_path = Path(tmp)

    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(tmp_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return tmp_path


# --- Операции с Drive ---

def backup_now() -> dict[str, Any]:
    """Залить текущую БД в указанную папку Google Drive. Возвращает метаданные файла."""
    folder_id = config.google_drive_folder_id()
    if not folder_id:
        raise RuntimeError("Не задан Google Drive folder_id в настройках")

    service = _get_drive_service()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"{BACKUP_PREFIX}{timestamp}.db"

    snapshot = _create_snapshot()
    try:
        metadata = {"name": file_name, "parents": [folder_id]}
        media = MediaFileUpload(str(snapshot), mimetype=BACKUP_MIME, resumable=False)
        result = (
            service.files()
            .create(body=metadata, media_body=media, fields="id, name, size, createdTime")
            .execute()
        )
        logger.info("Backup uploaded: %s (%s bytes)", result.get("name"), result.get("size"))
        return result
    finally:
        try:
            snapshot.unlink()
        except OSError:
            pass


def list_backups() -> list[dict[str, Any]]:
    """Список бэкапов в указанной папке Drive. Сортировка: новые первые."""
    folder_id = config.google_drive_folder_id()
    if not folder_id:
        raise RuntimeError("Не задан Google Drive folder_id в настройках")

    service = _get_drive_service()
    # name содержит префикс, parent совпадает, файл не в корзине
    query = (
        f"'{folder_id}' in parents "
        f"and name contains '{BACKUP_PREFIX}' "
        f"and trashed = false"
    )
    files: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        result = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, size, createdTime)",
                orderBy="createdTime desc",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return files


def cleanup_old_backups(keep: int = 14) -> int:
    """Удалить все бэкапы кроме последних `keep`. Возвращает кол-во удалённых."""
    backups = list_backups()
    to_delete = backups[keep:]
    if not to_delete:
        return 0

    service = _get_drive_service()
    deleted = 0
    for f in to_delete:
        try:
            service.files().delete(fileId=f["id"]).execute()
            deleted += 1
        except HttpError as e:
            logger.warning("Не удалось удалить %s: %s", f.get("name"), e)
    return deleted


def backup_and_rotate(keep: int = 14) -> dict[str, Any]:
    """Удобный entrypoint для scheduler-а: бекап + ротация старых."""
    info = backup_now()
    deleted = cleanup_old_backups(keep)
    return {"backup": info, "deleted_old": deleted}


def test_credentials() -> tuple[bool, str]:
    """Проверка валидности credentials и доступа к указанной папке. Для веб-морды."""
    try:
        service = _get_drive_service()
    except Exception as e:
        return False, f"Credentials: {e}"

    folder_id = config.google_drive_folder_id()
    if not folder_id:
        return False, "Не задан folder_id"

    try:
        about = service.about().get(fields="user").execute()
        email = about.get("user", {}).get("emailAddress", "?")
        # Проверим что папка доступна сервисному аккаунту
        folder = service.files().get(fileId=folder_id, fields="id, name").execute()
        return True, f"OK. Сервисный аккаунт: {email}. Папка: {folder.get('name')}"
    except HttpError as e:
        return False, f"HTTP {e.resp.status}: {e._get_reason()}"
    except Exception as e:
        return False, f"Ошибка: {e}"
