# Оптимизация kleinanzeigen-bot — план и правила параллельных сессий (2026-10-20)

Оркестратор — сессия Claude Code на caramba (не правит код модулей, раздаёт задачи, ревьюит, сливает, перезапускает).
Владелец: проект работает плохо и нестабильно, много лишнего → разбить на модули, каждый ведёт своя сессия.

## Уже сделано (main)
- `260c903` LLM только через подписку Claude (Claude Code CLI, `modules/llm.py`), без Anthropic API. cost_usd = 0.
- `db482fa` через туннель cloudflared наружу только `/api/ma/*` и `/health` (остальное 404).

## Факты с прода (сутки до 20.10)
- `poll_all_accounts` (каждые 15 с) пропущен 2153 раза из ~5760 (37 %) — проход длиннее 15 с.
- IMAP таймауты `fetch_new` / `find_orphan_seen_uids` ≈ 24/сутки. ~14k строк лога/сутки, 75 % — шум APScheduler.
- LLM: 1–10 генераций в день. Через подписку: Haiku ≈ 3–4 с, Sonnet ≈ 3–10 с (старт CLI ≈ 2 с).

## Правила для всех сессий
1. **Свои файлы — строго.** Нужна правка чужого файла — написать оркестратору, не править.
2. Работа — в своём git worktree и ветке `opt/<модуль>` от свежего `main` (`git worktree add ~/kab-<модуль> -b opt/<модуль> main`).
   В рабочую папку прода `~/kleinanzeigen-bot` не писать, `main` не пушить, сервис не перезапускать — это делает оркестратор.
3. Прод-безопасность (AGENTS.md): режим отправки не трогать, реальных писем не слать, БД прода только читать
   (для экспериментов — копия в /tmp). Схема БД меняется только миграцией в `database.py` (идемпотентной).
4. Перед сдачей: `python3 -m pytest -q` зелёный (в worktree без БД — тесты сами создают временную), агенты
   `reviewer` и `silent-failure-hunter` из `.claude/agents/` (лежат в основной папке, путь `~/kleinanzeigen-bot/.claude/agents/`) — по своему диффу.
5. Коммиты маленькие, conventional-префикс + русское описание. Сдача — сообщение оркестратору: ветка, что сделано,
   что проверено, что осталось, нужен ли рестарт / миграция.
6. Поведение для оператора не урезать без вопроса. Удаление мёртвого кода — да, после grep-проверки, что не используется.

## Модули

### 1. mail — приём почты и конвейер входящих
Файлы: `modules/gmail.py`, `modules/incoming.py`, `modules/parser.py`, `modules/outgoing.py` (SMTP), в `scheduler.py` — джобы опроса
(`poll_all_accounts`, orphan-скан, `drain_all_deferred`, `send_approved_replies`).
- orphan-скан (`find_orphan_seen_uids`) — отдельный джоб раз в 15–30 мин + при старте, не каждый проход.
- IMAP только UID-командами (`imap.uid("SEARCH"|"FETCH"|"STORE")`) — сейчас mark_seen по номерам в новом соединении может пометить чужое письмо.
- пакетный FETCH/STORE, одна IMAP-сессия на аккаунт за проход; `known_message_ids_since` — один раз на проход.
- SMTP `timeout=30`; общий бюджет времени прохода; развести получение и обработку (очередь), чтобы LLM/Playwright/SMTP не держали опрос.
- Playwright `parse_ad` не запускать для ответа в известном треде (брать из БД / ad_briefs); один браузер на проход, а не на письмо.
- `_autopilot_dispatch`: учитывать `config.autopilot_shadow_mode()` и `autopilot_message_cap()` (сейчас игнор, лимит 20 захардкожен),
  проверять floor и язык через `guardrails.floor_violation` / `outgoing_language_ok` до отправки; при ошибке LLM — `send_for_review`, а не return.
- письма в статусе `sending` после падения — в `error_send_failed` при старте (без переотправки).
- классификатор Haiku — после бесплатных проверок (max-age, продано, from, нет ссылки).
- мёртвое: `outgoing.send_followup_ping` (NameError ad_brief), дубли `_row_get`.

### 2. llm — ответы, автопилот, брифы
Файлы: `modules/claude.py`, `modules/llm.py`, `modules/claude_utils.py`, `modules/ad_brief.py`, `modules/claude_scout.py`,
`modules/drafts.py`, `modules/guardrails.py`, `scripts/detect_sales.py`.
- История для LLM теряет наши ответы: `history_for` + сборка промпта берут `de_client or de_answer` — у in-строки наш ответ в той же строке.
  Отдавать обе реплики; out-строки только отправленные; фильтр текущего письма по id, а не по тексту; ограничить длину истории.
- Одна функция `_call_json(model, system, user, schema, max_tokens, effort, tools)` вместо 12 копий: проверка `stop_reason`,
  обязательных полей, понятная ошибка вместо сырого JSONDecodeError / KeyError.
- `translate_only` и обратные переводы, RU-зеркало auto-ack — Haiku; перевод + обратный перевод одним вызовом.
- «🤖 Предложить» идёт через `_regenerate_core` с инструкцией «другая формулировка» и без объявления — первый черновик делать полноценно
  (объявление, бриф, история). Regenerate/price/instruction — с контекстом.
- `generate_autopilot_reply` → вернуть ключи, которые читает API (`client_answer`, плоский бриф) или договориться с api о формате
  (превью автопилота сейчас пустое). Web search ограничить.
- константы моделей в одном месте (`claude_fast_model` в config — через оркестратора, config у db), убрать `test_api_key`-наследие.
- мёртвое: `generate_reply`/`summarize_thread`/`generate_followup_ping`, `drafts.generate_draft_for_msg` (NameError), неиспользуемые QUICK_STRATEGIES,
  блок «уроков» (таблица lessons всегда пуста — либо оживить `add_lesson`, либо убрать; спросить оркестратора).
- `llm.py`: мониторинг входа в подписку (если вышли из claude.ai — уведомление оператору через telegram_bot — попросить api/оркестратора).

### 3. api — бэкенд Mini App и безопасность
Файлы: `web/app.py`, `web/api_ma.py`, `web/templates/`, `modules/tg_init_data.py`, `modules/operator_lock.py`, `modules/telegram_bot.py`.
- Старая Jinja-морда мертва и опасна: свести `web/app.py` к FastAPI + CORS + `ma_router` + `/health`; удалить `web/templates/`, `jinja2`.
  Нужное из `/debug/*`, `/logs` — локально (127.0.0.1) или в MA. Проверить, что ничего из MA на старое не ссылается.
- Эндпоинты `async def` без `await` → `def` (threadpool); `_release_lock` / drain / Telegram-уведомления — `to_thread` или фоном.
- `/api/ma/pipeline`: N+1 `get_thread_autopilot` → JOIN (SQL — у db), обрезка `ru_client` на сервере, ETag/304; `/dashboard` не гонять pipeline дважды.
- `/health` лёгкий (polluted — из джоба), плюс возраст последнего успешного опроса почты (для watchdog).
- initData: срок жизни 1 ч → обмен на серверный токен сессии или 12–24 ч; пустой токен бота → 503; не логировать ожидаемый hash.
- `/debug/poll-now` не в обход `max_instances`; `ma_scout_verify` — защита от повторного запуска.
- `telegram_bot`: no-op `broadcast_*` убрать вместе с вызовами; `_THREAD_BUSY` под Lock; документация callback-poller.
- Тесты: каждый маршрут, кроме allowlist, требует `verify_init_data_dep`.

### 4. db — хранение и настройки
Файлы: `database.py`, `modules/db_threads.py`, `modules/db_sales.py`, `modules/db_scout.py`, `config.py`, `modules/backup.py`.
- Индексы: `messages(gmail_message_id)`, `messages(created_at)`, `messages(gmail_thread_id, direction, created_at)`, `messages(buyer_name)`, `messages(ad_id)`.
- Соединение на поток (`threading.local`), `journal_mode=WAL` один раз, `synchronous=NORMAL`; кэш настроек с TTL / сбросом в `set_setting`.
- Транзакции (`BEGIN IMMEDIATE`) в мульти-UPDATE: `apply_detection_to_messages`, `mark_thread_sold` и т. п.
- `pipeline_threads`, `find_reminder_candidates`, `list_clients`, `list_threads_for_client` — оконные функции вместо коррелированных
  подзапросов, `LEFT JOIN thread_autopilot`; даты диапазоном, а не `date(created_at)=`. Интеграционные тесты на временной БД.
- `upsert_scout_listing` — `INSERT … ON CONFLICT`, одна транзакция на пачку.
- Бэкап: на Drive через сервисный аккаунт не работает (нет квоты) — сейчас внешнего бэкапа нет. Предложить рабочий вариант (локальная
  ротация + копия на caramba / Shared Drive), согласовать с оркестратором.
- `config.py`: модель «быстрая» (`claude_fast_model`) — для llm.

### 5. web-app — Mini App, туннель, эксплуатация
Файлы: `web-app/`, `scripts/`, `main.py`, `log_buffer.py`, `kleinanzeigen-bot.service`, `.gitignore`, `CLAUDE.md`, `AGENTS.md`.
- `thread.js:912` beacon снятия блокировки шлёт относительный URL на github.io → `${API_BASE}/api/ma/...`.
- cache-bust: import map в `index.html` (версия в одном месте) + `scripts/bump_cache.sh`; ленивая загрузка редких экранов.
- гонки опроса: `pipeline.js` / `scout.js pollStatus` / `thread.js` — поколение или AbortController в teardown.
- открытие треда — 4–8 RTT последовательно (объединённый эндпоинт — согласовать с api).
- дубли `eur()`, мёртвые экспорты, `account_id` в шапке треда.
- логи: `apscheduler.executors.default`/`apscheduler.scheduler`/`httpx` → WARNING, uvicorn access_log без `/health` и pipeline.
- туннель: `update_tunnel_url.sh` делает `git reset --hard` в проде и коммитит URL + bump всех `?v=` — заменить на передачу API base
  в URL Mini App (`?api=` через setChatMenuButton, проверка `^https://[a-z0-9-]+\.trycloudflare\.com$`) или named tunnel (решение владельца).
- `CLAUDE.md` / `AGENTS.md` устарели (бот — не только notifier, Bootstrap не подключён, число тестов, подписка вместо API) — обновить в конце.

## Порядок
Все 5 стартуют параллельно. Оркестратор сливает по готовности: сначала db (индексы, соединения) и mail (стабильность опроса),
затем llm и api, затем web-app. После каждого слияния — pytest, рестарт, `qa`; в конце прохода — `ux-lazy`.
