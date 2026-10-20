# Память LLM: уроки оператора и память о клиенте для промптов + извлечение новых уроков.
#
#   lessons_for(msg_row)            → строки уроков, релевантных письму
#   format_lessons(rows)            → блок «уроки» для промпта
#   client_memory_text(msg_row)     → другие переписки этого покупателя + досье
#   learn_from_sent(message_id)     → после отправки: если оператор переписал черновик LLM,
#                                     Haiku формулирует урок → db.add_lesson (вызывает api/outgoing)
#   save_lesson(**fields)           → db.add_lesson, лишние поля (колонок ещё нет) отбрасываются
#
# Источники данных — функции db (find_relevant_lessons, find_related_inquiries, get_client_profile,
# thread_history). Новые функции db (мягкое совпадение покупателя, досье) берутся через getattr:
# пока их нет, соответствующая часть просто пропускается (с записью в лог один раз).

import difflib
import json
import logging
from typing import Any, Optional

import database as db

logger = logging.getLogger(__name__)

LESSONS_LIMIT = 5
LESSONS_MAX_CHARS = 1800
MEMORY_MAX_CHARS = 2200
MEMORY_THREADS = 5
# Текст считаем «не переписанным», если похож на черновик LLM не меньше чем на столько
SAME_TEXT_RATIO = 0.9

_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def _rget(row: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


def _clip(text: Optional[str], limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ─────────────────────────── Уроки ───────────────────────────

def ad_category(ad_id: Optional[str]) -> Optional[str]:
    """Категория объявления из брифа (key_facts.category) или None."""
    if not ad_id:
        return None
    bf = db.get_ad_brief(ad_id)
    if not bf:
        return None
    try:
        return (json.loads(bf["key_facts_json"] or "{}").get("category") or None)
    except (json.JSONDecodeError, AttributeError):
        return None


def lessons_for(msg_row: Any, limit: int = LESSONS_LIMIT) -> list[dict[str, Any]]:
    """Релевантные уроки: то же объявление → тот же покупатель → категория → аккаунт → прочие."""
    ad_id = _rget(msg_row, "ad_id")
    kwargs: dict[str, Any] = dict(ad_id=ad_id, account_id=_rget(msg_row, "account_id"), limit=limit)
    try:
        rows = db.find_relevant_lessons(
            **kwargs, buyer_email=_rget(msg_row, "buyer_name"), category=ad_category(ad_id),
        )
    except TypeError:
        # db ещё без ранжирования по покупателю/категории — старая сигнатура
        _warn_once("lessons_sig", "find_relevant_lessons без buyer_email/category — ранжирую по ad/аккаунту")
        rows = db.find_relevant_lessons(**kwargs)
    return [dict(r) for r in rows]


def format_lessons(lessons: Optional[list[dict[str, Any]]], max_chars: int = LESSONS_MAX_CHARS) -> str:
    """Блок уроков для промпта: короткие выводы (lesson_ru) или пары «бот → оператор»."""
    if not lessons:
        return ""
    items: list[str] = []
    total = 0
    for ls in lessons:
        lesson = (ls.get("lesson_ru") or "").strip()
        sit = _clip(ls.get("client_situation_ru"), 200)
        if lesson:
            item = f"- {_clip(lesson, 400)}" + (f" (ситуация: {sit})" if sit else "")
        else:
            bad = _clip(ls.get("bad_draft_ru"), 300)
            good = _clip(ls.get("good_answer_ru"), 300)
            if not good or bad == good:
                continue  # пара без исправления ничему не учит
            item = (f"- Клиент: {sit or '—'}\n  Бот предложил: {bad or '—'}\n"
                    f"  Оператор отправил (так правильно): {good}")
        if total + len(item) > max_chars and items:
            break
        items.append(item)
        total += len(item)
    if not items:
        return ""
    return ("=== УРОКИ ИЗ ПРОШЛЫХ ПЕРЕГОВОРОВ (учитывай стиль, тон, тактику) ===\n"
            + "\n".join(items))


_COLS_CACHE: dict[tuple[str, str], set[str]] = {}


def table_columns(table: str) -> set[str]:
    """Колонки таблицы (кэш на процесс и файл БД: миграции идут при старте)."""
    key = (str(db.DB_PATH), table)
    if key not in _COLS_CACHE:
        with db.get_conn() as conn:
            _COLS_CACHE[key] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return _COLS_CACHE[key]


def llm_draft_fields(client_text: str, ru_text: str) -> dict[str, Any]:
    """Поля «исходный черновик LLM» для messages, если db уже добавила колонки
    llm_draft_de / llm_draft_ru (по ним learn_from_sent видит правку оператора)."""
    cols = table_columns("messages")
    out: dict[str, Any] = {}
    if "llm_draft_de" in cols:
        out["llm_draft_de"] = client_text
    if "llm_draft_ru" in cols:
        out["llm_draft_ru"] = ru_text
    if not out:
        _warn_once("llm_draft_cols", "messages без llm_draft_de/llm_draft_ru — черновик LLM не сохраняю")
    return out


def save_lesson(**fields: Any) -> Optional[int]:
    """db.add_lesson только с существующими колонками (миграция db может отставать)."""
    cols = table_columns("lessons")
    dropped = sorted(k for k in fields if k not in cols)
    if dropped:
        _warn_once("lesson_cols:" + ",".join(dropped),
                   "lessons: нет колонок %s — поля не сохраняются (ждём миграцию db)", dropped)
    clean = {k: v for k, v in fields.items() if k in cols and k != "created_at"}
    if not clean:
        return None
    return db.add_lesson(**clean)


# ─────────────────────────── Память о клиенте ───────────────────────────

def _thread_outcome(rows: list[Any]) -> str:
    sold = [r for r in rows if _rget(r, "sold_price_eur")]
    if sold:
        return f"продано за {sold[-1]['sold_price_eur']:g} €"
    statuses = {_rget(r, "status") for r in rows}
    if "skipped_sold" in statuses:
        return "закрыт: товар продан (другому или ему — цена не записана)"
    last = rows[-1]
    if _rget(last, "direction") == "out" or _rget(last, "status") in ("sent", "sent_debug"):
        return "последним писали мы — клиент не ответил"
    if _rget(last, "status") == "skipped":
        return "оператор пропустил"
    return "переписка не завершена"


def _thread_line(tid: str, probable: bool) -> Optional[str]:
    rows = db.thread_history(tid)
    if not rows:
        return None
    first, last = rows[0], rows[-1]
    when = (_rget(first, "created_at") or "")[:10]
    last_when = (_rget(last, "sent_at") or _rget(last, "created_at") or "")[:10]
    title = _clip(next((_rget(r, "ad_title") for r in rows if _rget(r, "ad_title")), "?"), 70)
    summary = ""
    for r in reversed(rows):
        bj = _rget(r, "deal_brief_json")
        if bj:
            try:
                summary = _clip(json.loads(bj).get("summary_ru"), 160)
            except (json.JSONDecodeError, AttributeError):
                pass
            if summary:
                break
    if not summary:
        asks = [_rget(r, "ru_client") or _rget(r, "de_client") for r in rows if _rget(r, "direction") == "in"]
        summary = "писал: " + _clip(asks[-1] if asks else "", 160) if asks else ""
    mark = "ВОЗМОЖНО тот же человек (совпало имя, не email) — цены оттуда не факт; " if probable else ""
    dates = when if when == last_when else f"{when}…{last_when}"
    return f"- {mark}{dates}, «{title}»: {_thread_outcome(rows)}" + (f". {summary}" if summary else "")


def client_memory_text(msg_row: Any, max_chars: int = MEMORY_MAX_CHARS) -> str:
    """Что мы знаем об этом покупателе: досье + его другие переписки (точные и вероятные)."""
    buyer = _rget(msg_row, "buyer_name") or ""
    thread_id = _rget(msg_row, "gmail_thread_id") or ""
    parts: list[str] = []

    profile = db.get_client_profile(buyer) if buyer else None
    if profile is not None:
        dossier = (_rget(profile, "dossier_ru") or "").strip()
        note = (_rget(profile, "note") or "").strip()
        if dossier:
            parts.append(f"Досье: {_clip(dossier, 900)}")
        if note:
            parts.append(f"Заметка оператора: {_clip(note, 300)}")

    seen = {thread_id}
    lines: list[str] = []
    exact = db.find_related_inquiries(buyer, exclude_thread_id=thread_id, limit=MEMORY_THREADS) if buyer else []
    for r in exact:
        tid = _rget(r, "gmail_thread_id")
        if tid and tid not in seen:
            seen.add(tid)
            line = _thread_line(tid, probable=False)
            if line:
                lines.append(line)
    soft_fn = getattr(db, "find_related_inquiries_soft", None)
    if soft_fn is None:
        _warn_once("soft", "db.find_related_inquiries_soft нет — вероятные совпадения покупателя пропускаю")
    elif len(lines) < MEMORY_THREADS:
        try:
            soft = soft_fn(
                display_name=_rget(msg_row, "buyer_display_name"),
                account_id=_rget(msg_row, "account_id"),
                exclude_thread_id=thread_id, limit=MEMORY_THREADS,
            )
        except Exception:  # noqa: BLE001 — память вспомогательная, генерацию не валим
            logger.exception("find_related_inquiries_soft упал для msg=%s", _rget(msg_row, "id"))
            soft = []
        for r in soft:
            tid = _rget(r, "gmail_thread_id")
            if tid and tid not in seen and len(lines) < MEMORY_THREADS:
                seen.add(tid)
                line = _thread_line(tid, probable=True)
                if line:
                    lines.append(line)
    if lines:
        parts.append("Другие переписки с ним (от новых к старым):\n" + "\n".join(lines))
    if not parts:
        return ""
    text = "=== ЧТО МЫ ЗНАЕМ ОБ ЭТОМ ПОКУПАТЕЛЕ ===\n" + "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text + ("\nИспользуй это: не переспрашивай известное, учитывай прошлые договорённости и цены "
                   "(кроме помеченных «ВОЗМОЖНО»).")


# ─────────────────────────── Урок из правки оператора ───────────────────────────

def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip(), (b or "").strip()).ratio()


def learn_from_sent(
    message_id: int,
    llm_draft_client: Optional[str] = None,
    llm_draft_ru: Optional[str] = None,
) -> Optional[int]:
    """Вызвать ПОСЛЕ успешной отправки письма #message_id.

    llm_draft_client / llm_draft_ru — черновик LLM до правок оператора. Если не переданы,
    берутся из колонок messages.llm_draft_de / llm_draft_ru (когда db их добавит).
    Если отправленный текст почти совпадает с черновиком (или черновика не было) — урока нет.
    Иначе Haiku формулирует, что исправил оператор → lessons (source='operator_edit').
    Возвращает id урока или None. Исключения LLM пробрасываются — вызывающий логирует
    (вызывать в фоне: на отправку это не влияет).
    """
    from modules import claude
    row = db.get_message(message_id)
    if row is None:
        return None
    draft_client = llm_draft_client if llm_draft_client is not None else (_rget(row, "llm_draft_de") or "")
    draft_ru = llm_draft_ru if llm_draft_ru is not None else (_rget(row, "llm_draft_ru") or "")
    sent_client = _rget(row, "de_answer") or ""
    if not draft_client.strip() or not sent_client.strip():
        return None
    if _similar(draft_client, sent_client) >= SAME_TEXT_RATIO:
        return None
    lesson = claude.extract_operator_lesson(
        client_message=_rget(row, "de_client") or "",
        llm_draft=draft_client, llm_draft_ru=draft_ru,
        sent_text=sent_client, sent_text_ru=_rget(row, "ru_translation") or "",
        ad_title=_rget(row, "ad_title") or "",
    )
    if not lesson.get("lesson_ru"):
        return None  # модель решила, что правка не содержит урока (опечатка, мелочь)
    return save_lesson(
        message_id=message_id,
        account_id=_rget(row, "account_id"),
        ad_id=_rget(row, "ad_id"),
        client_lang=_rget(row, "client_lang"),
        client_situation_ru=lesson.get("situation_ru") or _clip(_rget(row, "ru_client"), 300),
        bad_draft_ru=draft_ru or None,
        bad_draft_de=draft_client,
        good_answer_ru=_rget(row, "ru_translation") or None,
        good_answer_de=sent_client,
        lesson_ru=lesson["lesson_ru"],
        source="operator_edit",
        gmail_thread_id=_rget(row, "gmail_thread_id"),
        buyer_email=_rget(row, "buyer_name"),
        category=ad_category(_rget(row, "ad_id")),
    )
