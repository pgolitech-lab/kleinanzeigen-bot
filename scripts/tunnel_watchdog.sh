#!/usr/bin/env bash
# tunnel_watchdog.sh — рестарт cloudflared, если quick-tunnel умер посреди работы.
# Сценарий: Cloudflare edge сбрасывает регистрацию quick-туннеля, процесс cloudflared
# остаётся active (running) и вечно ретраит, DNS-имя туннеля исчезает — MA лежит.
# Restart=on-failure это не ловит. Запускается из tunnel-watchdog.timer каждые 5 мин (root).
# После рестарта cloudflared новый URL раздаёт боту tunnel-url-sync (PartOf).
#
# Рестарт cloudflared МЕНЯЕТ адрес (старые кнопки в уведомлениях перестают работать),
# поэтому рестартим только при устойчивом отказе: внешняя проба провалилась 3 раза подряд
# в ДВУХ тиках подряд (~5 мин), при живом локальном API и живом интернете. Не больше
# MAX_RESTARTS_PER_HOUR рестартов в час — дальше только алерт (Cloudflare не выдаёт туннели,
# лимит по IP и т. п. — рестарты не помогут, а адрес будет крутиться впустую).
# Каждый рестарт/старт — алерт в Telegram (notify.sh от pg).
# Ключи: --dry-run (не рестартит и не шлёт, только пишет решение), --url URL (проверить этот адрес).
set -u

SCRIPTS="$(dirname "$(readlink -f "$0")")"
# shellcheck source=lib_tunnel.sh
. "$SCRIPTS/lib_tunnel.sh"

DRY=0; URL_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --url) URL_ARG="${2:-}"; shift ;;
    *) URL_ARG="$1" ;;   # совместимость: раньше URL передавали первым аргументом
  esac
  shift
done

STATE="${TUNNEL_WD_STATE:-/run/kab-tunnel-watchdog}"
if ! mkdir -p "$STATE" 2>/dev/null || ! [ -w "$STATE" ]; then
  echo "ERROR: state-каталог $STATE недоступен для записи — без счётчика тиков рестарт невозможен" >&2
  exit 1
fi
if [ "$DRY" = 1 ]; then
  # dry-run на копии state — боевой счётчик тиков не трогаем
  DRY_STATE=$(mktemp -d) || exit 1
  cp -a "$STATE/." "$DRY_STATE/" 2>/dev/null
  STATE="$DRY_STATE"
  trap 'rm -rf "$DRY_STATE"' EXIT
fi
FAIL_FILE="$STATE/fail_ticks"
RESTARTS_FILE="$STATE/restarts"     # unix-время рестартов, по строке
MAX_RESTARTS_PER_HOUR=3
TICK_MAX_GAP=900                    # провал старше 15 мин (3 интервала) — уже не «подряд»

log() { echo "$*"; }

notify() {
  if [ "$DRY" = 1 ]; then log "[dry-run] notify: $1"; return 0; fi
  # Скрипт идёт от root, а бот/его БД — от pg
  if ! runuser -u pg -- "$SCRIPTS/notify.sh" "$1" >/dev/null 2>&1; then
    log "ERROR: алерт в Telegram не доставлен: $1"
    logger -t tunnel-watchdog -p user.crit -- "алерт в Telegram не доставлен: $1" 2>/dev/null
  fi
}

reset_fails() { rm -f "$FAIL_FILE"; }

# Число подряд идущих провальных тиков (+1); мусор в файле или давний провал → с нуля.
bump_fails() {
  local n=0 age v
  if [ -f "$FAIL_FILE" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$FAIL_FILE" 2>/dev/null || echo 0) ))
    v=$(head -1 "$FAIL_FILE" 2>/dev/null | tr -d '[:space:]')
    [ "$age" -le "$TICK_MAX_GAP" ] && [[ "$v" =~ ^[0-9]+$ ]] && n=$v
  fi
  n=$((n + 1))
  echo "$n" > "$FAIL_FILE" || { echo "ERROR: не записать $FAIL_FILE" >&2; exit 1; }
  echo "$n"
}

# Сколько рестартов за последний час (и чистка старых записей)
recent_restarts() {
  local now cutoff
  now=$(date +%s); cutoff=$((now - 3600))
  [ -f "$RESTARTS_FILE" ] || { echo 0; return; }
  grep -E '^[0-9]+$' "$RESTARTS_FILE" 2>/dev/null | awk -v c="$cutoff" '$1 >= c' > "$RESTARTS_FILE.tmp"
  mv -f "$RESTARTS_FILE.tmp" "$RESTARTS_FILE"
  wc -l < "$RESTARTS_FILE" | tr -d ' '
}

# 0. cloudflared вообще не запущен (упал и Restart=on-failure сдался, или stopped)
if ! systemctl is-active --quiet cloudflared; then
  st=$(systemctl is-active cloudflared 2>/dev/null)
  if [ "$DRY" = 1 ]; then log "[dry-run] cloudflared $st — запустил бы"; exit 0; fi
  log "cloudflared $st — запускаю"
  if systemctl start cloudflared; then
    notify "🟥 <b>tunnel-watchdog</b>: cloudflared был $st — запустил. Новый адрес MA раздаст tunnel-url-sync (кнопка меню)."
  else
    notify "🟥 <b>tunnel-watchdog</b>: cloudflared $st и НЕ запускается — MA недоступна. journalctl -u cloudflared -n 50."
    exit 1
  fi
  exit 0
fi

# 0a. Grace-период: 3 мин после старта cloudflared туннель ещё регистрируется
started_us=$(systemctl show cloudflared -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
now_us=$(awk '{printf "%d", $1*1000000}' /proc/uptime)
if [[ "$started_us" =~ ^[0-9]+$ ]] && [ "$started_us" -gt 0 ] && [ $((now_us - started_us)) -lt 180000000 ]; then
  exit 0
fi

# 1. Если локальный API мёртв — рестарт туннеля не поможет, только зря сменит URL.
#    (Поднимать бота — работа bot_health.sh.)
if ! curl -fsS -m 10 -o /dev/null http://127.0.0.1:8080/health; then
  log "локальный API 127.0.0.1:8080 не отвечает — туннель не при чём, пропускаю"
  reset_fails
  exit 0
fi

# 2. Проверяем тот адрес, что сейчас зарегистрирован у cloudflared (его и раздаёт бот).
URL="$URL_ARG"
[ -n "$URL" ] || URL=$(cf_live_url || read_url_file "$TUNNEL_URL_FILE" || true)
if ! tunnel_url_valid "$URL"; then
  log "cloudflared active, но адрес туннеля не определяется (метрики, журнал, $TUNNEL_URL_FILE) — пропускаю"
  exit 0
fi

# 3. 3 попытки с паузой — одиночный блип edge не повод ротировать URL.
code=000
for _ in 1 2 3; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$URL/health")
  if [ "$code" = "200" ]; then reset_fails; exit 0; fi
  sleep 10
done

# 4. Не виноват ли интернет у NUC? Тогда новый туннель тоже не поднимется — не трогаем.
if ! curl -s -o /dev/null -m 10 https://www.cloudflare.com/cdn-cgi/trace; then
  log "туннель не отвечает ($code), но и cloudflare.com недоступен — нет интернета, рестарт не поможет"
  reset_fails
  exit 0
fi

ready=$(cf_ready_connections || true)
fails=$(bump_fails)
if [ "$fails" -lt 2 ]; then
  log "туннель не отвечает ($URL/health → $code, readyConnections=${ready:-?}), тик $fails/2 — жду следующий"
  exit 0
fi

nrest=$(recent_restarts)
if [ "$nrest" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
  log "туннель мёртв, но рестартов за час уже $nrest — больше не рестартую"
  # дедуп: не чаще раза в час
  if [ ! -f "$STATE/giveup_alert" ] || [ -n "$(find "$STATE/giveup_alert" -mmin +60 2>/dev/null)" ]; then
    notify "🟥 <b>tunnel-watchdog</b>: туннель MA не отвечает ($URL/health → $code), $nrest рестарта cloudflared за час не помогли — рестарты приостановлены до конца часа. journalctl -u cloudflared -n 50."
    [ "$DRY" = 1 ] || touch "$STATE/giveup_alert"
  fi
  exit 0
fi

if [ "$DRY" = 1 ]; then
  log "[dry-run] рестартовал бы cloudflared: $URL/health → $code, readyConnections=${ready:-?}, тиков подряд: $fails, рестартов за час: $nrest"
  exit 0
fi
log "туннель мёртв ($URL/health → $code, readyConnections=${ready:-?}, $fails тика подряд) — рестартую cloudflared"
reset_fails
date +%s >> "$RESTARTS_FILE"
if ! systemctl restart cloudflared; then
  notify "🟥 <b>tunnel-watchdog</b>: туннель MA мёртв ($code), а systemctl restart cloudflared НЕ удался. MA недоступна."
  exit 1
fi
rm -f "$STATE/giveup_alert"
notify "🟡 <b>tunnel-watchdog</b>: туннель MA не отвечал ~5 мин ($code) — перезапустил cloudflared. Адрес сменился: открывайте MA кнопкой меню (кнопки в старых уведомлениях не сработают)."
log "cloudflared перезапущен; tunnel-url-sync передаст новый адрес боту и обновит кнопку меню"
