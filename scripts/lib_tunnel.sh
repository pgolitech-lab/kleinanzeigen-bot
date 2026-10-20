# shellcheck shell=bash disable=SC2034
# lib_tunnel.sh — общие функции для скриптов quick-tunnel cloudflared.
# Подключается через `source`: update_tunnel_url.sh, tunnel_watchdog.sh, bot_health.sh.
#
# Источник правды об адресе API для Mini App — файл $TUNNEL_URL_FILE (вне репозитория).
# Его пишет update_tunnel_url.sh, читает бот (telegram_bot.tunnel_url()) и подставляет
# в web_app-ссылки как ?api=<url>. Коммитов в git и пересборки GitHub Pages больше нет.

KAB_STATE_DIR="${KAB_STATE_DIR:-/home/pg/.local/state/kleinanzeigen-bot}"
# Адрес, который бот подставляет в ?api= (читается ботом на каждый вызов)
TUNNEL_URL_FILE="${KAB_TUNNEL_URL_FILE:-$KAB_STATE_DIR/tunnel_url}"
# Адрес, под который УСПЕШНО обновлена кнопка меню у всех операторов
MENU_SYNCED_FILE="$KAB_STATE_DIR/menu_synced_url"

# Строгий формат — тот же регэксп, что в боте и во фронте
TUNNEL_RE='^https://[a-z0-9-]+\.trycloudflare\.com$'

tunnel_url_valid() { [[ "${1:-}" =~ $TUNNEL_RE ]]; }

# Метрики cloudflared слушают случайный порт (--metrics 127.0.0.1:0); порт печатается
# в журнал при старте: «Starting metrics server on 127.0.0.1:37627/metrics».
_cf_since() { systemctl show -p ActiveEnterTimestamp --value cloudflared 2>/dev/null; }

cf_metrics_addr() {
  local start
  start=$(_cf_since)
  [ -n "$start" ] || return 1
  journalctl -u cloudflared --since "$start" --no-pager 2>/dev/null \
    | grep -oE 'Starting metrics server on 127\.0\.0\.1:[0-9]+' \
    | tail -1 | grep -oE '127\.0\.0\.1:[0-9]+'
}

# Живой адрес туннеля. Сначала /quicktunnel у метрик (точно то, что зарегистрировано
# сейчас), иначе — последний trycloudflare-URL из журнала с момента старта cloudflared.
# Печатает URL и возвращает 0, либо возвращает 1 без вывода.
cf_live_url() {
  local addr host url start
  addr=$(cf_metrics_addr)
  if [ -n "$addr" ]; then
    host=$(curl -s -m 5 "http://$addr/quicktunnel" 2>/dev/null \
      | grep -oE '"hostname":"[a-z0-9-]+\.trycloudflare\.com"' | cut -d'"' -f4)
    if [ -n "$host" ] && tunnel_url_valid "https://$host"; then
      echo "https://$host"; return 0
    fi
  fi
  start=$(_cf_since)
  [ -n "$start" ] || return 1
  url=$(journalctl -u cloudflared --since "$start" --no-pager 2>/dev/null \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
    | grep -v '^https://api\.trycloudflare\.com$' | tail -1)
  if tunnel_url_valid "$url"; then echo "$url"; return 0; fi
  return 1
}

# Число живых соединений туннеля с edge Cloudflare (из /ready). Пусто — метрики недоступны.
cf_ready_connections() {
  local addr
  addr=$(cf_metrics_addr) || return 1
  [ -n "$addr" ] || return 1
  curl -s -m 5 "http://$addr/ready" 2>/dev/null \
    | grep -oE '"readyConnections":[0-9]+' | cut -d: -f2
}

# Содержимое state-файла, только если оно валидно.
read_url_file() {
  local v
  [ -r "$1" ] || return 1
  v=$(tr -d '[:space:]' < "$1")
  tunnel_url_valid "$v" || return 1
  echo "$v"
}

# Атомарная запись (tmp в той же папке + mv), чтобы бот не прочёл полстроки.
write_url_file() {
  local dst="$1" val="$2" tmp
  mkdir -p "$(dirname "$dst")" || return 1
  tmp=$(mktemp "$dst.XXXXXX") || return 1
  if printf '%s\n' "$val" > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$dst"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}
