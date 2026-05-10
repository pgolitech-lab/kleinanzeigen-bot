# Telegram Mini App — Phase 3b (Action grid + lock + broadcast) — design

**Status:** approved (2026-05-10)
**Scope:** оператор в MA берёт lock, выполняет любое из 9 действий (send/skip/sold/regenerate{strategy}/edit-ru/edit-de/price/instruction), TG-карточки бота обновляются через broadcast. Чтение payload — Phase 3a; здесь только мутации и UI вокруг них.

## Цель

Phase 3a показал что мы хотим отправить. Phase 3b даёт оператору кнопки чтобы это **сделать**: подтвердить и отправить, отклонить, попросить регенерацию с другой стратегией, поправить текст вручную.

## Non-goals для 3b

- Compose action (✉️ Написать клиенту, operator-initiated reply) — Phase 4
- Autopilot start/stop UI — Phase 4
- Settings screen — Phase 4
- Live polling lock-state каждые 5 сек — пока polling только при retry
- Reminder card в MA — не относится к review

## Архитектура

```
Frontend (web-app/js/screens/thread.js):
  Phase 3a baseline (header, events, pending-draft block)
  + auto-acquire lock на mount (если есть pending msg_id)
  + action-grid под pending-draft block
  + state-machine: idle / confirm-pending / editing / loading / locked / error
  + auto-release lock на unmount (pagehide / visibilitychange / hashchange)

Backend (web/api_ma.py):
  POST /api/ma/messages/{id}/lock/acquire    ← 200 {holder, remaining_min} | 409
  POST /api/ma/messages/{id}/lock/release    ← 204
  POST /api/ma/messages/{id}/send            ← 200 {ok, status}
  POST /api/ma/messages/{id}/skip            ← 200
  POST /api/ma/messages/{id}/sold            ← 200
  POST /api/ma/messages/{id}/regenerate      ← 200 {review_payload}
  POST /api/ma/messages/{id}/edit-ru         ← 200 {review_payload}
  POST /api/ma/messages/{id}/edit-de         ← 200 {review_payload}
  POST /api/ma/messages/{id}/price           ← 200 {review_payload}
  POST /api/ma/messages/{id}/instruction     ← 200 {review_payload}

Backend (modules/telegram_bot.py):
  + broadcast_after_external_action(msg_id) — НОВОЕ public async helper
    1. читает row из db
    2. строит text + keyboard используя существующие _format_review_text + _review_keyboard
    3. dispatches через _broadcast_card(_app_context, msg_id, text, kb)

Backend (web/api_ma.py helpers):
  + actor_from_user(user) — формат строки holder'а из TG initData user
```

## Lock endpoints (POST acquire/release)

### Actor format

Actor строка для lock — формируется из TG initData user.id и username/first_name:

```python
def actor_from_user(user: dict) -> str:
    """Формат actor совпадает с тем что бот использует в callback handlers."""
    uid = user.get("id")
    name = user.get("username") or user.get("first_name") or "?"
    prefix = "@" if user.get("username") else ""
    return f"{prefix}{name}#{uid}"
```

Пример: оператор `username='PG2202', id=242994225` → actor = `@PG2202#242994225`. Если только first_name — `Pg#242994225`.

### `POST /api/ma/messages/{msg_id}/lock/acquire`

**Request body:** пусто.

**Behavior:**
1. Проверить msg_id существует — иначе 404.
2. Получить actor из initData user.
3. Вызвать `telegram_bot._check_lock(msg_id, actor)`:
   - Возвращает None → lock свободен или мой → `telegram_bot._acquire_lock(msg_id, actor)` → 200 `{holder: actor, remaining_min: 5}`
   - Возвращает имя другого actor → 409 `{holder: <other>, remaining_min: <int>}`

**Note:** зовём именно bot's `_check_lock`/`_acquire_lock` wrappers (а не низкоуровневый `operator_lock.remember`) чтобы получить корректную orchestration с `mark_thread_busy` + (на release) `drain_deferred_thread`. Это критично для interoperability с polling job-ом.

### `POST /api/ma/messages/{msg_id}/lock/release`

**Request body:** пусто.

**Behavior:**
1. Проверить msg_id — иначе 404.
2. Вызвать `telegram_bot._release_lock(msg_id)` (permissive — кто угодно может release).
3. → 204 No Content.

**Permissive release rationale:** мы доверяем frontend'у не вызывать release когда lock не наш. Если злоупотребление — bot's auto-expire (5 мин) защитит. Альтернатива (проверять holder) усложнит код без значимого выигрыша.

### Тесты `tests/test_api_ma_lock_actions.py` (acquire/release)

- `test_lock_acquire_succeeds_when_free` (200 + holder=us)
- `test_lock_acquire_409_when_held_by_other` (409 + holder=other)
- `test_lock_acquire_succeeds_when_already_held_by_self` (200, не creates double)
- `test_lock_acquire_404_when_msg_missing` (404)
- `test_lock_release_succeeds` (204)
- `test_lock_release_idempotent` (204 даже если не holder)

## Action endpoints (9 шт)

Общий контракт каждого endpoint'а:
1. `Depends(verify_init_data_dep)` — auth
2. `db.get_message(msg_id)` — 404 если отсутствует
3. `actor = actor_from_user(user)` — формирует строку для lock
4. `holder = telegram_bot._check_lock(msg_id, actor)` — если возвращает имя ≠ actor → 409 `{detail: "locked by @other", holder, remaining_min}`. Если None — мы holder OR lock свободен; запускаем acquire (idempotent для нас).
5. **Run action** (sync work через `asyncio.to_thread()` где applicable).
6. `telegram_bot.broadcast_after_external_action(msg_id)` — обновить TG-карточки (best-effort, log error).
7. **For final actions** (send/skip/sold) — `telegram_bot._release_lock(msg_id)`. Для intermediate (regenerate/edit/price/instruction) — lock остаётся за нами.
8. Return — для final: `{ok: true, status: <new>}`; для intermediate: fresh review payload (то же что GET /api/ma/messages/{id}).

### Список endpoint'ов

**`POST /messages/{id}/send`**
- Action: `result = await asyncio.to_thread(scheduler.send_one, msg_id)`
- Если `result["kind"] == "error"` → 500 с deatil из result
- На success: лок освобождаем (final).
- Response: `{ok: true, status: row["status"]}` (sent / sent_debug / skipped — зависит от send_mode).

**`POST /messages/{id}/skip`**
- Action: `db.update_message(msg_id, status="skipped")`
- Final → release lock.
- Response: `{ok: true, status: "skipped"}`.

**`POST /messages/{id}/sold`**
- Action: `db.update_message(msg_id, status="skipped_sold")`. Также `db.update_ad_brief_sold(ad_id)` если есть `ad_briefs.sold_at` helper (бот это делает); если нет — пропускаем (не критично для MA).
- Final → release lock.
- Response: `{ok: true, status: "skipped_sold"}`.

**`POST /messages/{id}/regenerate`** body: `{strategy: "harsh"|"friend"|"short"|"regen"|"fest"}`
- Strategy whitelist жёсткий — invalid → 422.
- Action: подгружаем history + lessons как делает бот (helper `_load_regen_context(msg_row)` или similar — реюзаем существующий из scheduler/telegram_bot). Затем `await asyncio.to_thread(claude.regenerate_with_strategy, row, strategy, ...)` → возвращает обновлённые поля.
- Update db через возвращённый dict (как делает bot regenerate handlers).
- Intermediate → lock keeps.
- Response: fresh review payload.

**`POST /messages/{id}/edit-ru`** body: `{text: str}`
- Validate: text не пустой, не более 4000 chars. Иначе 422.
- Action: `de_answer = await asyncio.to_thread(claude.translate_only, text, source_lang="ru", target_lang=row["client_lang"])`.
  Затем `ru_translation = await asyncio.to_thread(claude.translate_only, de_answer, source_lang=row["client_lang"], target_lang="ru")` (back-translate для верификации).
  `db.update_message(msg_id, ru_answer=text, de_answer=de_answer, ru_translation=ru_translation, status='edited')`.
- Intermediate → lock keeps.
- Response: fresh review payload.

**`POST /messages/{id}/edit-de`** body: `{text: str}`
- Validate: text не пустой, не более 4000 chars.
- Action: `de_answer = text` (as-is, no translation forward). 
  `ru_translation = await asyncio.to_thread(claude.translate_only, text, source_lang=row["client_lang"], target_lang="ru")`.
  `db.update_message(msg_id, de_answer=text, ru_translation=ru_translation, status='edited')`. `ru_answer` НЕ трогаем (он остаётся «идеей от Sonnet»).
- Intermediate → lock keeps.
- Response: fresh review payload.

**`POST /messages/{id}/price`** body: `{eur: float}`
- Validate: eur is positive number, < 100000. Иначе 422.
- Action: same context-loading как для regenerate. `await asyncio.to_thread(claude.regenerate_with_price, row, eur, ...)` → updates db.
- Intermediate → lock keeps.
- Response: fresh review payload.

**`POST /messages/{id}/instruction`** body: `{text: str}`
- Validate: text не пустой, не более 1000 chars.
- Action: same context-loading. `await asyncio.to_thread(claude.regenerate_with_instruction, row, text, ...)` → updates db.
- Intermediate → lock keeps.
- Response: fresh review payload.

## Bot ↔ MA broadcast (синхронизация TG-карточек)

После каждого action endpoint в MA — карточка в TG-боте должна отразить новое состояние (или финальное «отправлено»).

### Новый helper в `modules/telegram_bot.py`

```python
async def broadcast_after_external_action(msg_id: int) -> None:
    """Обновить все DM-копии review-карточки после внешнего (MA) действия.

    Читает свежий row, строит text+keyboard используя существующие helpers
    (_format_review_text + _review_keyboard / финальная клавиатура для terminal-state),
    dispatches через _broadcast_card.

    Best-effort: если bot Application ещё не запущен или edit fails — log warning, не падаем.
    """
```

### Доступ к bot Application

Внутри `telegram_bot.py` есть `build_application()` который создаёт `Application`. После старта main.py хранит ссылку (или helper-функция возвращает singleton). Реализация в плане — проверить как сейчас доступен app context из других мест (scheduler.py зовёт `telegram_bot.send_for_review(msg_id)` — значит механизм доступа уже есть). Скорее всего модульный `_app: Optional[Application] = None`, set'ится при старте, читается helper'ами.

**Note:** broadcast — best-effort. Если bot не запущен (тест, dev) — лог + возврат. Action всё равно завершён.

## Frontend — расширение `thread.js`

### Lifecycle

```
mount /thread/{id}:
  → fetch /api/ma/threads/{id}
  → если есть pending: fetch /api/ma/messages/{pending_id}
  → если pending && status позволяет (pending/new/edited/approved):
      → POST /api/ma/messages/{pending_id}/lock/acquire
        → 200: render full UI (action-grid enabled)
        → 409: render lock-conflict UI (banner + retry button, no action-grid)
        → 5xx/network: render с warning, без action-grid
  → если нет pending: render как Phase 2 (read-only thread, no action-grid)

unmount /thread/{id}:
  → если we held lock: POST /api/ma/messages/{pending_id}/lock/release (best-effort)

Listeners:
  - hashchange (route navigation away)
  - pagehide (TG WebApp close)
  - visibilitychange to "hidden"
```

### Action-grid component

**File:** `web-app/js/screens/thread.js` (расширяем) или новый `web-app/js/components/action-grid.js` (модуль) — по предпочтению. План определит.

**Layout (sticky-bottom):**
```
[ ✅ ОТПРАВИТЬ ]                        ← full-width, primary
[✏️ Правка RU] [✏️ Правка DE]
[💎 Без торга]│[💸 Своя цена]
[👊 Жёстче]   │[☺️ Мягче]
[✂️ Короче]   │[🔁 Переформ.]
[📝 Своя инстр.]│[❌ Пропустить]
[💰 Продано]                            ← full-width, danger style
```

(Phase 2 «📋 История» убираем — thread-detail и есть история, кнопка не нужна.)

### State machine

States:
- `idle`: action-grid рендерит все кнопки enabled
- `confirm-pending {action_key}`: tapped кнопка morphит в `⚠️ <label>? [Да] [Нет]` inline; остальные кнопки `aria-disabled` + visual dim. Tap [Нет] / 5-сек timeout → revert to idle. Tap [Да] → fire action.
- `editing {field: ru|de|price|instruction}`: pending-draft block sub-section этого field превращается в textarea/input + `[💾 Сохранить][✕ Отмена]`. Action-grid hidden. Save → POST → re-render fresh data → idle. Cancel → revert.
- `loading`: spinner overlay + grid disabled (для regenerate / send / skip / sold операции в полёте).
- `locked-by-other`: банner + retry-кнопка. Action-grid hidden.
- `error`: error toast + idle (action-grid restored).

### Edit-inline rendering

Для `edit-ru`: textarea pre-filled с `review.draft.ru_answer`, рядом save/cancel.
Для `edit-de`: textarea pre-filled с `review.draft.de_answer`.
Для `price`: numeric input (`type=number` step=10), placeholder = текущая цена объявления или 0.
Для `instruction`: textarea (меньше — короткая инструкция), placeholder подсказка.

### XSS guards

Все динамические данные (review.draft.*, lock.holder, deal_brief.*) — через `.textContent` (Phase 1 урок). Action-grid HTML — статика; динамика только через `setAttribute` для `aria-label` / `data-action` (атрибуты — браузер escape-ит автоматически).

## Frontend module structure

Phase 3a имел один `screens/thread.js` (~150 строк). Phase 3b добавит ~300 строк (state machine, action handlers, edit forms). Для compactности предлагаю:

- `screens/thread.js` — render flow, lifecycle (mount/unmount, lock acquire/release)
- `components/action-grid.js` — компонент action-grid + state machine
- `components/edit-form.js` — компоненты для edit-ru/edit-de/price/instruction
- `screens/thread.js` импортирует action-grid и edit-form

Plan может варьировать decision (один файл vs модули) при необходимости.

## Cache-bust

Bump `?v=20260510-8` (или 20260511 если день сменится) во всех ESM импортах + index.html.

## Тесты

### Backend

`tests/test_api_ma_lock_actions.py` (~6 tests):
- acquire когда свободно → 200 + holder=us
- acquire когда уже у нас → 200 (idempotent)
- acquire когда у other → 409
- acquire когда msg missing → 404
- release → 204
- release когда не holder → 204 (permissive)

`tests/test_api_ma_actions.py` (~14 tests, по 1-2 на endpoint):
- send: success → 200 (mock scheduler.send_one); send when SMTP fails → 500
- send: 409 если locked by other
- skip: success → 200, status='skipped'
- sold: success → 200, status='skipped_sold'
- regenerate: valid strategy → 200 + payload; invalid strategy → 422
- edit-ru: success → ru_answer + de_answer + ru_translation updated
- edit-de: success → de_answer set as-is + ru_translation back-translated
- price: valid → 200; invalid (negative/string) → 422
- instruction: valid → 200; empty → 422
- requires_auth: 422 на отсутствие header
- locked_by_other: 409 на любой action endpoint когда foreign holder

Mocks: `web.api_ma.db`, `web.api_ma.scheduler`, `web.api_ma.claude`, `web.api_ma.telegram_bot` (для broadcast и lock wrappers), `web.api_ma.operator_lock`.

### Frontend

JS syntax checks через `node --input-type=module --check`. Visual smoke в TG.

Unit-тесты frontend не делаем (overkill для phase). Тесты пишем для backend поведения.

## Risks / Open questions

- **Bot Application access из MA endpoint** — нужен механизм. План должен:
  1. Проверить как сейчас scheduler/web вызывают `telegram_bot.send_for_review` (наверняка через module-level singleton).
  2. Использовать тот же механизм для `broadcast_after_external_action`.
  3. Если механизма нет — добавить минимальный (set'ить module-level `_app` при `build_application` end).
- **Sync vs async work** — `claude.regenerate_*` делает HTTP request в Anthropic API (~5-10 сек). `scheduler.send_one` — SMTP (~2-3 сек). FastAPI default sync route handlers идут в threadpool, async требует `asyncio.to_thread` для CPU/IO blocking sync work. Все наши endpoint'ы — `async def` с `to_thread()` обёртками.
- **Lock keep on intermediate actions** — после regenerate/edit/price/instruction lock НЕ освобождаем — оператор продолжит работать с карточкой. Auto-expire через 5 мин если он отвлёкся.
- **Concurrent edits** — два MA-сессии одного оператора (десктоп + мобильник) — обе видят свой lock как valid. Ничего не ломаем — mutex по msg_id.
- **Bot pollution** — после MA-action бот может ещё пытаться обработать deferred-задачи треда. `drain_deferred_thread` после release должен сам разрулить.
- **Network races** — оператор тапнул send в MA, через 100ms отвалилась сеть. Action на backend завершился (email ушёл!), но клиент 504. Frontend retry → второй send. Решение: после успешного send статус row=`sent` — повторный POST вернёт 4xx (status уже не actionable). Acceptable для Phase 3b; можем усилить в Phase 4.

## References

- Phase 1 spec: `docs/superpowers/specs/2026-05-09-tg-mini-app-design.md`
- Phase 3a spec: `docs/superpowers/specs/2026-05-10-tg-mini-app-phase3a-design.md`
- Existing helpers:
  - `scheduler.send_one(message_id) -> dict` (scheduler.py:1148)
  - `claude.regenerate_with_strategy(msg_row, strategy, ...)` (claude.py:733)
  - `claude.regenerate_with_price(msg_row, price_eur, ...)` (claude.py:752)
  - `claude.regenerate_with_instruction(msg_row, text, ...)` (claude.py:774)
  - `claude.translate_only(text, source_lang, target_lang)` (claude.py:504)
  - `telegram_bot._broadcast_card(context, msg_id, text, reply_markup)` (telegram_bot.py:945)
  - `telegram_bot._check_lock`, `_acquire_lock`, `_release_lock`, `_lock_remaining_min` (Phase 3a wrappers)
  - `db.update_message(msg_id, **fields)` (database.py)
- Bot's QUICK_STRATEGIES + TWEAK_INSTRUCTIONS (claude.py module-level dicts)
