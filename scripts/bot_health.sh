#!/usr/bin/env bash
# bot_health.sh — детерминированный watchdog «поддержания работоспособности».
# Не требует LLM. Запускается из bot-health.timer каждые ~3 мин.
# Задачи:
#   1. kleinanzeigen-bot должен быть active → иначе reset-failed + restart + алерт.
#   2. Локальный API 127.0.0.1:8080/health должен отвечать 200 → иначе (если сервис
#      active, но завис ДВА тика подряд) restart + алерт.
#   3. Почта реально забирается: poll_age_sec из /health < POLL_STALE → иначе (при живом
#      DNS, два тика подряд) restart + алерт.
#   4. Кнопка меню MA должна смотреть на живой cloudflared quick-tunnel URL —
#      иначе self-heal через update_tunnel_url.sh, алерт только если не помогло.
#   5. OAuth-сессия claude CLI не должна истечь молча — предупредить за WARN_DAYS
#      до истечения refresh-токена (через неё идут все LLM-вызовы бота).
#   6. Бэкап БД свежий (если kab-backup.timer включён): метка last_backup_ok < 26 ч.
# Watchdog-рестарт — не чаще раза в 30 мин (дальше только алерты).
# Алерты дедуплицируются (раз в час на сигнатуру); отметка пишется только после доставки.
# --dry-run: проверки на копии state, без рестартов, Telegram и записи синка туннеля.
set -u

REPO=/home/pg/kleinanzeigen-bot
SCRIPTS="$(dirname "$(readlink -f "$0")")"
# shellcheck source=lib_tunnel.sh
. "$SCRIPTS/lib_tunnel.sh"
STATE="${BOT_HEALTH_STATE:-/home/pg/.cache/bot_health}"
mkdir -p "$STATE" || { echo "не могу создать $STATE" >&2; exit 1; }
# --dry-run: всё проверяет, но не рестартит, не шлёт в Telegram и синк туннеля гоняет вхолостую.
# Работает на КОПИИ state — боевые счётчики тиков и дедуп алертов не трогает.
DRY=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY=1
  DRY_STATE=$(mktemp -d) || exit 1
  cp -a "$STATE/." "$DRY_STATE/" 2>/dev/null
  STATE="$DRY_STATE"
  trap 'rm -rf "$DRY_STATE"' EXIT
fi
SVC=kleinanzeigen-bot
HEALTH_URL="${BOT_HEALTH_URL:-http://127.0.0.1:8080/health}"
COOLDOWN=3600   # сек между повторными алертами об одной и той же проблеме
TICK_MAX_GAP=600  # «плохой» тик старше 10 мин (>3 интервалов таймера) — уже не «подряд»

log() { echo "[$(date '+%F %T')] $*"; }

# Целое из state-файла; мусор/нет файла → значение по умолчанию (а не падение скрипта).
read_int() {
  local v
  v=$(head -c 32 "$1" 2>/dev/null | head -1 | tr -d '[:space:]')
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo "${2:-0}"
}

# Отправка в Telegram (notify.sh возвращает 1, если сообщение не доставлено никому).
notify() {
  local text="$1"
  if [ "$DRY" = 1 ]; then log "[dry-run] notify: $text"; return 0; fi
  if ! KAB_REPO="$REPO" "$SCRIPTS/notify.sh" "$text" >/dev/null 2>"$STATE/notify_err"; then
    log "telegram notify FAILED: $(tail -1 "$STATE/notify_err" 2>/dev/null)"
    logger -t bot-health -p user.crit -- "алерт в Telegram не доставлен: $text" 2>/dev/null
    return 1
  fi
  return 0
}

# Алерт с дедупом: шлём, если сигнатура сменилась ИЛИ прошёл COOLDOWN. Отметку о
# дедупе пишем только ПОСЛЕ успешной доставки — недошедший алерт повторится в следующий тик.
alert() {
  local key="$1" sig="$2" text="$3" now last_sig last_ts
  local f="$STATE/alert_$key"
  now=$(date +%s)
  if [ -f "$f" ]; then
    last_sig=$(sed -n 1p "$f")
    last_ts=$(sed -n 2p "$f" | tr -dc '0-9'); last_ts=${last_ts:-0}
    if [ "$last_sig" = "$sig" ] && [ $((now - last_ts)) -lt $COOLDOWN ]; then
      return 0
    fi
  fi
  notify "$text" && printf '%s\n%s\n' "$sig" "$now" > "$f"
}

clear_alert() { rm -f "$STATE/alert_$1"; }

# Управление сервисом (в --dry-run только пишет, что сделал бы). Код возврата — systemctl.
svc_ctl() {
  if [ "$DRY" = 1 ]; then log "[dry-run] systemctl $*"; return 0; fi
  sudo -n systemctl "$@" 2>"$STATE/svc_err" || { log "systemctl $* FAILED: $(tail -1 "$STATE/svc_err")"; return 1; }
}

# --- 1. Сервис жив? ---
if ! systemctl is-active --quiet "$SVC"; then
  log "СЕРВИС НЕ active — восстанавливаю"
  svc_ctl reset-failed "$SVC"
  if ! svc_ctl restart "$SVC"; then
    st="рестарт НЕ выполнен ($(tail -1 "$STATE/svc_err" 2>/dev/null))"
  else
    # 30 с, а не 8: при crash-loop сервис успевает побыть active между падениями
    [ "$DRY" = 1 ] || sleep 30
    if systemctl is-active --quiet "$SVC"; then st="восстановлен"; else st="НЕ ПОДНЯЛСЯ"; fi
  fi
  nr=$(systemctl show "$SVC" -p NRestarts --value 2>/dev/null)
  alert svc "$st" "🔴 <b>watchdog</b>: сервис $SVC был не active → рестарт: <b>$st</b> (NRestarts=${nr:-?}). Проверь journalctl -u $SVC."
  exit 0
fi
# Crash-loop: Restart=always поднимает сервис сам, и он почти всегда «active» в момент проверки.
NR=$(read_int <(systemctl show "$SVC" -p NRestarts --value 2>/dev/null) 0)
NR_PREV=$(read_int "$STATE/nrestarts" "$NR")
echo "$NR" > "$STATE/nrestarts"
if [ "$NR" -ge $((NR_PREV + 3)) ]; then
  alert crashloop "$NR" "🔴 <b>watchdog</b>: $SVC перезапускался systemd $((NR - NR_PREV)) раз(а) за ~3 мин (NRestarts=$NR) — похоже на crash-loop. journalctl -u $SVC -n 50."
fi
clear_alert svc

# Сколько секунд сервис в состоянии active (для grace-периода после старта/рестарта).
# Не смогли узнать — считаем «давно» (иначе завис /health никогда не привёл бы к рестарту).
svc_uptime() {
  local mono now_us
  mono=$(systemctl show "$SVC" -p ActiveEnterTimestampMonotonic --value 2>/dev/null)
  now_us=$(awk '{printf "%d", $1*1000000}' /proc/uptime)
  if [[ "$mono" =~ ^[0-9]+$ ]] && [ "$mono" -gt 0 ]; then echo $(( (now_us - mono) / 1000000 )); else echo 999999; fi
}

# Watchdog-рестарт: не чаще раза в RESTART_MIN_GAP, иначе — только алерт (защита от
# цикла «рестарт → не помогло → рестарт», который сам роняет работу бота).
# 0 — рестарт выполнен; 1 — пропущен из-за RESTART_MIN_GAP; 2 — systemctl не смог.
RESTART_MIN_GAP=1800
watchdog_restart() {
  local why="$1" now last
  now=$(date +%s)
  last=$(read_int "$STATE/last_restart_ts" 0)
  if [ $((now - last)) -lt $RESTART_MIN_GAP ]; then
    log "рестарт ($why) НЕ делаю: предыдущий watchdog-рестарт был $((now - last)) с назад"
    return 1
  fi
  log "рестарт сервиса: $why"
  svc_ctl restart "$SVC" || return 2
  echo "$now" > "$STATE/last_restart_ts"
  return 0
}

# Счётчик «плохих» тиков подряд: рестарт только при устойчивой проблеме, не из-за кратких пауз.
# Тик, записанный давно (ранний exit, выключенный таймер), подряд не считается.
bump_ticks() {
  local f="$STATE/ticks_$1" n=0 age
  if [ -f "$f" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || echo 0) ))
    [ "$age" -le "$TICK_MAX_GAP" ] && n=$(read_int "$f" 0)
  fi
  n=$((n + 1)); echo "$n" > "$f"; echo "$n"
}
reset_ticks() { rm -f "$STATE/ticks_$1"; }

# --- 2. Локальный API отвечает? ---
# 3 попытки по 10 с в тике; рестарт — только если не отвечал ДВА тика подряд (~3–6 мин).
# Одиночный долгий ответ (тяжёлый запрос в threadpool, сборка мусора) — не повод.
HEALTH_JSON=""
code=000
for _ in 1 2 3; do
  HEALTH_JSON=$(curl -s -m 10 -w '\n%{http_code}' "$HEALTH_URL" 2>/dev/null)
  code=$(printf '%s' "$HEALTH_JSON" | tail -1)
  [ "$code" = "200" ] && break
  sleep 5
done
HEALTH_JSON=$(printf '%s' "$HEALTH_JSON" | sed '$d')

if [ "$code" != "200" ]; then
  up=$(svc_uptime)
  if [ "$up" -lt 120 ]; then
    log "health = $code, но сервис active всего ${up}с — ещё стартует, жду"
    exit 0
  fi
  n=$(bump_ticks health)
  if [ "$n" -lt 2 ]; then
    log "health = $code (тик $n/2) — жду следующий тик перед рестартом"
    exit 0
  fi
  watchdog_restart "/health = $code $n тика подряд"; rc=$?
  case $rc in
    0) reset_ticks health
       [ "$DRY" = 1 ] || sleep 15
       code2=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$HEALTH_URL" 2>/dev/null)
       alert health "$code->$code2" "🟥 <b>watchdog</b>: /health не отвечал (код $code, $n проверки подряд) при active-сервисе → рестарт. После: код $code2." ;;
    1) alert health "norestart-$code" "🟥 <b>watchdog</b>: /health не отвечает (код $code), но рестарт уже был меньше 30 мин назад — не помог. Нужна ручная проверка: journalctl -u $SVC." ;;
    *) alert health "restartfail-$code" "🟥 <b>watchdog</b>: /health не отвечает (код $code), а рестарт НЕ выполнился: $(tail -1 "$STATE/svc_err" 2>/dev/null | sed 's/[<>&]//g'). Нужна ручная проверка." ;;
  esac
  exit 0
fi
reset_ticks health
clear_alert health

# --- 3. ФУНКЦИОНАЛЬНАЯ проверка: бот реально забирает почту? ---
# Мотивация (инцидент 2026-07-17): процесс жил, /health отдавал 200, APScheduler писал
# "executed successfully" — но IMAP падал по всем 5 аккаунтам с [Errno -2] 13 часов подряд,
# и никто этого не заметил. Настоящий признак жизни — poll_age_sec из /health: сколько
# секунд назад был последний УСПЕШНЫЙ опрос почты хотя бы одного ящика (null — ещё не было).
# poll_tracking=false / поля нет (сборка без статуса опроса) — запасной путь: скан журнала.
POLL_STALE=${POLL_STALE:-900}   # 15 мин при интервале опроса 60 с = явно не «краткая пауза»
read -r POLL_AGE POLL_PAUSED < <(printf '%s' "$HEALTH_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    assert isinstance(d, dict)
except Exception:
    print("parse_error 0"); sys.exit()
paused = 1 if d.get("polling_paused") else 0
if "poll_age_sec" not in d or d.get("poll_tracking") is False:
    print("untracked", paused); sys.exit()
age = d["poll_age_sec"]
try:
    print("null" if age is None else int(age), paused)
except (TypeError, ValueError):
    print("parse_error", paused)
' 2>/dev/null)
POLL_AGE=${POLL_AGE:-parse_error}

POLL_PROBLEM=""
if [ "${POLL_PAUSED:-0}" = 1 ]; then
  log "опрос почты на паузе (polling_paused) — проверку возраста опроса пропускаю"
elif [ "$POLL_AGE" = "untracked" ] || [ "$POLL_AGE" = "parse_error" ]; then
  # Нарушен контракт /health — проверка почты слабеет до грепа журнала. Сообщаем, раз в час.
  if [ "$POLL_AGE" = "parse_error" ] && [ "$(bump_ticks contract)" -ge 2 ]; then
    alert contract "parse" "🟡 <b>watchdog</b>: /health отвечает 200, но не JSON со статусом опроса — проверка «почта забирается» работает только по журналу. Проверь web/app.py:/health."
  fi
  IMAP_ERR=$(journalctl -u "$SVC" --since "10 min ago" --no-pager 2>/dev/null \
    | grep -icE "IMAP fetch .* упал|name or service not known|temporary failure in name resolution")
  [ "${IMAP_ERR:-0}" -ge 20 ] && POLL_PROBLEM="IMAP-ошибок за 10 мин: $IMAP_ERR"
elif [ "$POLL_AGE" = "null" ]; then
  up=$(svc_uptime)
  [ "$up" -ge "$POLL_STALE" ] && POLL_PROBLEM="с момента старта (${up}с) ни одного успешного опроса почты"
elif [ "$POLL_AGE" -ge "$POLL_STALE" ]; then
  POLL_PROBLEM="последний успешный опрос почты $((POLL_AGE / 60)) мин назад"
fi
[ "$POLL_AGE" = "parse_error" ] || { reset_ticks contract; clear_alert contract; }

if [ -n "$POLL_PROBLEM" ]; then
  # Отличаем «залип процесс» от «лежит сеть/DNS на хосте»: рестарт бота лечит только первое.
  if getent hosts imap.gmail.com >/dev/null 2>&1; then
    n=$(bump_ticks poll)
    if [ "$n" -lt 2 ]; then
      log "$POLL_PROBLEM, DNS хоста жив (тик $n/2) — жду следующий тик"
      exit 0
    fi
    watchdog_restart "$POLL_PROBLEM при живом DNS"; rc=$?
    case $rc in
      0) reset_ticks poll
         alert imap "stuck" "🔴 <b>watchdog</b>: бот active и /health 200, но почта не забиралась ($POLL_PROBLEM) при живом DNS хоста → процесс залип, сделал рестарт." ;;
      1) alert imap "stuck-norestart" "🟥 <b>watchdog</b>: почта не забирается ($POLL_PROBLEM), рестарт меньше 30 мин назад не помог. Нужна ручная проверка: journalctl -u $SVC." ;;
      *) alert imap "stuck-restartfail" "🟥 <b>watchdog</b>: почта не забирается ($POLL_PROBLEM), а рестарт НЕ выполнился: $(tail -1 "$STATE/svc_err" 2>/dev/null | sed 's/[<>&]//g')." ;;
    esac
  else
    # Рестарт бота тут бесполезен — проблема ниже, на уровне сети/резолвера хоста.
    reset_ticks poll
    log "$POLL_PROBLEM И хост НЕ резолвит imap.gmail.com → проблема сети/DNS, рестарт не поможет"
    alert imap "hostdns" "🟥 <b>watchdog</b>: DNS хоста не резолвит imap.gmail.com — бот не забирает почту ($POLL_PROBLEM). Рестарт бота НЕ поможет: это сеть/резолвер (WiFi/роутер). Нужна твоя проверка."
  fi
  exit 0
fi
reset_ticks poll
clear_alert imap

# --- 4. Кнопка меню / ссылки MA смотрят на живой tunnel URL? ---
# Адрес API передаётся в Mini App как ?api=<tunnel> (см. update_tunnel_url.sh), git и
# GitHub Pages в этом не участвуют. Сверяем живой адрес cloudflared с тем, под который
# кнопка меню успешно обновлена. Расхождение = tunnel-url-sync не дошёл (сеть, Telegram,
# бэкенд) → self-heal повтором синка; алерт, если не помогло.
MA_URL_STATE="$STATE/ma_url_mismatch_since"
GRACE_SELFHEAL=120   # сек — tunnel-url-sync сам ждёт регистрации туннеля до 90 с
GRACE_ALERT=600      # сек — после нескольких попыток self-heal эскалируем

LIVE_URL=$(cf_live_url || true)
SYNCED_URL=$(read_url_file "$MENU_SYNCED_FILE" || true)

if [ -z "$LIVE_URL" ]; then
  # cloudflared лежит — это ловит и чинит tunnel-watchdog; active, но адреса нет — странно
  if systemctl is-active --quiet cloudflared && [ "$(bump_ticks nourl)" -ge 3 ]; then
    alert nourl "nourl" "🟡 <b>watchdog</b>: cloudflared active, но адрес туннеля не определяется (ни /quicktunnel, ни журнал) уже ~10 мин — синк кнопки меню MA не проверяется. Формат логов cloudflared сменился?"
  fi
  log "MA URL check пропущен — не узнал живой адрес у cloudflared"
elif [ "$LIVE_URL" = "$SYNCED_URL" ]; then
  reset_ticks nourl
  rm -f "$MA_URL_STATE"
  clear_alert ma_url
else
  reset_ticks nourl
  now=$(date +%s)
  if [ ! -f "$MA_URL_STATE" ]; then
    echo "$now" > "$MA_URL_STATE"
    log "кнопка меню на ${SYNCED_URL:-<нет>}, живой туннель $LIVE_URL — наблюдаю (синк может ещё идти)"
  else
    since=$(read_int "$MA_URL_STATE" "$now")
    elapsed=$((now - since))
    if [ "$elapsed" -ge "$GRACE_SELFHEAL" ]; then
      log "расхождение адреса держится ${elapsed}с — self-heal: update_tunnel_url.sh"
      if [ "$DRY" = 1 ]; then
        "$SCRIPTS/update_tunnel_url.sh" --url "$LIVE_URL" --dry-run 2>&1 | sed 's/^/  /'
      elif "$SCRIPTS/update_tunnel_url.sh" --url "$LIVE_URL" >"$STATE/last_tunnel_sync.log" 2>&1; then
        rm -f "$MA_URL_STATE"
        clear_alert ma_url
        log "self-heal OK: кнопка меню на $LIVE_URL"
      elif [ "$elapsed" -ge "$GRACE_ALERT" ]; then
        alert ma_url "mismatch-$LIVE_URL" "🟥 <b>watchdog</b>: кнопка меню MA не обновлена на живой адрес туннеля уже $((elapsed/60)) мин. Сейчас: ${SYNCED_URL:-нет}, актуальный: $LIVE_URL. Повтор синка падает: $(tail -1 "$STATE/last_tunnel_sync.log" 2>/dev/null | sed 's/[<>&]//g'). Проверь: journalctl -t tunnel-url-sync."
      fi
    fi
  fi
fi

# --- 5. OAuth-сессия claude CLI не близка к истечению? ---
# Мотивация (инциденты 2026-08-08 и 2026-09-05): refreshToken живёт ровно ~28 дней от
# /login, ежедневный авторефреш access-токена срок жизни refresh-токена НЕ продлевает.
# С 2026-10 через подписку (claude CLI) идут ВСЕ LLM-вызовы бота: драфты, классификатор,
# переводы, автопилот — и дневной bot-autofix. Предупреждаем заранее, а не постфактум.
CREDS=/home/pg/.claude/.credentials.json
OAUTH_STATE="$STATE/oauth_alert_ts"
OAUTH_COOLDOWN=79200   # ~22ч — предупреждать не чаще раза в сутки (не раза в час, как остальные)
WARN_DAYS=3

REFRESH_LEFT=$(python3 -c "
import json, time
d = json.load(open('$CREDS'))['claudeAiOauth']
print(int(d['refreshTokenExpiresAt'] / 1000 - time.time()))
" 2>"$STATE/oauth_err")

oauth_notify() {
  local now last
  now=$(date +%s)
  last=$(read_int "$OAUTH_STATE" 0)
  [ $((now - last)) -ge $OAUTH_COOLDOWN ] || return 0
  notify "$1" && echo "$now" > "$OAUTH_STATE"
}

if ! [[ "${REFRESH_LEFT:-}" =~ ^-?[0-9]+$ ]]; then
  log "не удалось прочитать refreshTokenExpiresAt из $CREDS: $(tail -1 "$STATE/oauth_err" 2>/dev/null)"
  oauth_notify "🟡 <b>watchdog</b>: не могу прочитать срок OAuth-сессии claude CLI ($CREDS) — не залогинен или сменился формат. Если сессии нет, все LLM-функции бота стоят. Проверь: claude /status."
elif [ "$REFRESH_LEFT" -lt $((WARN_DAYS * 86400)) ]; then
  if [ "$REFRESH_LEFT" -lt 0 ]; then
    oauth_notify "🟥 <b>watchdog</b>: OAuth-сессия claude CLI истекла — драфты, классификатор, переводы и автопилот бота не работают, дневной bot-autofix тоже. Сделай /login."
  else
    oauth_notify "🟡 <b>watchdog</b>: OAuth-сессия claude CLI (refresh-токен) истекает через ~$((REFRESH_LEFT / 86400))д. После истечения встанут все LLM-функции бота (драфты, классификатор, автопилот) — сделай /login заранее."
  fi
else
  rm -f "$OAUTH_STATE"
fi

# --- 6. Бэкап БД свежий? ---
# backup.sh сам алертит о провале, но не о том, что он вообще не запускался (таймер не
# включён после переустановки, NUC спал в 03:30 и Persistent не догнал, скрипт убит).
# Источник — метка last_backup_ok, которую backup.sh пишет после ПОЛНОГО успеха
# (снимок + облако); её же отдаёт /health как backup_age_sec.
BACKUP_MARKER=/home/pg/.local/state/kleinanzeigen-bot/last_backup_ok
BACKUP_MAX_AGE_H=26
if systemctl is-enabled --quiet kab-backup.timer 2>/dev/null; then
  b_ts=$(read_int "$BACKUP_MARKER" 0); age_h=999
  [ "$b_ts" -gt 0 ] && age_h=$(( ($(date +%s) - b_ts) / 3600 ))
  if [ "$age_h" -ge "$BACKUP_MAX_AGE_H" ]; then
    alert backup "stale" "🟥 <b>watchdog</b>: свежего бэкапа БД нет (последний полный: $([ "$b_ts" -gt 0 ] && echo "${age_h} ч назад" || echo "никогда")). Проверь: systemctl status kab-backup.service, journalctl -t kab-backup."
  else
    clear_alert backup
  fi
fi

log "ok (service active, health 200, poll_age_sec=$POLL_AGE)"
exit 0
