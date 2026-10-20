#!/usr/bin/env python3
"""Ручной вход на сайт Kleinanzeigen для аккаунта бота (сохранить сессию / снять блок).

Нужен, когда плановое чтение сайта остановилось (капча, код 2FA, неверный пароль):
    python3 scripts/ka_site_login.py --account 3            # вход паролем из «Аккаунты»;
                                                            # код 2FA спросит в терминале
    python3 scripts/ka_site_login.py --account 3 --cookies cookies.json
        # аварийно: куки kleinanzeigen.de, выгруженные из своего браузера (Cookie-Editor → JSON)
    python3 scripts/ka_site_login.py --status               # блоки и сессии по аккаунтам
Успешный вход сохраняет storage_state (~/.local/state/kleinanzeigen-bot/ka_session_<id>.json,
0600) и снимает блок. На сайте ничего не нажимается, кроме входа.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database as db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account", type=int)
    ap.add_argument("--cookies", help="JSON с куками из браузера")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--headed", action="store_true", help="видимый браузер (нужен X-дисплей)")
    ap.add_argument("--db", help="путь к БД")
    args = ap.parse_args()
    if args.db:
        db.DB_PATH = Path(args.db)

    from modules import ka_site, ka_site_sync

    if args.status:
        state = ka_site_sync._load_state()
        for acc in db.list_accounts():
            st = state.get("accounts", {}).get(str(acc["id"]), {})
            em, pw = ka_site_sync._creds(acc)
            print(f"#{acc['id']} {acc['name']:<14} active={acc['is_active']} "
                  f"login={'да' if em and pw else 'нет'} "
                  f"session={'да' if ka_site.session_path(acc['id']).exists() else 'нет'} "
                  f"block={ka_site_sync.block_status(acc['id'], (em, pw), state) or '-'} "
                  f"last_run={st.get('last_run_at') or '-'}")
        return 0
    if not args.account:
        ap.error("нужен --account")
    acc = db.get_account(args.account)
    if acc is None:
        ap.error(f"нет аккаунта {args.account}")

    with ka_site_sync._single_run() as mine:
        if not mine:
            print("сайт сейчас читает другой процесс (бот/скрипт) — повторите через пару минут")
            return 3
        return _login(args, acc)


def _login(args: argparse.Namespace, acc) -> int:  # type: ignore[no-untyped-def]
    from modules import ka_site, ka_site_sync

    sp = ka_site.session_path(args.account)
    backup = None
    if args.cookies:
        with open(args.cookies) as f:
            raw = json.load(f)
        state = ka_site.cookies_to_storage_state(raw if isinstance(raw, list) else raw.get("cookies", []))
        ka_site_sync._ensure_dir()
        if sp.exists():  # старую сессию не теряем, пока новые куки не проверены
            backup = sp.with_suffix(".bak")
            os.replace(sp, backup)
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        print("куки записаны; проверяю вход…")

    em, pw = ka_site_sync._creds(acc)
    try:
        with ka_site.KaSession(args.account, email=em, password=pw, headless=not args.headed,
                               mfa_code_cb=lambda: input("Код подтверждения с почты/SMS: ")) as s:
            s.open_messages()
            s.save_state()
    except Exception as e:  # интерактивно: понятное сообщение вместо трейсбека
        if backup is not None:
            os.replace(backup, sp)
            print("старая сессия восстановлена")
        kind = e.kind if isinstance(e, ka_site.KaBlocked) else type(e).__name__
        print(f"НЕ вошли ({kind}): {e}. Блок не снят.")
        return 2
    if backup is not None:
        backup.unlink(missing_ok=True)
    ka_site_sync.clear_block(args.account)
    print(f"OK: вошли (паролем: {'да' if s.logged_in_now else 'нет, по сохранённой сессии'}), "
          f"сессия сохранена, блок снят.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
