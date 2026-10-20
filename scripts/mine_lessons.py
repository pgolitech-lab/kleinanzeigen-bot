#!/usr/bin/env python3
"""Разбор всей истории переписок: уроки для LLM + досье покупателей.

Идёт по ЛЮДЯМ: аккаунт + нормализованное имя покупателя (у одного человека на разных
объявлениях разные relay-email Kleinanzeigen; покупатели без имени — по email). Для каждого —
один вызов основной модели со всеми его тредами:
  * итог каждого треда (продано / сорвалось / замолчал / открыт), финальная цена;
  * 0–2 обобщённых урока на тред → lessons (source_type='history_review',
    source_ref='<thread>#<n>' — повтор обновляет, а не дублирует);
  * досье (кто, когда, о чём, цены, договорённости) → client_dossiers (db.upsert_client_dossier).

Идемпотентно: человек пропускается, если досье уже учитывает его последнее письмо
(dossier_max_msg_id) — независимо от того, нашлись ли уроки.
LLM — по подписке (modules/llm.py), не больше 2 вызовов параллельно.
Перед записью в живую БД: cp kleinanzeigen.db kleinanzeigen.db.bak-$(date +%F).

Usage (результаты с реальными данными покупателей — только во /tmp, не в репозиторий):
    python3 scripts/mine_lessons.py --dry-run --limit 10 --db /tmp/copy.db --out /tmp/mine_sample.json
    python3 scripts/mine_lessons.py --thread <gmail_thread_id> --dry-run
    python3 scripts/mine_lessons.py            # весь прогон (пишет в БД)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database as db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mine_lessons")

THREAD_MAX_CHARS = 4000
BUYER_MAX_CHARS = 14000
MAX_THREADS_PER_BUYER = 8

SYSTEM = (
    "Ты аналитик переговоров продавца на Kleinanzeigen.de (б/у автозапчасти: сиденья, скамейки, "
    "консоли для минивэнов PSA и др.). Тебе дают все переписки одного покупателя с нами. "
    "Задачи: (1) итог каждого треда; (2) уроки для бота, который ведёт переговоры за продавца; "
    "(3) короткое досье покупателя для оператора и бота."
)

_AD_FACT = {
    "type": "object",
    "properties": {
        "ad_id": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "our_price": {"type": ["number", "null"]},
        "buyer_offer": {"type": ["number", "null"]},
        "final_price": {"type": ["number", "null"]},
    },
    "required": ["ad_id", "title", "our_price", "buyer_offer", "final_price"],
    "additionalProperties": False,
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "object",
            "properties": {
                "first_contact_at": {"type": ["string", "null"], "description": "ГГГГ-ММ-ДД"},
                "last_contact_at": {"type": ["string", "null"], "description": "ГГГГ-ММ-ДД"},
                "ads": {"type": "array", "items": _AD_FACT},
                "agreements": {"type": "array", "items": {"type": "string"},
                               "description": "договорённости: встреча, доставка, оплата, резерв"},
                "outcome": {"type": "string", "enum": ["sold", "lost", "silent", "open"],
                            "description": "итог по последнему треду"},
            },
            "required": ["first_contact_at", "last_contact_at", "ads", "agreements", "outcome"],
            "additionalProperties": False,
        },
        "dossier_ru": {
            "type": "string",
            "description": "Досье на русском, до 600 символов: кто (имя, откуда, если известно), когда и "
                           "что спрашивал/покупал, какие цены звучали, договорённости, особенности поведения",
        },
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "метка треда из входа: T1, T2, …"},
                    "outcome": {"type": "string", "enum": ["sold", "lost", "silent", "open"]},
                    "final_price_eur": {"type": ["number", "null"]},
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "situation_ru": {"type": "string"},
                                "lesson_ru": {"type": "string"},
                            },
                            "required": ["situation_ru", "lesson_ru"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["ref", "outcome", "final_price_eur", "lessons"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["dossier_ru", "facts", "threads"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "Для КАЖДОГО треда верни ref, outcome и final_price_eur:\n"
    "- sold — договорились и (судя по переписке) сделка состоялась; lost — клиент отказался/купил другое; "
    "silent — последним писали мы, клиент пропал; open — неясно / переписка не закончена.\n"
    "- final_price_eur — цена сделки или последняя согласованная; null если не было.\n"
    "lessons (0–2 на тред) — ТОЛЬКО обобщаемые выводы для будущих переговоров, не пересказ: "
    "что сработало (довело до продажи), какая наша реплика/тактика оттолкнула клиента, "
    "как лучше отвечать на такой вопрос. Пиши правило: «Если клиент …, то …». "
    "Если вывода нет (короткий вопрос без ответа, нет наших реплик) — пустой список. "
    "Не выдумывай: опирайся только на текст.\n"
    "dossier_ru — факты о покупателе из всех тредов, с датами (ГГГГ-ММ-ДД). Без оценок вроде «хороший человек». "
    "Треды с разными адресами (Адрес A/B…) могут быть РАЗНЫМИ людьми с одинаковым именем: если по "
    "содержанию это явно другой человек — так и напиши в досье и не смешивай факты.\n"
    "facts.ads — по объявлению на тред: ad_id — как в заголовке треда (null, если «неизвестен»), our_price — наша последняя цена, buyer_offer — последнее "
    "предложение покупателя, final_price — цена сделки (null если не было)."
)


def people_to_scan(limit: int | None, only_thread: str | None, only_buyer: str | None) -> list[dict[str, Any]]:
    """Люди к разбору, самые свежие первыми: {account_id, name, name_key, emails}.

    Ключ — (account_id, norm_name(buyer_display_name)); без имени — отдельный человек на email.
    """
    sql = (
        "SELECT account_id, norm_name(buyer_display_name) AS nk, MAX(buyer_display_name) AS name, "
        "GROUP_CONCAT(DISTINCT buyer_name) AS emails, MAX(created_at) AS last "
        "FROM messages WHERE direction = 'in' AND COALESCE(buyer_name, '') != '' "
        "AND COALESCE(gmail_thread_id, '') != '' {where} "
        "GROUP BY account_id, COALESCE(nk, buyer_name) ORDER BY last DESC"
    )
    where, params = "", []
    if only_thread or only_buyer:
        with db.get_conn() as conn:
            if only_thread:
                r = conn.execute(
                    "SELECT account_id, norm_name(buyer_display_name), buyer_name FROM messages "
                    "WHERE gmail_thread_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 1",
                    (only_thread,)).fetchone()
            else:
                r = conn.execute(
                    "SELECT account_id, norm_name(buyer_display_name), buyer_name FROM messages "
                    "WHERE buyer_name = ? AND direction = 'in' ORDER BY id DESC LIMIT 1",
                    (only_buyer,)).fetchone()
        if r is None:
            return []
        if r[1]:
            where, params = "AND account_id = ? AND norm_name(buyer_display_name) = ?", [r[0], r[1]]
        else:
            where, params = "AND buyer_name = ?", [r[2]]
    with db.get_conn() as conn:
        rows = conn.execute(sql.format(where=where), params).fetchall()
    people = [{"account_id": r["account_id"], "name_key": r["nk"], "name": r["name"],
               "emails": sorted((r["emails"] or "").split(","))} for r in rows]
    return people[:limit] if limit else people


def person_threads(person: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """[(thread_id, rows)] человека по времени первого письма (не больше MAX_THREADS_PER_BUYER свежих)."""
    marks = ",".join("?" * len(person["emails"]))
    with db.get_conn() as conn:
        tids = [r[0] for r in conn.execute(
            f"SELECT gmail_thread_id, MIN(created_at) AS first FROM messages "
            f"WHERE COALESCE(gmail_thread_id, '') != '' AND account_id = ? AND buyer_name IN ({marks}) "
            f"GROUP BY gmail_thread_id ORDER BY first ASC",
            (person["account_id"], *person["emails"])).fetchall()]
    tids = tids[-MAX_THREADS_PER_BUYER:]
    return [(tid, db.thread_history(tid)) for tid in tids]


def person_max_id(person: dict[str, Any]) -> int:
    marks = ",".join("?" * len(person["emails"]))
    with db.get_conn() as conn:
        return conn.execute(f"SELECT MAX(id) FROM messages WHERE account_id = ? AND buyer_name IN ({marks})",
                            (person["account_id"], *person["emails"])).fetchone()[0] or 0


def _field(row: Any, key: str) -> Any:
    return row[key] if key in row.keys() else None


def build_prompt(threads: list[tuple[str, list[Any]]]) -> str:
    from modules import claude
    parts: list[str] = []
    budget = BUYER_MAX_CHARS
    letters: dict[str, str] = {}
    for i, (tid, rows) in enumerate(threads, 1):
        email = next((_field(r, "buyer_name") for r in rows if _field(r, "buyer_name")), "")
        letter = letters.setdefault(email, chr(ord("A") + len(letters)))
        title = next((_field(r, "ad_title") for r in rows if _field(r, "ad_title")), "?")
        price = next((_field(r, "ad_price") for r in rows if _field(r, "ad_price")), "?")
        sold = next((_field(r, "sold_price_eur") for r in rows if _field(r, "sold_price_eur")), None)
        statuses = sorted({_field(r, "status") or "" for r in rows})
        one_sided = all(_field(r, "status") == "archived" for r in rows)
        text, _ = claude.format_history(claude.rows_to_history(rows),
                                        max_chars=min(THREAD_MAX_CHARS, max(800, budget)), with_dates=True)
        budget -= len(text)
        ad_id = next((_field(r, "ad_id") for r in rows if _field(r, "ad_id")), None)
        head = (f"### T{i} (Адрес {letter}) — «{title}», ad_id: {ad_id or 'неизвестен'}, "
                f"цена в объявлении: {price}")
        if sold:
            head += f"; оператор отметил продажу за {sold:g} €"
        elif "skipped_sold" in statuses:
            head += "; оператор закрыл тред как «товар продан»"
        if one_sided:
            head += "\n(старый тред из архива: сохранены только письма покупателя, наших ответов нет)"
        parts += [head, text or "(пусто)", ""]
    parts.append(INSTRUCTIONS)
    return "\n".join(parts)


def mine_person(person: dict[str, Any], model: str) -> dict[str, Any]:
    """Один человек → результат LLM + метаданные (без записи в БД)."""
    from modules.claude_utils import call_json
    threads = person_threads(person)
    if not threads:
        return {"person": person, "skipped": "no_threads"}
    data, meta = call_json(
        label=f"mine_lessons {person['name'] or person['emails'][0][:24]}", model=model, system=SYSTEM,
        user=build_prompt(threads), schema=SCHEMA, max_tokens=3500, nonempty=("dossier_ru",),
    )
    by_ref = {f"T{i}": (tid, rows) for i, (tid, rows) in enumerate(threads, 1)}
    missing = sorted(set(by_ref) - {t.get("ref") for t in data.get("threads") or []})
    if missing:
        logger.warning("%s: модель не вернула итог по %s", person["emails"][0], ", ".join(missing))
    return {"person": person, "data": data, "by_ref": by_ref, "max_id": person_max_id(person),
            "meta": meta, "model": model}


def needs_review(person: dict[str, Any]) -> bool:
    """Досье ещё нет или появились письма новее учтённых в нём."""
    dossier = db.get_dossier(person["account_id"], person["name"]) if person["name"] else \
        db.get_dossier_for_email(person["emails"][0])
    if dossier is None:
        return True
    return (_field(dossier, "dossier_max_msg_id") or 0) < person_max_id(person)


def write_result(res: dict[str, Any]) -> int:
    """Записать уроки (upsert по source_ref) и досье. Возвращает число записанных уроков."""
    from modules import llm_memory
    person = res["person"]
    written = 0
    for t in res["data"].get("threads") or []:
        ref = t.get("ref")
        if ref not in res["by_ref"]:
            logger.warning("%s: модель вернула неизвестный ref %r — пропуск", person["emails"][0], ref)
            continue
        tid, rows = res["by_ref"][ref]
        last_in = next((r for r in reversed(rows) if r["direction"] == "in"), rows[-1])
        for n, ls in enumerate(t.get("lessons") or [], 1):
            if not (ls.get("lesson_ru") or "").strip():
                continue
            lid = llm_memory.save_lesson(
                message_id=last_in["id"], account_id=_field(last_in, "account_id"),
                ad_id=_field(last_in, "ad_id"), client_lang=_field(last_in, "client_lang"),
                client_situation_ru=ls.get("situation_ru"), lesson_ru=ls["lesson_ru"].strip(),
                source_type="history_review", source_ref=f"{tid}#{n}",
                buyer_email=_field(last_in, "buyer_name"), outcome=t.get("outcome"),
                category=llm_memory.ad_category(_field(last_in, "ad_id")),
            )
            if lid is None:
                raise RuntimeError(f"урок по треду {tid} не сохранён (см. лог llm_memory)")
            written += 1
    db.upsert_client_dossier(
        person["account_id"], person["name"] or person["emails"][0],
        res["data"]["dossier_ru"].strip(),
        json.dumps(res["data"].get("facts") or {}, ensure_ascii=False),
        [tid for tid, _ in res["by_ref"].values()], person["emails"], res["model"],
    )
    return written


def _preflight_write() -> None:
    """Перед записью — колонки и функции миграции db (иначе уроки/досье потеряются молча)."""
    from modules import llm_memory
    missing = [c for c in ("lesson_ru", "source_type", "source_ref")
               if c not in llm_memory.table_columns("lessons")]
    missing += [f"db.{f}" for f in ("upsert_client_dossier", "get_dossier", "get_dossier_for_email")
                if not hasattr(db, f)]
    if missing:
        raise SystemExit(f"Нет миграции db для записи: {', '.join(missing)}. Запустите с --dry-run "
                         "или дождитесь слияния модуля db.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="сколько покупателей (самые свежие)")
    ap.add_argument("--thread", default=None, help="только покупатель этого треда")
    ap.add_argument("--buyer", default=None, help="только этот покупатель (relay-email)")
    ap.add_argument("--dry-run", action="store_true", help="не писать в БД")
    ap.add_argument("--db", default=None, help="путь к БД (копия для экспериментов)")
    ap.add_argument("--out", default=None, help="сохранить результаты в JSON")
    ap.add_argument("--workers", type=int, default=2, help="параллельных вызовов LLM (≤2)")
    args = ap.parse_args()

    if args.db:
        db.DB_PATH = Path(args.db)
    if not args.dry_run:
        _preflight_write()
    from modules.claude_utils import smart_model
    model = smart_model()

    people = people_to_scan(None if not args.dry_run else args.limit, args.thread, args.buyer)
    if not args.dry_run:
        # идемпотентность: люди, чьё досье уже учитывает последнее письмо, не стоят вызова LLM
        people = [p for p in people if needs_review(p)]
        if args.limit:
            people = people[:args.limit]
    logger.info("Людей к разбору: %d (model=%s, dry_run=%s)", len(people), model, args.dry_run)

    stats = {"ok": 0, "error": 0, "lessons": 0, "dossiers": 0}
    samples: list[dict[str, Any]] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, min(2, args.workers))) as pool:
        futs = {pool.submit(mine_person, p, model): p for p in people}
        for i, fut in enumerate(as_completed(futs), 1):
            buyer = futs[fut]["name"] or futs[fut]["emails"][0]
            try:
                res = fut.result()
            except Exception:  # noqa: BLE001 — один человек не валит весь прогон
                logger.exception("[%d/%d] %s: ошибка разбора", i, len(people), buyer)
                stats["error"] += 1
                continue
            if res.get("skipped"):
                continue
            stats["ok"] += 1
            n_lessons = sum(len(t.get("lessons") or []) for t in res["data"].get("threads") or [])
            logger.info("[%d/%d] %s: тредов %d, уроков %d", i, len(people), buyer,
                        len(res["by_ref"]), n_lessons)
            samples.append({
                "buyer": buyer, "emails": res["person"]["emails"], "dossier_ru": res["data"]["dossier_ru"],
                "facts": res["data"].get("facts"),
                "threads": [{**t, "gmail_thread_id": res["by_ref"].get(t.get("ref"), ("?",))[0]}
                            for t in res["data"].get("threads") or []],
            })
            if not args.dry_run:
                try:
                    stats["lessons"] += write_result(res)
                    stats["dossiers"] += 1
                except Exception:  # noqa: BLE001
                    logger.exception("%s: запись в БД не удалась", buyer)
                    stats["error"] += 1
    if args.out:
        Path(args.out).write_text(json.dumps(samples, ensure_ascii=False, indent=1))
    logger.info("Готово за %.0f с: %s", time.monotonic() - t0, stats)
    if stats["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
