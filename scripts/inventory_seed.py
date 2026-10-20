#!/usr/bin/env python3
"""Начальное заполнение склада по истории переписки.

Группирует все ad_id из messages в товары (перепощенные объявления = один товар), показывает
группы и сомнительные пары. По умолчанию — dry-run на КОПИИ БД (оригинал не открывается на запись).

    python3 scripts/inventory_seed.py                       # dry-run по прод-БД, отчёт в stdout
    python3 scripts/inventory_seed.py --out seed.md         # отчёт в файл
    python3 scripts/inventory_seed.py --llm                 # сомнительные пары сначала спросить у Haiku
    python3 scripts/inventory_seed.py --db /tmp/copy.db --apply   # записать товары + продажи

--apply пишет в указанную БД (таблицы inv_*: товары, объявления, вопросы, продажи как движения).
Остатки и себестоимость не выдумываются — их вносит оператор (stock_tracked=0 до инвентаризации).
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database  # noqa: E402
from modules import inventory  # noqa: E402


def _copy_db(src: Path) -> Path:
    """Консистентная копия БД (sqlite backup API, оригинал открыт только на чтение)."""
    dst = Path(tempfile.mkdtemp(prefix="inv-seed-")) / "copy.db"
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst


def _fmt_price(p) -> str:
    return f"{p:.0f} €" if p else "—"


def render(plan: dict, ads_total: int, skipped: list[str]) -> str:
    groups = plan["groups"]
    multi = [g for g in groups if len(g.ads) > 1]
    single = [g for g in groups if len(g.ads) == 1]
    out = [
        "# Склад: начальное заполнение (dry-run)",
        "",
        f"Объявлений с названием: {ads_total}; без названия (рассылки, мусор): {len(skipped)}.",
        f"Товаров: {len(groups)} — из них перепощенных (> 1 объявления): {len(multi)}, одиночных: {len(single)}.",
        f"Сомнительных пар «это тот же товар?»: {len(plan['doubtful'])}.",
        "",
        "## Товары с несколькими объявлениями",
        "",
    ]
    by_cat: dict[str, list] = {}
    for g in groups:
        by_cat.setdefault(inventory.detect_category(g.title), []).append(g)
    for g in sorted(multi, key=lambda g: (inventory.detect_category(g.title), g.title)):
        out.append(f"### #{g.gid} {g.title}  [{inventory.detect_category(g.title)}]")
        for a in g.ads:
            st = "" if a.state == "active" else f" ({a.state})"
            out.append(f"- {a.ad_id} · акк {a.account_id} · {_fmt_price(a.price)} · "
                       f"{a.first_seen[:10]}…{a.last_seen[:10]}{st} · «{a.title}»")
        out.append("")
    out += ["## Сомнительные — спросить оператора", ""]
    if not plan["doubtful"]:
        out.append("нет")
    for d in plan["doubtful"]:
        a = d["ad"]
        sg = next(g for g in groups if g.gid == d["suggested_gid"])
        out.append(f"- {a.ad_id} «{a.title}» ({_fmt_price(a.price)}) → товар #{d['gid']}; "
                   f"возможно, это #{sg.gid} «{sg.title}» ({_fmt_price(sg.latest.price)}) · "
                   f"score {d['score']:.2f} · {d['reason']}")
    out += ["", "## Одиночные товары по категориям", ""]
    for cat in sorted(by_cat):
        gs = [g for g in by_cat[cat] if len(g.ads) == 1]
        if not gs:
            continue
        out.append(f"**{cat}** ({len(gs)}):")
        for g in sorted(gs, key=lambda g: g.title):
            a = g.ads[0]
            out.append(f"- #{g.gid} {a.ad_id} · {_fmt_price(a.price)} · {a.last_seen[:10]} · {g.title}")
        out.append("")
    out += ["## Без названия (не заведены)", "", ", ".join(skipped) or "нет", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=database.DB_PATH, help="путь к БД (по умолчанию прод)")
    ap.add_argument("--apply", action="store_true", help="записать в --db (без флага — dry-run на копии)")
    ap.add_argument("--llm", action="store_true", help="сомнительные пары спросить у Haiku")
    ap.add_argument("--out", type=Path, help="куда записать отчёт (markdown)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if not args.db.exists():
        print(f"БД не найдена: {args.db}", file=sys.stderr)
        return 2
    database.DB_PATH = args.db if args.apply else _copy_db(args.db)

    ads = inventory.seed_ads_from_messages()
    with database.get_conn() as conn:
        all_ids = {r["ad_id"] for r in conn.execute(
            "SELECT DISTINCT ad_id FROM messages WHERE ad_id IS NOT NULL AND ad_id != ''")}
    skipped = sorted(all_ids - {a.ad_id for a in ads})
    plan = inventory.plan_seed(ads, judge=inventory.llm_same_product if args.llm else None)
    text = render(plan, len(ads), skipped)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"отчёт: {args.out}")
    else:
        print(text)

    if args.apply:
        database.init_db()
        from modules import db_inventory
        db_inventory.ensure_schema()
        stats = inventory.apply_seed(plan)
        sales = inventory.sync_sales()
        print(f"записано: {stats}; продажи: recorded={sales['recorded']} already={sales['already']} "
              f"voided={sales['voided']} без товара={len(sales['unmatched'])}")
        for u in sales["unmatched"]:
            print(f"  продажа без товара: {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
