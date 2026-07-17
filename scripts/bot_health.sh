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

# --- 3. ФУНКЦИОНАЛЬНАЯ проверка: бот реально забирает почту? ---
# Мотивация (инцидент 2026-07-17): процесс жил, /health отдавал 200, APScheduler писал
# "executed successfully" — но IMAP падал по всем 5 аккаунтам с [Errno -2] 13 часов подряд
# (600 ошибок/час), и никто этого не заметил. Сервис «здоров», основная функция мертва.
# Ошибка ловится поаккаунтно внутри job'а, поэтому наверх не всплывает — нужен скан логов.
IMAP_ERR=$(journalctl -u "$SVC" --since "10 min ago" --no-pager 2>/dev/null \
  | grep -icE "IMAP fetch .* упал|name or service not known|temporary failure in name resolution" )

if [ "${IMAP_ERR:-0}" -ge 20 ]; then
  # Отличаем «залип процесс» от «лежит сеть/DNS на хосте»: рестарт бота лечит только первое.
  if getent hosts imap.gmail.com >/dev/null 2>&1; then
    log "IMAP-ошибок за 10 мин: $IMAP_ERR, но хост резолвит imap.gmail.com → залип процесс, рестарт"
    sudo systemctl restart "$SVC" 2>/dev/null
    sleep 10
    if should_alert imap "stuck-$IMAP_ERR"; then
      notify "🔴 <b>watchdog</b>: бот был active и /health 200, но IMAP не работал ($IMAP_ERR ошибок за 10 мин) при живом DNS хоста → процесс залип, сделал рестарт. Почта снова забирается."
    fi
  else
    # Рестарт бота тут бесполезен — проблема ниже, на уровне сети/резолвера хоста.
    log "IMAP-ошибок: $IMAP_ERR И хост НЕ резолвит imap.gmail.com → проблема сети/DNS, рестарт не поможет"
    if should_alert imap "hostdns-$IMAP_ERR"; then
      notify "🟥 <b>watchdog</b>: DNS хоста не резолвит imap.gmail.com — бот не забирает почту ($IMAP_ERR ошибок за 10 мин). Рестарт бота НЕ поможет: это сеть/резолвер (WiFi/роутер). Нужна твоя проверка."
    fi
  fi
  exit 0
fi
clear_alert imap

log "ok (service active, health 200, IMAP-ошибок за 10 мин: ${IMAP_ERR:-0})"
exit 0
