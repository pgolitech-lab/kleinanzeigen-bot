# Память LLM: уроки оператора и память о клиенте для промптов + извлечение новых уроков.
#
#   lessons_for(msg_row)            → строки уроков, релевантных письму
#   format_lessons(rows)            → блок «уроки» для промпта
#   client_memory_text(msg_row)     → другие переписки этого покупателя + досье
#   learn_from_sent(message_id)     → после отправки: если оператор переписал черновик LLM,
#                                     Haiku формулирует урок → db.add_lesson (вызывает api/outgoing)
#   save_lesson(**fields)           → db.add_lesson, лишние поля (колонок ещё нет) отбрасываются
#
# Источники данных — функции db (find_relevant_lessons, find_related_inquiries, get_dossier_for_email /
# get_dossier, get_client_profile, thread_history). Функции и колонки, которые добавляет миграция db
# (досье, поля уроков, llm_draft_*), проверяются при обращении: если их нет — ERROR в лог один раз за
# процесс (monitor_errors поднимет алерт), а генерация ответа продолжается без этой части.

import difflib
import inspect
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


def _error_once(key: str, msg: str, *args: Any) -> None:
    """ERROR один раз за процесс: отсутствие миграции db — поломка, а не норма."""
    if key not in _warned:
        _warned.add(key)
        logger.error(msg, *args)


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
    params = inspect.signature(db.find_relevant_lessons).parameters
    if "buyer_email" in params and "category" in params:
        kwargs.update(buyer_email=_rget(msg_row, "buyer_name"), category=ad_category(ad_id))
    if "include_mined" in params:
        kwargs["include_mined"] = True  # format_lessons выводит lesson_ru смайненных уроков
    else:
        _error_once("lessons_sig", "db.find_relevant_lessons без buyer_email/category (нет миграции db) — "
                                   "уроки ранжируются только по объявлению/аккаунту")
    return [dict(r) for r in db.find_relevant_lessons(**kwargs)]


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
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            return cols  # таблицы ещё нет — не кэшируем, иначе не увидим её до рестарта
        _COLS_CACHE[key] = cols
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
        _error_once("llm_draft_cols", "messages без llm_draft_de/llm_draft_ru (нет миграции db) — "
                                      "исходный черновик LLM не сохраняется, уроки из правок не появятся")
    return out


def save_lesson(**fields: Any) -> Optional[int]:
    """db.add_lesson только с существующими колонками (миграция db может отставать).

    Если нет колонки lesson_ru, а урок передан — НЕ пишем строку без самого урока:
    ERROR в лог и None.
    """
    cols = table_columns("lessons")
    dropped = sorted(k for k in fields if k not in cols)
    if "lesson_ru" in dropped and fields.get("lesson_ru"):
        logger.error("lessons: нет колонки lesson_ru (нет миграции db) — урок не сохранён: %s",
                     _clip(fields["lesson_ru"], 120))
        return None
    if dropped:
        _error_once("lesson_cols:" + ",".join(dropped),
                    "lessons: нет колонок %s (нет миграции db) — эти поля не сохраняются", dropped)
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
    if _rget(last, "direction") == "out" or _rget(last, "status") in ("sent", "sent_debug", "sent_external"):
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


def _dossier_text(msg_row: Any) -> str:
    """Досье покупателя (client_dossiers: по relay-email, иначе по аккаунту + имени)."""
    get_by_email = getattr(db, "get_dossier_for_email", None)
    get_by_name = getattr(db, "get_dossier", None)
    if get_by_email is None and get_by_name is None:
        _error_once("dossier_fn", "db.get_dossier_for_email/get_dossier нет (нет миграции db) — "
                                  "досье покупателя в промпт не попадает")
        return ""
    row = None
    buyer = _rget(msg_row, "buyer_name")
    if get_by_email is not None and buyer:
        row = get_by_email(buyer)
    probable = False
    if row is None and get_by_name is not None and _rget(msg_row, "buyer_display_name"):
        row = get_by_name(_rget(msg_row, "account_id"), _rget(msg_row, "buyer_display_name"))
        probable = row is not None  # нашли только по имени — возможно, другой человек
    if row is None:
        return ""
    summary = (_rget(row, "summary_md") or "").strip()
    if not summary:
        return ""
    head = "Досье (ВОЗМОЖНО другой человек с тем же именем — проверяй): " if probable else "Досье: "
    return head + _clip(summary, 900)


def client_memory_text(msg_row: Any, max_chars: int = MEMORY_MAX_CHARS) -> str:
    """Что мы знаем об этом покупателе: досье + заметка оператора + его другие переписки.

    Другие треды — db.find_related_inquiries(include_maybe=True): maybe_same=1 (совпало имя
    в том же аккаунте, другой relay-email) помечается как «ВОЗМОЖНО тот же», цены оттуда не факт.
    """
    buyer = _rget(msg_row, "buyer_name") or ""
    thread_id = _rget(msg_row, "gmail_thread_id") or ""
    parts: list[str] = []

    dossier = _dossier_text(msg_row)
    if dossier:
        parts.append(dossier)
    profile = db.get_client_profile(buyer) if buyer else None
    note = (_rget(profile, "note") or "").strip() if profile is not None else ""
    if note:
        parts.append(f"Заметка оператора: {_clip(note, 300)}")

    lines: list[str] = []
    if buyer:
        related = db.find_related_inquiries(
            buyer, exclude_thread_id=thread_id or None, limit=MEMORY_THREADS,
            display_name=_rget(msg_row, "buyer_display_name"),
            account_id=_rget(msg_row, "account_id"), include_maybe=True,
        )
        seen = {thread_id}
        account_id = _rget(msg_row, "account_id")
        for r in related:
            tid = _rget(r, "gmail_thread_id")
            # Только переписки ЭТОГО аккаунта: иначе бот выдаст клиенту связь между
            # нашими 5 аккаунтами и подсунет цены чужого товара
            if account_id is not None and _rget(r, "account_id") != account_id:
                continue
            if tid and tid not in seen:
                seen.add(tid)
                line = _thread_line(tid, probable=bool(_rget(r, "maybe_same")))
                if line:
                    lines.append(line)
    if lines:
        parts.append("Другие переписки с ним (сначала точные совпадения, от новых к старым):\n"
                     + "\n".join(lines))
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
    Иначе Sonnet формулирует, что исправил оператор → lessons (source_type='operator_edit',
    source_ref=message_id — повторный вызов обновляет тот же урок).
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
    if not draft_client.strip():
        if llm_draft_client is None and "llm_draft_de" not in table_columns("messages"):
            _error_once("learn_cols", "learn_from_sent: нет колонки messages.llm_draft_de (нет миграции db) — "
                                      "уроки из правок оператора не извлекаются")
        else:
            logger.info("learn_from_sent msg=%s: черновика LLM не было (оператор писал сам) — урока нет",
                        message_id)
        return None
    if not sent_client.strip():
        logger.warning("learn_from_sent msg=%s: отправленный текст пуст — урока нет", message_id)
        return None
    ratio = _similar(draft_client, sent_client)
    if ratio >= SAME_TEXT_RATIO:
        logger.info("learn_from_sent msg=%s: отправлено почти как предложил LLM (%.2f) — урока нет",
                    message_id, ratio)
        return None
    lesson = claude.extract_operator_lesson(
        client_message=_rget(row, "de_client") or "",
        llm_draft=draft_client, llm_draft_ru=draft_ru,
        sent_text=sent_client, sent_text_ru=_rget(row, "ru_translation") or "",
        ad_title=_rget(row, "ad_title") or "",
    )
    if not lesson.get("lesson_ru"):
        logger.info("learn_from_sent msg=%s: правка косметическая — урока нет", message_id)
        return None
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
        # идемпотентно (uq_lessons_source): повторная отправка/правка обновляет урок письма
        source="operator_edit",           # db: source → source_type, фильтр find_relevant_lessons
        source_type="operator_edit",
        source_ref=str(message_id),
        gmail_thread_id=_rget(row, "gmail_thread_id"),
        buyer_email=_rget(row, "buyer_name"),
        category=ad_category(_rget(row, "ad_id")),
    )
