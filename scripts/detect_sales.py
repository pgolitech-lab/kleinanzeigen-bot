#!/usr/bin/env python3
"""LLM-сканер старых тредов для извлечения фактов продажи.

Идёт по distinct gmail_thread_id (created_at >= 2025-05-01), для каждого тредa:
  1. Собирает всю историю (in + out) в хронологическом порядке.
  2. Просит Claude Sonnet определить — была ли продажа, цена, дата, товар.
  3. Записывает результат в `detected_sales`.
  4. High-confidence → autocommit сразу в messages (mark sold + close_thread).

Usage:
    python3 scripts/detect_sales.py           # full scan
    python3 scripts/detect_sales.py --limit 5  # debug, первые 5 тредов
    python3 scripts/detect_sales.py --thread <id>  # один тред
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import claude  # noqa: E402
from modules.claude_utils import call_json  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("detect_sales")

CUTOFF = "2025-05-01"

SYSTEM = (
    "Ты — аналитик продаж объявлений Kleinanzeigen. На вход даётся ВСЯ переписка "
    "одного треда (incoming от покупателя + outgoing от продавца). "
    "Определи: ЗАВЕРШЁННА ли в этом треде продажа товара покупателю.\n\n"
    "Сильные сигналы продажи:\n"
    "- 'abgeholt' / 'забрал' / 'wir haben das mitgenommen'\n"
    "- 'überwiesen' / 'bezahlt' / 'gezahlt' / 'перевёл деньги'\n"
    "- 'Danke, alles war super' / 'sehr nett'\n"
    "- Bewertung — автоматическое письмо от Kleinanzeigen после сделки\n"
    "- Договорённость о встрече с временем/адресом ('Komme Samstag 14 Uhr')\n"
    "- Цена согласована после торга и финальное 'ок, беру'\n\n"
    "Сигналы НЕ-продажи:\n"
    "- Только запрос цены без следующих сообщений\n"
    "- 'Leider zu teuer' / 'doch nicht interessiert'\n"
    "- Игнор клиента\n"
    "- Просто sehr formell первое касание\n\n"
    "Confidence:\n"
    "- high: явный сигнал (Bewertung от Kleinanzeigen, 'abgeholt für X €', överwiesen)\n"
    "- medium: вероятная сделка, есть meet или 'ja ich nehme das'\n"
    "- low: возможно сделка но детали неясны\n\n"
    "Если sold_price_eur не упомянут явно — оставь null (можно отрабатывать вручную потом).\n"
    "sold_at_date — лучшая оценка из последнего relevant события (формат YYYY-MM-DD).\n"
    "evidence — короткая ЦИТАТА (макс 200 символов) ключевой фразы.\n\n"
    "Поля ответа: is_sale, confidence, sold_price_eur, ad_title (короткое название товара), "
    "sold_at_date, evidence."
)

SALE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_sale": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "sold_price_eur": {"type": ["number", "null"]},
        "ad_title": {"type": "string"},
        "sold_at_date": {"type": ["string", "null"]},
        "evidence": {"type": "string"},
    },
    "required": ["is_sale", "confidence", "sold_price_eur", "ad_title", "sold_at_date", "evidence"],
    "additionalProperties": False,
}


def _format_thread(gmail_thread_id: str, rows: list) -> str:
    """История треда → текстовый промпт.

    Строка in хранит И вопрос покупателя, И наш ответ — разворачивает
    claude.format_history (наши реплики — только отправленные).
    """
    parts: list[str] = []
    ad_title = next((r["ad_title"] for r in rows if r["ad_title"]), None)
    ad_price = next((r["ad_price"] for r in rows if r["ad_price"]), None)
    if ad_title or ad_price:
        parts.append(f"ОБЪЯВЛЕНИЕ: {ad_title or '?'} | цена: {ad_price or '?'}")
    parts.append("---")
    text, _ = claude.format_history(claude.rows_to_history(rows), max_chars=12000, with_dates=True)
    parts.append(text)
    return "\n".join(parts)


def detect_one(model: str, gmail_thread_id: str) -> dict:
    rows = db.thread_history(gmail_thread_id)
    if not rows:
        return {"skipped": "empty_thread"}
    data, meta = call_json(
        label=f"detect_sales {gmail_thread_id[:16]}",
        model=model, system=SYSTEM, user=_format_thread(gmail_thread_id, rows),
        schema=SALE_SCHEMA, max_tokens=600,
    )
    return {
        "is_sale": bool(data.get("is_sale")),
        "confidence": data.get("confidence") or "low",
        "sold_price_eur": data.get("sold_price_eur"),
        "ad_title": data.get("ad_title"),
        "sold_at_date": data.get("sold_at_date"),
        "evidence": data.get("evidence"),
        "tokens_in": meta["tokens_in"],
        "tokens_out": meta["tokens_out"],
        "cost_usd": meta["cost_usd"],
    }


def candidate_threads(cutoff: str, limit: int | None, only_thread: str | None) -> list[str]:
    if only_thread:
        return [only_thread]
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT m.gmail_thread_id FROM messages m "
            "WHERE m.created_at >= ? AND m.gmail_thread_id IS NOT NULL "
            "AND m.gmail_thread_id != '' "
            # Пропускаем треды у которых уже есть sold_price_eur (manual sold)
            "AND m.gmail_thread_id NOT IN ("
            "    SELECT gmail_thread_id FROM messages WHERE sold_price_eur IS NOT NULL"
            ") "
            # Пропускаем треды у которых уже есть detection
            "AND m.gmail_thread_id NOT IN ("
            "    SELECT gmail_thread_id FROM detected_sales"
            ") "
            "ORDER BY m.created_at ASC",
            (cutoff,),
        ).fetchall()
    out = [r["gmail_thread_id"] for r in rows]
    if limit:
        out = out[:limit]
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--thread", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Не пишет в БД, только печатает результаты")
    parser.add_argument("--no-autocommit", action="store_true",
                        help="High-confidence не применяется в messages автоматом")
    args = parser.parse_args()

    model = config.claude_model()

    threads = candidate_threads(CUTOFF, args.limit, args.thread)
    logger.info("Threads to scan: %d (model=%s, autocommit=%s, dry_run=%s)",
                len(threads), model, not args.no_autocommit, args.dry_run)

    total_cost = 0.0
    stats = {"high": 0, "medium": 0, "low": 0, "not_sale": 0, "error": 0, "applied": 0}
    t0 = time.monotonic()

    for i, tid in enumerate(threads, 1):
        try:
            result = detect_one(model, tid)
        except Exception as e:
            logger.exception("detect failed for %s: %s", tid, e)
            stats["error"] += 1
            continue

        if "skipped" in result or "error" in result:
            stats["error"] += 1
            continue

        is_sale = result["is_sale"]
        conf = result["confidence"]
        price = result["sold_price_eur"]
        date_ = result["sold_at_date"]
        title = result["ad_title"]
        evid = result["evidence"]
        total_cost += result["cost_usd"]

        if not is_sale:
            stats["not_sale"] += 1
        else:
            stats[conf] = stats.get(conf, 0) + 1

        logger.info(
            "[%d/%d] %s | sale=%s conf=%s price=%s date=%s | %s | $%.4f",
            i, len(threads), tid[:16], is_sale, conf, price, date_,
            (evid or "")[:60], result["cost_usd"],
        )

        if not args.dry_run:
            det_id = db.record_detection(
                gmail_thread_id=tid, is_sale=is_sale, confidence=conf,
                sold_price_eur=price, detected_ad_title=title,
                detected_sold_at=date_, evidence=evid, model=model,
                tokens_in=result["tokens_in"], tokens_out=result["tokens_out"],
                cost_usd=result["cost_usd"],
            )
            # Autocommit high-confidence ВСЕГДА если is_sale и есть price
            if (not args.no_autocommit and is_sale and conf == "high"
                    and price is not None and price > 0):
                try:
                    applied = db.apply_detection_to_messages(det_id)
                    stats["applied"] += 1
                    logger.info("    → autocommitted: msg=%s thread=%s €%s",
                                applied["msg_id"], applied["thread_id"], applied["price"])
                except Exception as e:
                    logger.warning("    autocommit failed: %s", e)

    elapsed = time.monotonic() - t0
    logger.info(
        "Done in %.1fs. Stats: %s | total cost ≈ $%.4f",
        elapsed, stats, total_cost,
    )


if __name__ == "__main__":
    main()
