# Модуль конфигурации. Хранит настройки в таблице settings БД.
# Все значения могут редактироваться через веб-морду; здесь — только дефолты и геттеры.

from typing import Optional

import database as db

# --- Системный промпт для Claude (по умолчанию) ---
DEFAULT_SYSTEM_PROMPT = """Ты опытный продавец автозапчастей с многолетним стажем на немецком рынке Kleinanzeigen.de.

Твоя задача — вести переписку с покупателем от имени продавца. Стиль общения:
- Уверенный, вежливый, профессиональный
- Держишь цену уверенно, не уступаешь без причины
- Максимальная скидка — 10% от исходной цены, и только при настойчивом торге
- На технические вопросы отвечаешь по существу на основе описания объявления
- Не выдумываешь характеристики, которых нет в описании
- Кратко: 2–4 предложения в ответе

Контекст объявления, текущий торг и история переписки будут переданы в сообщении пользователя."""

# --- Дефолтные значения настроек ---
DEFAULTS: dict[str, str] = {
    "anthropic_api_key": "",
    "claude_model": "claude-sonnet-4-6",
    "telegram_bot_token": "",
    "telegram_chat_id": "242994225",
    # Comma-separated список chat_id которым разрешено пользоваться ботом.
    # Пусто = только telegram_chat_id. Использовать для group-mode (один отрицательный
    # group_id) или для multi-operator (несколько личных DM-id через запятую).
    "telegram_authorized": "",
    # CSV chat_id-ов операторских DM. Если задано — карточки рассылаются КАЖДОМУ
    # из них вместо одного `telegram_chat_id`. Локи (5 мин на оператора) применяются.
    # Пустое = старое поведение (один `telegram_chat_id`, обычно группа).
    "telegram_operator_dm_ids": "",
    "google_drive_credentials_json": "",
    "google_drive_folder_id": "",
    "gmail_poll_interval_sec": "60",
    # IMAP-фильтр FROM. Substring-match по заголовку отправителя.
    # "kleinanzeigen.de" покрывает @kleinanzeigen.de и @*.kleinanzeigen.de.
    # Пустое значение = брать всё подряд (только для отладки).
    "gmail_from_filter": "kleinanzeigen.de",
    # Максимальный возраст inquiry в днях (по Date-header). Старше — скип.
    # 0 = без лимита (для дебага). Защита от древних писем по проданным/удалённым объявлениям.
    "inquiry_max_age_days": "7",
    "backup_interval_hours": "24",
    "max_discount_percent": "10",
    # Автопилот (Track B Increment 2): персистентный cap на кол-во авто-отправок
    # на тред + shadow-режим (генерировать/логировать, но НЕ слать). Раскатка — Инкремент 3.
    "autopilot_message_cap": "20",
    "autopilot_shadow_mode": "1",
    "web_port": "8080",
    "web_host": "0.0.0.0",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    # Предохранитель отправки. По умолчанию — disabled,
    # чтобы при первом запуске бот ничего не отправил случайно.
    "send_mode": "disabled",          # production | redirect | disabled
    "debug_email": "",                # куда слать в режиме redirect
    # Напоминалки клиентам, которые не ответили N дней.
    # По умолчанию выключены — оператор должен явно включить.
    "reminders_enabled": "0",         # "0" / "1"
    "reminder_after_days": "1",       # через сколько дней молчания предлагать ping
    # Пауза polling-а (через /pause в Telegram). Когда "1" — scheduler.poll_all_accounts ничего не делает.
    "polling_paused": "0",
    # Дата последней daily summary (ISO). Используется для дедупа.
    "last_daily_summary_date": "",
    # Snapshot баланса Anthropic API (вводится оператором в /settings когда сверяет с
    # console.anthropic.com). Текущий остаток оценивается как snapshot - SUM(cost_usd)
    # по всем messages с created_at > snapshot_at.
    "api_balance_snapshot_usd": "",  # напр. "17.92"
    "api_balance_snapshot_at": "",   # ISO datetime, напр. "2026-05-04 00:00:00"

    # --- Параметры стиля чат-пузырей в веб-морде (/threads/{id}, /clients/{email}) ---
    # Все значения числовые, единицы — в комментариях. Меняются через /settings → секция «💬 Стиль чата»
    "chat_font_em": "1.0",            # размер шрифта пузыря (em от родителя)
    "chat_padding_v_rem": "0.25",     # верт. padding пузыря (rem)
    "chat_padding_h_rem": "0.55",     # гор. padding пузыря (rem)
    "chat_max_width_pct": "62",       # макс. ширина пузыря (%)
    "chat_radius_rem": "0.5",         # border-radius пузыря (rem)
    "chat_row_gap_rem": "0.35",       # промежуток между рядами (rem)
    "chat_meta_font_em": "0.78",      # шрифт мета-строки (📤 timestamp · sent)
    "chat_secondary_font_em": "0.92", # шрифт RU-перевода (от шрифта пузыря)

    # --- Разведка рынка (market scout) ---
    # Авто-прогон разведки по расписанию. "1" = scheduler сам обновляет данные.
    # По умолчанию ВЫКЛ — оператор запускает вручную (скрапинг может ловить
    # блокировки/пустые страницы, что нежелательно делать без присмотра).
    "scout_auto_enabled": "0",
    # Интервал авто-прогона в часах.
    "scout_interval_hours": "6",
    # Пауза между загрузками страниц поиска (вежливость к сайту), секунды.
    "scout_page_delay_sec": "1.5",
    # Через сколько дней БЕЗ повторного обнаружения объявление считается снятым
    # (active=0). Деактивация по возрасту, а не «не виден в этом прогоне» —
    # чтобы один упавший/заблокированный прогон не обнулял всю базу.
    "scout_stale_days": "7",
}


def bootstrap() -> None:
    """Записать дефолтные значения для отсутствующих ключей. Вызывается при старте."""
    db.init_db()
    existing = db.all_settings()
    for key, value in DEFAULTS.items():
        if key not in existing:
            db.set_setting(key, value)


# --- Геттеры с приведением типов ---

def get(key: str, default: Optional[str] = None) -> Optional[str]:
    """Получить строковое значение настройки."""
    return db.get_setting(key, default if default is not None else DEFAULTS.get(key))


def get_int(key: str, default: Optional[int] = None) -> int:
    """Получить целочисленное значение."""
    raw = get(key)
    if raw is None or raw == "":
        if default is not None:
            return default
        return int(DEFAULTS.get(key, "0"))
    try:
        return int(raw)
    except ValueError:
        return default if default is not None else 0


def set(key: str, value: str) -> None:
    """Записать значение."""
    db.set_setting(key, value)


# --- Удобные обёртки для частых ключей ---

def anthropic_api_key() -> str:
    return get("anthropic_api_key") or ""


def claude_model() -> str:
    return get("claude_model") or DEFAULTS["claude_model"]


def telegram_bot_token() -> str:
    return get("telegram_bot_token") or ""


def telegram_chat_id() -> str:
    return get("telegram_chat_id") or DEFAULTS["telegram_chat_id"]


def telegram_operator_dm_ids() -> list[str]:
    """Список chat_id-ов операторских DM для fanout-режима.

    Если пусто — fanout выключен, используется старый одиночный telegram_chat_id.
    """
    raw = (get("telegram_operator_dm_ids") or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def telegram_authorized_ids() -> "set[str]":
    """Allowlist chat_id-ов которым разрешено пользоваться ботом.

    ВСЕГДА включает telegram_chat_id (primary куда идут уведомления) +
    дополнительные id из настройки telegram_authorized (comma-list).
    """
    # NB: builtins.set нужен потому что в этом модуле есть `def set(key, value)`
    # шадоит builtin (см. CLAUDE.md → "не используй имя set в config.py").
    import builtins
    ids = builtins.set()
    primary = telegram_chat_id()
    if primary:
        ids.add(primary)
    extra = (get("telegram_authorized") or "").strip()
    if extra:
        ids.update(x.strip() for x in extra.split(",") if x.strip())
    return ids


def google_drive_credentials_json() -> str:
    return get("google_drive_credentials_json") or ""


def google_drive_folder_id() -> str:
    return get("google_drive_folder_id") or ""


def gmail_poll_interval_sec() -> int:
    return get_int("gmail_poll_interval_sec", 60)


def gmail_from_filter() -> str:
    """Фильтр FROM на стороне IMAP. Может быть пустой строкой."""
    val = get("gmail_from_filter")
    return val if val is not None else DEFAULTS["gmail_from_filter"]


def inquiry_max_age_days() -> int:
    """Макс. возраст входящего inquiry. 0 = без лимита."""
    return get_int("inquiry_max_age_days", 7)


def backup_interval_hours() -> int:
    return get_int("backup_interval_hours", 24)


def max_discount_percent() -> int:
    return get_int("max_discount_percent", 10)


def autopilot_message_cap() -> int:
    """Максимум авто-отправок автопилота на тред (персистентный cap)."""
    return get_int("autopilot_message_cap", 20)


def autopilot_shadow_mode() -> bool:
    """Shadow-режим автопилота: генерировать и логировать, но НЕ отправлять.
    По умолчанию включён (безопасно) — снимается явно при раскатке в Инкременте 3."""
    return (get("autopilot_shadow_mode") or "1").strip() == "1"


def web_port() -> int:
    return get_int("web_port", 8080)


def web_host() -> str:
    return get("web_host") or DEFAULTS["web_host"]


def system_prompt() -> str:
    return get("system_prompt") or DEFAULT_SYSTEM_PROMPT


def send_mode() -> str:
    """Режим отправки: production | redirect | disabled.
    Любое неизвестное значение трактуется как disabled (fail-safe)."""
    val = (get("send_mode") or "disabled").strip().lower()
    return val if val in ("production", "redirect", "disabled") else "disabled"


def debug_email() -> str:
    return (get("debug_email") or "").strip()


def reminders_enabled() -> bool:
    return (get("reminders_enabled") or "0").strip() == "1"


def reminder_after_days() -> float:
    """Через сколько дней молчания предлагать ping. Поддерживается дробное значение
    (0.5 = 12ч, 0.04 ≈ 1ч) для оперативной настройки порога без изменения единицы."""
    raw = (get("reminder_after_days") or "1").strip()
    try:
        return float(raw)
    except ValueError:
        return 1.0


def scout_auto_enabled() -> bool:
    return (get("scout_auto_enabled") or "0").strip() == "1"


def scout_interval_hours() -> int:
    return get_int("scout_interval_hours", 6)


def scout_page_delay_sec() -> float:
    raw = (get("scout_page_delay_sec") or "1.5").strip()
    try:
        return float(raw)
    except ValueError:
        return 1.5


def scout_stale_days() -> int:
    return get_int("scout_stale_days", 7)


def polling_paused() -> bool:
    return (get("polling_paused") or "0").strip() == "1"


def set_polling_paused(paused: bool) -> None:
    set("polling_paused", "1" if paused else "0")


if __name__ == "__main__":
    # Ручной bootstrap: python config.py
    bootstrap()
    print("Настройки инициализированы дефолтами:")
    for k, v in db.all_settings().items():
        shown = v if len(v or "") < 60 else v[:57] + "..."
        print(f"  {k} = {shown}")
