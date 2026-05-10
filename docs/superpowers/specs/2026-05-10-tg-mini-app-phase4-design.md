# Telegram Mini App — Phase 4 (Compose + Autopilot + Settings + deep-link) — design

**Status:** approved (2026-05-10)
**Scope:** последние недостающие функции в MA для того чтобы Phase 5 мог удалить дублирующие UI из бота. Compose, autopilot start/stop, settings KV editor, deep-link `/review/{msg_id}`.

## Цель

Phase 1-3 закрыли core review actions. Phase 4 добавляет 4 функции которые сейчас живут только в боте. После Phase 4 → Phase 5 trim бот.

## Non-goals для 4

- Reminder approve в MA — пока остаётся в боте (offer + approve flow). Phase 6 если нужно.
- Live polling autopilot status — только при mount. Phase 6 если нужно.
- Settings: account edit (создание/удаление gmail accounts) — оставляем в `web/app.py` /accounts.
- Daily summary — оставляем в боте (текстовое сообщение, не интерактив).

## 1. Compose endpoint + UI

### Backend `POST /api/ma/threads/{thread_id}/compose`

**Request body:** `{text: str}` (1-4000 chars).

**Behavior:**
1. Verify auth.
2. Validate: thread_id has at least one `direction='in'` row (otherwise 404).
3. Run `await asyncio.to_thread(scheduler.send_manual_compose, thread_id, body.text)` — existing helper that translates RU → client_lang, sends SMTP, creates new out-row.
4. Return `{ok: true, sent_msg_id: <new_id>, status: <new_row.status>}`.

**Errors:**
- 401/403/422 (auth)
- 404 (thread не существует)
- 422 (text empty / too long)
- 500 (translate fail / SMTP fail — сообщение из result.message)

**Tests** (`tests/test_api_ma_compose.py`, ~3 tests):
- success
- 404 на отсутствующий thread
- 422 на пустой text

### Frontend

В `screens/thread.js` добавить кнопку `✉️ Написать клиенту` рядом с `↩ К pipeline` (всегда видна, даже без pending). Tap → `compose-form.js` (новый компонент похожий на edit-form.js):

```javascript
buildComposeForm({threadId, onSubmitComplete, onCancel, onError}) → element
```

Form: textarea (placeholder «Введите текст на русском…») + `[💾 Отправить][✕ Отмена]`. Save → POST → onSubmitComplete (re-render thread).

Validation client-side: text non-empty, < 4000 chars.

## 2. Autopilot start/stop

### Backend `POST /api/ma/threads/{thread_id}/autopilot/start`

**Request body:** `{floor_eur: float, notify_mode: "silent"|"notify"}`.

**Behavior:**
1. Auth + 404 на отсутствующий thread.
2. Validate: floor > 0, < 100000; notify_mode in whitelist.
3. `db.start_thread_autopilot(thread_id, floor_eur, notify_mode, started_by=actor_from_user(user))`.
4. Если `notify_mode == "notify"` → `telegram_bot.send_autopilot_start_notification(thread_id)` (helper существует).
5. Return updated thread payload (как `GET /threads/{id}`).

**Errors:** 401/403/422/404.

### Backend `POST /api/ma/threads/{thread_id}/autopilot/stop`

**Request body:** пусто.

**Behavior:**
1. Auth + 404.
2. `db.stop_thread_autopilot(thread_id, reason='manual')`.
3. Return updated thread payload.

**Tests** (`tests/test_api_ma_autopilot.py`, ~5 tests):
- start success
- start invalid floor (negative, too high) → 422
- start invalid notify_mode → 422
- stop success
- stop когда уже stopped → 200 idempotent

### Frontend

В `screens/thread.js` в pending-draft block (или в header справа от autopilot badge):
- Если `header.is_autopilot === false`: текст «🚀 Автопилот не активен» + кнопка `🚀 Запустить`
- Если `is_autopilot === true`: текст «🤖 активен N/20 · floor: X€ · silent/notify» + кнопка `🛑 Остановить`

Tap «🚀 Запустить» → `autopilot-form.js`:
- floor input (number, step=10, min=0)
- radio: `🤫 Silent` / `🔔 Notify`
- `[💾 Старт][✕ Отмена]`

Tap «🛑 Остановить» → confirm dialog (как для skip/sold) → POST stop.

Получаем N/20 + floor + notify_mode из response review payload (`autopilot.{messages_sent,floor_eur,notify_mode}` уже есть из Phase 3a).

## 3. Settings

### Backend `GET /api/ma/settings`

Возвращает фиксированный набор ключей (whitelist):

```json
{
  "send_mode": "disabled",
  "gmail_poll_interval_sec": "60",
  "inquiry_max_age_days": "7",
  "gmail_from_filter": "kleinanzeigen.de",
  "reminders_enabled": "1",
  "reminder_after_days": "1",
  "polling_paused": "0",
  "telegram_authorized": "242994225",
  "telegram_operator_dm_ids": "242994225",
  "max_discount_percent": "10",
  "claude_model": "claude-sonnet-4-6",
  "system_prompt": "...",
  "chat_font_em": "0.875",
  "chat_padding_v_rem": "0.5",
  "chat_padding_h_rem": "0.75",
  "chat_max_width_pct": "75",
  "chat_radius_rem": "0.5",
  "chat_row_gap_rem": "0.5",
  "chat_meta_font_em": "0.75",
  "chat_secondary_font_em": "0.875",
  "api_balance_snapshot_usd": "0.0",
  "api_balance_snapshot_at": "",
  "anthropic_api_key": "•••••",
  "telegram_bot_token": "•••••"
}
```

Sensitive keys (API keys, tokens, passwords) маскируются как `•••••` (или `••••• <last4>` если хочется).

### Backend `POST /api/ma/settings`

**Request body:** `{key: str, value: str}`.

**Behavior:**
1. Auth.
2. Whitelist: `key` must be in `ALLOWED_SETTING_KEYS` set (~25 ключей выше).
3. Validation per key (e.g. `send_mode` must be one of `{disabled, redirect, production}`; numbers must parse).
4. `config.set(key, value)` (или `db.set_setting(key, value)` — что есть в проекте).
5. Return `{ok: true, key, value}` (echo, не маскируем — frontend обновит локальный state).

Для sensitive keys: фронт шлёт реальное значение через POST; backend сохраняет; GET потом маскирует.

**Tests** (`tests/test_api_ma_settings.py`, ~5 tests):
- get returns dict с whitelist keys
- get masks secrets
- post valid update succeeds
- post invalid key → 422
- post invalid value (e.g. send_mode='evil') → 422

### Frontend

Новый экран `screens/settings.js` (~150 строк):

```
┌──────────────────────────────────┐
│ ⚙ Настройки                       │  Header
├──────────────────────────────────┤
│ Send mode                         │
│  ( ) disabled  ( ) redirect      │  Radio
│  ( ) production                   │
├──────────────────────────────────┤
│ Gmail polling interval (sec)      │
│ [   60   ]              [💾 Save] │  Each field has Save
├──────────────────────────────────┤
│ ...                               │
├──────────────────────────────────┤
│ 🔐 Anthropic API key              │
│ ••••• [Заменить]                  │  Click → input expanded
├──────────────────────────────────┤
│ ...                               │
│ [↩ К pipeline]                    │
└──────────────────────────────────┘
```

Каждое поле имеет свой save-кнопку (отдельные POST'ы — пользователь меняет одно поле за раз). Inline error если invalid.

Доступ: новый router pattern `^#\/settings\/?$` → screens/settings.js.

Также добавляем ⚙ icon в pipeline header (и thread header?) который ведёт на `#/settings`.

## 4. Deep-link `/review/{msg_id}`

Новый router pattern: `^#\/review\/(.+)$` → screens/review.js.

`screens/review.js` — тонкий redirector:

```javascript
import { api } from "../api.js?v=...";
import { setLoading, setError } from "../utils.js?v=...";

export async function render(mount, params) {
  setLoading(mount, "Открываю карточку…");
  try {
    const review = await api(`/api/ma/messages/${params.msg_id}`);
    if (!review.thread_id) {
      setError(mount, "Тред не найден");
      return;
    }
    location.hash = `#/thread/${encodeURIComponent(review.thread_id)}`;
    // hashchange listener в router.js дёрнет thread.js render
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

**Bot deep-link integration:** уже работает с Phase 1 — bot шлёт сообщение с `web_app: {url: "https://...?tgWebAppStartParam=review_123"}`. SPA app.js парсит start_param `review_123` → location.hash = `#/review/123` → review.js → редирект на `#/thread/abc`.

В Phase 5 cleanup это будет основным механизмом перехода из бот-pipeline в MA review.

## File Structure

**Создаются:**
- `web-app/js/components/compose-form.js` (~80 строк, по модели edit-form.js)
- `web-app/js/components/autopilot-form.js` (~100 строк)
- `web-app/js/screens/settings.js` (~200 строк)
- `web-app/js/screens/review.js` (~30 строк, thin redirect)
- `tests/test_api_ma_compose.py` (~3 tests)
- `tests/test_api_ma_autopilot.py` (~5 tests)
- `tests/test_api_ma_settings.py` (~5 tests)

**Модифицируются:**
- `web/api_ma.py` — +5 endpoints (compose, autopilot start/stop, settings get/post), ~+150 lines
- `web-app/js/screens/thread.js` — +compose button + autopilot section, ~+80 lines
- `web-app/js/screens/pipeline.js` — +⚙ link to settings
- `web-app/js/router.js` — +/settings и /review/{msg_id} routes
- `web-app/index.html` — bump cache-bust
- All ESM imports — bump cache-bust to `?v=20260510-12`

## Risks для Phase 4

- **`scheduler.send_manual_compose`** — proverit signature, может быть требует context или иные параметры. Plan task должен grep-нуть фактическую сигнатуру.
- **`telegram_bot.send_autopilot_start_notification`** — есть в боте. Проверить какие параметры принимает (msg_id или thread_id?).
- **Settings whitelist** — если какой-то ключ забыли — frontend покажет «Field locked». Не критично, добавим в Phase 6.
- **Settings race condition** — два оператора одновременно меняют один ключ. Last-write-wins; не критично для personal-use.
- **Sensitive value display** — после save секрет не показываем заново. Если оператор хочет проверить — нужно cancel + re-edit (вводит заново). Acceptable.
- **`autopilot/start` без pending msg_id** — autopilot работает per-thread, но spec'd принимает только thread_id. Backend должен зачитать message_id для notification helper'а — взять MAX(id) WHERE direction='in' AND thread_id=...

## Что НЕ делаем в Phase 4

- Compose в треде где нет ни одного direction='in' row (новый conversation) — операторы такое не делают; bot тоже не поддерживает.
- Autopilot live polling — пока на page-mount только.
- Settings reordering / grouping by category UI — flat list, как в /settings web.
- Account edit (создание/удаление accounts) — остаётся в /accounts.
- Whitelist UI keys — hard-coded на backend, фронт получает все ключи.

## References

- Phase 3b spec: `docs/superpowers/specs/2026-05-10-tg-mini-app-phase3b-design.md`
- Existing helpers:
  - `scheduler.send_manual_compose(thread_id, ru_text)` (CLAUDE.md says it exists)
  - `db.start_thread_autopilot(thread_id, floor_eur, notify_mode, started_by)` (Phase 1 spec docs)
  - `db.stop_thread_autopilot(thread_id, reason)`
  - `telegram_bot.send_autopilot_start_notification(...)` / `_progress` / `_stop`
  - `config.get(key)` / `db.set_setting(key, value)`
  - `config.DEFAULTS` — fallback values
