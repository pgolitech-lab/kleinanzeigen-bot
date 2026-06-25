"""Сгенерить стартовые scout-запросы через LLM и добавить (без дублей).
Запуск: PYTHONPATH=/home/pg/kleinanzeigen-bot python3 scripts/scout_seed.py
"""
import database as db
from modules import claude_scout

db.init_db()
existing = [q["keywords"] for q in db.list_scout_queries()]
r = claude_scout.generate_scout_queries(existing_keywords=existing)
added = 0
for q in r["queries"]:
    if db.scout_query_exists(q["kind"], q["keywords"], q["category"]):
        continue
    db.add_scout_query(kind=q["kind"], keywords=q["keywords"],
                       category=q["category"], label=q["label"],
                       max_pages=q["max_pages"], source="llm")
    added += 1

print(f"LLM вернул {len(r['queries'])} запросов, добавлено {added}, "
      f"стоимость ${r['cost_usd']:.4f}")
for q in db.list_scout_queries():
    print(f"  #{q['id']} [{q['kind']}] {q['category']} mp={q['max_pages']}  {q['keywords']}")
