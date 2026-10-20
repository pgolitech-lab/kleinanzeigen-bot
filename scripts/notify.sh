#!/usr/bin/env bash
# notify.sh "<текст>" — узкая обёртка над telegram_bot.notify().
# Существует чтобы autofix-агенту можно было разрешить ТОЛЬКО отправку в Telegram,
# не открывая ему произвольный `python3 -c` (= произвольное исполнение кода).
# Код возврата: 0 — сообщение доставлено хотя бы одному оператору, 1 — никому.
# (При DM-fanout telegram_bot.notify глотает ошибки по каждому чату и возвращает {}
#  — без этой проверки протухший токен делал все алерты невидимыми при rc=0.)
set -eu
cd "${KAB_REPO:-/home/pg/kleinanzeigen-bot}"
exec python3 -c 'import sys
from modules import telegram_bot
r = telegram_bot.notify(sys.argv[1])
if not (isinstance(r, dict) and r.get("message_id")):
    sys.exit("notify: Telegram не принял сообщение ни в один чат (результат %r)" % (r,))' "$1"
