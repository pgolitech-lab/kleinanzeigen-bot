# Печатает схему БД, которую создаёт init_db, на пустой временной БД (без прод-данных)
import sys, tempfile, pathlib
sys.path.insert(0, sys.argv[1])
import database as db
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "schema.db"
db.init_db()
with db.get_conn() as c:
    for (name,) in c.execute("select name from sqlite_master where type='table'"):
        cols = [r["name"] for r in c.execute(f"pragma table_info({name})")]
        print(name, ":", ", ".join(cols))
