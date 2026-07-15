#!/usr/bin/env bash
# bot_autofix.sh — LLM-слой мониторинга. Запускается из bot-autofix.timer каждые 30 мин.
# Дешёвый grep ищет реальные ошибки в логах за последние ~35 мин. ТОЛЬКО если нашёл —
# зовёт claude headless (Opus) чинить в рамках AGENTS.md. Чистые логи → LLM не зовётся → $0.
# Дедуп: одна и та же сигнатура ошибки не запускает claude чаще раза в COOLDOWN.
set -u

REPO=/home/pg/kleinanzeigen-bot
STATE=/home/pg/.cache/bot_autofix
LOGDIR="$STATE/runs"
mkdir -p "$LOGDIR"
SIGFILE="$STATE/last_sig"
COOLDOWN=$((6*3600))   # 6ч: не долбить claude одной и той же неустранённой ошибкой
LOCK="$STATE/lock"
CLAUDE=/home/pg/.local/bin/claude

log() { echo "[$(date '+%F %T')] $*"; }

# Один инстанс за раз (claude-прогон может идти минутами).
exec 9>"$LOCK"
if ! flock -n 9; then
  log "уже выполняется — выход"
  exit 0
fi

# --- 1. Дешёвый grep реальных ошибок за 35 мин ---
ERR=$(journalctl -u kleinanzeigen-bot --since "35 min ago" --no-pager 2>/dev/null \
  | grep -iE "error|traceback|exception|critical|fatal|failed|refused|timed? ?out" \
  | grep -viE "monitor_errors_job|next run at|executed successfully|0 error|no error|/health HTTP" )

if [ -z "$ERR" ]; then
  log "логи чистые — LLM не зову"
  exit 0
fi

# --- 2. Дедуп по сигнатуре (нормализуем: убираем таймстампы/pid/числа) ---
NORM=$(echo "$ERR" | sed -E 's/^[а-я]+ +[0-9]+ [0-9:]+//; s/[0-9]+//g' | sort -u)
SIG=$(echo "$NORM" | sha1sum | cut -d' ' -f1)
now=$(date +%s)
if [ -f "$SIGFILE" ]; then
  last_sig=$(sed -n 1p "$SIGFILE"); last_ts=$(sed -n 2p "$SIGFILE")
  if [ "$last_sig" = "$SIG" ] && [ $((now - last_ts)) -lt $COOLDOWN ]; then
    log "та же сигнатура ошибки в пределах cooldown — пропускаю"
    exit 0
  fi
fi
printf '%s\n%s\n' "$SIG" "$now" > "$SIGFILE"

log "найдены ошибки (sig=$SIG) — запускаю claude autofix"
RUNLOG="$LOGDIR/$(date '+%Y%m%d-%H%M%S')-$SIG.log"

# --- 3. Промпт с жёсткими рамками AGENTS.md ---
PROMPT=$(cat <<'EOF'
Ты автономный дежурный по проду kleinanzeigen-bot (/home/pg/kleinanzeigen-bot).
В логах journalctl -u kleinanzeigen-bot за последние ~35 минут найдены ошибки.
Задача: разобраться в первопричине и починить, СОБЛЮДАЯ рамки AGENTS.md.

Шаги:
1. Прочитай ошибки: journalctl -u kleinanzeigen-bot --since "40 min ago" --no-pager | grep -iE "error|traceback|exception|critical|fatal|failed" | grep -viE "monitor_errors_job|next run at|executed successfully"
2. Найди первопричину в коде.
3. Если это простой, безопасный, однозначный баг в коде — почини его. Затем:
   - python3 -m pytest tests/ -q  (чинить только если тесты зелёные)
   - GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add -A && commit (conventional prefix + русское описание, автор Claude)
   - sudo systemctl restart kleinanzeigen-bot && проверь journalctl -u kleinanzeigen-bot -n 30 на чистый старт
   - Уведоми оператора в Telegram что ПОЧИНИЛ (что за баг, что сделал):
     python3 -c "from modules import telegram_bot; telegram_bot.notify('✅ автофикс: <текст>')"

ЖЁСТКИЕ ЗАПРЕТЫ (нарушать нельзя ни при каких условиях):
- НЕ менять send_mode, НЕ слать письма покупателям.
- НЕ делать DELETE/DROP/схема-миграций в БД.
- НЕ пушить в origin (только локальный коммит) — если не уверен, вообще не коммить.
- Если баг требует смены поведения отправки, миграции схемы, затрагивает деньги/клиентов,
  или причина неоднозначна / у тебя нет уверенного безопасного фикса — НИЧЕГО НЕ МЕНЯЙ.
  Вместо этого отправь оператору в Telegram описание проблемы + предлагаемое решение:
  python3 -c "from modules import telegram_bot; telegram_bot.notify('⚠️ нужна твоя реакция: <текст>')"

Если ошибки транзиентные (сетевой блип IMAP/SMTP, разовый таймаут туннеля) и код чинить нечего —
ничего не делай и молчи (не уведомляй). Работай кратко и по делу.
EOF
)

cd "$REPO" || exit 1
timeout 900 "$CLAUDE" -p "$PROMPT" \
  --permission-mode bypassPermissions \
  --model claude-opus-4-8 \
  --output-format json \
  > "$RUNLOG" 2>&1
rc=$?
log "claude завершился rc=$rc, лог: $RUNLOG"

# Если claude упал (не транзиент) — уведомить, чтобы не проглотить молча.
if [ $rc -ne 0 ]; then
  cd "$REPO"
  python3 -c "import sys; from modules import telegram_bot; telegram_bot.notify(sys.argv[1])" \
    "🟥 <b>autofix</b>: claude-прогон завершился с кодом $rc (лог на сервере: $RUNLOG). Ошибки в логах остались — нужна ручная проверка." \
    >/dev/null 2>&1 || true
fi
exit 0
