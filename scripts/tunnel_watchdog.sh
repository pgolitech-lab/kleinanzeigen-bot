#!/usr/bin/env bash
# tunnel_watchdog.sh — рестарт cloudflared, если quick-tunnel умер посреди работы.
# Сценарий: Cloudflare edge сбрасывает регистрацию quick-туннеля, процесс cloudflared
# остаётся active (running) и вечно ретраит, DNS-имя туннеля исчезает — MA лежит.
# Restart=on-failure это не ловит. Запускается из tunnel-watchdog.timer каждые 5 мин.
# После рестарта cloudflared новый URL подхватывает tunnel-url-sync (PartOf).
set -u

API_JS=/home/pg/kleinanzeigen-bot/web-app/js/api.js

log() { echo "$*"; }

# 0. Grace-период: 3 мин после старта cloudflared sync+Pages ещё догоняют,
#    api.js может содержать старый URL — не рестартим по ложному срабатыванию.
started_us=$(systemctl show cloudflared -p ActiveEnterTimestampMonotonic --value 2>/dev/null || echo 0)
now_us=$(awk '{printf "%d", $1*1000000}' /proc/uptime)
if [ -n "$started_us" ] && [ "$started_us" -gt 0 ] && [ $((now_us - started_us)) -lt 180000000 ]; then
  exit 0
fi

# 1. Если локальный API мёртв — рестарт туннеля не поможет, только зря сменит URL.
if ! curl -fsS -m 5 -o /dev/null http://127.0.0.1:8080/health; then
  log "локальный API 127.0.0.1:8080 не отвечает — туннель не при чём, пропускаю"
  exit 0
fi

# 2. Текущий URL — тот, которым реально пользуется MA (из api.js).
#    Для теста можно передать URL первым аргументом.
URL="${1:-$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$API_JS" | head -1)}"
if [ -z "$URL" ]; then
  log "не нашёл trycloudflare-URL в $API_JS — пропускаю"
  exit 0
fi

# 3. 3 попытки с паузой — одиночный блип edge не повод ротировать URL.
code=000
for attempt in 1 2 3; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$URL/health")
  [ "$code" = "200" ] && exit 0
  sleep 10
done

log "туннель мёртв ($URL/health → $code, 3 попытки) — рестартую cloudflared"
systemctl restart cloudflared
log "cloudflared перезапущен; tunnel-url-sync обновит api.js и GitHub Pages (~90с)"
