# Phase 4.5 — UX polish (lock-self / suggest-reply / autopilot preview) — design

**Status:** approved (2026-05-10)
**Scope:** три UX-улучшения по фидбэку с production: (1) исправить lock-self-display bug, (2) добавить «🤖 Предложить ответ» для тредов без pending, (3) превью первого ответа автопилота до старта.

## 1. Lock-self-display fix

**Problem:** Когда оператор открывает review-screen и MA auto-acquire берёт lock, lock-state в payload показывает holder=ourselves. Frontend в `threadHeader` рендерит badge `🟥 в работе у @ourselves (5 мин)` — что бессмысленно (мы и так знаем что мы здесь).

**Fix:**
- В `screens/thread.js` хранить `_ourActor` после auto-acquire (lockRes.holder)
- В `threadHeader(header, lock, ourActor)` показывать badge только если `lock.holder && lock.holder !== ourActor`
- ourActor доступен в render-closure через captured переменную

## 2. «🤖 Предложить ответ» feature

**Problem:** На тредах без активного pending draft (всё уже отправлено / отклонено) оператор хочет попросить ИИ предложить новый ответ к последнему incoming. Сейчас единственный путь — Compose, но там оператор сам пишет.

**Backend:** новый endpoint

`POST /api/ma/threads/{thread_id}/suggest-reply` (auth required)

**Behavior:**
1. Найти latest `direction='in'` row в треде (через `db.thread_history` + filter).
2. Если такой row нет → 404 «no incoming to reply to».
3. Через `asyncio.to_thread(scheduler.regenerate_draft_for, in_msg_id)` или эквивалентный existing helper → backend генерит fresh draft (full schema: ru_answer + de_answer + ru_translation + deal_brief).
4. Обновить row статус на `pending`, заполнить fields.
5. `telegram_bot.broadcast_after_external_action(in_msg_id)` — обновить TG-карточки.
6. Return `_thread_dict(thread_id)` (full thread payload — frontend re-renders).

**Реализационный нюанс:** существующий helper `scheduler.regenerate_draft_for_message(msg_id)` (или подобный) — используется в callback handlers. Если не существует — придётся написать обёртку на основе `claude.generate_reply` + `db.update_message`.

**Frontend:** в `screens/thread.js` `backRow(threadId, onCompose, onSuggest)` имеет 3 кнопки: `↩ К pipeline`, `✉️ Написать клиенту`, `🤖 Предложить ответ`. Tap «🤖 Предложить» → spinner → re-render thread с pending-draft + action-grid.

**Tests:** ~3 (success, 404 no incoming, requires auth).

## 3. Autopilot preview

**Problem:** Оператор хочет видеть какой ответ AI-автопилот сгенерирует ПЕРВЫМ перед тем как нажать старт. Потенциально rejecting/regenerating перед commitment.

**Backend:** новый endpoint

`POST /api/ma/threads/{thread_id}/autopilot/preview` (auth required)

Body: `{floor_eur: float, notify_mode: "silent"|"notify"}`

**Behavior:**
1. Найти latest `direction='in'` row.
2. Через `asyncio.to_thread(claude.generate_autopilot_reply, msg_row, floor_eur=..., last_our_price_eur=...)` — генерит preview reply (использует Sonnet+web_search, может стоить ~5-10s).
3. **НЕ сохраняем в db** — просто возвращаем frontend'у:
   ```json
   {
     "preview": {
       "ru_text": "...",
       "client_text": "...",
       "ru_translation": "...",
       "deal_brief": {...}
     }
   }
   ```

**Real start flow change:**

Существующий `POST /autopilot/start {floor_eur, notify_mode}` модифицируется — добавляется опциональное поле `{preview_text: str | null}`.

Если `preview_text` передан:
1. Найти latest in-row.
2. Update db: `de_answer=preview_text`, `status='approved'`, плюс ru_translation если есть в preview-payload.
3. Затем `db.start_thread_autopilot(...)`.
4. Scheduler `send_approved_replies` подхватит и отправит первый ответ на следующем цикле.

Если `preview_text` не передан — start работает как сейчас (autopilot сам генерит на следующий incoming).

**Frontend:** `autopilot-form.js`:
- После floor + notify radios, новая секция «Превью первого ответа»
- Кнопка `👁 Сгенерировать превью` → POST /autopilot/preview → показать blocks (RU/DE/back-translate) + кнопки `🔁 Другой вариант` (повторный POST preview) / `🚀 Старт с этим ответом` (POST /autopilot/start с preview_text=client_text + ru_translation)
- Если оператор не сгенерил preview — обычный «🚀 Старт» работает как сейчас (без preview, autopilot сам начнёт со следующего incoming)

**Tests:** ~4 (preview success, preview no incoming → 404, start with preview_text updates db, start without preview_text works as before).

## File Structure

**Modified:**
- `web/api_ma.py` — +2 endpoints (`/suggest-reply`, `/autopilot/preview`) + расширение `/autopilot/start` под opt'ный `preview_text`
- `web-app/js/screens/thread.js` — backRow с 3 кнопками + ourActor tracking + lock-badge guard
- `web-app/js/components/autopilot-form.js` — preview-секция

**New tests:**
- `tests/test_api_ma_suggest_reply.py` (3 tests)
- `tests/test_api_ma_autopilot_preview.py` (4 tests)
- + 1 modified test for `/autopilot/start` accepting preview_text

## Risks

- **`scheduler.regenerate_draft_for_message`** — если такого helper'а нет, нужно либо найти эквивалент в scheduler.py, либо написать новый. Plan task должен grep-нуть.
- **`claude.generate_autopilot_reply` сигнатура** — preview endpoint вызывает её. Нужно verify args.
- **autopilot preview cost** — Sonnet+web_search ~$0.05-0.20 per call. Если оператор кликает «другой вариант» — каждый клик стоит. Не критично для personal use.
- **Lock fix** — простой, но важно проверить что `lock.holder !== ourActor` сравнение строки идёт buffer-safe.

## Что НЕ делаем

- Persist preview text за пределами sessions — preview всегда live-genned.
- Multi-preview history — нет «прошлый preview vs новый».
- Edit preview перед стартом — only regenerate, not edit (если хотите поправить — старт без preview, потом edit-RU/DE как обычно).

## Acceptance

1. На review-screen `🟥 lock-badge` показывается ТОЛЬКО когда holder ≠ self.
2. На любом thread (с pending или без) виден `🤖 Предложить ответ` button → генерит fresh draft → action-grid появляется.
3. В autopilot start form появляется секция preview → tap «👁 Сгенерировать» → показывает текст → tap «🚀 Старт с этим» → autopilot started + preview applied as first approved reply.
4. ~104 unit-тестов проходят (97 + 7 new).

## References

- Phase 4 plan: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase4.md`
- claude.generate_reply / regenerate_with_strategy / generate_autopilot_reply (claude.py)
