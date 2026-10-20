# Локальный бэкенд для QA: тот же FastAPI-app, но на синтетической БД (seed.py),
# без планировщика и без LLM (все вызовы modules.claude → исключение).
# Запуск: python3 serve.py <repo_root> <db_path> [port]
import sys
import pathlib

sys.path.insert(0, sys.argv[1])
import database as db

db.DB_PATH = pathlib.Path(sys.argv[2])
port = int(sys.argv[3]) if len(sys.argv) > 3 else 8798

from modules import claude  # noqa: E402


def _no_llm(*a, **k):
    raise RuntimeError("QA: LLM отключён в тестовом бэкенде")


_LLM_PREFIXES = ("generate_", "regenerate_", "translate", "detect_", "classify_", "summarize_")
for _name in dir(claude):
    _obj = getattr(claude, _name)
    if (callable(_obj) and _name.startswith(_LLM_PREFIXES) and _name != "detect_lang_override"
            and getattr(_obj, "__module__", "") == claude.__name__):
        setattr(claude, _name, _no_llm)

import uvicorn  # noqa: E402
from web.app import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
