"""Пересчитать эвристические атрибуты (fuel/gearbox/model/part_type/condition/year)
по уже сохранённым title+description — без повторного скрапинга.
Идемпотентно. Запуск: PYTHONPATH=/home/pg/kleinanzeigen-bot python3 scripts/scout_reclassify.py
"""
import database as db
from modules import scout

with db.get_conn() as conn:
    rows = conn.execute("SELECT * FROM scout_listings").fetchall()
    n = 0
    for r in rows:
        blob = f"{r['title'] or ''}\n{r['description'] or ''}"
        if r["kind"] == "car":
            fields = {
                "fuel": scout.extract_fuel(blob),
                "gearbox": scout.extract_gearbox(blob),
                "model_family": scout.extract_model_family(blob),
            }
        else:
            fields = {
                "part_type": scout.extract_part_type(blob),
                "condition": scout.extract_condition(blob),
                "model_family": scout.extract_model_family(blob),
            }
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE scout_listings SET {sets} WHERE ad_id=?",
                     (*fields.values(), r["ad_id"]))
        n += 1
print(f"Пересчитано строк: {n}")
