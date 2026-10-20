#!/usr/bin/env python3
"""Импорт наших реплик с сайта Kleinanzeigen (ответы владельца из приложения KZ).

Только чтение сайта: вход, навигация на m-nachrichten, JSON переписки. Покупателям ничего
не отправляется. Аккаунт без логина/пароля и без сохранённой сессии пропускается.
Перед первым боевым (не --dry-run) прогоном: cp kleinanzeigen.db kleinanzeigen.db.bak-$(date +%F).

Usage:
    python3 scripts/ka_site_sync.py --dry-run                 # что импортировали бы
    python3 scripts/ka_site_sync.py --dry-run --account 3 --limit 3
    python3 scripts/ka_site_sync.py --conversation 49sml:2cxlxdm:2pxg5bw9w --account 3 [--raw F]
    python3 scripts/ka_site_sync.py                           # импорт (пишет в БД)
    --db PATH  — другая БД (по умолчанию kleinanzeigen.db рядом с кодом)
Вывод dry-run содержит тексты наших реплик — не сохранять в репозиторий.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database as db  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать в БД")
    ap.add_argument("--limit", type=int, default=15, help="макс. переписок за запуск")
    ap.add_argument("--account", type=int, help="только этот аккаунт")
    ap.add_argument("--max-age-days", type=float, default=21)
    ap.add_argument("--conversation", help="прочитать одну переписку и показать (без записи)")
    ap.add_argument("--raw", help="с --conversation: сохранить сырой JSON в файл (0600)")
    ap.add_argument("--no-translate", action="store_true", help="не переводить реплики на RU")
    ap.add_argument("--db", help="путь к БД")
    args = ap.parse_args()
    if args.db:
        db.DB_PATH = Path(args.db)

    from modules import ka_site, ka_site_sync

    if args.conversation:
        if not args.account:
            ap.error("--conversation требует --account")
        if args.raw:
            # Сырой ответ — для сверки формата парсера на живых данных.
            import os
            orig = ka_site.KaSession.get_conversation

            def _dump(self, cid):  # type: ignore[no-untyped-def]
                data = orig(self, cid)
                fd = os.open(args.raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                return data
            ka_site.KaSession.get_conversation = _dump  # type: ignore[method-assign]
        msgs = ka_site_sync.fetch_conversation(args.account, args.conversation)
        for m in msgs:
            print(f"{m['sent_at']}  {m['sender']:6}  {'НАШ ' if m['ours'] else '    '} {m['text'][:160]!r}")
        return 0

    rep = ka_site_sync.sync_unanswered(
        limit=args.limit, dry_run=args.dry_run, account_id=args.account,
        max_age_days=args.max_age_days, force=True, translate=not args.no_translate,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    if rep.get("errors"):
        return 1
    accs = rep.get("accounts") or {}
    if rep.get("skipped") == "busy" or (accs and not rep.get("threads")
                                        and all("skipped" in a for a in accs.values())):
        return 2  # ничего не прочитали: лок / блок / нет кредов — см. accounts.*.skipped
    return 0


if __name__ == "__main__":
    sys.exit(main())
