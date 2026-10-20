#!/usr/bin/env bash
# update_tunnel_url.sh — передаёт актуальный quick-tunnel URL боту БЕЗ git и GitHub Pages.
#
# Было: каждая ротация URL = правка web-app/js/api.js + bump всех ?v= + коммит + push +
# `git reset --hard` в проде (мог стереть чужие незакоммиченные правки) + 60–90 с, пока
# Pages пересоберётся. Стало: адрес API передаётся в URL самой Mini App (?api=<tunnel>).
#   1. Узнаём живой URL у cloudflared (метрики /quicktunnel, запасной путь — журнал).
#   2. Атомарно пишем его в $TUNNEL_URL_FILE — бот подставляет его во все новые web_app-кнопки.
#   3. Через бота обновляем кнопку меню (setChatMenuButton) у всех операторов.
#   4. Отмечаем успех в $MENU_SYNCED_FILE — по нему bot_health.sh видит, что синк дошёл.
#
# Запуск: tunnel-url-sync.service (после старта cloudflared) и self-heal из bot_health.sh.
# Ключи: --dry-run (ничего не пишет и не вызывает бота), --url URL (не спрашивать cloudflared),
#        --force (обновить кнопку меню, даже если URL не менялся).
set -uo pipefail

REPO="${KAB_REPO:-/home/pg/kleinanzeigen-bot}"
# shellcheck source=lib_tunnel.sh
. "$(dirname "$(readlink -f "$0")")/lib_tunnel.sh"

DRY=0; FORCE=0; URL_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --url) URL_ARG="${2:-}"; shift ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { logger -t tunnel-url-sync -- "$*" 2>/dev/null; echo "$*"; }
err() { logger -t tunnel-url-sync -p user.err -- "ERROR: $*" 2>/dev/null; echo "ERROR: $*" >&2; }

# 0. Сериализация: unit и self-heal из bot_health.sh могут стартовать одновременно
mkdir -p "$KAB_STATE_DIR" || { err "не могу создать $KAB_STATE_DIR"; exit 1; }
exec 9>"$KAB_STATE_DIR/update_tunnel_url.lock"
if ! flock -w 120 9; then
  err "другой экземпляр держит лок больше 120 с — выхожу"
  exit 1
fi

# 1. Живой URL: ждём, пока cloudflared зарегистрирует туннель (до ~90 с после старта)
NEW_URL="$URL_ARG"
if [ -z "$NEW_URL" ]; then
  for _ in $(seq 1 30); do
    NEW_URL=$(cf_live_url) && break
    sleep 3
  done
fi
if ! tunnel_url_valid "$NEW_URL"; then
  err "нет валидного quick-tunnel URL (получено: '${NEW_URL}'): --url не по формату или cloudflared не поднялся за 90 с"
  exit 1
fi

CUR_URL=$(read_url_file "$TUNNEL_URL_FILE" || true)
SYNCED_URL=$(read_url_file "$MENU_SYNCED_FILE" || true)

if [ "$NEW_URL" = "$CUR_URL" ] && [ "$NEW_URL" = "$SYNCED_URL" ] && [ "$FORCE" = 0 ]; then
  log "URL не изменился и кнопка меню уже на нём ($NEW_URL) — ничего не делаю"
  exit 0
fi

if [ "$DRY" = 1 ]; then
  log "[dry-run] записал бы $NEW_URL в $TUNNEL_URL_FILE (сейчас: ${CUR_URL:-<нет>})"
  log "[dry-run] вызвал бы telegram_bot.set_menu_button() из $REPO (кнопка сейчас на: ${SYNCED_URL:-<нет>})"
  exit 0
fi

# 2. Бот читает этот файл на каждый web_app-URL — новые уведомления сразу с новым ?api=
if [ "$NEW_URL" != "$CUR_URL" ]; then
  if ! write_url_file "$TUNNEL_URL_FILE" "$NEW_URL"; then
    err "не удалось записать $TUNNEL_URL_FILE"
    exit 1
  fi
  log "адрес API для Mini App: ${CUR_URL:-<нет>} -> $NEW_URL"
fi

# 3. Кнопка меню. Отдельный процесс python — работает, даже если сам бот лежит.
#    set_menu_button() возвращает число чатов, где Telegram принял кнопку. Успех — только
#    если приняли ВСЕ операторы: иначе у кого-то кнопка останется со старым (мёртвым) адресом,
#    а menu_synced_url сказал бы «всё ок». Частичный успех → exit 1 → bot_health повторит.
out=$( { cd "$REPO" && timeout 120 python3 -c '
import sys
import config
from modules import telegram_bot
total = len(config.telegram_operator_dm_ids() or [config.telegram_chat_id()])
n = telegram_bot.set_menu_button()
if not isinstance(n, int):
    sys.exit("set_menu_button() вернула %r вместо числа чатов — бэкенд без поддержки ?api=" % (n,))
if n < total:
    sys.exit("set_menu_button(): кнопку приняли %d из %d чатов (ошибки выше)" % (n, total))
print(n)
'; } 2>&1)
rc=$?
if [ $rc -ne 0 ]; then
  err "кнопка меню НЕ обновлена (rc=$rc): $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  exit 1
fi

# 4. Фиксируем успех — сравнивая с ним живой URL, bot_health.sh ловит недошедший синк
if ! write_url_file "$MENU_SYNCED_FILE" "$NEW_URL"; then
  err "кнопка обновлена, но не записан $MENU_SYNCED_FILE — watchdog будет повторять синк"
  exit 1
fi
log "OK: кнопка меню обновлена в $(printf '%s' "$out" | tail -1) чат(ах), API: $NEW_URL"
exit 0
