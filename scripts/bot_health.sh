#!/usr/bin/env bash
# bot_health.sh — детерминированный watchdog «поддержания работоспособности».
# Не требует LLM. Запускается из bot-health.timer каждые ~3 мин.
# Задачи:
#   1. kleinanzeigen-bot должен быть active → иначе reset-failed + restart + алерт.
#   2. Локальный API 127.0.0.1:8080/health должен отвечать 200 → иначе (если сервис
#      active, но завис) restart + алерт.
# Алерты дедуплицируются: одна и та же проблема не спамит чаще раза в час.
set -u

REPO=/home/pg/kleinanzeigen-bot
STATE=/home/pg/.cache/bot_health
mkdir -p "$STATE"
SVC=kleinanzeigen-bot
COOLDOWN=3600   # сек между повторными алертами об одной и той же проблеме

log() { echo "[$(date '+%F %T')] $*"; }

# Отправка в Telegram через модуль проекта (запускается от пользователя pg).
notify() {
  local text="$1"
  cd "$REPO" || return 0
  python3 -c "import sys; from modules import telegram_bot; telegram_bot.notify(sys.argv[1])" "$text" \
    >/dev/null 2>&1 || log "telegram notify FAILED"
}

# Дедуп: алертить только если сигнатура сменилась ИЛИ прошёл COOLDOWN.
should_alert() {
  local key="$1" sig="$2" f="$STATE/alert_$key" now last_sig last_ts
  now=$(date +%s)
  if [ -f "$f" ]; then
    last_sig=$(sed -n 1p "$f"); last_ts=$(sed -n 2p "$f")
    if [ "$last_sig" = "$sig" ] && [ $((now - last_ts)) -lt $COOLDOWN ]; then
      return 1
    fi
  fi
  printf '%s\n%s\n' "$sig" "$now" > "$f"
  return 0
}

clear_alert() { rm -f "$STATE/alert_$1"; }

# --- 1. Сервис жив? ---
if ! sudo systemctl is-active --quiet "$SVC"; then
  log "СЕРВИС НЕ active — восстанавливаю"
  sudo systemctl reset-failed "$SVC" 2>/dev/null
  sudo systemctl restart "$SVC" 2>/dev/null
  sleep 8
  if sudo systemctl is-active --quiet "$SVC"; then
    st="восстановлен"
  else
    st="НЕ ПОДНЯЛСЯ"
  fi
  if should_alert svc "$st"; then
    notify "🔴 <b>watchdog</b>: сервис $SVC был не active → рестарт: <b>$st</b>. Проверь journalctl."
  fi
  exit 0
fi
clear_alert svc

# --- 2. Локальный API отвечает? (3 попытки) ---
code=000
for i in 1 2 3; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8080/health 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 5
done

if [ "$code" != "200" ]; then
  log "health = $code (сервис active, но API не отвечает) — рестарт"
  sudo systemctl restart "$SVC" 2>/dev/null
  sleep 8
  code2=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8080/health 2>/dev/null)
  if should_alert health "$code->$code2"; then
    notify "🟥 <b>watchdog</b>: /health не отвечал (код $code), API завис при active-сервисе → рестарт. После: код $code2."
  fi
  exit 0
fi
clear_alert health

log "ok (service active, health 200)"
exit 0
