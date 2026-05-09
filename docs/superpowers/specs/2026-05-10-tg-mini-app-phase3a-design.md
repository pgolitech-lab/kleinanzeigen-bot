# Telegram Mini App — Phase 3a (Read-only review card) — design

**Status:** approved (2026-05-10)
**Scope:** оператор открывает тред в MA → видит pending-draft блок (RU/DE/back-translation/deal_brief) и lock-state в header. Никаких mutating-действий — только read. Фундамент под Phase 3b где появятся send/skip/edit/regenerate.

## Цель

Phase 2 закрыл read-only обзор переписки (chat-style лог). Phase 3a добавляет видимость самого важного: **что бот собирается отправить дальше**. Оператор видит draft (RU + DE + точный обратный перевод), deal-brief (что торг показал), и кто сейчас работает с этой карточкой (lock).

Это minimum viable «обзор review-карточки» — без действий. Действия добавит 3b.

## Non-goals для 3a

- Никаких POST endpoint'ов на `/api/ma/messages/{id}/*` (защищено отсутствием маршрута; если frontend случайно зовёт — 405).
- Никакого автоматического acquire lock-а при открытии экрана. Lock state читается, но не захватывается.
- Никаких action-кнопок (send/skip/edit/regenerate/sold). Action-grid не рендерится.
- Никакого `_broadcast_card` интегрирования с MA — это под 3b.
- Никакого compose / autopilot UI — Phase 4.

## Архитектура (в дополнение к Phase 1-2)

```
Frontend (web-app):
  thread.js (Phase 2 baseline)
    + lock badge в header (если занято)
    + pending-draft block (если есть pending)
    Загружает: /api/ma/threads/{id} (Phase 2 endpoint)
    Дополнительно: /api/ma/messages/{latest_pending_msg_id}

Backend (web/api_ma.py):
  GET /api/ma/messages/{id}        ← НОВОЕ: review payload
  GET /api/ma/messages/{id}/lock   ← НОВОЕ: lock-state poll endpoint

Backend (modules/operator_lock.py): ← НОВЫЙ модуль
  state(msg_id) -> tuple|None     # текущий holder (auto-expire через 300s)
  remember(msg_id, actor) -> None # низкоуровневый set
  forget(msg_id) -> None          # низкоуровневый del
  remaining_min(msg_id) -> int    # для UI

Backend (modules/telegram_bot.py): ← MODIFIED (без поведенческих изменений)
  Удаляются модульные _THREAD_LOCKS dict + LOCK_TIMEOUT_SEC
  Импортируется operator_lock
  Wrappers _acquire_lock/_release_lock/_check_lock/_lock_remaining_min
   делегируют примитивам, сохраняют orchestration thread_busy + drain_deferred
```

## operator_lock module (рефакторинг)

Цель: вынести dict из `telegram_bot.py:861` в общий модуль, чтобы Phase 3b мог его читать/писать из `web/api_ma.py`.

**Новый файл `modules/operator_lock.py`:**

```python
"""Shared in-memory lock на review-карточку (msg_id).

Используется и telegram_bot, и web/api_ma. Bot wrappers
(_acquire_lock/_release_lock в telegram_bot.py) держат orchestration
с thread_busy + drain_deferred — здесь только примитивы.
"""
from __future__ import annotations
import time

LOCK_TIMEOUT_SEC = 300

# msg_id -> (actor_str, acquired_at_unix)
_LOCKS: dict[int, tuple[str, float]] = {}


def state(msg_id: int) -> tuple[str, float] | None:
    """Текущий holder, None если свободно или auto-expired."""
    e = _LOCKS.get(msg_id)
    if e is None:
        return None
    if time.time() - e[1] > LOCK_TIMEOUT_SEC:
        _LOCKS.pop(msg_id, None)
        return None
    return e


def remember(msg_id: int, actor: str) -> None:
    """Низкоуровневый set."""
    _LOCKS[msg_id] = (actor, time.time())


def forget(msg_id: int) -> None:
    """Низкоуровневый del."""
    _LOCKS.pop(msg_id, None)


def remaining_min(msg_id: int) -> int:
    """Минут до auto-release (для UI)."""
    e = state(msg_id)
    if e is None:
        return 0
    age = time.time() - e[1]
    return max(0, int((LOCK_TIMEOUT_SEC - age) // 60) + 1)
```

**Изменения в `modules/telegram_bot.py`** (~строки 858-960):

- Удалить:
  ```python
  _THREAD_LOCKS: dict[int, tuple[str, datetime]] = {}
  LOCK_TIMEOUT_SEC = 300
  ```
- Добавить вверху файла (рядом с другими `from modules import ...`):
  ```python
  from modules import operator_lock
  ```
- `_check_lock(msg_id, actor)` — переписать через `operator_lock.state()`:
  ```python
  def _check_lock(msg_id: int, actor: str) -> Optional[str]:
      """Возвращает имя текущего владельца если лок занят ДРУГИМ. None если свободно/мой."""
      st = operator_lock.state(msg_id)
      if st is None:
          # Lock мог только что auto-expire-нуть — синхронизируем busy-flag.
          clear_thread_busy(_msg_thread_id(msg_id), "operator")
          return None
      owner, _ = st
      if owner == actor:
          return None
      return owner
  ```
- `_acquire_lock(msg_id, actor)`:
  ```python
  def _acquire_lock(msg_id: int, actor: str) -> None:
      operator_lock.remember(msg_id, actor)
      mark_thread_busy(_msg_thread_id(msg_id), "operator", actor)
  ```
- `_release_lock(msg_id)`:
  ```python
  def _release_lock(msg_id: int) -> None:
      operator_lock.forget(msg_id)
      thread_id = _msg_thread_id(msg_id)
      clear_thread_busy(thread_id, "operator")
      if thread_id:
          try:
              import scheduler as _sched
              _sched.drain_deferred_thread(thread_id)
          except Exception:
              logger.exception("drain_deferred_thread fail")
  ```
- `_lock_remaining_min(msg_id)`:
  ```python
  def _lock_remaining_min(msg_id: int) -> int:
      return operator_lock.remaining_min(msg_id)
  ```

**Поведенческое отличие** (намеренное, мелкое): когда `_check_lock` находит auto-expired lock через `operator_lock.state()` (которая сама удаляет dict-entry), мы дополнительно зовём `clear_thread_busy`. Это сейчас делается как side-effect внутри старого `_check_lock` — после refactor разделено, но эффект тот же.

**Тесты `tests/test_operator_lock.py`:**

```python
def test_state_returns_none_when_empty(): ...
def test_remember_and_state_roundtrip(): ...
def test_state_returns_none_after_timeout(): ...    # mock time.time
def test_state_purges_expired_entry(): ...          # после первого state-call dict empty
def test_forget_idempotent(): ...
def test_remaining_min_decreases_over_time(): ...
```

## Backend endpoints (Phase 3a)

### `GET /api/ma/messages/{msg_id}`

Полный review payload — всё что нужно frontend'у для рендера pending-draft секции.

**Response (200):**
```json
{
  "msg_id": 123,
  "thread_id": "abc",
  "status": "pending",
  "ad": {
    "title": "Sitzbank, Sitze Peugeot",
    "price": "1500€",
    "url": "https://www.kleinanzeigen.de/s-anzeige/.../2812345678",
    "id": "2812345678",
    "buyer_display_name": "Osman",
    "buyer_email": "osman@gmx.de"
  },
  "client_lang": "de",
  "client_message": {
    "raw": "Können Sie 1300?",
    "ru": "Можете уступить до 1300?"
  },
  "draft": {
    "ru_answer": "Минимум 1400, при самовывозе...",
    "de_answer": "Mindestens 1400€, bei Selbstabholung... MfG",
    "ru_translation": "Минимум 1400€, при самовывозе... С уважением"
  },
  "deal_brief": {
    "summary_ru": "Клиент торгуется, готов до 1300",
    "expected_next": "ждём ответ",
    "negotiated_price_eur": 1300,
    "client_assessment": "серьёзный"
  },
  "related": {
    "buyer_display_name": "Osman",
    "matches": [{"thread_id":"...", "ad_title":"...", "ad_price":"...", "last_at":"..."}]
  },
  "lock": {"holder": null, "remaining_min": 0},
  "autopilot": {"active": false, "messages_sent": 0, "floor_eur": null, "notify_mode": null},
  "extra_notes": null,
  "is_auto_ack": false
}
```

**Errors:**
- 401 invalid initData
- 403 user not in `telegram_authorized_ids()`
- 404 msg_id не существует
- 422 missing X-Telegram-Init-Data header

Endpoint возвращает payload для любой существующей row (включая `direction='out'` если такой msg_id запрошен — поля `ad`/`client_message`/`draft` могут быть partially-null, frontend handle).

**Implementation:**

Тонкая обёртка над:
- `db.get_message(msg_id)` (существует)
- `db.find_related_inquiries(buyer_display_name, exclude_thread_id, limit=10)` (существует)
- `db.get_thread_autopilot(thread_id)` (существует)
- `operator_lock.state(msg_id)` (новое)
- `json.loads(deal_brief_json)` если есть

`deal_brief` парсится из `messages.deal_brief_json` (TEXT). Если null или невалидный JSON → возвращаем `null`.

`related.matches` — формат тот же что в Phase 2 `/threads/{id}` related (повторно используем `_related_match` хелпер из api_ma).

### `GET /api/ma/messages/{msg_id}/lock`

Лёгкий poll endpoint — только lock state, без всего review payload. В Phase 3b frontend может polling-ить каждые 5 сек чтобы видеть live-обновление "🟥 занято @other".

**Response (200):**
```json
{"holder": null, "remaining_min": 0}
```
или
```json
{"holder": "@PG2202#242994225", "remaining_min": 4}
```

**Errors:** 401/403/422 как у предыдущего. 404 если msg_id не существует.

`actor` в боте формируется как `@username#user_id` или `first_name#user_id` — формат уже устоявшийся, используем как есть.

### Тесты `tests/test_api_ma_messages.py`

```python
def test_message_review_returns_full_payload(): ...
def test_message_review_404_when_missing(): ...
def test_message_review_lock_holder_when_held(): ...
def test_message_review_deal_brief_parses_json(): ...
def test_message_review_invalid_deal_brief_json_returns_null(): ...
def test_message_lock_state_returns_holder(): ...
def test_message_lock_state_returns_null_when_free(): ...
def test_message_lock_404_when_msg_missing(): ...
def test_messages_require_auth(): ...   # 422 на оба endpoint'a
```

## Frontend changes — `web-app/js/screens/thread.js`

Минимально-инвазивные изменения к Phase 2 thread.js. Сохраняем существующий `render(mount, params)` shape.

### Новый flow внутри `render`:

```javascript
export async function render(mount, params) {
  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);

    // Найти latest pending msg_id (если есть)
    const pendingStatuses = new Set(["pending", "new", "edited", "approved"]);
    const latestPendingMsgId = findLatestPending(data.events, pendingStatuses);

    // Если есть pending — fetch'им review payload параллельно
    let review = null;
    if (latestPendingMsgId !== null) {
      try {
        review = await api(`/api/ma/messages/${latestPendingMsgId}`);
      } catch (e) {
        console.warn("[thread] review fetch failed:", e);
        // Не фатально — рендерим без pending-draft секции
      }
    }

    const container = el(`<div></div>`);
    container.appendChild(threadHeader(data.header, review?.lock));
    const related = relatedBlock(data.related);
    if (related) container.appendChild(related);
    if (data.events.length === 0) {
      container.appendChild(el(`<p class="text-muted">Событий пока нет.</p>`));
    } else {
      data.events.forEach(e => container.appendChild(eventBubble(e, latestPendingMsgId)));
    }
    if (review) {
      container.appendChild(pendingDraftBlock(review));
    }
    container.appendChild(backToPipeline());
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

### Helper: `findLatestPending(events, statuses)`

```javascript
function findLatestPending(events, statuses) {
  let candidate = null;
  for (const ev of events) {
    if (ev.kind === "in" && ev.status && statuses.has(ev.status) && ev.msg_id) {
      // events отсортированы хронологически (по ts ASC) — последний overrides
      candidate = ev.msg_id;
    }
  }
  return candidate;
}
```

### Helper: `pendingDraftBlock(review)`

Рендерит секцию с `📝 НАШ ОТВЕТ #N (черновик)`. Все динамические данные — через `.textContent` (XSS-safe).

```javascript
function pendingDraftBlock(review) {
  const block = el(`
    <div class="border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold draft-header"></div>
      <div class="ru-answer-block mb-2">
        <div class="text-muted small">RU (идея от GPT)</div>
        <div class="ru-answer p-2 rounded bg-secondary-subtle"></div>
      </div>
      <div class="de-answer-block mb-2">
        <div class="text-muted small">DE → клиенту</div>
        <div class="de-answer p-2 rounded bg-primary-subtle"></div>
      </div>
      <div class="ru-translation-block mb-2">
        <div class="text-muted small">RU обратный перевод (для верификации)</div>
        <div class="ru-translation p-2 rounded fst-italic small"></div>
      </div>
      <div class="deal-brief-block text-muted small"></div>
    </div>
  `);
  block.querySelector(".draft-header").textContent =
    `📝 Наш ответ #${review.msg_id} (черновик · ${review.status})`;
  block.querySelector(".ru-answer").textContent = review.draft?.ru_answer ?? "";
  block.querySelector(".de-answer").textContent = review.draft?.de_answer ?? "";
  block.querySelector(".ru-translation").textContent = review.draft?.ru_translation ?? "";

  const brief = review.deal_brief;
  if (brief) {
    const parts = [];
    if (brief.summary_ru) parts.push(`💬 ${brief.summary_ru}`);
    if (brief.negotiated_price_eur) parts.push(`💰 торг: ${brief.negotiated_price_eur}€`);
    if (brief.client_assessment) parts.push(`🏷 ${brief.client_assessment}`);
    if (brief.expected_next) parts.push(`⏳ ${brief.expected_next}`);
    block.querySelector(".deal-brief-block").textContent = parts.join(" · ");
  } else {
    block.querySelector(".deal-brief-block").remove();
  }

  return block;
}
```

### Helper: `threadHeader` обновляется (lock badge)

Существующий Phase 2 `threadHeader(header)` дополняется до `threadHeader(header, lock)`. Новый параметр опциональный — `null/undefined` если lock-state не получен. К существующей разметке (которая уже формирует card с `.ad-title`, `.ad-price-buyer`, `.account-info`, `.autopilot-badge`, `.ad-link`) добавляется badge:

```javascript
function threadHeader(header, lock) {
  const card = el(`
    <div class="border-bottom pb-2 mb-3">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <div class="fw-semibold ad-title"></div>
          <div class="text-muted small ad-price-buyer"></div>
          <div class="text-muted small account-info"></div>
          <div class="lock-slot"></div>
        </div>
        <div class="text-end small">
          <div class="autopilot-badge"></div>
          <a class="ad-link" target="_blank" rel="noopener">📎</a>
        </div>
      </div>
    </div>
  `);
  card.querySelector(".ad-title").textContent = header.ad_title ?? "(без названия)";
  card.querySelector(".ad-price-buyer").textContent =
    `${header.ad_price ?? "?"} · 👤 ${header.buyer_display_name ?? "?"}${header.buyer_email ? ` · ${header.buyer_email}` : ""}`;
  card.querySelector(".account-info").textContent =
    `🏪 ${header.account_name ?? "?"} (${header.account_email ?? "?"})`;
  if (header.ad_url) {
    card.querySelector(".ad-link").href = header.ad_url;
  } else {
    card.querySelector(".ad-link").remove();
  }
  if (header.is_autopilot) {
    const b = el(`<span class="badge bg-warning text-dark">🤖 автопилот</span>`);
    card.querySelector(".autopilot-badge").appendChild(b);
  }
  if (lock?.holder) {
    const badge = el(`<div class="text-danger small mt-1 lock-badge">🟥 в работе у <span class="holder"></span> (<span class="mins"></span> мин)</div>`);
    badge.querySelector(".holder").textContent = lock.holder;
    badge.querySelector(".mins").textContent = String(lock.remaining_min);
    card.querySelector(".lock-slot").appendChild(badge);
  }
  return card;
}
```

(Это Phase 2 версия плюс одна вставка — `.lock-slot` div + блок добавления `.lock-badge` если `lock.holder`.)

### Helper: `eventBubble` принимает latestPendingMsgId для маркера 📝

В существующий event bubble — если `event.kind === "in" && event.msg_id === latestPendingMsgId && pendingStatuses.has(event.status)` — добавляем маленький маркер 📝 рядом с временем.

### Cache-bust

Bump `?v=20260510-7` во всех ESM-импортах + index.html.

### Тестирование

JS syntax check (node --input-type=module --check). E2E в TG: открыть тред с pending → должен показать pending-draft блок и (если занят кем-то ещё) lock-badge.

Phase 3b enhancement: action-grid под pending-draft block будет добавлен под отдельным feature-флагом или просто следующим коммитом.

## Risks для 3a

- **`db.get_thread_autopilot` уже добавлен в Phase 2** — переиспользуем.
- **`db.find_related_inquiries` принимает display_name str | None** — None возвращает []. OK.
- **`messages.deal_brief_json` может быть NULL/empty/invalid** — обернём в try/except.
- **`messages.ru_translation` колонка добавлена недавно (Phase 1 era)** — для старых row может быть None. Frontend handle через nullish coalescing.
- **`actor` формат для lock** — bot использует `@username#user_id` или `first_name#user_id`. Frontend просто отображает строку.
- **Multi-pending в треде** — `findLatestPending` возвращает последний по хронологии. Старые pending видны в логе с маркером 📝 но не получают свой draft-block. Это сознательное упрощение для 3a; deep-link на конкретный msg_id появится в Phase 3b.

## Что точно НЕ делаем в 3a

- POST endpoints (acquire/release/send/skip/sold/regenerate/edit-ru/edit-de/price/instruction)
- Action-grid в UI
- Edit-inline pattern
- Auto-acquire lock при открытии экрана
- `_broadcast_card` интеграция с MA
- Deep-link `/review/{msg_id}` (отдельная страница для конкретного pending)
- Compose / autopilot config
- Settings screen

## References

- Phase 1 spec: `docs/superpowers/specs/2026-05-09-tg-mini-app-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-05-09-tg-mini-app-phase1.md`
- Phase 2 plan: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase2.md`
- Bot lock current location: `modules/telegram_bot.py:858-960`
- Bot existing helper functions: `_acquire_lock`, `_release_lock`, `_check_lock`, `_lock_remaining_min`, `mark_thread_busy`, `clear_thread_busy`, `_msg_thread_id`
- DB helpers reused: `db.get_message`, `db.find_related_inquiries`, `db.get_thread_autopilot`, `db.thread_events`, `db.thread_history`
