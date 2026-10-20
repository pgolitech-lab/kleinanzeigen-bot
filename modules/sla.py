"""Напоминание оператору, если входящее слишком долго ждёт ответа (SLA).

Медиана ответа — 12 мин, но 10 % писем ждут дольше 5,7 ч. Джоб раз в
CHECK_MINUTES ищет входящие без нашего ответа, у которых только что наступил срок:
обычные — 30 мин (если срок ночью — переносится на утро), горячие — 10 мин
круглосуточно. На каждое письмо — одно напоминание, все — одним сообщением.
Срок считается от created_at (Date-заголовок письма).

Настройки (settings, через config.get; дефолты здесь, пока их нет в config.DEFAULTS):
  sla_reply_minutes      — порог для обычного письма, мин (30)
  sla_hot_reply_minutes  — порог для горячего, мин (10)
  sla_day_start_hour / sla_day_end_hour — «день» по Берлину для обычных писем (8 / 22)
  sla_enabled            — 0 выключает напоминания (1)
"""

from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

import config
import database as db

logger = logging.getLogger(__name__)

CHECK_MINUTES = 5
# Старше этого не напоминаем: иначе первый прогон вывалит весь старый бэклог.
MAX_AGE_HOURS = 12
MAX_ITEMS_IN_MESSAGE = 10
_BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")

# Ждут нашего ответа: входящие в этих статусах.
_WAITING_STATUSES = ("new", "pending", "edited", "deferred")

# Напоминаем только о письмах, чей срок наступил ПОСЛЕ прошлого успешного прохода
# (окно (_LAST_RUN, now]). Так рестарт не повторяет сводку по старым письмам даже
# без колонки sla_reminded_at; срок, наступивший пока бот лежал, не догоняем.
_LAST_RUN: Optional[datetime] = None
# Доп. дедуп внутри процесса (и колонка sla_reminded_at, если её добавила db).
_REMINDED: set[int] = set()


def _int_setting(key: str, default: int) -> int:
    raw = config.get(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _has_column(name: str) -> bool:
    from modules.incoming import _messages_has_column
    return _messages_has_column(name)


_UTC = zoneinfo.ZoneInfo("UTC")


def _shift_into_daytime(due_utc: datetime) -> datetime:
    """Срок обычного письма, выпавший на ночь, переносим на начало дня (по Берлину)."""
    start_h = _int_setting("sla_day_start_hour", 8)
    end_h = _int_setting("sla_day_end_hour", 22)
    local = due_utc.replace(tzinfo=_UTC).astimezone(_BERLIN)
    if start_h <= local.hour < end_h:
        return due_utc
    day = local if local.hour < start_h else local + timedelta(days=1)
    moved = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
    return moved.astimezone(_UTC).replace(tzinfo=None)


def find_overdue(now_utc: Optional[datetime] = None,
                 since_utc: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Входящие без нашего ответа, у которых срок ответа наступил в окне (since, now].

    Срок = created_at + порог (горячее — sla_hot_reply_minutes, обычное —
    sla_reply_minutes, ночью переносится на утро). since по умолчанию — now минус
    два интервала джоба."""
    now = now_utc or datetime.utcnow()
    since = since_utc or (now - timedelta(minutes=2 * CHECK_MINUTES))
    normal_min = _int_setting("sla_reply_minutes", 30)
    hot_min = _int_setting("sla_hot_reply_minutes", 10)
    has_hot = _has_column("is_hot")
    has_reminded = _has_column("sla_reminded_at")
    oldest = (now - timedelta(hours=MAX_AGE_HOURS)).isoformat()
    newest = (now - timedelta(minutes=min(normal_min, hot_min))).isoformat()
    marks = ",".join("?" * len(_WAITING_STATUSES))
    hot_col = "m.is_hot" if has_hot else "0"
    reminded_cond = "AND m.sla_reminded_at IS NULL" if has_reminded else ""
    sql = f"""
        SELECT m.id, m.gmail_thread_id, m.created_at, m.ad_title, m.buyer_display_name,
               m.buyer_name, m.account_id, {hot_col} AS is_hot
        FROM messages m
        WHERE m.direction = 'in' AND COALESCE(m.is_auto_ack, 0) = 0
          AND m.status IN ({marks})
          AND COALESCE(m.gmail_thread_id, '') != ''
          AND m.created_at >= ? AND m.created_at <= ?
          {reminded_cond}
          AND NOT EXISTS (
              SELECT 1 FROM messages r
              WHERE r.gmail_thread_id = m.gmail_thread_id
                AND r.status IN ('sent', 'sent_debug') AND COALESCE(r.is_auto_ack, 0) = 0
                AND r.sent_at IS NOT NULL AND r.sent_at >= m.created_at
          )
        ORDER BY m.created_at
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (*_WAITING_STATUSES, oldest, newest)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] in _REMINDED:
            continue
        hot = bool(r["is_hot"])
        try:
            created = datetime.fromisoformat(r["created_at"][:19])
        except ValueError:
            continue
        due = created + timedelta(minutes=hot_min if hot else normal_min)
        if not hot:
            due = _shift_into_daytime(due)  # обычные письма ночью не будим
        if not (since < due <= now):
            continue
        out.append({**dict(r), "is_hot": hot, "waited_min": (now - created).total_seconds() / 60})
    return out


def _fmt_wait(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} мин"
    return f"{minutes / 60:.1f} ч"


def sla_reminders_job(now_utc: Optional[datetime] = None) -> str:
    """Job: одно сообщение оператору со всеми письмами, которые только что просрочили порог."""
    if config.get("sla_enabled") in ("0", "false"):
        return "sla: выключено"
    global _LAST_RUN
    from modules import telegram_bot
    now = now_utc or datetime.utcnow()
    overdue = find_overdue(now, _LAST_RUN)
    if not overdue:
        _LAST_RUN = now
        return "sla: ok"
    overdue.sort(key=lambda x: (not x["is_hot"], -x["waited_min"]))
    h = telegram_bot._html
    lines = [f"⏰ <b>Ждут ответа</b>: {len(overdue)}"]
    for it in overdue[:MAX_ITEMS_IN_MESSAGE]:
        who = it.get("buyer_display_name") or it.get("buyer_name") or "?"
        lines.append(f"{'🔥 ' if it['is_hot'] else ''}#{it['id']} · {h(str(who)[:40])} · "
                     f"{h((it.get('ad_title') or '')[:40])} · ждёт {_fmt_wait(it['waited_min'])}")
    if len(overdue) > MAX_ITEMS_IN_MESSAGE:
        lines.append(f"…и ещё {len(overdue) - MAX_ITEMS_IN_MESSAGE}")
    try:
        telegram_bot.notify("\n".join(lines), "dashboard", label="📋 Открыть входящие")
    except Exception as e:
        # Не помечаем напомненными — попробуем в следующий проход.
        logger.warning("sla: напоминание не ушло: %s", e)
        return f"sla: отправка не удалась: {e}"
    _LAST_RUN = now
    ids = [it["id"] for it in overdue]
    _REMINDED.update(ids)
    if _has_column("sla_reminded_at"):
        stamp = datetime.utcnow().isoformat()
        for mid in ids:
            db.update_message(mid, sla_reminded_at=stamp)
    return f"sla: напомнили про {len(ids)}"
