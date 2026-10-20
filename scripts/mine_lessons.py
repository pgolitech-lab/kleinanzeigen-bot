#!/usr/bin/env python3
"""Разбор всей истории переписок: уроки для LLM + досье покупателей.

Идёт по покупателям (messages.buyer_name — relay-email Kleinanzeigen), для каждого —
один вызов основной модели со всеми его тредами:
  * итог каждого треда (продано / сорвалось / замолчал / открыт), финальная цена;
  * 0–2 обобщённых урока на тред (что сработало, какие ошибки) → lessons (source='mined');
  * досье покупателя (кто, когда, о чём, цены, договорённости) → client_profiles.dossier_ru.

Идемпотентно: тред с уже записанными mined-уроками пропускается; досье пересчитывается,
только если у покупателя появились письма новее dossier_max_msg_id.
LLM — по подписке (modules/llm.py), не больше 2 вызовов параллельно.

Usage:
    python3 scripts/mine_lessons.py --dry-run --limit 10 --db /tmp/copy.db --out sample.json
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

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
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
    "required": ["dossier_ru", "threads"],
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
    "dossier_ru — факты о покупателе из всех тредов, с датами (ГГГГ-ММ-ДД). Без оценок вроде «хороший человек»."
)


def buyers_to_scan(limit: int | None, only_thread: str | None, only_buyer: str | None) -> list[str]:
    """Покупатели (relay-email), начиная с самых свежих."""
    with db.get_conn() as conn:
        if only_thread:
            rows = conn.execute(
                "SELECT DISTINCT buyer_name FROM messages WHERE gmail_thread_id = ? "
                "AND direction = 'in' AND COALESCE(buyer_name, '') != ''", (only_thread,)).fetchall()
            return [r[0] for r in rows]
        if only_buyer:
            return [only_buyer]
        rows = conn.execute(
            "SELECT buyer_name, MAX(created_at) AS last FROM messages "
            "WHERE direction = 'in' AND COALESCE(buyer_name, '') != '' "
            "AND COALESCE(gmail_thread_id, '') != '' GROUP BY buyer_name ORDER BY last DESC").fetchall()
    out = [r[0] for r in rows]
    return out[:limit] if limit else out


def buyer_threads(buyer: str) -> list[tuple[str, list[Any]]]:
    """[(thread_id, rows)] покупателя, по времени первого письма (не больше MAX_THREADS_PER_BUYER свежих)."""
    with db.get_conn() as conn:
        tids = [r[0] for r in conn.execute(
            "SELECT gmail_thread_id, MIN(created_at) AS first FROM messages "
            "WHERE buyer_name = ? AND COALESCE(gmail_thread_id, '') != '' "
            "GROUP BY gmail_thread_id ORDER BY first ASC", (buyer,)).fetchall()]
    tids = tids[-MAX_THREADS_PER_BUYER:]
    return [(tid, db.thread_history(tid)) for tid in tids]


def _field(row: Any, key: str) -> Any:
    return row[key] if key in row.keys() else None


def build_prompt(threads: list[tuple[str, list[Any]]]) -> str:
    from modules import claude
    parts: list[str] = []
    budget = BUYER_MAX_CHARS
    for i, (tid, rows) in enumerate(threads, 1):
        title = next((_field(r, "ad_title") for r in rows if _field(r, "ad_title")), "?")
        price = next((_field(r, "ad_price") for r in rows if _field(r, "ad_price")), "?")
        sold = next((_field(r, "sold_price_eur") for r in rows if _field(r, "sold_price_eur")), None)
        statuses = sorted({_field(r, "status") or "" for r in rows})
        one_sided = all(_field(r, "status") == "archived" for r in rows)
        text, _ = claude.format_history(claude.rows_to_history(rows),
                                        max_chars=min(THREAD_MAX_CHARS, max(800, budget)), with_dates=True)
        budget -= len(text)
        head = f"### T{i} — «{title}», цена в объявлении: {price}"
        if sold:
            head += f"; оператор отметил продажу за {sold:g} €"
        elif "skipped_sold" in statuses:
            head += "; оператор закрыл тред как «товар продан»"
        if one_sided:
            head += "\n(старый тред из архива: сохранены только письма покупателя, наших ответов нет)"
        parts += [head, text or "(пусто)", ""]
    parts.append(INSTRUCTIONS)
    return "\n".join(parts)


def mine_buyer(buyer: str, model: str) -> dict[str, Any]:
    """Один покупатель → результат LLM + метаданные (без записи в БД)."""
    from modules.claude_utils import call_json
    threads = buyer_threads(buyer)
    if not threads:
        return {"buyer": buyer, "skipped": "no_threads"}
    data, meta = call_json(
        label=f"mine_lessons {buyer[:24]}", model=model, system=SYSTEM,
        user=build_prompt(threads), schema=SCHEMA, max_tokens=3000, nonempty=("dossier_ru",),
    )
    by_ref = {f"T{i}": (tid, rows) for i, (tid, rows) in enumerate(threads, 1)}
    max_id = max(r["id"] for _, rows in threads for r in rows)
    return {"buyer": buyer, "data": data, "by_ref": by_ref, "max_id": max_id, "meta": meta}


def _needs_dossier(buyer: str) -> bool:
    prof = db.get_client_profile(buyer)
    if prof is None or "dossier_max_msg_id" not in prof.keys():
        return True
    with db.get_conn() as conn:
        max_id = conn.execute("SELECT MAX(id) FROM messages WHERE buyer_name = ?", (buyer,)).fetchone()[0]
    return (prof["dossier_max_msg_id"] or 0) < (max_id or 0)


def _thread_done(tid: str) -> bool:
    return bool(db.has_mined_lessons(tid))


def write_result(res: dict[str, Any]) -> tuple[int, bool]:
    """Записать уроки и досье. Возвращает (сколько уроков записано, записано ли досье)."""
    from modules import llm_memory
    buyer = res["buyer"]
    written = 0
    for t in res["data"].get("threads") or []:
        ref = t.get("ref")
        if ref not in res["by_ref"]:
            logger.warning("%s: модель вернула неизвестный ref %r — пропуск", buyer, ref)
            continue
        tid, rows = res["by_ref"][ref]
        if _thread_done(tid):
            continue
        last_in = next((r for r in reversed(rows) if r["direction"] == "in"), rows[-1])
        for ls in t.get("lessons") or []:
            if not (ls.get("lesson_ru") or "").strip():
                continue
            llm_memory.save_lesson(
                message_id=last_in["id"], account_id=_field(last_in, "account_id"),
                ad_id=_field(last_in, "ad_id"), client_lang=_field(last_in, "client_lang"),
                client_situation_ru=ls.get("situation_ru"), lesson_ru=ls["lesson_ru"].strip(),
                source="mined", gmail_thread_id=tid, buyer_email=buyer, outcome=t.get("outcome"),
                category=llm_memory.ad_category(_field(last_in, "ad_id")),
            )
            written += 1
    dossier_written = False
    if _needs_dossier(buyer):
        db.set_client_dossier(buyer, res["data"]["dossier_ru"].strip(), res["max_id"])
        dossier_written = True
    return written, dossier_written


def _preflight_write() -> None:
    """Перед записью — нужные колонки/функции db (миграция модуля db)."""
    from modules import llm_memory
    missing = [c for c in ("lesson_ru", "source", "gmail_thread_id", "buyer_email", "outcome")
               if c not in llm_memory.table_columns("lessons")]
    missing += [f"db.{f}" for f in ("has_mined_lessons", "set_client_dossier") if not hasattr(db, f)]
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

    buyers = buyers_to_scan(args.limit, args.thread, args.buyer)
    if not args.dry_run:
        # идемпотентность: покупатели, у которых всё уже разобрано, не стоят вызова LLM
        buyers = [b for b in buyers
                  if _needs_dossier(b) or not all(_thread_done(t) for t, _ in buyer_threads(b))]
    logger.info("Покупателей к разбору: %d (model=%s, dry_run=%s)", len(buyers), model, args.dry_run)

    stats = {"ok": 0, "error": 0, "lessons": 0, "dossiers": 0}
    samples: list[dict[str, Any]] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, min(2, args.workers))) as pool:
        futs = {pool.submit(mine_buyer, b, model): b for b in buyers}
        for i, fut in enumerate(as_completed(futs), 1):
            buyer = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 — один покупатель не валит весь прогон
                logger.error("[%d/%d] %s: ошибка: %s", i, len(buyers), buyer, e)
                stats["error"] += 1
                continue
            if res.get("skipped"):
                continue
            stats["ok"] += 1
            n_lessons = sum(len(t.get("lessons") or []) for t in res["data"].get("threads") or [])
            logger.info("[%d/%d] %s: тредов %d, уроков %d", i, len(buyers), buyer,
                        len(res["by_ref"]), n_lessons)
            samples.append({
                "buyer": buyer, "dossier_ru": res["data"]["dossier_ru"],
                "threads": [{**t, "gmail_thread_id": res["by_ref"].get(t.get("ref"), ("?",))[0]}
                            for t in res["data"].get("threads") or []],
            })
            if not args.dry_run:
                try:
                    w, d = write_result(res)
                except Exception:  # noqa: BLE001
                    logger.exception("%s: запись в БД не удалась", buyer)
                    stats["error"] += 1
                    continue
                stats["lessons"] += w
                stats["dossiers"] += int(d)
    if args.out:
        Path(args.out).write_text(json.dumps(samples, ensure_ascii=False, indent=1))
    logger.info("Готово за %.0f с: %s", time.monotonic() - t0, stats)
    if stats["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
