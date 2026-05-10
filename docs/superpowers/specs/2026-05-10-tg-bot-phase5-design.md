# Phase 5 — TG bot cleanup (slim бот) — design

**Status:** approved (2026-05-10)
**Scope:** удалить из `modules/telegram_bot.py` все UI handlers которые теперь покрыты MA. Превратить бот в «pipeline + notifications + reminders» surface, всё actionable перенести в MA через web_app deep-link buttons.

## Цель

После Phase 1-4 MA полностью покрывает review actions, compose, autopilot, settings. Бот всё ещё несёт legacy UI: 25+ callback handlers, `_PENDING_INPUTS` input-mode state machine, edit-mode rendering, confirm-gate. Это ~2000 LOC дублирования. Phase 5 убирает дубликаты — telegram_bot.py с 3507 → ~1000-1200 LOC.

## Non-goals для 5

- Reminder approve flow — оставляем в боте (offer + approve buttons). Phase 6 если нужно.
- Daily summary — оставляем (текстовое сообщение, не интерактив).
- Hourly error monitor — оставляем (digest, не интерактив).
- Polling job dependencies (`thread_busy`, lock wrappers, drain_deferred) — НЕ ТРОГАЕМ (зависят scheduler.py + web/api_ma.py).

## Что удаляем

### 1. Callback action handlers в `_on_callback`

Удалить ветки:
- `send` — review send → MA `/messages/{id}/send`
- `skip` — review skip
- `sold` — mark sold
- `editru` / `editde` — edit text
- `price` — custom price
- `instr` — custom instruction
- `q:*` (q:fest / q:minus5 / q:minus10 / q:ask / q:meet) — quick strategies → MA `/regenerate`
- `t:*` (t:harsh / t:friend / t:short / t:regen) — tweaks
- `compose` — operator-initiated message → MA `/compose`
- `apstart` / `apconfirm` / `apstop` — autopilot config → MA `/autopilot/*`
- `clienthist` — client history view → MA `/client/{email}`
- `opencard` — re-open review card (was `/card N`)
- `back` (back to pipeline) — MA hash router

Оставляем callback handlers:
- `pipe:N` — pipeline thread → должен теперь не работать (мы заменим pipeline cards на web_app buttons), но оставим обработчик-нop для backwards compat (старые сообщения в чате могут иметь старые callback'ы)
- Action-handlers reminders'а: `rem:approve:N` / `rem:skip:N` / `rem:snooze:*:N` (если есть) — оставляем, это Phase 6

### 2. `_PENDING_INPUTS` state machine

Удалить:
- `_PENDING_INPUTS: dict[(chat_id, user_id), {action, msg_id, payload, ts}] = {}`
- `_enter_input_mode(...)` / `_exit_input_mode(...)`
- В `_on_text`: ветки `if pending["action"] == "edit_ru" / "edit_de" / "price" / "instr" / "compose" / "ap_floor"`
- Cleanup `_PENDING_INPUTS` при back/cancel callbacks

### 3. Review-card rendering функции

Удалить:
- `_format_review_text(msg, **kwargs)` — заменим простой mini-card builder
- `_review_keyboard(msg_id, ...)` — больше не нужен (MA даёт keyboard)
- `_format_review_text` под-функции для разных state'ов (locked / input-mode / confirm-pending)
- `_truncate_html_safe` — оставляем (используется broadcast)
- `_lock_remaining_min` text формирование

### 4. Confirm-gate logic

Удалить:
- `NEEDS_CONFIRM = {...}` set
- `_render_confirm_preview(...)` функция
- Confirm-gate проверка в начале `_on_callback`

### 5. Helpers которые станут unused

После удаления выше — некоторые helpers станут unused. Удаляем:
- `_PENDING_INPUTS`-related helpers
- `_format_review_text` под-функции
- `_review_keyboard` builders для разных state'ов
- `_AUTOPILOT_STOP_REASONS` — оставляем (нужен `send_autopilot_stop_notification`)
- `QUICK_STRATEGIES` references — оставляем в claude.py, бот их больше не дёргает

## Что меняем

### `send_for_review(message_id)` — мини-карточка с web_app

**Сейчас:** шлёт review-карточку с full keyboard (~10 кнопок) в DM-fanout. Текст ~500-1000 символов с HTML formatting.

**Станет:** шлёт компактную карточку:

```
📨 Новое от Osman
🏷 Sitzbank, Sitze Peugeot · 1500€
💬 Können Sie 1300?

[📋 Открыть в MA]
```

Inline-keyboard: одна кнопка с `web_app: {url: "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/?tgWebAppStartParam=review_<msg_id>"}`.

Tap → открывается MA на review-screen для этого msg_id.

После Phase 4 deep-link `?tgWebAppStartParam=review_<id>` → router.js → screens/review.js (thin redirect) → screens/thread.js с focus на pending msg_id.

**Поведение:**
- DM-fanout сохраняется (через _http_post)
- card_dispatches table заполняется как обычно (для broadcast_after_external_action)
- broadcast после MA-actions — обновляет ТЕКСТ карточки (новый mini-card text), keyboard не меняем (та же одна кнопка)

### Pipeline rendering

**Сейчас:** каждый тред — кнопка с `callback_data: "pipe:N"` → `_on_callback` → шлёт thread-detail в чат.

**Станет:** каждый тред — кнопка с `web_app: {url: "...?tgWebAppStartParam=thread_<thread_id>"}` → MA открывается на thread.

Header pipeline-сообщения остаётся (статистика 🔴 N · 🟢 N).

«🔄 Обновить» persistent reply keyboard кнопка — оставляем (рестрит pipeline). Логика обновления (cleanup старых сообщений) — упрощается т.к. больше не нужно удалять thread-detail-сообщения (их теперь нет).

### Bot ↔ MA broadcast после action в MA

`telegram_bot.broadcast_after_external_action(msg_id)` — оставляем. Но теперь обновляет mini-card text, не full review.

Helper нужно адаптировать: `_format_review_text(msg)` → переименовать в `_format_minicard_text(msg)` или extract в новую функцию.

## Что оставляем неизменным

### Сохраняемые helpers (нужны полу-внешним зависимостям)

- `_http_post` / `_http_post_single` — sync HTTP к Telegram API. Используется scheduler.py + Phase 3b broadcast.
- `_truncate_html_safe` — broadcast safety.
- `broadcast_after_external_action` — Phase 3b.
- `mark_thread_busy` / `clear_thread_busy` / `thread_is_busy` / `_THREAD_BUSY` — нужны scheduler.poll_all_accounts (defer when busy).
- `_acquire_lock` / `_release_lock` / `_check_lock` / `_lock_remaining_min` — нужны web/api_ma.py lock endpoints (Phase 3b).
- `_msg_thread_id` — used by lock wrappers.
- Persistent reply keyboard handlers (`PIPELINE_BUTTON_LABEL`, `_on_text` ветка для «🔄 Обновить»).
- Pipeline-rendering функции (`_build_pipeline_text`, `_pipeline_thread_card`, `_pipeline_threads_data` — переписать под web_app buttons).
- `send_autopilot_start_notification` / `_progress` / `_stop_notification` — пингуют операторов из scheduler-а.
- `send_reminder_offer` + ассоциированные callback handlers (`rem:approve:N` / `rem:skip:N`) — Phase 6 candidate.
- Daily summary (`send_daily_summary`).
- Hourly error monitor.
- `build_application` — bootstrap бота.

### `_on_text` остатки

После cleanup в `_on_text` остаётся:
- Хендлер для «🔄 Обновить» (persistent reply keyboard) → re-render pipeline.
- Команда `/menu` → переустановить keyboard.
- Команда `/pipeline` (alias).
- Прочие `/help` / `/stats` / `/threads` — оставляем.

## Risks для Phase 5

- **scheduler.py ссылается на удалённые функции** — нужно grep `telegram_bot.<name>` в scheduler.py перед удалением каждого helper'а. Особенно: `send_for_review` (используется), `send_autopilot_*` (используется), `mark_thread_busy/clear_thread_busy/thread_is_busy`.
- **main.py может импортировать удалённое** — grep `from modules.telegram_bot import` в main.py.
- **web/api_ma.py зависимости** — grep `telegram_bot.<name>` в api_ma.py. Нашли: `_acquire_lock`, `_release_lock`, `_check_lock`, `broadcast_after_external_action`, `send_autopilot_start_notification`. Все эти оставляем.
- **TG WebView не примет `web_app` button если URL не HTTPS** — у нас cloudflared HTTPS, ок.
- **Pages URL содержит `web-app/` суффикс** (после изменения GitHub Pages folder в Phase 1) — `tgWebAppStartParam` должен передаваться как `?tgWebAppStartParam=review_<id>`. Проверить что app.js читает start_param корректно.
- **Backward compat** — старые TG-карточки в чате могут иметь callback'и на удалённые actions (q:fest, send, skip и т.д.). Если оператор на них клацнет — bot ответит «UnknownCallback». Лечится: в `_on_callback` дефолт-ветка просто `query.answer(text="Открой в MA")` без падения.
- **Production safety** — backup `pre-phase4-2026-05-10` доступен, plus DB snapshot.

## Acceptance

- Все 97 unit-тестов проходят.
- Бот стартует без ImportError.
- `/pipeline` показывает список тредов, каждый — кнопка `[📋 Открыть в MA]`.
- Новый incoming → бот шлёт мини-карточку с одной web_app кнопкой.
- Tap кнопки в боте → MA открывается на review screen.
- Action в MA (send/skip/...) → бот видит обновление текста мини-карточки через broadcast_after_external_action.
- Reminders (offer/approve) работают — старый bot flow.
- Old callback на удалённое action → graceful no-op с подсказкой «Открой в MA».
- telegram_bot.py: 3507 → ~1000-1200 LOC.

## File Structure

**Modified:**
- `modules/telegram_bot.py` — удаляем ~2000 LOC (handlers, state machine, render functions). Меняем `send_for_review` + pipeline rendering.

**Tests:**
- Существующие 97 тестов должны пройти без изменений.
- Опционально: smoke-тест что `from modules import telegram_bot` импортируется чисто.

## References

- Phase 1 spec: `docs/superpowers/specs/2026-05-09-tg-mini-app-design.md`
- Phase 4 plan: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase4.md`
- Backup: `pre-phase4-2026-05-10` git tag, `/home/pg/backups/db-pre-phase4-2026-05-10.db`
- Phase 3b broadcast: `telegram_bot.broadcast_after_external_action(msg_id)`
- MA endpoints: `/api/ma/messages/{id}/{send,skip,sold,regenerate,edit-ru,edit-de,price,instruction}`
- Pipeline data API: `GET /api/ma/pipeline` (Phase 2)
