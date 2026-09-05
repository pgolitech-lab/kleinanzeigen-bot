#!/usr/bin/env bash
# bot_autofix.sh — LLM-слой мониторинга. Запускается из bot-autofix.timer раз в сутки в 5:00.
# Дешёвый grep ищет реальные ошибки в логах за последние ~25ч. ТОЛЬКО если нашёл —
# зовёт claude headless (Opus) чинить. Чистые логи → LLM не зовётся → $0.
# Дедуп: одна и та же сигнатура ошибки не запускает claude чаще раза в COOLDOWN.
#
# Режим: SCOPED-автофикс. Система разрешений claude НЕ отключается — агенту выдан
# явный allowlist (--allowedTools). Всё вне списка (git push, произвольный bash,
# работа с БД, sqlite, curl, правка settings) автоматически запрещается движком.
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

# --- 1. Дешёвый grep реальных ошибок за 25ч (окно = период между суточными прогонами) ---
ERR=$(journalctl -u kleinanzeigen-bot --since "25 hours ago" --no-pager 2>/dev/null \
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
log "найдены ошибки (sig=$SIG) — запускаю claude autofix (scoped)"
RUNLOG="$LOGDIR/$(date '+%Y%m%d-%H%M%S')-$SIG.log"

# --- 3. Промпт с жёсткими рамками AGENTS.md ---
PROMPT=$(cat <<'EOF'
Ты автономный дежурный по проду kleinanzeigen-bot (/home/pg/kleinanzeigen-bot).
В логах journalctl -u kleinanzeigen-bot за последние ~25 часов найдены ошибки.
Задача: разобраться в первопричине и починить в рамках AGENTS.md.

Шаги:
1. Прочитай ошибки:
   journalctl -u kleinanzeigen-bot --since "26 hours ago" --no-pager | grep -iE "error|traceback|exception|critical|fatal|failed" | grep -viE "monitor_errors_job|next run at|executed successfully"
2. Найди первопричину в коде.
3. Если это простой, безопасный, ОДНОЗНАЧНЫЙ баг в коде — почини. Затем строго по порядку:
   - python3 -m pytest tests/ -q        → коммитить ТОЛЬКО если тесты зелёные
   - git add -A && git commit           → conventional prefix + русское описание
   - sudo systemctl restart kleinanzeigen-bot
   - sudo systemctl status kleinanzeigen-bot  → убедись что старт чистый
   - Уведоми оператора что ПОЧИНИЛ (что за баг + что сделал):
     scripts/notify.sh "✅ автофикс: <текст>"
   Если pytest красный после твоей правки — откати правку (git checkout -- <файл>),
   ничего не коммить, и эскалируй оператору через scripts/notify.sh.

ЖЁСТКИЕ ЗАПРЕТЫ (нарушать нельзя ни при каких условиях):
- НЕ менять send_mode, НЕ слать письма покупателям.
- НЕ делать DELETE/DROP/схема-миграций в БД.
- НЕ пушить в origin (push тебе технически запрещён — даже не пытайся).
- Если баг требует смены поведения отправки, миграции схемы, затрагивает деньги/клиентов,
  или причина неоднозначна / нет уверенного безопасного фикса — НИЧЕГО НЕ МЕНЯЙ.
  Вместо этого эскалируй оператору описание проблемы + предлагаемое решение:
  scripts/notify.sh "⚠️ нужна твоя реакция: <текст>"

Если ошибки транзиентные (сетевой блип IMAP/SMTP, разовый таймаут туннеля) и чинить в коде
нечего — ничего не делай и молчи (не уведомляй). Работай кратко и по делу.
EOF
)

cd "$REPO" || exit 1

# --- 4. Scoped allowlist: ровно то, что нужно для починки, и ничего больше ---
timeout 900 "$CLAUDE" -p "$PROMPT" \
  --model claude-opus-4-8 \
  --output-format json \
  --allowedTools \
      "Read" "Grep" "Glob" "Edit" "Write" \
      "Bash(journalctl:*)" \
      "Bash(python3 -m pytest:*)" \
      "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" \
      "Bash(git add:*)" "Bash(git commit:*)" "Bash(git checkout --:*)" \
      "Bash(sudo systemctl restart kleinanzeigen-bot)" \
      "Bash(sudo systemctl status kleinanzeigen-bot)" \
      "Bash(scripts/notify.sh:*)" \
  --disallowedTools \
      "Bash(git push:*)" "Bash(sqlite3:*)" "Bash(curl:*)" "Bash(rm:*)" \
      "WebFetch" "WebSearch" \
  > "$RUNLOG" 2>&1
rc=$?

# --- 5. Детект неудачи ---
# ВАЖНО (инцидент 2026-07-17): claude CLI отдаёт rc=0 даже когда прогон провалился
# (`"is_error":true`, например "Unable to connect to API (ENOTFOUND)"). Проверка только
# по rc проглатывала это молча — слой автофикса был мёртв 13 часов и никто не узнал.
# Поэтому смотрим И код возврата, И поле is_error в JSON-результате.
FAILED=0
[ $rc -ne 0 ] && FAILED=1
grep -q '"is_error":true' "$RUNLOG" 2>/dev/null && FAILED=1

if [ "$FAILED" = "1" ]; then
  REASON=$(grep -oE '"result":"[^"]{0,200}' "$RUNLOG" 2>/dev/null | head -1 | cut -c11-)
  [ -z "$REASON" ] && REASON="rc=$rc"
  log "ПРОГОН ПРОВАЛИЛСЯ: $REASON (лог: $RUNLOG)"
  # Сигнатуру НЕ фиксируем: анализа не было, пусть следующий тик попробует снова,
  # иначе неудачный прогон блокировал бы повтор на все 6ч cooldown-а.
  rm -f "$SIGFILE"
  "$REPO/scripts/notify.sh" \
    "🟥 <b>autofix</b>: прогон провалился — ${REASON}. Ошибки в логах бота остались без разбора, нужна ручная проверка. Лог: $RUNLOG" \
    >/dev/null 2>&1 || true
  exit 0
fi

# Успех — фиксируем сигнатуру, чтобы не долбить тем же багом на каждом суточном прогоне.
printf '%s\n%s\n' "$SIG" "$now" > "$SIGFILE"
log "claude отработал успешно, лог: $RUNLOG"
exit 0
