# Kleinanzeigen Bot

## Проект
Автоматизация переговоров на Kleinanzeigen.de через Gmail IMAP/SMTP + Anthropic Claude (Sonnet/Haiku) + Telegram-бот для оператора.

## Сервер
Linux Mint, IP: 192.168.88.28, user: pg
Папка проекта: /home/pg/kleinanzeigen-bot
WSL2 — dev-зеркало, sshfs смонтирован → правки синхронны с продом.

## Стек
- Python 3.11+
- FastAPI + Jinja2 (порт 8080)
- SQLite + WAL
- Google Drive API (бекап SQLite)
- IMAP/SMTP Gmail (App Password)
- Anthropic API: claude-sonnet-4-6 (драфты, бриф) + claude-haiku-4-5 (classifier, auto-ack, similarity)
- python-telegram-bot 20+ (long polling)
- Playwright Python (парсинг Kleinanzeigen)
- APScheduler
- systemd

## Структура
- `main.py` — entry: bootstrap БД → scheduler в фоне → Telegram polling в потоке → uvicorn
- `config.py` — настройки (KV в БД с дефолтами в `DEFAULTS`); геттеры с приведением типов
- `database.py` — SQLite схема + миграции через `_add_column_if_missing` + helpers
- `log_buffer.py` — кольцевой буфер 2000 записей для веб-морды (`/api/logs`)
- `scheduler.py` — APScheduler jobs: `poll_gmail`, `send_replies`, `drive_backup`, `check_reminders`, `daily_summary`, `monitor_errors`. Plus action-функции `send_one`, `regenerate_draft*`, `send_followup_ping`, `send_manual_compose`, `_send_auto_ack`
- `modules/gmail.py` — IMAP fetch (UNSEEN/include_seen) + SMTP send. `_decode` нормализует CR/LF в header (RFC 5322 folding)
- `modules/parser.py` — Playwright `parse_ad`, regex для URL/ad_id, `clean_email_body`, `is_junk_subject` blacklist, `detect_ad_state` (Gelöscht/Reserviert/active)
- `modules/claude.py` — Anthropic SDK обёртки:
  - `generate_reply` (Sonnet, full schema: client_lang+ru_client+ru_answer+de_answer+**ru_translation**+deal_brief)
  - `regenerate_with_strategy` / `regenerate_with_price` / `regenerate_with_instruction` через `_regenerate_core`
  - **`generate_autopilot_reply`** (Sonnet с web_search tool, floor-aware, stop-detection)
  - `translate_only(text, source_lang, target_lang)` — для back-translate в Edit DE
  - `detect_and_translate_to_ru` (Haiku, для backfill-translate в related-client warning)
  - `generate_followup_ping`, `summarize_thread`
  - `classify_email_is_inquiry` (Haiku, junk filter)
  - `generate_auto_ack` (Haiku, для приветствия-заглушки)
  - ~~`detect_similar_buyer`~~ (удалена 2026-05-04)
  - `lang_display`, `detect_lang_override` (директива «на немецком: …»)
  - `GERMAN_CLOSING_RULE` — каждое исходящее на немецком ОБЯЗАНО заканчиваться MfG/Viele Grüße
- `modules/ad_brief.py` — генератор брифа объявления (Sonnet с json_schema, кэшируется в `ad_briefs` по `ad_id`)
- `modules/telegram_bot.py` — основной фронт оператора (см. отдельный раздел ниже)
- `modules/backup.py` — Google Drive backup через service account
- `web/app.py` — FastAPI: `/`, `/clients`, `/clients/{email}`, `/threads`, `/threads/{id}`, `/messages`, `/accounts`, `/settings`, `/logs` + `/api/*` + `/debug/*`
- `web/templates/` — Bootstrap 5.3 dark theme + simple-datatables 9.0.3 (CDN) для клиент-side сортировки/фильтрации в таблицах. `base.html` определяет глобальный хелпер `window.initDataTable(selector, opts)` — все табличные страницы (`/clients`, `/threads`, `/messages`, `/accounts`, dashboard «recent») используют его. Дефолт-сортировка: дата DESC (свежие сверху). Навигация — pills вместо navbar; компактная типографика (14px база)
- `docs/superpowers/specs/` — design-спеки фич (`auto-ack-design.md`, `tg-rework-design.md`)

## БД (текущая схема, актуально 2026-05-03)
**`accounts`**: id, name, gmail_email, gmail_app_password, kleinanzeigen_email/password, is_active, created_at, **`auto_ack_enabled`** (NEW: per-account toggle для auto-приветствия)

**`messages`** — главная таблица. Базовые поля + накопившиеся колонки:
- direction='in'/'out', status (new/pending/edited/approved/sent/sent_debug/skipped/skipped_sold/error_*/`archived`)
- ad_url, ad_id, ad_title, ad_price, ad_description, seller_name, buyer_name, **buyer_display_name**
- de_client (raw письмо), ru_client (Claude-перевод)
- ru_answer (черновик RU как «инструкция»), de_answer (текст для клиента на client_lang), **ru_translation** (точный обратный перевод de_answer → RU для верификации оператором)
- client_lang, answer_lang, gmail_message_id (buyer's), gmail_thread_id, telegram_message_id
- email_subject, tokens_in/out, cost_usd, sent_at, sent_message_id (наш SMTP)
- reminder_state ('none'/'offered'/'approved'/'skipped'), is_reminder, reminder_snooze_until
- **is_auto_ack** (NEW: 1 если row — auto-приветствие)
- **is_autopilot_reply** (NEW: 1 если row отправлена в режиме автопилота — для аналитики и отображения 🤖)
- **history_summary_ru** (Claude-summary для блока «История»)
- **extra_notes** (NEW: лог операторских инструкций «💸 цена / 📝 свобод. инстр»)
- **deal_brief_json** (NEW: JSON {summary_ru, expected_next, negotiated_price_eur, client_assessment} — генерится Sonnet вместе с reply)
- ~~**similar_buyers_json**~~ (deprecated 2026-05-04 — фича удалена; колонка пустая, не пишется/не читается; в схеме оставлена ради старых данных)

**`settings`**: KV (key, value, updated_at). Управляются через web `/settings` или `/api/settings`.

**`ad_briefs`**: ad_id (PK), ad_title, ad_url, ad_price, brief_md, key_facts_json, **sold_at** (если установлен — auto-skip новых inquiries по этому объявлению), created_at, updated_at

**`lessons`**: пары (плохой_бот_драфт → правка_оператора). Топ-5 релевантных передаются в Claude при генерации (in-context learning). Создаются при ручной правке оператором.

**`card_dispatches`** (NEW для DM-fanout): id, message_id (FK→messages), chat_id, tg_msg_id, sent_at. Хранит fanout-копии review-карточки в DM каждого оператора, нужен для broadcast-обновлений.

**`thread_autopilot`** (NEW): per-thread state автопилота. PK=`gmail_thread_id`. Поля: `active`, `floor_price_eur`, `notify_mode` ('silent'|'notify'), `messages_sent` (counter, max 20), `started_by/at`, `stopped_at`, `stop_reason`. UPSERT при start (можно переактивировать после стопа).

**`processed_messages`** (NEW 2026-05-07, для orphan-recovery): `gmail_message_id` PK, `account_id`, `processed_at`, `reason`. Журнал ВСЕХ писем которые бот видел и решил по ним что-то — даже skip-ы (junk/noreply/sold/etc). Используется `gmail.find_orphan_seen_uids` чтобы отличать «никогда не видели» от «видели и сознательно skip-нули»; без этого orphan-scan в цикле дёргает Haiku-classifier на одних и тех же junk-письмах. Заполняется через `db.mark_processed(msg_id, account_id, reason)` из единого хелпера `scheduler._skip_email`. Reason: `inquiry` / `skipped_dedup` / `skipped_noreply` / `skipped_junk` / `skipped_purchase_side` / `skipped_classifier` / `skipped_max_age` / `skipped_sold` / `skipped_no_ad_ref`.

## Telegram-бот (`modules/telegram_bot.py`)

### Режимы доставки
1. **Group-mode** (legacy): `telegram_chat_id` = chat_id группы (отрицательное число, supergroup `-100xxx` после миграции). Все сообщения в группе.
2. **DM-fanout** (текущее использование, активируется когда `config.telegram_operator_dm_ids()` непуст): CSV chat_id-ов операторов в settings → каждое broadcast-сообщение шлётся каждому DM-у. Ловится прямо в `_http_post` (если method=sendMessage AND target == primary chat_id → fanout).

### Authorization
`config.telegram_authorized_ids()` — set chat_id-ов которые могут жать кнопки и слать команды. Включает primary `telegram_chat_id` + CSV из настройки `telegram_authorized`.

### Persistent reply keyboard
Внизу чата — одна кнопка `🔄 Обновить / ↩ Назад` (`PIPELINE_BUTTON_LABEL`). При тапе шлёт текст «🔄 Обновить / ↩ Назад» → `_on_text` ловит → запускает `_on_pipeline`. Re-anchored на header pipeline-сообщения каждый раз.

Установить вручную: `/menu`. Очистить command-menu Telegram (нет автокомплита `/`): `setMyCommands([])` уже выполнено.

### Card lifecycle (review card в DM-fanout)
1. **Приход inquiry** → `scheduler._process_incoming` → Sonnet draft → `telegram_bot.send_for_review(msg_id)`
2. `send_for_review` чистит `card_dispatches` для этого msg_id, шлёт `_review_keyboard` каждому DM-у, добавляет dispatches
3. Любой оператор кликает кнопку → callback `<action>:N` → `_on_callback`
4. **Confirmation gate**: если `action_key in NEEDS_CONFIRM` (send/skip/sold/clienthist/price/instr/q:fest/t:*) и нет `confirm:` префикса → показываем confirm-preview (только клику оператора), broadcast soft-lock «🔒 @A в подтверждении» остальным
5. На `confirm:<original>:N` — bypass gate, lock acquire, action runs, `_broadcast_card` обновляет ВСЕ копии
6. На `cancel:N` — _safe_edit query (всегда работает) + broadcast best-effort
7. На action complete (sent/sent_debug/skipped/sold) — `_broadcast_card` финальное состояние, `_release_lock(msg_id)`

### Lock (in-memory, 5 мин)
`_THREAD_LOCKS: dict[msg_id, (actor_str, acquired_at)]`. Lock acquire-ится на любой `LOCKABLE_ACTIONS` клик. Block check на старте handler-а. Auto-expire через `LOCK_TIMEOUT_SEC = 300`. Release при successful action complete OR `_exit_input_mode`.

Для input-режимов (Edit RU/DE/Price/Instr): lock держится ПОКА оператор печатает или жмёт Cancel. `_enter_input_mode` дополнительно broadcast-ит «🟥🟨 КАРТОЧКА В РАБОТЕ У @A» к остальным dispatches (не A).

### Кнопки review-карточки (текущая раскладка)
```
✏️ Правка RU       │ ✏️ Правка DE
💎 Без торга        │ 💸 Своя цена
📝 Своя инструкция │ ❌ Пропустить
✅ ОТПРАВИТЬ (full-width)
👊 Жёстче           │ ☺️ Мягче
✂️ Короче           │ 🔁 Переформулировать
💰 Товар продан    │ 📋 История клиента
↩ Назад к pipeline (full-width)
```
- **Правка RU/DE** — direct (без confirm), input-mode. RU — через `translate_only(target=client_lang)`. DE — текст as-is + back-translate в RU для зеркала.
- **Без торга / Жёстче / Мягче / Короче / Переформулировать** — preset стратегии в `claude.QUICK_STRATEGIES` / `TWEAK_INSTRUCTIONS`
- **Своя цена** — числовой input → `claude.regenerate_with_price`
- **Своя инструкция** — свободный текст → `claude.regenerate_with_instruction`
- **↩ Назад** — callback `back:N` → удаляет всё накопленное + рисует свежий pipeline (то же что `🔄 Обновить`)

### Color palette (имитация цвета через эмодзи)
- 🟩🟢 — успешно (отправлено / продано)
- 🟥🟨 — лок (другой оператор работает)
- 🟨🟧 — disabled mode
- 🟥🟥 — ошибка SMTP / Claude
- ⏱ — нейтральные / pending состояния

### Pipeline (`/pipeline` или тап `🔄 Обновить`)
Каждый активный тред = отдельное Telegram-сообщение-карточка:
```
1 🟢📝 · osman · ack+draft       ← header (bold)
Sitzbank, Sitze Peugeot · 2+1 · 📦 б/у · 1000€   ← товар + конфиг + состояние + цена
🤝 Клиент интересуется... · 💰 1500€ · ⏳ ждём ответ · 🏷 серьёзный   ← deal_brief
[ ⏱ 17:42 · 5мин назад ]         ← кнопка → callback pipe:N → thread-detail
```
- Шапка перед карточками: `🔴 ждут нас: N · 🟢 ждём клиента: N · 📝 = есть готовый черновик от бота`
- **Деление**: `last_event_kind` (in/out из `db.thread_events`) — `kind='in'` → `🔴 ждут нас`, `kind='out'` → `🟢 ждём клиента`. Сортировка ВНУТРИ секции ASC по `last_event_at`.
- **Порядок секций**: 🟢 сверху (старые завершённые), 🔴 снизу (срочные у поля ввода). Самые свежие события — внизу чата ближе к оператору.
- 📝 sub-marker: pending Sonnet draft ждёт оператора
- **Status_txt** (по хронологии последнего события):
  - `last_kind='out'`: «ответили · ждём клиента» / «ack · ждём клиента» (+`+N draft` если pending)
  - `last_kind='in'`: «draft xN · ждёт нас» / «draft · ждёт нас» / «ack · ждёт нас» / «новый · ждёт нас»
- Состояние товара (📦 б/у / 🆕 новое / ⚠️ дефект): `_ad_condition_marker` с проверкой отрицания (`ohne Beschädigung` НЕ дефект)
- Конфигурация (2+1, одиночное, лавка, и т.п.): `_detect_config`

### Cleanup при «🔄 Обновить / ↩ Назад»
- Шлёт fresh pipeline ПЕРВЫМ (chat не пустеет — нет «Запустить бота» CTA placeholder)
- Затем удаляет всё старое: tracked-минус-новый pipeline + sweep последних 50 IDs назад от триггера
- Использует `deleteMessages` batch API (Bot API 7.4+) для скорости

При тапе на тред в pipeline (`pipe:N`): отправляет thread-detail (один или несколько чанков для длинных тредов через `_split_for_telegram`), удаляет ВСЕ tracked-msg-id младше первого нового чанка кроме самих чанков. Так чисто уходят header + все карточки + любой residual от прошлых открытий.

### Thread-detail card
Лента событий через `db.thread_events()` (in.created_at + out.sent_at, sorted chronologically): один row 'in' с заполненным de_answer ⇒ ДВА события. **Только sent/sent_debug** для outgoing — pending/edited/approved драфты НЕ показываются (это часть review-карточки, не лога). Auto-ack показывается как обычное наше сообщение с маркером 🤖 ack — клиент его получил, скрывать смысла нет. Header: #id, ad_title, цена, клиент, **🏪 Наш: account-name (gmail-email)**, ссылка на объявление, last_event_at. Related-warning блок (только точное совпадение `buyer_display_name`). Кнопки `✉️ Написать клиенту`, `📋 Открыть карточку ревью`, `↩ Назад к pipeline` — на ПОСЛЕДНЕМ чанке.

### Compose (operator-initiated message)
Из thread-detail клик `✉️ Написать` → input-mode → текст оператора → `scheduler.send_manual_compose` (translate если нужно + SMTP reply на последнее incoming) → новая out-row.

### Related client warning
В review-карточке и thread-detail если найдены другие inquiries от этого же клиента — выводится жёлто-красный блок:
- **Точное совпадение** по `buyer_display_name` (мгновенно из `db.find_related_inquiries`)

Style-similarity (`detect_similar_buyer` через Haiku, score ≥ 6/10) РАНЬШЕ был — удалён 2026-05-04: давал шум, ел токены. Колонка `messages.similar_buyers_json` оставлена в схеме (старые данные), но не пишется и не отображается. Функция `claude.detect_similar_buyer` и `db.find_recent_other_buyer_inquiries` — удалены.

### Autopilot (полная авто-беседа с клиентом)
- Кнопка `🚀 Полный автопилот` на review-карточке (активирует через input flow: floor цены → выбор `🤫 Silent` / `🔔 Notify` → старт)
- При активном автопилоте каждое следующее incoming в треде → `claude.generate_autopilot_reply` (Sonnet с web_search tool) → автоматическая отправка БЕЗ operator review
- DB: `thread_autopilot` table per-thread + `messages.is_autopilot_reply=1` на каждом авто-row
- **Stop conditions** (любой триггер → `db.stop_thread_autopilot(thread_id, reason)` + DM-нотификация):
  - `limit` — 20 авто-сообщений
  - `ready_to_buy` / `wants_contact` — Sonnet детектит что клиент дозрел / просит контакты-реквизиты-встречу. На этом messages не отправляется автопилотом, оператор получает обычную review-карточку
  - `threat` — Sonnet детектит угрозу. **Только в этом случае** auto-reply ОТПРАВЛЯЕТСЯ (предупреждение клиенту), потом стоп
  - `manual` — оператор кликнул `🛑 Остановить автопилот` (silent — без пинга)
- **Bridge в `_process_incoming`**: после Sonnet (или autopilot reply) → `_autopilot_dispatch` → counter increment ПОСЛЕ успешного `_send_reply` (sent или skipped/disabled). SMTP fail → counter не растёт, автопилот active
- **Floor enforcement** — system prompt передаёт `floor_eur` + `last_our_price_eur` (из `deal_brief_json` предыдущего outgoing). Sonnet по идее не уступит ниже. **NB:** программной валидации цены НЕТ — полагаемся на Sonnet (можно усилить если drift обнаружится)
- **Pipeline-карточка**: status_txt = «🤖 автопилот N/20» (перебивает обычные ответили/draft/etc)
- **Review-карточка активного автопилота**: жёлтый header-блок «🤖 АВТОПИЛОТ АКТИВЕН (N/20) · floor: X€ · режим: …» + клавиатура заменена на `🛑 Остановить + ↩ Назад`
- **Web search**: `tools=[{"type": "web_search_20250305"}]` — Anthropic native. Sonnet решает сам когда использовать (промпт ограничивает «только при специфичных вопросах, мы платим»). Версия tool-а может потребовать обновления при изменении API
- **Notifications** через `_http_post` (fanout DMs): `send_autopilot_start_notification` (notify mode), `send_autopilot_progress` (notify mode, на каждый авто-ответ), `send_autopilot_stop_notification` (всегда кроме `manual`)
- Spec: `docs/superpowers/specs/2026-05-03-autopilot-design.md`

### Auto-ack (накрутка метрики «отвечает в течение X часов»)
- Per-account toggle `accounts.auto_ack_enabled` (default 0)
- При первом incoming в новом thread-id → ДО Playwright/Sonnet вызывается `claude.generate_auto_ack` (Haiku ~$0.001) → SMTP send → row в БД с `is_auto_ack=1`
- Уважает `send_mode=disabled`
- Защита от повторной отправки: `_ack_already_sent_for_thread`
- В Telegram-картчке (блок «История»): `🤖 Auto-ack [sent]: <text>`. В chat-стиле thread-detail — скрыт (только счётчик внизу).
- AUTO_ACK_EXCUSES — module-level список «поводов» в `scheduler.py` (рандомизация: за рулём, на встрече, и т.д.)

## Команды бота (зарегистрированы в `build_application`)
- `/pipeline` — показать pipeline (то же что тап «🔄 Обновить»)
- `/menu` — переустановить persistent reply-keyboard если случайно убрался
- `/card N` — переслать карточку ревью для msg #N (для jump-to)
- `/pending` / `/stats` / `/threads` / `/mode` / `/pause` / `/resume` / `/start` / `/help` — служебные

Command-menu в Telegram очищено (`setMyCommands([])`) — нет автокомплита, но команды всё равно работают если набрать руками.

## Settings (актуальные ключи)
- `anthropic_api_key`, `claude_model` (default `claude-sonnet-4-6`), `system_prompt`
- `telegram_bot_token`, `telegram_chat_id`, `telegram_authorized` (CSV), **`telegram_operator_dm_ids`** (CSV — активирует DM-fanout если непуст)
- `gmail_poll_interval_sec` (default 60), `gmail_from_filter` (default `kleinanzeigen.de`), `inquiry_max_age_days` (default 7)
- `send_mode` (`disabled`/`redirect`/`production`), `debug_email`
- `reminders_enabled`, `reminder_after_days` (**float**: 1=сутки, 0.5=12ч, 0.04≈1ч — для теста)
- `polling_paused`, `last_daily_summary_date`
- `google_drive_credentials_json`, `google_drive_folder_id`, `backup_interval_hours`
- `max_discount_percent`, `web_port`, `web_host`
- **API balance estimator**: `api_balance_snapshot_usd` (snapshot $ от console.anthropic.com), `api_balance_snapshot_at` (ISO datetime сверки). Остаток = snapshot − SUM(cost_usd) с created_at>snapshot_at. Burn-rate из last-7d-spend, days_remaining = remaining/burn_per_day. Дашборд: цветная stat-карточка (🟢 ≥ $10, 🟡 < $10, 🔴 < $2, клик → /settings).
- **Стиль чат-пузырей в веб-морде** (см. /settings → «💬 Стиль чата»): `chat_font_em`, `chat_padding_v_rem`, `chat_padding_h_rem`, `chat_max_width_pct`, `chat_radius_rem`, `chat_row_gap_rem`, `chat_meta_font_em`, `chat_secondary_font_em`. Применяются через CSS-переменные в `base.html` через Jinja-global `chat_style()` (`web/app.py`).

## Правила
- Комментарии в коде на русском
- Код давать полными блоками (Edit с большим контекстом, не маленькими кусочками)
- Telegram polling (не webhook)
- Playwright напрямую (не Docker)
- pip install с `--break-system-packages --ignore-installed` (debian python3-typing-extensions конфликтует)
- Эмодзи использовать ТОЛЬКО там, где явно дизайн-логика (palette в боте) — иначе не злоупотреблять

## Деплой
WSL2 через sshfs смонтирован на /home/pg/kleinanzeigen-bot — это **тот же путь что прод**. Никакого rsync — правки сразу на сервере.
GitHub: `origin` = https://github.com/pgolitech-lab/kleinanzeigen-bot.git. Через ssh использовать `git -C /home/pg/kleinanzeigen-bot ...` (default ssh cwd = /home/pg, не репо).
```
ssh pg@192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
ssh pg@192.168.88.28 'journalctl -u kleinanzeigen-bot -f'
```
БД на сервере хранится в `/home/pg/kleinanzeigen-bot/kleinanzeigen.db`. Не редактировать руками — только через `database.py` helpers или `db.update_message`.

**systemd unit** (`kleinanzeigen-bot.service`, копия в `/etc/systemd/system/`): `enabled` → стартует при загрузке. `Restart=always` + `RestartSec=5` — перезапуск через 5 сек при любом завершении. `OOMPolicy=continue` — рестартим даже после OOM-killer (по умолчанию systemd этого не делал бы). `StartLimitBurst=10` / `StartLimitIntervalSec=300` — если бот крэшится 10+ раз за 5 минут, systemd прекращает попытки и переводит в `failed` (защита от бесконечного crash-loop). `TimeoutStopSec=30` + `KillMode=mixed` — graceful 30 сек на завершение перед SIGKILL. После правок unit-файла: `sudo cp …/kleinanzeigen-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart kleinanzeigen-bot`.

## Что важно знать (чтобы не сломать)
- **send_mode** имеет 3 режима в settings: `disabled` (default — ничего не шлёт), `redirect` (на debug_email с банером), `production` (реально покупателю). Перед отладкой проверь режим.
- **Данные одной row**: при `direction='in'` `de_client` хранит вопрос, `de_answer` — наш ответ. После send статус становится `sent`/`sent_debug` но direction остаётся 'in'.
- **gmail_message_id vs sent_message_id**: первый — buyer's incoming Message-ID (для дедупа и In-Reply-To), второй — наш SMTP outgoing.
- **de_answer ≠ обязательно немецкий** (исторически), хранит ответ на ЯЗЫКЕ КЛИЕНТА (детектится в client_lang).
- **ru_answer ≠ ru_translation**: ru_answer — это «инструкция/идея от Sonnet RU». ru_translation — точный buckward-перевод de_answer на RU для верификации оператором (NEW поле, добавлено вместе с extended schema).
- **Брифы кэшируются** в ad_briefs по ad_id навсегда (один раз при первом inquiry).
- **deal_brief_json** генерится Sonnet при каждом draft (включая регенерации) — отражает текущее состояние сделки. Не путать с `ad_briefs` (статичные ad-факты).
- **Уроки** (lessons) — пары (плохой_бот → правка_оператора). Топ-5 релевантных в каждом Claude-вызове.
- **Auto-skip проданных**: `ad_briefs.sold_at` → `_process_incoming` пропускает inquiry, шлёт мини-нотификацию.
- **Polling может быть на паузе** через `/pause` (`config.polling_paused()`).
- **Telegram карточка = живой экран**: новые callback action в `_on_callback` должны редактировать ту же карточку (`_safe_edit` для query.message + `_broadcast_card` для всех DM-копий), а не создавать новую.
- **`_PENDING_INPUTS`** keyed by `(chat_id, user_id)` — в группе/мульти-DM не хайджечит сессию другого оператора.
- **DM-fanout критичные точки**: `send_for_review` чистит `card_dispatches` через `db.clear_card_dispatches(message_id)` перед новым fanout. `_broadcast_card` итерирует dispatches, ошибки edit-ов («Message to edit not found» для удалённых) — silently ignored. `_safe_edit(query, ...)` всегда работает на текущей карточке оператора (даже если broadcast partial-fail).
- **Не используй имя `set` в config.py** (шадоит builtin) — для типов `"set[str]"` или `Set[str]` из typing.
- **Defense-in-depth от мусора** (5 уровней в `_process_incoming`): IMAP from-filter → noreply skip → junk-subject blacklist → AI Haiku classifier → max-age check → ad-reference required.
- **HTML-эскейп в Telegram-карточках**: `result['message']` от send_one содержит SMTP Message-ID в `<...>` — Telegram при `parse_mode=HTML` ломается. Всегда `_html()` перед интерполяцией.
- **Guard от регенерации после send**: q/t/regenerate handlers в `_on_callback` проверяют `msg["status"]` — если sent/sent_debug/skipped*, не выполняют.
- **Group-mode Telegram** (legacy): `telegram_authorized_ids()` ВСЕГДА включает `telegram_chat_id` + extras из `telegram_authorized`.
- **Haiku не поддерживает `effort`/`thinking`**: при `claude-haiku-4-5` (classifier, auto-ack, similarity) НЕ передавать эти параметры в `output_config` — bad request. Sonnet 4.6/4.7 поддерживают.
- **Hourly мониторинг логов** (`scheduler.monitor_errors_job`): journalctl scan ERROR/Traceback → Telegram digest. Дедуп через `_REPORTED_ERROR_HASHES` (in-memory).
- **IMAP timeout + orphan-recovery** (2026-05-07): `gmail.IMAP_TIMEOUT=30s` на ВСЕХ `IMAP4_SSL` calls — раньше без таймаута socket.recv мог висеть часами и блокировать polling-job (APScheduler пропускал каждый следующий запуск с `maximum running instances reached`, письма терялись). После регулярного UNSEEN-fetch в `poll_all_accounts` идёт **orphan-recovery scan**: `gmail.find_orphan_seen_uids` ищет SEEN-письма за последние 2 дня которых нет в `messages.gmail_message_id` ∪ `processed_messages.gmail_message_id` (за 3 дня), `gmail.unmark_seen` снимает с них `\\Seen` — следующий poll-цикл подхватит как обычные UNSEEN. Дёшево: только Message-ID per UID. Все skip-точки `_process_incoming` зовут `_skip_email(account, email, reason)` который делает `mark_seen + db.mark_processed` атомарно — без этого recovery в цикле реклассифицировал бы junk на Haiku ($0.001 каждое).
- **`send_for_review` НЕ сбрасывает status в pending** если row уже не `new` (предотвращает sent → pending корраптинг pipeline-классификации). Безопасно перепосылать карточки через `/card N`.
- **`messages.status='archived'`** — backfill-загруженные историч. inquiries (~762 шт). НЕ попадают в pipeline (фильтр SQL пропускает только active статусы).
- **Хронология событий, а не id**: `db.thread_history` сортирует по `created_at ASC, id ASC`. `db.thread_events()` строит flat-список «событий» — incoming.created_at + outgoing.sent_at — корректно для recovery/backfill (новый id, старая дата). MAX(id) больше НЕ используется в `pipeline_threads`/`find_reminder_candidates`/UI-render.
- **`db.add_message`** принимает `created_at` от вызывающего (по умолчанию utcnow). `scheduler._process_incoming` парсит email Date-header и передаёт его — реальные таймстампы и при live-fetch, и при recovery.
- **Бэкфилл RU для auto-ack**: `_send_auto_ack` после Haiku-генерации вызывает `claude.translate_only(target='ru')` и пишет в `ru_answer` + `ru_translation`. Если client_lang=ru — копия текста.
- **Classifier bypass для follow-up'ов**: если в gmail_thread_id уже есть сохранённый incoming — Haiku-classifier обходится (это продолжение разговора, не системка). Защита от ложно-junk коротких follow-up'ов («Danke», «Sorry…»).
- **Time zone в UI**: БД хранит UTC, оператор в Берлине. `modules/telegram_bot._to_berlin(iso, fmt)` + Jinja-фильтр `| berlin('%fmt')` (зарегистрирован в `web/app.py`) — везде где таймстампы видны оператору.
- **Чекбоксы в формах** с hidden fallback `value="0"`: сравнивать со строкой `"1"` явно (`1 if value == "1" else 0`), а не truthy — иначе hidden "0" даёт truthy → галка не выключается. См. POST /accounts/{id}/edit.
- **Telegram message limits — defense-in-depth**:
  - Лимит 4096 chars. Превышение → `BadRequest: text is too long` или `parse entities` если разорвало тег.
  - `_truncate_html_safe(text, limit=4000, suffix=...)` — режет по последней безопасной HTML-границе (`</blockquote>`, `</b>`, `</i>`, `</code>`, `</a>`, `</pre>`, `\n\n`, `\n`, ` `). Применён в `_safe_edit`, `_bot_edit`, `_broadcast_card`, `send_for_review`, и автоматически в `_http_post_single` для `sendMessage`/`editMessageText`.
  - `_split_for_telegram(text, limit=4000)` — для путей где multi-message OK (thread-detail, compose). Tag-aware: режет только в позициях где depth(`<blockquote>/<pre>/<code>`) == 0. Иначе чанк[0] остаётся с открытым тегом → `can't find end tag`.
  - **pipe-handler**: посылает thread-detail чанками (клавиатура только на последнем), затем чистит ВСЕ tracked-id младше первого нового чанка кроме самих чанков. Старая логика `range(clicked-5..new_detail-1)` была багом для длинных threads и multi-chunk send.
  - **compose handler**: после SMTP-успеха УДАЛЯЕТ старое «⏳ перевожу…» и шлёт fresh thread-detail чанками. Раньше edit_message_text упирался в лимит и UI висел.
  - **Auto-truncate при edit_message_text**: если контент длиннее 4000 — auto-cut. Без этого «Message_too_long» проглатывался в `_safe_edit`/`_bot_edit` (только warning в лог) → карточка зависала на промежуточном состоянии.
- **«↩ Назад к pipeline» — ОБЯЗАТЕЛЬНО на ВСЕХ карточках/экранах бота** (правило 2026-05-06): любая клавиатура — review-card во всех state'ах, thread-detail, locked-state, input-mode, confirm-gate, reminder-карточка, autopilot start/stop notif, история клиента, компоуз — должна содержать `↩ Назад к pipeline` (callback `back:N`) хотя бы одной строкой. Промежуточные progress-сообщения (`⏳ отправляю...`, `⏳ перевожу...`) с `reply_markup=None` приемлемы только потому что финальный edit перепишет на полную клавиатуру через секунды. Новые keyboard-builders ОБЯЗАНЫ включать back. Helper `_back_only_keyboard(msg_id)` для финальных state'ов; в любых других — отдельная строка `[InlineKeyboardButton("↩ Назад к pipeline", callback_data=f"back:{msg_id}")]`.
- **Reminder candidate query**: `db.find_reminder_candidates(after_days: float)` использует events-CTE и берёт `kind='out'` как «последнее событие», age > after_days. Поддерживает дробные дни (0.5=12ч). Раньше была MAX(id) → ломалась после recovery.
- **`thread_autopilot.gmail_thread_id`** — PK. UPSERT на старт. После стопа можно переактивировать с теми же floor/notify (counter сбрасывается).
- **Style-similarity (was Haiku-based)**: УДАЛЕНА 2026-05-04 (`detect_similar_buyer`, `find_recent_other_buyer_inquiries`, schema `_SIMILAR_BUYER_SCHEMA`). Колонка `messages.similar_buyers_json` сохранена в схеме — старые данные не тронуты, но не пишется/не читается.

## История фич / спеки
- `docs/superpowers/specs/2026-05-03-auto-ack-design.md` — auto-приветствие (Haiku, накрутка метрики Kleinanzeigen)
- `docs/superpowers/specs/2026-05-03-tg-rework-design.md` — полный rewrite Telegram UX (раскладка, confirmation, edit RU/DE, custom price/instruction, draft preload)
- `docs/superpowers/specs/2026-05-03-autopilot-design.md` — полная авто-беседа с клиентом (Sonnet + web_search, floor + 20-msg cap + stop conditions)
