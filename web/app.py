# FastAPI веб-морда:
#   • HTML страницы (Jinja2): / /messages /accounts /settings
#   • JSON API под префиксом /api/*: /api/accounts /api/messages /api/settings

from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import database as db
import log_buffer
import scheduler as sched_mod

app = FastAPI(title="Kleinanzeigen Bot")

# Шаблоны лежат в web/templates/ относительно корня проекта
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Секретные ключи — маскируем в GET, не затираем при пустой строке в POST
SENSITIVE_KEYS = {
    "anthropic_api_key",
    "telegram_bot_token",
    "google_drive_credentials_json",
}


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 4 else "••••"


def _masked_settings() -> dict[str, str]:
    """Все настройки с замаскированными секретами — для GET."""
    raw = db.all_settings()
    return {k: (_mask(v) if k in SENSITIVE_KEYS else v) for k, v in raw.items()}


# ============================================================
# HTML страницы
# ============================================================

@app.get("/")
def page_dashboard(request: Request):
    """Дашборд: счётчики и последние сообщения."""
    accs = db.list_accounts(only_active=True)

    # Счётчики по статусам — простой подсчёт через list_messages
    def count(status: str) -> int:
        return len(db.list_messages(status=status, limit=10000))

    stats = {
        "active_accounts": len(accs),
        "pending": count("pending"),
        "approved": count("approved"),
        "sent": count("sent"),
    }
    recent = [dict(r) for r in db.list_messages(limit=10)]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"stats": stats, "recent": recent},
    )


@app.get("/messages")
def page_messages(request: Request, status: Optional[str] = None):
    """Список сообщений с фильтром по статусу."""
    rows = db.list_messages(status=status, limit=200)
    return templates.TemplateResponse(
        request, "messages.html",
        {
            "messages": [dict(r) for r in rows],
            "status_filter": status or "",
        },
    )


SENSITIVE_ACCOUNT_FIELDS = {"gmail_app_password", "kleinanzeigen_password"}


def _account_for_form(row) -> dict[str, Any]:
    """Аккаунт для рендера формы редактирования: секреты — маской."""
    d = dict(row)
    for k in SENSITIVE_ACCOUNT_FIELDS:
        d[k] = _mask(d.get(k))
    return d


@app.get("/accounts")
def page_accounts(request: Request, saved: int = 0, deleted: int = 0,
                  flash_type: str = "", flash_msg: str = ""):
    """Список аккаунтов."""
    rows = db.list_accounts()
    if saved:
        flash = {"type": "success", "message": "Аккаунт сохранён."}
    elif deleted:
        flash = {"type": "success", "message": "Аккаунт удалён."}
    elif flash_msg:
        flash = {"type": flash_type or "info", "message": flash_msg}
    else:
        flash = None
    return templates.TemplateResponse(
        request, "accounts.html",
        {"accounts": [dict(r) for r in rows], "flash": flash},
    )


@app.get("/accounts/new")
def page_account_new(request: Request):
    """Форма создания нового аккаунта."""
    empty = {
        "id": None, "name": "", "gmail_email": "", "gmail_app_password": "",
        "kleinanzeigen_email": "", "kleinanzeigen_password": "", "is_active": 1,
    }
    return templates.TemplateResponse(
        request, "account_form.html",
        {"account": empty, "mode": "new"},
    )


@app.post("/accounts/new")
async def post_account_new(
    name: str = Form(...),
    gmail_email: str = Form(...),
    gmail_app_password: str = Form(...),
    kleinanzeigen_email: str = Form(""),
    kleinanzeigen_password: str = Form(""),
    is_active: str = Form(""),
    auto_ack_enabled: str = Form(""),
):
    """Создать аккаунт."""
    if not name.strip() or not gmail_email.strip() or not gmail_app_password.strip():
        raise HTTPException(400, "Поля name, gmail_email, gmail_app_password обязательны")
    new_id = db.add_account(
        name=name.strip(),
        gmail_email=gmail_email.strip(),
        gmail_app_password=gmail_app_password.strip(),
        kleinanzeigen_email=kleinanzeigen_email.strip() or None,
        kleinanzeigen_password=kleinanzeigen_password.strip() or None,
    )
    db.update_account(
        new_id,
        is_active=1 if is_active else 0,
        auto_ack_enabled=1 if auto_ack_enabled else 0,
    )
    return RedirectResponse(url="/accounts?saved=1", status_code=303)


@app.get("/accounts/{account_id}/edit")
def page_account_edit(request: Request, account_id: int):
    """Форма редактирования. Пароли — маской; пустое поле = не менять."""
    row = db.get_account(account_id)
    if not row:
        raise HTTPException(404, "Аккаунт не найден")
    return templates.TemplateResponse(
        request, "account_form.html",
        {"account": _account_for_form(row), "mode": "edit"},
    )


@app.post("/accounts/{account_id}/edit")
async def post_account_edit(
    account_id: int,
    name: str = Form(...),
    gmail_email: str = Form(...),
    gmail_app_password: str = Form(""),
    kleinanzeigen_email: str = Form(""),
    kleinanzeigen_password: str = Form(""),
    is_active: str = Form(""),
    auto_ack_enabled: str = Form(""),
):
    """Обновить аккаунт. Пустое значение для пароля — не затирать существующий."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Аккаунт не найден")
    fields: dict[str, Any] = {
        "name": name.strip(),
        "gmail_email": gmail_email.strip(),
        "kleinanzeigen_email": kleinanzeigen_email.strip() or None,
        "is_active": 1 if is_active else 0,
        "auto_ack_enabled": 1 if auto_ack_enabled else 0,
    }
    if gmail_app_password.strip():
        fields["gmail_app_password"] = gmail_app_password.strip()
    # Пароль Kleinanzeigen может быть стёрт явно, если поле было непустым ранее, а теперь стало пустым.
    # Но в нашей схеме маскированный плейсхолдер = пустая строка в submit, поэтому — оставляем как с gmail.
    if kleinanzeigen_password.strip():
        fields["kleinanzeigen_password"] = kleinanzeigen_password.strip()
    db.update_account(account_id, **fields)
    return RedirectResponse(url="/accounts?saved=1", status_code=303)


@app.post("/accounts/{account_id}/delete")
def post_account_delete(account_id: int):
    """Удалить аккаунт (вместе с его сообщениями через CASCADE)."""
    if not db.get_account(account_id):
        raise HTTPException(404, "Аккаунт не найден")
    db.delete_account(account_id)
    return RedirectResponse(url="/accounts?deleted=1", status_code=303)


@app.post("/accounts/{account_id}/toggle")
def post_account_toggle(account_id: int):
    """Переключить is_active."""
    row = db.get_account(account_id)
    if not row:
        raise HTTPException(404, "Аккаунт не найден")
    db.update_account(account_id, is_active=0 if row["is_active"] else 1)
    return RedirectResponse(url="/accounts?saved=1", status_code=303)


@app.post("/accounts/{account_id}/test-imap")
def post_account_test_imap(account_id: int):
    """Проверить IMAP-логин для аккаунта."""
    from modules import gmail
    row = db.get_account(account_id)
    if not row:
        raise HTTPException(404, "Аккаунт не найден")
    try:
        ok, msg = gmail.test_connection(row["gmail_email"], row["gmail_app_password"])
    except Exception as e:
        ok, msg = False, f"исключение: {e}"
    flash_text = f"{row['gmail_email']}: {msg}"
    return RedirectResponse(
        url=f"/accounts?flash_type={'success' if ok else 'danger'}&flash_msg={quote(flash_text)}",
        status_code=303,
    )


@app.get("/settings")
def page_settings(request: Request, saved: int = 0, flash_type: str = "", flash_msg: str = ""):
    """Форма настроек. ?saved=1 либо ?flash_type=…&flash_msg=… → показать flash."""
    if saved:
        flash = {"type": "success", "message": "Настройки сохранены."}
    elif flash_msg:
        flash = {"type": flash_type or "info", "message": flash_msg}
    else:
        flash = None
    return templates.TemplateResponse(
        request, "settings.html",
        {"settings": _masked_settings(), "flash": flash},
    )


@app.post("/settings")
async def post_settings(request: Request):
    """Submit формы настроек. Принимает application/x-www-form-urlencoded.

    Пустая строка для секрета НЕ затирает значение — это маскированный плейсхолдер.
    """
    form = await request.form()
    for key, value in form.items():
        if not isinstance(value, str):
            continue
        if key in SENSITIVE_KEYS and value == "":
            continue
        db.set_setting(key, value)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


def _settings_redirect(ok: bool, message: str) -> RedirectResponse:
    """Редирект на /settings с flash-сообщением."""
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/settings?flash_type={'success' if ok else 'danger'}&flash_msg={quote(message)}",
        status_code=303,
    )


@app.post("/backup/test")
def post_backup_test():
    """Проверить credentials Drive и доступ к папке."""
    from modules import backup
    try:
        ok, msg = backup.test_credentials()
    except Exception as e:
        ok, msg = False, f"Ошибка: {e}"
    return _settings_redirect(ok, msg)


@app.post("/backup/run")
def post_backup_run():
    """Сделать бэкап прямо сейчас."""
    from modules import backup
    try:
        info = backup.backup_now()
        msg = f"Бэкап залит: {info.get('name')} ({info.get('size')} байт, id={info.get('id')})"
        return _settings_redirect(True, msg)
    except Exception as e:
        return _settings_redirect(False, f"Бэкап не удался: {e}")


@app.post("/debug/poll-now")
def post_debug_poll_now():
    """Запустить poll_all_accounts() прямо сейчас, не дожидаясь scheduler-а."""
    try:
        result = sched_mod.poll_all_accounts()
        return _settings_redirect(True, f"Poll выполнен: {result}")
    except Exception as e:
        return _settings_redirect(False, f"Poll упал: {e}")


@app.post("/debug/reprocess")
def post_debug_reprocess(count: int = Form(1)):
    """Тест: принудительно переобработать последние N писем (вкл. прочитанные)."""
    if count < 1 or count > 20:
        return _settings_redirect(False, "count должен быть 1..20")
    try:
        result = sched_mod.test_reprocess_latest(count)
        return _settings_redirect(True, f"Реплей выполнен: {result}")
    except Exception as e:
        return _settings_redirect(False, f"Реплей упал: {e}")


@app.post("/debug/send-approved-now")
def post_debug_send_approved_now():
    """Запустить send_approved_replies() прямо сейчас, не дожидаясь scheduler-а."""
    try:
        result = sched_mod.send_approved_replies()
        return _settings_redirect(True, f"Отправка выполнена: {result}")
    except Exception as e:
        return _settings_redirect(False, f"Отправка упала: {e}")


@app.post("/debug/check-reminders-now")
def post_debug_check_reminders_now():
    """Запустить check_reminders() прямо сейчас (не ждать ежечасный scheduler)."""
    try:
        result = sched_mod.check_reminders()
        return _settings_redirect(True, f"Проверка напоминалок: {result}")
    except Exception as e:
        return _settings_redirect(False, f"Проверка упала: {e}")


@app.post("/debug/monitor-now")
def post_debug_monitor_now():
    """Запустить monitor_errors_job() прямо сейчас."""
    try:
        result = sched_mod.monitor_errors_job()
        return _settings_redirect(True, f"Мониторинг логов: {result}")
    except Exception as e:
        return _settings_redirect(False, f"Мониторинг упал: {e}")


# ============================================================
# JSON API (под /api/*) — для интеграций и health-check
# ============================================================

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ============================================================
# Треды (переписка с покупателями)
# ============================================================

# Цвет badge-а для статуса сообщения (Bootstrap)
_STATUS_COLOR = {
    "new": "secondary", "pending": "warning", "approved": "info",
    "edited": "primary", "sent": "success", "sent_debug": "info",
    "skipped": "secondary", "not_sent_disabled": "secondary",
    "error_no_account": "danger", "error_no_original": "danger",
    "error_no_recipient": "danger", "error_send_failed": "danger",
    "error_no_debug_email": "danger",
}


def _status_color(status: Optional[str]) -> str:
    return _STATUS_COLOR.get(status or "", "secondary")


@app.get("/clients")
def page_clients(request: Request):
    """Список покупателей: один row на email, с агрегатами."""
    rows = db.list_clients()
    return templates.TemplateResponse(
        request, "clients.html",
        {"clients": [dict(r) for r in rows], "status_color": _status_color},
    )


@app.get("/clients/{buyer_email}")
def page_client_detail(request: Request, buyer_email: str):
    """Все треды покупателя — полные чаты в accordion-е."""
    threads = db.list_threads_for_client(buyer_email)
    if not threads:
        raise HTTPException(404, "Покупатель не найден")

    # Сводка
    with db.get_conn() as conn:
        info = conn.execute(
            """
            SELECT
                (SELECT buyer_display_name FROM messages
                 WHERE buyer_name = ? AND buyer_display_name IS NOT NULL
                 ORDER BY id DESC LIMIT 1) AS display_name,
                COUNT(*) AS total_msgs,
                COALESCE(SUM(cost_usd), 0) AS total_cost,
                MIN(created_at) AS first_at,
                MAX(created_at) AS last_at
            FROM messages
            WHERE buyer_name = ?
            """,
            (buyer_email, buyer_email),
        ).fetchone()

    # Подгружаем все сообщения каждого треда (для inline-чата в accordion-е)
    threads_full = []
    for t in threads:
        t_dict = dict(t)
        t_dict["messages"] = [dict(r) for r in db.thread_history(t["thread_id"])]
        threads_full.append(t_dict)

    return templates.TemplateResponse(
        request, "client_detail.html",
        {
            "buyer_email": buyer_email,
            "info": dict(info) if info else {},
            "threads": threads_full,
            "status_color": _status_color,
        },
    )


@app.get("/threads")
def page_threads(request: Request, flash_type: str = "", flash_msg: str = ""):
    """Список тредов: каждый тред = одна строка с краткой сводкой."""
    flash = {"type": flash_type or "info", "message": flash_msg} if flash_msg else None
    return templates.TemplateResponse(
        request, "threads.html",
        {
            "threads": [dict(t) for t in db.list_threads()],
            "flash": flash,
            "status_color": _status_color,
        },
    )


@app.get("/threads/{thread_id}")
def page_thread_detail(request: Request, thread_id: str):
    """Детальная страница: вся переписка треда + панель действий по pending-черновику."""
    import json as _json

    rows = db.thread_history(thread_id)
    if not rows:
        raise HTTPException(404, "Тред не найден")
    messages = [dict(r) for r in rows]
    pending = next(
        (m for m in reversed(messages)
         if m["direction"] == "out" or m["status"] in ("new", "pending", "edited")),
        None,
    )
    if pending and pending["status"] not in ("new", "pending", "edited"):
        pending = None

    # Шапка треда
    ad_id_val = next((m.get("ad_id") for m in messages if m.get("ad_id")), None)
    head = {
        "thread_id": thread_id,
        "ad_id": ad_id_val,
        "ad_title": next((m["ad_title"] for m in messages if m["ad_title"]), ""),
        "ad_url": next((m["ad_url"] for m in messages if m["ad_url"]), ""),
        "ad_price": next((m["ad_price"] for m in messages if m["ad_price"]), ""),
        "seller": next((m["seller_name"] for m in messages if m.get("seller_name")), ""),
        "buyer_display": next((m.get("buyer_display_name") for m in messages
                               if m["direction"] == "in" and m.get("buyer_display_name")), ""),
        "buyer_email": next((m["buyer_name"] for m in messages
                             if m["direction"] == "in" and m["buyer_name"]), ""),
    }

    # Бриф объявления (если есть)
    brief = None
    if ad_id_val:
        bf_row = db.get_ad_brief(ad_id_val)
        if bf_row:
            try:
                key_facts = _json.loads(bf_row["key_facts_json"] or "{}")
            except _json.JSONDecodeError:
                key_facts = {}
            brief = {
                "brief_md": bf_row["brief_md"],
                "key_facts": key_facts,
                "updated_at": bf_row["updated_at"],
            }

    # Уроки по этому объявлению
    lessons = [dict(r) for r in db.list_lessons_for_ad(ad_id_val or "", limit=10)]

    return templates.TemplateResponse(
        request, "thread_detail.html",
        {
            "head": head, "messages": messages, "pending": pending,
            "brief": brief, "lessons": lessons,
            "status_color": _status_color,
        },
    )


@app.post("/threads/{thread_id}/approve/{msg_id}")
def post_thread_approve(thread_id: str, msg_id: int):
    """Одобрить + сразу отправить. Scheduler — страховка на случай ошибки."""
    row = db.get_message(msg_id)
    if not row or row["gmail_thread_id"] != thread_id:
        raise HTTPException(404)
    db.update_message(msg_id, status="approved")
    result = sched_mod.send_one(msg_id)
    flash_type = "success" if result["kind"] == "sent" else (
        "warning" if result["kind"] == "skipped" else "danger"
    )
    return RedirectResponse(
        url=f"/threads/{quote(thread_id)}?flash_type={flash_type}&flash_msg={quote(result['message'])}",
        status_code=303,
    )


@app.post("/threads/{thread_id}/skip/{msg_id}")
def post_thread_skip(thread_id: str, msg_id: int):
    """Пропустить черновик."""
    row = db.get_message(msg_id)
    if not row or row["gmail_thread_id"] != thread_id:
        raise HTTPException(404)
    db.update_message(msg_id, status="skipped")
    return RedirectResponse(
        url=f"/threads/{quote(thread_id)}?flash_type=info&flash_msg={quote('Пропущено')}",
        status_code=303,
    )


# ============================================================
# Live логи и статус задач (в реальном времени)
# ============================================================

@app.get("/logs")
def page_logs(request: Request):
    """Страница с авто-обновляемым окном логов."""
    return templates.TemplateResponse(request, "logs.html", {})


@app.get("/api/logs")
def api_logs(since: Optional[int] = None, limit: int = 500) -> dict[str, Any]:
    """Полить новые записи лога с указанного id (или последние limit штук)."""
    return {
        "entries": log_buffer.get_since(since, limit=limit),
        "last_id": log_buffer.last_id(),
    }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """Снимок: scheduler-задачи + быстрые счётчики + стоимость API."""
    accs = db.list_accounts()
    counts = {
        "active_accounts": sum(1 for a in accs if a["is_active"]),
        "total_accounts": len(accs),
        "messages_pending": len(db.list_messages(status="pending", limit=10000)),
        "messages_approved": len(db.list_messages(status="approved", limit=10000)),
        "messages_sent": len(db.list_messages(status="sent", limit=10000)),
        "messages_sent_debug": len(db.list_messages(status="sent_debug", limit=10000)),
    }
    # Суммарная стоимость API за всё время
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS total_usd, "
            "COALESCE(SUM(tokens_in),0) AS tin, "
            "COALESCE(SUM(tokens_out),0) AS tout, "
            "COUNT(*) AS msg_count "
            "FROM messages WHERE cost_usd IS NOT NULL"
        ).fetchone()
    api_cost = {
        "total_usd": round(row["total_usd"] or 0.0, 6),
        "tokens_in": row["tin"] or 0,
        "tokens_out": row["tout"] or 0,
        "messages_processed": row["msg_count"] or 0,
        "model": config.claude_model(),
    }
    return {"jobs": sched_mod.get_jobs_info(), "counts": counts, "api_cost": api_cost}


@app.get("/api/accounts")
def api_accounts() -> list[dict[str, Any]]:
    def _get(r, key):
        try:
            return r[key]
        except (IndexError, KeyError):
            return None
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "gmail_email": r["gmail_email"],
            "kleinanzeigen_email": r["kleinanzeigen_email"],
            "is_active": bool(r["is_active"]),
            "auto_ack_enabled": bool(_get(r, "auto_ack_enabled")),
            "created_at": r["created_at"],
        }
        for r in db.list_accounts()
    ]


@app.get("/api/messages")
def api_messages(
    status: Optional[str] = None,
    account_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit должен быть от 1 до 1000")
    return [dict(r) for r in db.list_messages(account_id=account_id, status=status, limit=limit)]


@app.get("/api/settings")
def api_get_settings() -> dict[str, str]:
    return _masked_settings()


@app.post("/api/settings")
async def api_post_settings(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Ожидается JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Ожидается JSON-объект")

    updated: list[str] = []
    for key, value in data.items():
        if key in SENSITIVE_KEYS and value == "":
            continue
        db.set_setting(key, "" if value is None else str(value))
        updated.append(key)
    return {"updated": updated, "count": len(updated)}
