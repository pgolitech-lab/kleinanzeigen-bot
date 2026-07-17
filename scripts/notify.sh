#!/usr/bin/env bash
# notify.sh "<текст>" — узкая обёртка над telegram_bot.notify().
# Существует чтобы autofix-агенту можно было разрешить ТОЛЬКО отправку в Telegram,
# не открывая ему произвольный `python3 -c` (= произвольное исполнение кода).
set -eu
cd /home/pg/kleinanzeigen-bot
exec python3 -c 'import sys
from modules import telegram_bot
telegram_bot.notify(sys.argv[1])' "$1"
