#!/usr/bin/env bash
# backup.sh — ночной бэкап живой SQLite-БД бота: локальная ротация + копия в OneDrive (rclone).
# Заменяет Drive-бэкап через сервисный аккаунт (у SA нет квоты — с 23.06 бэкапа не было,
# а run_backup каждый день тихо писал «не настроен»).
#
#   1. sqlite3 .backup — онлайн-снимок, согласованный с WAL; бот останавливать не нужно.
#   2. PRAGMA integrity_check + наличие таблицы messages — битый бэкап хуже отсутствующего.
#   3. zstd, права 600: в БД пароли Gmail, токен Telegram и переписка покупателей.
#   4. Ротация в ~/backups/kab/: daily/ — 14 последних дней, weekly/ — 8 последних (новый
#      недельный, если последнему больше 6 дней). Имя = дата (kleinanzeigen-YYYYMMDD.db.zst),
#      считаем по числу файлов, а не по mtime:
#      если NUC неделю был выключен, старые снимки не исчезнут разом.
#   5. Шифрование age публичным ключом (~/.config/kab-backup/age.pub) + проверка расшифровкой;
#      rclone copy .db.zst.age в onedrive:kleinanzeigen-bot-backup/{daily,weekly}/ — только
#      добавление; старое на той стороне чистится по возрасту (--min-age), поэтому даже
#      потеря локальной папки не сотрёт облачные копии.
# Любой провал шага → exit 1 (юнит в failed) + уведомление в Telegram (scripts/notify.sh).
# Ключ --dry-run: снимок и проверка во временной папке; ротация и rclone — только «что сделал бы».
set -uo pipefail

SRC="${KAB_DB:-/home/pg/kleinanzeigen-bot/kleinanzeigen.db}"
DEST="${KAB_BACKUP_DIR:-/home/pg/backups/kab}"
REMOTE="${KAB_BACKUP_REMOTE-onedrive:kleinanzeigen-bot-backup}"   # пусто = без облака
KEEP_DAILY="${KAB_BACKUP_KEEP_DAILY:-14}"
KEEP_WEEKLY="${KAB_BACKUP_KEEP_WEEKLY:-8}"
# В облаке держим чуть дольше локального (запас на выключенный NUC)
REMOTE_DAILY_AGE="${KAB_BACKUP_REMOTE_DAILY_AGE:-21d}"
REMOTE_WEEKLY_AGE="${KAB_BACKUP_REMOTE_WEEKLY_AGE:-70d}"
# Шифрование для облака (age): публичным ключом шифруем, приватным — проверяем расшифровку.
# В облако НИКОГДА не уходит незашифрованный файл: нет ключа/age → бэкап в облако падает.
AGE_RECIPIENTS="${KAB_BACKUP_AGE_RECIPIENTS:-/home/pg/.config/kab-backup/age.pub}"
AGE_IDENTITY="${KAB_BACKUP_AGE_KEY:-/home/pg/.config/kab-backup/age.key}"
NOTIFY="$(dirname "$(readlink -f "$0")")/notify.sh"
# Метка последнего ПОЛНОГО успеха (снимок + облако): строка 1 — unix-время, 2 — имя файла,
# 3 — remote. Читают /health (поле backup_age_sec, api) и bot_health.sh (алерт > 26 ч).
MARKER="${KAB_BACKUP_MARKER:-/home/pg/.local/state/kleinanzeigen-bot/last_backup_ok}"

DRY=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ;;
  *) echo "неизвестный аргумент: $1 (есть только --dry-run)" >&2; exit 2 ;;
esac

log() { logger -t kab-backup -- "$*" 2>/dev/null; echo "$*"; }
fail() {
  logger -t kab-backup -p user.err -- "ERROR: $*" 2>/dev/null
  echo "ERROR: $*" >&2
  if [ "$DRY" = 0 ]; then
    if ! "$NOTIFY" "🟥 <b>бэкап БД</b>: $(printf '%s' "$*" | sed 's/[<>&]//g')" >/dev/null 2>&1; then
      logger -t kab-backup -p user.crit -- "алерт о провале бэкапа в Telegram НЕ ушёл" 2>/dev/null
      echo "ERROR: и алерт в Telegram не ушёл" >&2
    fi
  fi
  exit 1
}

umask 077
[ -r "$SRC" ] || fail "нет БД $SRC"
for bin in sqlite3 zstd; do command -v "$bin" >/dev/null || fail "не установлен $bin"; done
[ -n "$REMOTE" ] || log "WARN: KAB_BACKUP_REMOTE пуст — копии в облако не будет, только локально"
if [ -n "$REMOTE" ]; then
  command -v rclone >/dev/null || fail "не установлен rclone (sudo apt install rclone)"
  command -v age >/dev/null || fail "не установлен age (sudo apt install age) — без шифрования в облако не шлю"
  grep -qE '^age1[0-9a-z]+$' "$AGE_RECIPIENTS" 2>/dev/null \
    || fail "нет публичного ключа age в $AGE_RECIPIENTS — без шифрования в облако не шлю"
  [ -r "$AGE_IDENTITY" ] || fail "нет приватного ключа age $AGE_IDENTITY — не могу проверить, что бэкап расшифровывается"
fi

# Шифрует $1 → $2 и проверяет: расшифровка приватным ключом даёт целый zstd-архив
# с тем же содержимым. Любая ошибка — fail (алерт), в облако ничего не уходит.
encrypt_verify() {
  local src="$1" dst="$2"
  age -e -R "$AGE_RECIPIENTS" -o "$dst" "$src" 2>"$WORK/age_err" \
    || fail "шифрование age упало: $(head -c 300 "$WORK/age_err")"
  [ -s "$dst" ] || fail "шифрование age дало пустой файл"
  if ! age -d -i "$AGE_IDENTITY" "$dst" 2>"$WORK/age_err" | cmp -s - "$src"; then
    fail "зашифрованный бэкап не расшифровывается в исходный: $(head -c 300 "$WORK/age_err")"
  fi
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/kab-backup.XXXXXX") || fail "mktemp не удался"
trap 'rm -rf "$WORK"' EXIT

# Одно имя на день: повторный запуск в тот же день перезаписывает снимок, а не вытесняет
# из ротации «14 последних» старые дни.
NAME="kleinanzeigen-$(date +%Y%m%d).db"
SNAP="$WORK/$NAME"

# 1. Онлайн-снимок; .timeout — подождать, если бот держит write-лок
if ! sqlite3 "$SRC" ".timeout 30000" ".backup '$SNAP'" 2>"$WORK/err"; then
  fail "sqlite3 .backup упал: $(head -c 300 "$WORK/err")"
fi

# 2. Целостность
ic=$(sqlite3 "$SNAP" "PRAGMA integrity_check;" 2>&1 | head -5 | tr '\n' ' ')
[ "$ic" = "ok " ] || fail "integrity_check снимка: $ic"
rows=$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM messages;" 2>&1)
[[ "$rows" =~ ^[0-9]+$ ]] && [ "$rows" -gt 0 ] || fail "снимок без данных в messages: $rows"

# 3. Сжатие + проверка, что архив читается
zstd -q -19 --rm "$SNAP" -o "$SNAP.zst" || fail "zstd упал"
zstd -q -t "$SNAP.zst" || fail "zstd -t: архив снимка битый"
size=$(du -h "$SNAP.zst" | cut -f1)

# Недельный — если в weekly/ нет снимка моложе 6 дней
need_weekly=1
if [ -d "$DEST/weekly" ] && [ -n "$(find "$DEST/weekly" -maxdepth 1 -name 'kleinanzeigen-*.db.zst' -mtime -6 -print -quit)" ]; then
  need_weekly=0
fi

if [ "$DRY" = 1 ]; then
  log "[dry-run] снимок ок: messages=$rows, $size → $DEST/daily/$NAME.zst$([ $need_weekly = 1 ] && echo " (+ weekly/)")"
  if [ -n "$REMOTE" ]; then
    encrypt_verify "$SNAP.zst" "$WORK/$NAME.zst.age"
    log "[dry-run] шифрование age ок ($(du -h "$WORK/$NAME.zst.age" | cut -f1)), расшифровка совпала"
    if timeout 120 rclone lsjson "$REMOTE" --max-depth 1 >/dev/null 2>"$WORK/rclone_err" \
       || grep -qi "directory not found" "$WORK/rclone_err"; then
      log "[dry-run] rclone: $REMOTE доступен, скопировал бы туда"
    else
      fail "rclone: $REMOTE недоступен: $(head -c 300 "$WORK/rclone_err")"
    fi
  fi
  exit 0
fi

mkdir -p "$DEST/daily" "$DEST/weekly" || fail "не могу создать $DEST"
chmod 700 "$DEST" "$DEST/daily" "$DEST/weekly"
mv -f "$SNAP.zst" "$DEST/daily/" || fail "не могу записать в $DEST/daily"
OUT="$DEST/daily/$NAME.zst"
[ -s "$OUT" ] || fail "после записи $OUT пустой или отсутствует"
if [ "$need_weekly" = 1 ]; then
  cp -p "$OUT" "$DEST/weekly/" || fail "не могу записать недельный снимок в $DEST/weekly"
fi
log "OK: $OUT ($size, messages=$rows)$([ $need_weekly = 1 ] && echo ", + недельный")"

# 4. Ротация по количеству: оставить N самых новых (имена сортируются по дате)
prune() {
  local dir="$1" keep="$2"
  find "$dir" -maxdepth 1 -name 'kleinanzeigen-*.db.zst' -printf '%f\n' | sort -r \
    | tail -n +"$((keep + 1))" | while read -r f; do
        if rm -f -- "$dir/$f"; then log "удалён старый: $dir/$f"; else log "WARN: не удалить $dir/$f"; fi
      done
}
prune "$DEST/daily" "$KEEP_DAILY"
prune "$DEST/weekly" "$KEEP_WEEKLY"

# 5. Облако — только зашифрованное (.db.zst.age). Локальные снимки остаются .db.zst:
#    на NUC и так лежит открытая БД, шифрование локальных копий защиты не добавит.
PURGE_ERR=""
if [ -n "$REMOTE" ]; then
  ENC="$WORK/enc"
  mkdir -p "$ENC/daily" "$ENC/weekly" || fail "mktemp для шифрования не удался"
  encrypt_verify "$OUT" "$ENC/daily/$NAME.zst.age"
  # Недельный: самый свежий локальный weekly, если его зашифрованной копии в облаке ещё нет
  # (так облако догоняет и после включения шифрования, и после сбоя прошлой выгрузки).
  WEEKLY_LAST=$(find "$DEST/weekly" -maxdepth 1 -name 'kleinanzeigen-*.db.zst' -printf '%f\n' | sort -r | head -1)
  if [ -n "$WEEKLY_LAST" ] && ! timeout 120 rclone lsf "$REMOTE/weekly" --include "$WEEKLY_LAST.age" 2>/dev/null \
      | grep -qx "$WEEKLY_LAST.age"; then
    if [ "$WEEKLY_LAST.age" = "$NAME.zst.age" ]; then
      cp -p "$ENC/daily/$NAME.zst.age" "$ENC/weekly/"
    else
      encrypt_verify "$DEST/weekly/$WEEKLY_LAST" "$ENC/weekly/$WEEKLY_LAST.age"
    fi
  fi
  for kind in daily weekly; do
    [ -n "$(ls -A "$ENC/$kind")" ] || continue
    if ! timeout 900 rclone copy "$ENC/$kind" "$REMOTE/$kind" --include 'kleinanzeigen-*.db.zst.age' \
        --retries 3 --low-level-retries 10 2>"$WORK/rclone_err"; then
      fail "локально сохранён $OUT, но rclone copy в $REMOTE/$kind упал: $(tail -c 300 "$WORK/rclone_err")"
    fi
  done
  # Проверка, что сегодняшний файл реально лежит в облаке (rclone copy мог «успеть» не всё)
  if ! timeout 120 rclone lsf "$REMOTE/daily" --include "$NAME.zst.age" 2>/dev/null | grep -qx "$NAME.zst.age"; then
    fail "rclone copy вернул успех, но $NAME.zst.age в $REMOTE/daily не видно"
  fi
  # Незашифрованные копии в облаке (бэкапы до включения шифрования) — удалить
  if ! timeout 300 rclone delete "$REMOTE" --include 'kleinanzeigen-*.db.zst' 2>"$WORK/rclone_err"; then
    PURGE_ERR="не удалось удалить незашифрованные копии в $REMOTE: $(tail -c 200 "$WORK/rclone_err")"
  elif timeout 120 rclone lsf -R --files-only "$REMOTE" --include 'kleinanzeigen-*.db.zst' 2>/dev/null | grep -q .; then
    PURGE_ERR="в $REMOTE остались незашифрованные копии *.db.zst"
  fi
  timeout 300 rclone delete "$REMOTE/daily" --min-age "$REMOTE_DAILY_AGE" --include 'kleinanzeigen-*.db.zst.age' 2>"$WORK/rclone_err" \
    || log "WARN: чистка старых в $REMOTE/daily не удалась: $(tail -c 200 "$WORK/rclone_err")"
  timeout 300 rclone delete "$REMOTE/weekly" --min-age "$REMOTE_WEEKLY_AGE" --include 'kleinanzeigen-*.db.zst.age' 2>"$WORK/rclone_err" \
    || log "WARN: чистка старых в $REMOTE/weekly не удалась: $(tail -c 200 "$WORK/rclone_err")"
  log "OK: в облаке $REMOTE/daily/$NAME.zst.age (зашифрован age)"
fi

# 6. Метка успеха — атомарно (tmp + mv), чтобы читатель не увидел полстроки
mkdir -p "$(dirname "$MARKER")" || fail "не создать каталог метки $MARKER"
tmp=$(mktemp "$MARKER.XXXXXX") || fail "mktemp для метки не удался"
if ! { printf '%s\n%s\n%s\n' "$(date +%s)" "$NAME.zst" "${REMOTE:-local-only}" > "$tmp" \
       && chmod 644 "$tmp" && mv -f "$tmp" "$MARKER"; }; then
  rm -f "$tmp"
  fail "бэкап сделан, но метка $MARKER не записана — /health будет считать бэкап старым"
fi
# Бэкап есть (метка записана), но открытые копии в облаке — повод для алерта
[ -z "$PURGE_ERR" ] || fail "$PURGE_ERR"
exit 0
