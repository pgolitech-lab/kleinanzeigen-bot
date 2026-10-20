#!/usr/bin/env bash
# Доводит актуальный URL cloudflared quick-tunnel до Mini App.
# Запускается systemd-юнитом tunnel-url-sync.service после (ре)старта cloudflared.service.
# Quick-туннели Cloudflare ротируют hostname при каждом старте.
#
# Всегда: пишет URL в $STATE_FILE (~/.local/state/kab/tunnel_url) — бот читает
# его и подставляет ?api=<url> в Menu Button и web_app-кнопки (api.js берёт
# адрес бэкенда из ?api=).
#
# Режимы (TUNNEL_SYNC_MODE):
#   menu — только файл + просьба боту переставить Menu Button
#          (POST http://127.0.0.1:8080/internal/tunnel-url, только с localhost).
#          Ничего не коммитит.
#   git  — переходный (по умолчанию, пока бот не умеет ?api=): правит
#          FALLBACK_API_BASE в web-app/js/api.js и пушит в main. Коммит делается
#          в ОТДЕЛЬНОМ служебном worktree ($SYNC_WT), а не в рабочей папке прода:
#          прод-дерево не трогаем — ни reset --hard, ни rebase/autostash
#          (раньше это могло снести чужие незакоммиченные правки).
set -uo pipefail

REPO="${KAB_REPO:-/home/pg/kleinanzeigen-bot}"
MODE="${TUNNEL_SYNC_MODE:-git}"
STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/kab/tunnel_url"
SYNC_WT="${TUNNEL_SYNC_WORKTREE:-$HOME/.cache/kab-tunnel-sync}"
BOT_NOTIFY_URL="http://127.0.0.1:8080/internal/tunnel-url"
URL_RE='https://[a-z0-9-]+\.trycloudflare\.com'

log() { logger -t tunnel-url-sync "$*"; echo "$*"; }

# 0. Сериализация: не допускаем параллельных запусков
exec 9>/tmp/tunnel-url-sync.lock
if ! flock -n 9; then
  log "другой экземпляр уже выполняется — выходим"
  exit 0
fi

# 1. Ждём, пока cloudflared напечатает quick-tunnel URL в журнал (до ~90с)
get_url() {
  local start
  start=$(systemctl show -p ActiveEnterTimestamp --value cloudflared 2>/dev/null)
  if [ -n "$start" ]; then
    journalctl -u cloudflared --since "$start" --no-pager 2>/dev/null \
      | grep -oE "$URL_RE" \
      | grep -v 'https://api\.trycloudflare\.com' | tail -1
  fi
}

NEW_URL=""
for _ in $(seq 1 30); do
  NEW_URL=$(get_url)
  [ -n "$NEW_URL" ] && break
  sleep 3
done

if [ -z "$NEW_URL" ]; then
  log "ERROR: quick-tunnel URL не найден в журнале cloudflared за 90с"
  exit 1
fi

# 2. Файл состояния (атомарно: tmp + mv)
mkdir -p "$(dirname "$STATE_FILE")"
if ! { printf '%s\n' "$NEW_URL" > "$STATE_FILE.tmp" && mv -f "$STATE_FILE.tmp" "$STATE_FILE"; }; then
  log "ERROR: не удалось записать $STATE_FILE"
  exit 1
fi
log "URL туннеля: $NEW_URL (записан в $STATE_FILE, режим $MODE)"

# 3a. menu: попросить бота переставить Menu Button. После ребута бот может
#     подняться позже туннеля — ждём до ~2 мин. Бот при старте сам читает файл,
#     так что неудача здесь не фатальна, но логируется как ERROR.
sync_menu() {
  local code
  for _ in $(seq 1 24); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST "$BOT_NOTIFY_URL")
    if [ "$code" = 200 ] || [ "$code" = 204 ]; then
      log "OK: бот переставил Menu Button на $NEW_URL"
      return 0
    fi
    if [ "$code" = 404 ]; then
      log "ERROR: бот не знает $BOT_NOTIFY_URL (серверная часть ?api= не выкачена) — нужен TUNNEL_SYNC_MODE=git"
      return 1
    fi
    sleep 5
  done
  log "ERROR: бот не ответил на $BOT_NOTIFY_URL за 2 мин (последний код: ${code:-нет}); Menu Button обновится при его старте"
  return 1
}

# 3b. git: коммит FALLBACK_API_BASE в служебном worktree и push в main.
sync_git() {
  local api_js cur_url attempt
  if [ ! -e "$SYNC_WT/.git" ]; then
    git -C "$REPO" fetch -q origin main || { log "ERROR: git fetch не удался"; return 1; }
    git -C "$REPO" worktree prune
    if ! git -C "$REPO" worktree add -q --detach "$SYNC_WT" origin/main; then
      log "ERROR: не удалось создать служебный worktree $SYNC_WT"
      return 1
    fi
  fi
  api_js="$SYNC_WT/web-app/js/api.js"
  for attempt in 1 2 3; do
    # Служебный worktree — только наш: сброс к origin/main здесь безопасен.
    if ! git -C "$SYNC_WT" fetch -q origin main \
       || ! git -C "$SYNC_WT" checkout -q --detach --force origin/main; then
      log "ERROR: не удалось синхронизировать $SYNC_WT с origin/main"
      return 1
    fi
    cur_url=$(grep -oE "$URL_RE" "$api_js" | head -1)
    if [ "$cur_url" = "$NEW_URL" ]; then
      log "в origin/main уже $NEW_URL — ничего не делаю"
      return 0
    fi
    sed -i -E "s|$URL_RE|$NEW_URL|g" "$api_js"
    if ! "$SYNC_WT/scripts/bump_cache.sh" >/dev/null; then
      log "ERROR: bump_cache.sh упал — коммит не делаю"
      return 1
    fi
    git -C "$SYNC_WT" add web-app/js/api.js web-app/index.html
    if ! git -C "$SYNC_WT" commit -q -m "chore(ma): авто-обновление cloudflared quick-tunnel URL ($NEW_URL)"; then
      log "ERROR: git commit не удался"
      return 1
    fi
    if git -C "$SYNC_WT" push -q origin HEAD:main; then
      log "OK: запушено в main, GitHub Pages пересоберётся ~60с. URL: $NEW_URL"
      return 0
    fi
    log "push attempt $attempt отклонён — повтор от свежего origin/main"
  done
  log "ERROR: git push не удался после 3 попыток"
  return 1
}

case "$MODE" in
  menu) sync_menu ;;
  git)  sync_git ;;
  *)    log "ERROR: неизвестный TUNNEL_SYNC_MODE=$MODE (menu|git)"; exit 1 ;;
esac
