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

# QA_LLM=fake: перевод — эхо с пометкой, язык — по буквам (кириллица → ru, ąęł… → pl, éèç… → fr, иначе de).
# Остальные LLM-функции по-прежнему падают — проверяем, как экраны переживают недоступный LLM.
import os  # noqa: E402
import re as _re  # noqa: E402

if os.environ.get("QA_LLM") == "fake":
    def _fake_translate(text, direction="ru_to_de", target_lang=None, source_lang=None, context=None, **k):
        return {"translation": f"[QA-перевод] {text}", "back_translation": f"[QA-перевод] {text}",
                "target_lang": target_lang or "de", "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

    def _fake_lang(text, **k):
        t = text or ""
        lang = ("ru" if _re.search("[а-яё]", t, _re.I) else "pl" if _re.search("[ąęłśżźćń]", t, _re.I)
                else "fr" if _re.search("[éèçêàùûôœ]|bonjour", t, _re.I) else "de")
        return {"lang": lang, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}

    claude.translate_only = _fake_translate
    claude.detect_text_lang = _fake_lang
    for _n in dir(claude):
        if _n.startswith("translate") and _n != "translate_only":
            setattr(claude, _n, _fake_translate)

# Сеть наружу закрыта: никакого SMTP/IMAP и Telegram, даже если запрос дойдёт до отправки
import imaplib  # noqa: E402
import smtplib  # noqa: E402


class _NoNet:
    def __init__(self, *a, **k):
        raise RuntimeError("QA: сеть (SMTP/IMAP) отключена в тестовом бэкенде")


smtplib.SMTP = smtplib.SMTP_SSL = _NoNet
imaplib.IMAP4 = imaplib.IMAP4_SSL = _NoNet
try:
    from modules import telegram_bot  # noqa: E402

    def _tg_off(method, payload):
        print(f"QA tg suppressed: {method}", flush=True)
        return {}

    telegram_bot._http_post_single = _tg_off
except Exception as e:  # noqa: BLE001
    print("QA: telegram_bot не пропатчен:", e)

import uvicorn  # noqa: E402
from web.app import app  # noqa: E402

uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
