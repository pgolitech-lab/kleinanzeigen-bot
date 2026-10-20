# ops — туннель, watchdog'и, юниты, бэкап

Ветка `opt/ops`. Systemd вживую не менялся, сервисы не перезапускались — всё применяет оркестратор по разделу «Порядок применения».

## 1. Туннель без коммитов: адрес API в URL Mini App

**Было.** Каждая ротация quick-tunnel URL: `update_tunnel_url.sh` правил `web-app/js/api.js`, менял `?v=` во всех файлах,
коммитил и пушил в main, при расхождении с remote делал `git reset --hard` в проде (мог стереть чужие правки), после чего
GitHub Pages пересобирался 60–90 с. `bot_health.sh` раз в 3 мин скачивал `api.js` с Pages, чтобы сверить URL.

**Стало.**

```
cloudflared ──(метрики /quicktunnel, запасной путь — журнал)──▶ update_tunnel_url.sh
   │                                                              │ атомарно пишет
   │                                                              ▼
   │                                ~/.local/state/kleinanzeigen-bot/tunnel_url
   │                                                              │ читает на каждый вызов
   │                                                              ▼
   │                         telegram_bot._ma_deep_link() → …/web-app/?tgWebAppStartParam=…&api=<url>
   │                                                              │
   │                         set_menu_button() (из скрипта) ──────┘  кнопка меню — всегда свежая
   ▼
Mini App: api.js берёт ?api= (строго ^https://[a-z0-9-]+\.trycloudflare\.com$) → sessionStorage → запасная константа
```

- Git, Pages и `?v=` в ротации больше не участвуют. Простоя на пересборку Pages нет: новый адрес в кнопке меню через секунды.
- `~/.local/state/kleinanzeigen-bot/menu_synced_url` — адрес, под который кнопка меню успешно обновлена. `bot_health.sh`
  сверяет его с живым адресом cloudflared; расхождение дольше 2 мин → повтор синка, дольше 10 мин и повтор падает → алерт.
- Ограничение: кнопки в **старых** уведомлениях держат прежний `?api=`; после ротации они откроют MA с мёртвым адресом —
  фронт покажет «Адрес сервера сменился, откройте кнопкой меню». Кнопка меню всегда актуальна.
- Живой адрес берётся у метрик cloudflared (`http://127.0.0.1:<порт>/quicktunnel`; порт случайный, читается из журнала
  «Starting metrics server on …»). Там же `/ready` → `readyConnections` — пишется в лог watchdog'а для диагностики.

Интерфейсы, переданные оркестратору:

- **api** (`modules/telegram_bot.py`): `TUNNEL_URL_FILE` (env `KAB_TUNNEL_URL_FILE`), `tunnel_url() -> Optional[str]`
  (читать файл на каждый вызов, `fullmatch` регэкспа, без исключений), `_ma_deep_link` добавляет `&api=<quote(url)>`,
  `set_menu_button() -> int` (число чатов, принявших кнопку), `/health` += `polling_paused: bool`.
- **webapp** (`web-app/js/api.js`): `resolveApiBase()` — `?api=` → `sessionStorage["kab_api_base"]` → `FALLBACK_API_BASE`;
  каждое обращение к sessionStorage в try/catch; понятный текст при сетевой ошибке.

`update_tunnel_url.sh` сам проверяет, что бэкенд новый: если `set_menu_button()` вернула не число — выходит с ошибкой
«бэкенд без поддержки ?api=». Поэтому применять скрипт только после выката api.

### Альтернатива: named tunnel со своим доменом (решение владельца)

| | quick tunnel + `?api=` (сделано) | named tunnel (`api.<домен>`) |
|---|---|---|
| Адрес | меняется при каждом рестарте cloudflared | постоянный |
| Старые кнопки в уведомлениях | ломаются после ротации | работают всегда |
| Обвязка | `update_tunnel_url.sh`, `tunnel-url-sync`, сверка в `bot_health.sh` | не нужна; `api.js` — обычная константа |
| Гарантии Cloudflare | quick tunnel — «для тестов», без SLA, лимит ~200 одновременных запросов | штатный продукт, можно Cloudflare Access/WAF перед API |
| Цена | 0 | домен ~10–15 €/год (если своего на Cloudflare нет); аккаунт Cloudflare бесплатный |
| Разовая работа | — | ~30 мин: владелец логинится `cloudflared tunnel login` (браузер), `tunnel create`, CNAME, `config.yml`, юнит |
| Секрет на NUC | нет | `~/.cloudflared/<id>.json` (credentials туннеля) — в `.gitignore` уже `credentials*.json` |

Рекомендация: named tunnel надёжнее и проще в эксплуатации, если владельцу не жалко домена. `?api=` работает уже
сейчас и без действий владельца; при переходе на named tunnel регэксп в боте/фронте меняется на свой домен.

## 2. Watchdog'и

`bot_health.sh` (каждые 3 мин, pg):
- `/health` не отвечает → рестарт только если **два тика подряд** (3 попытки по 10 с в тике) и сервис active > 2 мин.
  «Подряд» = предыдущий плохой тик не старше 10 мин; мусор в state-файлах читается как 0, а не роняет скрипт.
- Признак жизни — `poll_age_sec` из `/health` (int; float приводится): ≥ 15 мин (`POLL_STALE`) два тика подряд при
  живом DNS → рестарт; DNS хоста мёртв → только алерт. `polling_paused: true` → проверка пропускается. `null` при
  `poll_tracking: true` → проблема, только если сервис active ≥ 15 мин. `poll_tracking: false` или поля нет — запасной
  путь (скан журнала на IMAP-ошибки); `/health` 200, но не JSON два тика → отдельный алерт «контракт /health нарушен».
- Watchdog-рестарт не чаще раза в 30 мин, дальше только алерт. `systemctl` проверяется: несостоявшийся рестарт —
  свой алерт, а не «сделал рестарт».
- Сервис не active → `reset-failed` + `restart`, проверка через 30 с (а не 8 — чтобы crash-loop не выглядел «восстановлен»).
  Рост `NRestarts` на ≥ 3 за тик → алерт «crash-loop».
- **Алерты с подтверждением доставки**: `notify.sh` возвращает 1, если Telegram не принял сообщение ни в один чат
  (раньше `telegram_bot.notify` при DM-fanout глотал ошибки и возвращал `{}` — протухший токен делал все алерты
  невидимыми при rc=0). Отметка дедупа пишется только после доставки; недоставленный алерт → `logger -p user.crit`.
- Скачивания `api.js` с Pages больше нет; блок 4 сверяет живой URL cloudflared с `menu_synced_url`. cloudflared active,
  а адрес не определяется ~10 мин → алерт.
- OAuth claude CLI: текст алерта про реальные последствия (встают все LLM-функции бота); нечитаемый
  `.credentials.json` → алерт раз в сутки (раньше только строка в лог).
- Блок 6: если `kab-backup.timer` включён, а свежего снимка в `~/backups/kab/daily/` нет > 36 ч → алерт.
- Исправлен старый баг: `local key="$1" f="$STATE/alert_$key"` — `$key` ещё пуст, все алерты писались в один файл
  `alert_`, дедуп путал разные проблемы, а `clear_alert` его не чистил.
- `--dry-run`: проверки на **копии** state (боевые счётчики и дедуп не трогаются), рестарты/Telegram/синк туннеля —
  только в лог. `BOT_HEALTH_URL`, `BOT_HEALTH_STATE` — для теста.
- Осознанно: при проблеме с почтой/health скрипт выходит, не доходя до блоков 4–6 (следующий тик их проверит).

`tunnel_watchdog.sh` (каждые 5 мин, root):
- cloudflared не active → `systemctl start` + алерт. **Если cloudflared останавливают намеренно — сначала
  `sudo systemctl stop tunnel-watchdog.timer`**, иначе через ≤5 мин он будет запущен снова.
- проверяет живой адрес cloudflared (а не константу из `api.js`);
- нет интернета (не открывается cloudflare.com) → не рестартит (новый туннель всё равно не поднимется);
- рестарт cloudflared только после **двух тиков подряд** (≤15 мин между ними) с провалом 3 проб — рестарт меняет
  адрес и ломает старые кнопки; не больше 3 рестартов в час, дальше алерт раз в час и ожидание;
- каждый старт/рестарт — алерт в Telegram (`runuser -u pg -- notify.sh`);
- state в `/var/lib/kab-tunnel-watchdog` (`StateDirectory=`); недоступен для записи → exit 1, а не «тихий» счётчик;
  `--dry-run` (на копии state), `--url URL`.
- Не сделано: проба `/api/ma/*` через туннель (сейчас только `/health`). Риск: юнит от root исполняет скрипты из
  `/home/pg/kleinanzeigen-bot/scripts` — кто может писать туда, получает root (как и раньше; при желании —
  копировать скрипт в `/usr/local/lib/kab/` при установке).

## 3. Юниты

- `kleinanzeigen-bot.service`: `Environment=HOME`, `PATH` = `/home/pg/.local/bin` (Claude CLI) + дефолтный PATH systemd, `MemoryHigh=2800M`,
  `MemoryMax=3G` (бот + Chromium Playwright + параллельные claude CLI; пик за неделю 186 МБ, RAM NUC 7,6 ГБ).
  `StartLimit*`, `TimeoutStopSec=30`, `KillMode=mixed`, `OOMPolicy=continue` уже были.
- В репо добавлены копии прод-юнитов, которых там не было: `tunnel-url-sync.service` (+`TimeoutStartSec=360`),
  `tunnel-watchdog.service` (+`StateDirectory`, `TimeoutStartSec`), `tunnel-watchdog.timer`.
- `bot-autofix.timer` в репо приведён к проду (ежедневно 05:00, было «каждые 30 мин» — устаревшая копия).
- `bot-health.service`: `TimeoutStartSec=600`.

## 4. Бэкап БД (решение владельца: OneDrive)

`scripts/backup.sh` + `scripts/kab-backup.{service,timer}` (ежедневно 03:30, `Persistent=true` — догоняет после выключения):

1. `sqlite3 .backup` — онлайн-снимок, согласованный с WAL (бот не останавливается).
2. `PRAGMA integrity_check` = ok и `messages` не пустая.
3. zstd -19 (5,7 МБ → ~1 МБ) + `zstd -t`, права 600 (в БД пароли Gmail, токен Telegram, переписка покупателей).
4. `~/backups/kab/daily/kleinanzeigen-YYYYMMDD.db.zst` — 14 последних дней; `~/backups/kab/weekly/` — 8 последних (новый
   недельный, если последнему > 6 дней). Имя = дата: повторный запуск в тот же день перезаписывает снимок, а не вытесняет
   историю. Ротация по количеству файлов, не по возрасту: после недели выключенного NUC старые снимки не пропадут разом.
5. `rclone copy` → `onedrive:kleinanzeigen-bot-backup/{daily,weekly}/` (только добавление) + проверка, что сегодняшний
   файл виден в облаке; в облаке старше 21 дн. (daily) / 70 дн. (weekly) удаляется `rclone delete --min-age`.
   Потеря локальной папки облачные копии не стирает.

Любой провал шага → exit 1 (юнит `failed`) + сообщение в Telegram через `scripts/notify.sh` (`telegram_bot.notify`);
если и Telegram не ответил — `logger -p user.crit`. `--dry-run`: снимок и проверка во временной папке, доступность remote
(`rclone lsjson`), без записи. Env для теста: `KAB_DB`, `KAB_BACKUP_DIR`, `KAB_BACKUP_REMOTE` (пусто = без облака;
можно локальный путь).

Проверено: `--dry-run` на проде (снимок ok, `onedrive:` доступен); полный прогон в /tmp с локальным remote — ротация
16→14, недельный создаётся/не дублируется, файл в «облаке»; несуществующий remote → exit 1.

**rclone на NUC уже установлен (v1.60.1) и remote `onedrive:` настроен** (personal, 1 ТБ свободно) — одноразовая
настройка не нужна. Инструкция ниже — если токен протухнет (`rclone lsd onedrive:` → `invalid_grant` / `token expired`;
у личного OneDrive refresh-токен живёт ~90 дней без использования, ежедневный бэкап его продлевает).

### Переавторизация OneDrive на headless NUC

На своём компьютере (Windows/macOS/Linux с браузером) поставить rclone той же или новее версии (https://rclone.org/downloads/).

1. На NUC: `rclone config` → `e` (edit existing remote) → `onedrive` (или `n` → имя `onedrive` → тип `onedrive`, если
   создаётся заново).
2. `client_id`, `client_secret` — пусто (Enter); region — `1` (Microsoft Cloud Global).
3. «Edit advanced config?» — `n`.
4. «Use web browser to automatically authenticate rclone with remote?» — **`n`** (на NUC нет браузера).
5. rclone напечатает команду: `rclone authorize "onedrive"` — выполнить её **на своём компьютере**, войти в браузере
   в тот Microsoft-аккаунт, где должны лежать бэкапы, разрешить доступ.
6. Компьютер выведет JSON-токен между строками `Paste the following into your remote machine --->` и
   `<---End paste` — скопировать его целиком и вставить в NUC в ответ на `config_token>`.
7. Тип диска — `onedrive` (личный OneDrive) → выбрать предложенный drive → «Is that okay?» `y` → `y` (keep) → `q`.
8. Проверка на NUC: `rclone lsd onedrive:` и `~/kleinanzeigen-bot/scripts/backup.sh --dry-run` → «onedrive:… доступен».

Токен хранится в `~/.config/rclone/rclone.conf` (владелец pg, права 600) — это доступ ко всему OneDrive; в репо не попадает.
**Вопрос владельцу — шифрование.** Снимок сжимается, но не шифруется: при утечке OneDrive-аккаунта утекут App
Password'ы всех 5 Gmail, токен бота и переписка покупателей. Рекомендую шифровать: rclone `crypt` поверх OneDrive
(ниже) или `age` перед загрузкой. Сейчас по умолчанию — без шифрования, как решено. Как включить: шифровать бэкапы поверх OneDrive remote'ом `crypt` (`rclone config` → `n` → тип
`crypt` → remote `onedrive:kleinanzeigen-bot-backup-crypt`) и указать его в `KAB_BACKUP_REMOTE` в юните. Пароль crypt
тогда нужно хранить отдельно от NUC — без него бэкап не расшифровать.

### Восстановление из бэкапа

```bash
# 0. Выбрать снимок: локально или скачать из облака
ls -lt ~/backups/kab/daily/ ~/backups/kab/weekly/ | head
rclone lsl onedrive:kleinanzeigen-bot-backup/daily/ | sort -k2,3 | tail      # если NUC/диск потерян
rclone copy onedrive:kleinanzeigen-bot-backup/daily/kleinanzeigen-YYYYMMDD.db.zst /tmp/restore/

# 1. Распаковать и проверить ДО остановки бота
zstd -d /tmp/restore/kleinanzeigen-YYYYMMDD.db.zst -o /tmp/restore/kleinanzeigen.db
sqlite3 /tmp/restore/kleinanzeigen.db "PRAGMA integrity_check; SELECT COUNT(*), MAX(created_at) FROM messages;"

# 2. Остановить бота, отложить текущую БД (вместе с -wal/-shm), подложить снимок
sudo systemctl stop kleinanzeigen-bot
cd ~/kleinanzeigen-bot
mkdir -p ~/backups/pre-restore && mv kleinanzeigen.db* ~/backups/pre-restore/
install -m 600 /tmp/restore/kleinanzeigen.db kleinanzeigen.db

# 3. Запустить и проверить
sudo systemctl start kleinanzeigen-bot
journalctl -u kleinanzeigen-bot -n 30 --no-pager
curl -s 127.0.0.1:8080/health
```

После восстановления: письма, пришедшие после снимка, бот подберёт сам — orphan-recovery снимает `\Seen` с писем за
2 дня, которых нет в БД (`gmail.find_orphan_seen_uids`). Ответы, отправленные после снимка, в БД не появятся —
сверить с «Отправленными» в Gmail. Перед восстановлением проверить `send_mode` в снимке
(`sqlite3 … "SELECT value FROM settings WHERE key='send_mode'"`) — он вернётся к значению на момент снимка.
`rm -rf /tmp/restore` в конце: там незашифрованная БД.

**Для db**: `modules/backup.py` (Drive) и джоб `drive_backup` в `scheduler.py` можно удалить — бэкап делает systemd-timer
вне процесса бота (не зависит от того, жив ли бот). Вызывать `backup.sh` из бота не нужно.

## Порядок применения на проде

1. **webapp**: фронт с `resolveApiBase()` (без `?api=` работает как раньше) → push в main, Pages пересобрался.
2. **api**: `tunnel_url()`, `?api=` в `_ma_deep_link`, `set_menu_button() -> int`, `polling_paused` в `/health` → слить,
   `sudo systemctl restart kleinanzeigen-bot`, `curl -s 127.0.0.1:8080/health` (видны `poll_age_sec`, `polling_paused`).
3. **ops** (эта ветка) слить в main, в проде обновить рабочее дерево. Затем:
   ```bash
   ~/kleinanzeigen-bot/scripts/update_tunnel_url.sh --dry-run
   ~/kleinanzeigen-bot/scripts/update_tunnel_url.sh          # пишет tunnel_url, обновляет кнопку меню
   cat ~/.local/state/kleinanzeigen-bot/menu_synced_url       # = живой URL
   ~/kleinanzeigen-bot/scripts/bot_health.sh --dry-run       # ok, poll_age_sec=<число>
   sudo ~/kleinanzeigen-bot/scripts/tunnel_watchdog.sh --dry-run
   ~/kleinanzeigen-bot/scripts/backup.sh --dry-run
   cd ~/kleinanzeigen-bot/scripts
   sudo cp tunnel-url-sync.service tunnel-watchdog.service tunnel-watchdog.timer \
           bot-health.service bot-autofix.service bot-autofix.timer \
           kab-backup.service kab-backup.timer /etc/systemd/system/
   sudo cp ../kleinanzeigen-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now kab-backup.timer
   sudo systemctl start kab-backup.service && ls -l ~/backups/kab/daily/ && rclone lsl onedrive:kleinanzeigen-bot-backup/daily/
   sudo systemctl restart kleinanzeigen-bot     # подхватить PATH и лимиты памяти
   systemctl show kleinanzeigen-bot -p MemoryHigh,MemoryMax,Environment
   journalctl -u kleinanzeigen-bot -n 30 --no-pager
   ```
   Проверка в Telegram: открыть MA кнопкой меню — URL содержит `&api=https://….trycloudflare.com`, экраны грузятся.
   Ротацию проверить `sudo systemctl restart cloudflared` → через ~1 мин `journalctl -t tunnel-url-sync -n 5` = OK,
   MA по кнопке меню открывается с новым адресом (Pages не пересобирался, коммита нет).
4. После недели без проблем — webapp может удалить зависимость от `FALLBACK_API_BASE` (оставить как есть тоже безопасно).

**Откат**: `git revert` коммита ops, `sudo cp` старых юнитов из `git show <старый>:…`, `daemon-reload`. Старый
`update_tunnel_url.sh` снова начнёт коммитить URL — фронт с `?api=` с этим совместим.
