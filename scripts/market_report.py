#!/usr/bin/env python3
"""Отчёт «Рынок-советник» по копии БД: цена против рынка, позиция, дубли, перепост.

Источник открывается ТОЛЬКО на чтение и копируется во временный файл — все таблицы
market_* создаются в копии, исходная БД не меняется. В сеть не ходит (позиции — только
из уже собранных данных scout); LLM — только с --llm.

    python3 scripts/market_report.py --db /path/to/kleinanzeigen.db [--limit 10] [--llm] [--out report.md]
"""
import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _copy_ro(src: Path) -> Path:
    dst = Path(tempfile.mkdtemp(prefix="market_report_")) / "copy.db"
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()
    return dst


def _eur(v):
    return "—" if v is None else f"{int(round(v))} €"


def build(limit: int, use_llm: bool) -> str:
    from modules import market_advisor as ma
    ctx = ma._Ctx()
    items = ma.own_items(ctx)
    out = [f"# Рынок-советник — отчёт по копии БД\n",
           f"Живых товаров: **{len(items)}**; объявлений рынка (запчасти): "
           f"{sum(1 for m in ctx.market.values() if m['ek'] == 'part')}; из них наших: "
           f"{sum(1 for a in ctx.market if ctx.is_ours(a) and ctx.market[a]['ek'] == 'part')} "
           f"(по ad_id {sum(1 for a in ctx.market if a in ctx.own_ids)}, по городу+заголовку "
           f"{len(ctx.own_by_location)}).\n"]
    dup = ma.duplicates_report(_ctx=ctx)
    out.append(f"## Дубли: опасно {dup['danger']}, внимание {dup['warn']}, снять {dup['ads_to_remove']}\n")
    for g in dup["groups"][:15]:
        out.append(f"- **{g['title']}** — {g['level']} (score {g['score']}): {', '.join(g['reasons_ru'])}. "
                   f"Оставить {g['keep']['ad_id']} ({g['keep']['account'] or '?'}, {g['keep']['city'] or '?'}); "
                   f"{g['message_ru']}")
    out.append("\n## Товары: цена и позиция\n")
    shown = 0
    for it in items:
        if shown >= limit:
            break
        adv = ma.advise_price(item_id=it["item_id"], _ctx=ctx)
        rr = ma.rank_report(item_id=it["item_id"], now=ctx.now)["items"]
        rank = rr[0] if rr else None
        shown += 1
        out.append(f"### {it['title']}  ·  {_eur(it['price_eur'])}  ·  живых объявлений {it['live_ads']}")
        f = adv["features"]
        out.append(f"- признаки: {f['category']}, {f['material'] or 'обивка ?'}, мест {f['seats'] or '?'}, "
                   f"{f['condition'] or 'сост. ?'}, модели {', '.join(f['models']) or '—'}")
        out.append(f"- рынок: n={adv['n']} (актив. {adv['n_active']}), P25 {_eur(adv['p25'])} · "
                   f"медиана {_eur(adv['median'])} · P75 {_eur(adv['p75'])} → **{adv['verdict_ru']}**; "
                   f"выставить за {_eur(adv['recommended_eur'])}, минимум {_eur(adv['min_eur'])} ({adv['min_basis']})")
        for s in adv["similar"][:5]:
            ch = " (цена менялась)" if s["price_change"] else ""
            out.append(f"    - {_eur(s['price_eur'])} · {s['title']} · {s['city'] or '?'} · {s['url']}{ch}")
        if rank:
            b = rank["best"]
            pos = f", лучшее: «{b['keywords']}» стр. {b['page']} место {b['organic_pos'] or b['pos']}" if b else ""
            out.append(f"- позиция: {rank['status_ru']}{pos}; без писем {rank['days_quiet']} дн.")
        out.append("")
    reps = ma.repost_suggestions(limit=limit, use_llm=use_llm, _ctx=ctx)
    out.append(f"## Перепост ({len(reps)})\n")
    for r in reps:
        out.append(f"- **{r['title']}** [{r['action']}] — {', '.join(r['reasons_ru'])}; когда: {r['window']['label_ru']}")
        if r["text"]:
            out.append(f"    - заголовок: `{r['text']['title']}` ({r['text']['source']})")
            out.append(f"    - цена: {_eur(r['price']['current'])} → {_eur(r['price']['suggested'])} {r['price']['advice_ru']}")
        for st in r["steps_ru"]:
            out.append(f"    - {st}")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--llm", action="store_true", help="тексты перепоста через LLM (подписка)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if not a.db.exists():
        print(f"нет файла {a.db}", file=sys.stderr)
        return 2
    import database
    database.DB_PATH = _copy_ro(a.db)
    text = build(a.limit, a.llm)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
        print(f"отчёт: {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
