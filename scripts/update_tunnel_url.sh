#!/usr/bin/env bash
# Синхронизирует текущий quick-tunnel URL cloudflared в web-app/js/api.js и пушит на GitHub Pages.
# Запускается systemd-юнитом tunnel-url-sync.service после (ре)старта cloudflared.service.
# Quick-туннели Cloudflare ротируют hostname при каждом старте — этот скрипт убирает ручную правку.
set -uo pipefail

REPO=/home/pg/kleinanzeigen-bot
API_JS="$REPO/web-app/js/api.js"
WEBAPP="$REPO/web-app"

log() { logger -t tunnel-url-sync "$*"; echo "$*"; }

# 0. Сериализация: не допускаем параллельных запусков (иначе гонка за ref main при push)
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
      | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
      | grep -v 'https://api\.trycloudflare\.com' | tail -1
  fi
}

NEW_URL=""
for i in $(seq 1 30); do
  NEW_URL=$(get_url)
  [ -n "$NEW_URL" ] && break
  sleep 3
done

if [ -z "$NEW_URL" ]; then
  log "ERROR: quick-tunnel URL не найден в журнале cloudflared за 90с"
  exit 1
fi

# 2. Текущий URL, зашитый в api.js
CUR_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$API_JS" | head -1)

if [ "$NEW_URL" = "$CUR_URL" ]; then
  log "URL не изменился ($NEW_URL) — ничего не делаю"
  exit 0
fi

log "URL сменился: ${CUR_URL:-<none>} -> $NEW_URL — обновляю api.js + cache-bust"

# 3. Меняем URL и бампаем cache-bust (?v=YYYYMMDD-HHMMSS) во всех файлах web-app
STAMP=$(date +%Y%m%d-%H%M%S)
sed -i "s|https://[a-z0-9-]\+\.trycloudflare\.com|$NEW_URL|g" "$API_JS"
grep -rlE '\?v=[0-9]+-[0-9]+' "$WEBAPP" | while read -r f; do
  sed -i -E "s|\?v=[0-9]+-[0-9]+|?v=$STAMP|g" "$f"
done

# 4. Коммит
git -C "$REPO" add web-app/
if git -C "$REPO" diff --cached --quiet; then
  log "нет изменений для коммита — выходим"
  exit 0
fi
git -C "$REPO" commit -q -m "chore(ma): авто-обновление cloudflared quick-tunnel URL ($NEW_URL)"

# 5. Push с устойчивостью к расхождению remote (fetch+rebase, идемпотентность)
push_ok=0
for attempt in 1 2 3; do
  if git -C "$REPO" push -q origin main 2>/dev/null; then push_ok=1; break; fi
  log "push attempt $attempt отклонён — fetch и проверка remote"
  git -C "$REPO" fetch -q origin main
  remote_url=$(git -C "$REPO" show origin/main:web-app/js/api.js 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  if [ "$remote_url" = "$NEW_URL" ]; then
    git -C "$REPO" reset -q --hard origin/main
    log "remote уже на актуальном URL ($NEW_URL) — синхронизирован параллельным прогоном"
    push_ok=1; break
  fi
  # --autostash: посторонние незакоммиченные правки в репо (например, недоделанная
  # фича из другой сессии) НЕ должны блокировать синк tunnel-URL — git сам спрячет
  # их на время rebase и вернёт обратно. Инцидент 2026-08-13: ребут после потери
  # питания сменил tunnel URL, а рабочее дерево было грязным от предыдущей сессии —
  # rebase упал с "У вас есть непроиндексированные изменения", MA осталась недоступна
  # до ручного вмешательства.
  if ! git -C "$REPO" rebase -q --autostash origin/main; then
    git -C "$REPO" rebase --abort 2>/dev/null
    log "ERROR: rebase-конфликт при синхронизации с remote"
    exit 1
  fi
done

if [ "$push_ok" = 1 ]; then
  log "OK: GitHub Pages пересоберётся ~60с. Актуальный URL: $NEW_URL"
else
  log "ERROR: git push не удался после 3 попыток"
  exit 1
fi
