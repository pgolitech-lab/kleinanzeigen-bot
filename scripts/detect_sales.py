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
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
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
    "Ответь СТРОГО JSON, без markdown-обёртки:\n"
    "{\n"
    '  "is_sale": true | false,\n'
    '  "confidence": "high" | "medium" | "low",\n'
    '  "sold_price_eur": number | null,\n'
    '  "ad_title": "короткое название товара",\n'
    '  "sold_at_date": "YYYY-MM-DD" | null,\n'
    '  "evidence": "цитата"\n'
    "}"
)


def _format_thread(rows: list) -> str:
    """История треда → текстовый промпт."""
    parts: list[str] = []
    ad_title = None
    ad_price = None
    for r in rows:
        if not ad_title and r["ad_title"]:
            ad_title = r["ad_title"]
        if not ad_price and r["ad_price"]:
            ad_price = r["ad_price"]
    if ad_title or ad_price:
        parts.append(f"ОБЪЯВЛЕНИЕ: {ad_title or '?'} | цена: {ad_price or '?'}")
    parts.append("---")
    for r in rows:
        ts = (r["created_at"] or "")[:19]
        if r["direction"] == "in":
            who = "ПОКУПАТЕЛЬ"
            body = (r["de_client"] or "")[:1500]
        else:
            who = "МЫ (продавец)"
            body = (r["de_answer"] or "")[:1500]
            ts = (r["sent_at"] or ts)[:19]
        if not body.strip():
            continue
        parts.append(f"[{ts}] {who}:\n{body}")
    return "\n\n".join(parts)


def _calc_cost(model: str, in_tok: int, out_tok: int) -> float:
    # Sonnet 4.6: $3/M input, $15/M output
    if "sonnet" in (model or "").lower():
        return in_tok * 3 / 1_000_000 + out_tok * 15 / 1_000_000
    if "haiku" in (model or "").lower():
        return in_tok * 1 / 1_000_000 + out_tok * 5 / 1_000_000
    return 0.0


def _parse_json(text: str) -> dict:
    """Достать JSON из ответа модели (модели иногда оборачивают ```json...```)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def detect_one(client, model: str, gmail_thread_id: str) -> dict:
    rows = db.thread_history(gmail_thread_id)
    if not rows:
        return {"skipped": "empty_thread"}
    formatted = _format_thread(rows)
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        system=SYSTEM,
        messages=[{"role": "user", "content": formatted}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    try:
        parsed = _parse_json(text)
    except json.JSONDecodeError as e:
        logger.warning("JSON parse failed for thread %s: %s | raw=%r",
                       gmail_thread_id, e, text[:200])
        return {"error": "json_parse"}

    in_tok = resp.usage.input_tokens
    out_tok = resp.usage.output_tokens
    cost = _calc_cost(model, in_tok, out_tok)

    return {
        "is_sale": bool(parsed.get("is_sale")),
        "confidence": parsed.get("confidence", "low"),
        "sold_price_eur": parsed.get("sold_price_eur"),
        "ad_title": parsed.get("ad_title"),
        "sold_at_date": parsed.get("sold_at_date"),
        "evidence": parsed.get("evidence"),
        "tokens_in": in_tok,
        "tokens_out": out_tok,
        "cost_usd": cost,
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

    api_key = config.anthropic_api_key()
    if not api_key:
        sys.exit("Нет anthropic_api_key в config")
    model = config.claude_model()
    client = anthropic.Anthropic(api_key=api_key)

    threads = candidate_threads(CUTOFF, args.limit, args.thread)
    logger.info("Threads to scan: %d (model=%s, autocommit=%s, dry_run=%s)",
                len(threads), model, not args.no_autocommit, args.dry_run)

    total_cost = 0.0
    stats = {"high": 0, "medium": 0, "low": 0, "not_sale": 0, "error": 0, "applied": 0}
    t0 = time.monotonic()

    for i, tid in enumerate(threads, 1):
        try:
            result = detect_one(client, model, tid)
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
