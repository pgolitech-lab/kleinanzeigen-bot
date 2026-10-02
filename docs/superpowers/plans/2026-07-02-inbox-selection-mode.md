# Inbox Selection Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить режим выборки во Входящих МА — кнопка «Выбрать», чекбоксы на карточках, bulk-действия: закрепить / прочитано / непрочитано / убрать.

**Architecture:** Новая таблица `thread_flags` хранит `is_pinned` и `operator_unread` для каждого треда. `pipeline_threads()` LEFT JOIN-ится к ней и возвращает оба поля. `ma_pipeline()` добавляет раздел `pinned` в ответ. Новый эндпоинт `POST /api/ma/threads/bulk-action` обрабатывает все четыре действия. `pipeline.js` добавляет selection state + fixed action bar.

**Tech Stack:** SQLite (через get_conn()), FastAPI + Pydantic, Vanilla JS ES-modules, Bootstrap 5.

## Global Constraints

- Python: все функции БД в `modules/db_threads.py`, экспорт через `database.py`
- Все новые API-эндпоинты: `Depends(verify_init_data_dep)`, возвращают `dict[str, Any]`
- JS: Vanilla ES-modules, никаких фреймворков; динамические данные только через `.textContent`, не через `innerHTML`
- Версии импортов JS: при изменении файла — bump version-строки в `router.js` и внутри файла

---

## Task 1: DB — таблица thread_flags + хелперы

**Files:**
- Modify: `database.py:355-370` (в `init_db()`, после блока `client_profiles`)
- Modify: `modules/db_threads.py` (добавить `set_thread_flags`, `get_thread_flags` в начало)
- Modify: `database.py` (re-export в конец блока `from modules.db_threads import (...)`)
- Create: `tests/test_db_thread_flags.py`

**Interfaces:**
- Produces:
  - `db.set_thread_flags(gmail_thread_id: str, *, is_pinned: int | None = None, operator_unread: int | None = None) -> None`
  - `db.get_thread_flags(gmail_thread_id: str) -> sqlite3.Row | None`  — columns: `gmail_thread_id, is_pinned, operator_unread, updated_at`

---

- [ ] **Step 1: Написать тесты**

Создать `tests/test_db_thread_flags.py`:

```python
"""Тесты для thread_flags — set_thread_flags / get_thread_flags."""
from __future__ import annotations
import sqlite3
import pytest
from unittest.mock import patch
from contextlib import contextmanager


def make_in_memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE thread_flags (
            gmail_thread_id TEXT PRIMARY KEY,
            is_pinned       INTEGER NOT NULL DEFAULT 0,
            operator_unread INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


@pytest.fixture
def conn():
    c = make_in_memory_conn()
    yield c
    c.close()


@pytest.fixture
def patched_db(conn):
    @contextmanager
    def fake_get_conn():
        yield conn
    with patch("modules.db_threads.get_conn", fake_get_conn):
        yield conn


def test_set_flags_creates_row(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    row = get_thread_flags("t1")
    assert row is not None
    assert row["is_pinned"] == 1
    assert row["operator_unread"] == 0


def test_set_flags_updates_existing(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    set_thread_flags("t1", operator_unread=1)
    row = get_thread_flags("t1")
    assert row["is_pinned"] == 1       # не сброшен
    assert row["operator_unread"] == 1


def test_set_flags_pin_then_unpin(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("t1", is_pinned=1)
    set_thread_flags("t1", is_pinned=0)
    row = get_thread_flags("t1")
    assert row["is_pinned"] == 0


def test_get_flags_returns_none_for_unknown(patched_db):
    from modules.db_threads import get_thread_flags
    assert get_thread_flags("nonexistent") is None


def test_set_flags_noop_on_empty_thread_id(patched_db):
    from modules.db_threads import set_thread_flags, get_thread_flags
    set_thread_flags("", is_pinned=1)  # must not raise
    assert get_thread_flags("") is None
```

- [ ] **Step 2: Запустить тесты — убедиться что падают**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest tests/test_db_thread_flags.py -v 2>&1 | head -30
```

Ожидаем: `ImportError` или `ModuleNotFoundError` (функций ещё нет).

- [ ] **Step 3: Добавить таблицу `thread_flags` в `init_db()`**

В `database.py`, сразу после блока `client_profiles` (после строки с `updated_at TEXT NOT NULL DEFAULT (datetime('now'))`), добавить:

```python
        # thread_flags — операторские флаги тредов: закрепить, непрочитано.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_flags (
                gmail_thread_id TEXT PRIMARY KEY,
                is_pinned       INTEGER NOT NULL DEFAULT 0,
                operator_unread INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT    NOT NULL
            )
        """)
```

- [ ] **Step 4: Добавить `set_thread_flags` и `get_thread_flags` в `modules/db_threads.py`**

Вставить после функции `is_thread_waiting` (примерно строка 95), перед `# --- PROCESSED MESSAGES`:

```python
# --- THREAD FLAGS ---

def get_thread_flags(gmail_thread_id: str) -> Optional[sqlite3.Row]:
    """Получить флаги треда (is_pinned, operator_unread). None если строки нет."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM thread_flags WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()


def set_thread_flags(
    gmail_thread_id: str,
    *,
    is_pinned: Optional[int] = None,
    operator_unread: Optional[int] = None,
) -> None:
    """Upsert флаги треда. Передавай только те поля, что нужно изменить."""
    if not gmail_thread_id:
        return
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO thread_flags (gmail_thread_id, updated_at) VALUES (?, ?)",
            (gmail_thread_id, now),
        )
        if is_pinned is not None:
            conn.execute(
                "UPDATE thread_flags SET is_pinned=?, updated_at=? WHERE gmail_thread_id=?",
                (is_pinned, now, gmail_thread_id),
            )
        if operator_unread is not None:
            conn.execute(
                "UPDATE thread_flags SET operator_unread=?, updated_at=? WHERE gmail_thread_id=?",
                (operator_unread, now, gmail_thread_id),
            )
```

- [ ] **Step 5: Добавить экспорт в `database.py`**

В блоке `from modules.db_threads import (  # noqa: F401` в конце `database.py` добавить две строки:

```python
    get_thread_flags,
    set_thread_flags,
```

- [ ] **Step 6: Запустить тесты — убедиться что проходят**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest tests/test_db_thread_flags.py -v
```

Ожидаем: 5 тестов PASSED.

- [ ] **Step 7: Запустить все тесты — нет регрессий**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest --tb=short -q 2>&1 | tail -15
```

Ожидаем: все тесты PASSED (или те же что падали до нас).

- [ ] **Step 8: Коммит**

```bash
cd /home/pg/kleinanzeigen-bot
git add database.py modules/db_threads.py tests/test_db_thread_flags.py
git commit -m "feat: add thread_flags table with set/get helpers (pin, unread)"
```

---

## Task 2: API — обновить pipeline + bulk-action эндпоинт

**Files:**
- Modify: `modules/db_threads.py:413+` (функция `pipeline_threads()` — добавить JOIN)
- Modify: `web/api_ma.py` (обновить `_row_to_pipeline_item`, `ma_pipeline`; добавить bulk-action)
- Modify: `tests/test_api_ma_pipeline.py` (добавить тесты на новые поля и раздел pinned)
- Create: `tests/test_api_ma_bulk_action.py`

**Interfaces:**
- Consumes: `db.set_thread_flags(...)`, `db.close_thread(...)` из Task 1
- Produces:
  - `GET /api/ma/pipeline` → `{"pinned": [...], "red": [...], "green": [...], "accounts": [...]}`  каждый элемент имеет `is_pinned: bool`, `operator_unread: bool`
  - `POST /api/ma/threads/bulk-action` body: `{"thread_ids": ["..."], "action": "pin"|"unpin"|"read"|"unread"|"close"}` → `{"ok": true, "affected": N}`

---

- [ ] **Step 1: Обновить существующий тест + написать тесты для pipeline с новыми полями**

В `tests/test_api_ma_pipeline.py` — заменить строку в `test_pipeline_empty_returns_empty_sections`:

```python
# было:
assert body == {"red": [], "green": [], "accounts": []}
# стало:
assert body == {"pinned": [], "red": [], "green": [], "accounts": []}
```

Затем добавить в конец файла:

```python
def _row_with_flags(**overrides):
    """Как _row(), но включает is_pinned и operator_unread."""
    defaults = {
        "id": 1,
        "gmail_thread_id": "thread_abc",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": None,
        "buyer_display_name": "Osman",
        "deal_brief_json": None,
        "last_event_at": "2026-05-10T10:00:00",
        "last_event_kind": "in",
        "pending_drafts_count": 0,
        "any_sent_count": 1,
        "real_sent_count": 1,
        "is_pinned": 0,
        "operator_unread": 0,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__.side_effect = defaults.__getitem__
    row.keys.return_value = list(defaults.keys())
    return row


def test_pipeline_includes_is_pinned_and_operator_unread(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row_with_flags(gmail_thread_id="t1", is_pinned=0, operator_unread=1),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    item = res.json()["red"][0]
    assert item["is_pinned"] is False
    assert item["operator_unread"] is True


def test_pipeline_pinned_thread_goes_to_pinned_section(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row_with_flags(gmail_thread_id="t1", is_pinned=1, last_event_kind="in"),
        _row_with_flags(gmail_thread_id="t2", is_pinned=0, last_event_kind="in"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    body = res.json()
    assert "pinned" in body
    assert len(body["pinned"]) == 1
    assert body["pinned"][0]["thread_id"] == "t1"
    assert len(body["red"]) == 1
    assert body["red"][0]["thread_id"] == "t2"
```

- [ ] **Step 2: Написать тесты для bulk-action**

Создать `tests/test_api_ma_bulk_action.py`:

```python
"""Тесты для POST /api/ma/threads/bulk-action."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from tests.conftest import make_init_data, TEST_BOT_TOKEN

TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        from web.app import app
        yield TestClient(app), mdb


def _post(c, body, user=TEST_USER):
    init = make_init_data(user)
    return c.post(
        "/api/ma/threads/bulk-action",
        json=body,
        headers={"X-Telegram-Init-Data": init},
    )


def test_bulk_close_calls_close_thread(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1", "t2"], "action": "close"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["affected"] == 2
    assert mdb.close_thread.call_count == 2


def test_bulk_pin_calls_set_thread_flags_with_pinned(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1"], "action": "pin"})
    assert res.status_code == 200
    mdb.set_thread_flags.assert_called_once_with("t1", is_pinned=1)


def test_bulk_unpin_calls_set_thread_flags_with_zero(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "unpin"})
    mdb.set_thread_flags.assert_called_once_with("t1", is_pinned=0)


def test_bulk_read_clears_operator_unread(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "read"})
    mdb.set_thread_flags.assert_called_once_with("t1", operator_unread=0)


def test_bulk_unread_sets_operator_unread(client):
    c, mdb = client
    _post(c, {"thread_ids": ["t1"], "action": "unread"})
    mdb.set_thread_flags.assert_called_once_with("t1", operator_unread=1)


def test_bulk_unknown_action_returns_400(client):
    c, mdb = client
    res = _post(c, {"thread_ids": ["t1"], "action": "nuke"})
    assert res.status_code == 400


def test_bulk_empty_thread_ids_returns_422(client):
    c, mdb = client
    res = _post(c, {"thread_ids": [], "action": "pin"})
    assert res.status_code == 422


def test_bulk_requires_auth(client):
    c, mdb = client
    res = c.post("/api/ma/threads/bulk-action", json={"thread_ids": ["t1"], "action": "pin"})
    assert res.status_code == 422
```

- [ ] **Step 3: Запустить тесты — убедиться что падают**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest tests/test_api_ma_pipeline.py::test_pipeline_includes_is_pinned_and_operator_unread tests/test_api_ma_pipeline.py::test_pipeline_pinned_thread_goes_to_pinned_section tests/test_api_ma_bulk_action.py -v 2>&1 | tail -20
```

Ожидаем: тесты FAIL (endpoint и поля не существуют).

- [ ] **Step 4: Обновить `pipeline_threads()` SQL в `modules/db_threads.py`**

В функции `pipeline_threads()`, в разделе `WITH ...`, добавить новую CTE прямо перед закрывающей скобкой блока `WITH` (после `last_in AS (...)`):

```sql
    last_in AS (
        ...существующий код...
    )
```

→ добавить запятую после last_in и новую CTE:

```sql
    last_in AS (
        ...существующий код...
    ),
    flags AS (
        SELECT gmail_thread_id,
               COALESCE(is_pinned, 0)       AS is_pinned,
               COALESCE(operator_unread, 0) AS operator_unread
          FROM thread_flags
    )
```

В финальном SELECT добавить поля и JOIN:

```sql
    SELECT li.*,
           le.last_event_at,
           le.last_event_kind,
           COALESCE(c.any_sent_count, 0)         AS any_sent_count,
           COALESCE(c.real_sent_count, 0)        AS real_sent_count,
           COALESCE(c.pending_drafts_count, 0)   AS pending_drafts_count,
           CASE WHEN COALESCE(c.any_sent_count, 0) > 0 THEN 1 ELSE 0 END   AS has_any_sent,
           CASE WHEN COALESCE(c.real_sent_count, 0) > 0 THEN 1 ELSE 0 END  AS has_real_reply,
           CASE WHEN COALESCE(c.pending_drafts_count, 0) > 0 THEN 1 ELSE 0 END AS has_pending_draft,
           COALESCE(f.is_pinned, 0)       AS is_pinned,
           COALESCE(f.operator_unread, 0) AS operator_unread
    FROM last_in li
    LEFT JOIN counts c ON c.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN last_event le ON le.gmail_thread_id = li.gmail_thread_id
    LEFT JOIN flags f ON f.gmail_thread_id = li.gmail_thread_id
    WHERE ...
```

- [ ] **Step 5: Обновить `_row_to_pipeline_item()` в `web/api_ma.py`**

Добавить два поля в возвращаемый dict (после строки с `"ru_client"`):

```python
        "is_pinned": bool(row["is_pinned"]) if "is_pinned" in row.keys() else False,
        "operator_unread": bool(row["operator_unread"]) if "operator_unread" in row.keys() else False,
```

- [ ] **Step 6: Обновить `ma_pipeline()` — добавить раздел pinned**

Заменить тело функции `ma_pipeline()` (после `rows = db.pipeline_threads()`):

```python
    pinned: list[dict[str, Any]] = []
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row["gmail_thread_id"]
        autopilot_row = db.get_thread_autopilot(thread_id)
        item = _row_to_pipeline_item(row, autopilot_row)
        if item["is_pinned"]:
            pinned.append(item)
        elif item["last_event_kind"] == "in":
            red.append(item)
        else:
            green.append(item)
    pinned.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    red.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    green.sort(key=lambda x: x["last_event_at"] or "", reverse=True)
    accounts = [{"id": a["id"], "name": a["name"]} for a in db.list_accounts()]
    return {"pinned": pinned, "red": red, "green": green, "accounts": accounts}
```

- [ ] **Step 7: Добавить bulk-action эндпоинт в `web/api_ma.py`**

После импорта `asyncio` (или после последнего `@router.post` — в конец роутера, после `/threads/{thread_id}/wait`), добавить:

```python
class BulkActionBody(BaseModel):
    thread_ids: list[str] = Field(..., min_length=1)
    action: str  # "pin" | "unpin" | "read" | "unread" | "close"


@router.post("/threads/bulk-action")
async def ma_bulk_action(
    body: BulkActionBody,
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Bulk-действия над несколькими тредами."""
    _ALLOWED = {"pin", "unpin", "read", "unread", "close"}
    if body.action not in _ALLOWED:
        raise HTTPException(400, f"unknown action: {body.action!r}")
    actor = user.get("username") or str(user.get("id", ""))
    for thread_id in body.thread_ids:
        if body.action == "close":
            db.close_thread(thread_id, closed_by=actor)
        elif body.action == "pin":
            db.set_thread_flags(thread_id, is_pinned=1)
        elif body.action == "unpin":
            db.set_thread_flags(thread_id, is_pinned=0)
        elif body.action == "read":
            db.set_thread_flags(thread_id, operator_unread=0)
        elif body.action == "unread":
            db.set_thread_flags(thread_id, operator_unread=1)
    return {"ok": True, "affected": len(body.thread_ids)}
```

**Важно**: маршрут `/threads/bulk-action` должен быть определён **до** `/threads/{thread_id}`, иначе FastAPI попытается матчить `"bulk-action"` как `thread_id`. Вставить до строки `@router.get("/threads/{thread_id}")`.

- [ ] **Step 8: Запустить тесты**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest tests/test_api_ma_pipeline.py tests/test_api_ma_bulk_action.py -v 2>&1 | tail -25
```

Ожидаем: все тесты PASSED.

- [ ] **Step 9: Запустить все тесты — нет регрессий**

```bash
cd /home/pg/kleinanzeigen-bot
python -m pytest --tb=short -q 2>&1 | tail -15
```

- [ ] **Step 10: Задеплоить и перезапустить**

```bash
rsync -av --exclude='.git' --exclude='.claude' --exclude='__pycache__' --exclude='kleinanzeigen.db' --exclude='bot.db' /home/pg/kleinanzeigen-bot/ pg@192.168.88.28:/home/pg/kleinanzeigen-bot/
ssh pg@192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
```

- [ ] **Step 11: Коммит**

```bash
cd /home/pg/kleinanzeigen-bot
git add modules/db_threads.py web/api_ma.py tests/test_api_ma_pipeline.py tests/test_api_ma_bulk_action.py
git commit -m "feat: add pinned/unread pipeline fields and bulk-action endpoint"
```

---

## Task 3: Frontend — режим выборки в pipeline.js

**Files:**
- Modify: `web-app/js/screens/pipeline.js` (полная переработка selection state + UI)
- Modify: `web-app/js/router.js` (bump version pipeline.js)

**Interfaces:**
- Consumes: `GET /api/ma/pipeline` → теперь `{pinned, red, green, accounts}`, поля `is_pinned`, `operator_unread` в каждом item
- Consumes: `POST /api/ma/threads/bulk-action`

---

- [ ] **Step 1: Добавить selection state в `pipeline.js`**

В начало файла, после строки `const state = { account: "", status: "all" };`, добавить:

```js
const sel = { active: false, ids: new Set() };
let _bulkBar = null;

function removeBulkBar() {
  if (_bulkBar) { _bulkBar.remove(); _bulkBar = null; }
}
```

В функцию `teardown()` добавить вызов `removeBulkBar()`:

```js
function teardown() {
  removeBulkBar();
  if (_timer) { clearInterval(_timer); _timer = null; }
  // ... остальное без изменений
}
```

- [ ] **Step 2: Обновить `threadCard()` — чекбокс + unread-индикатор**

Заменить функцию `threadCard` целиком:

```js
export function threadCard(thread, accountsById = {}, selMode = false) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start gap-2">
        <div class="flex-grow-1 me-2 min-w-0">
          <div class="title fw-semibold"></div>
          <div class="meta text-muted small mt-1"></div>
          <div class="ru-preview small mt-1"></div>
        </div>
        <div class="text-end small flex-shrink-0">
          <div class="when text-muted"></div>
          <div class="badges mt-1"></div>
        </div>
      </div>
    </a>
  `);

  if (selMode) {
    card.removeAttribute("href");
    // Чекбокс — слева перед контентом
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "form-check-input flex-shrink-0 mt-1";
    chk.checked = sel.ids.has(thread.thread_id);
    card.querySelector(".d-flex").prepend(chk);
    card.addEventListener("click", e => {
      e.preventDefault();
      if (sel.ids.has(thread.thread_id)) {
        sel.ids.delete(thread.thread_id);
        chk.checked = false;
      } else {
        sel.ids.add(thread.thread_id);
        chk.checked = true;
      }
      updateBulkBar();
    });
  } else {
    card.href = `#/thread/${encodeURIComponent(thread.thread_id)}`;
  }

  // Заголовок
  const title = card.querySelector(".title");
  if (thread.operator_unread) title.classList.add("fw-bold");
  const acc = accountsById[thread.account_id];
  if (acc) title.appendChild(accountBadge(thread.account_id, acc.name));
  title.appendChild(document.createTextNode(
    ` ${thread.ad_title ?? "(без названия)"} · ${thread.ad_price ?? "?"}`));
  if (thread.operator_unread) {
    const dot = document.createElement("span");
    dot.className = "ms-1 badge rounded-pill bg-primary";
    dot.style.cssText = "width:8px;height:8px;padding:0;vertical-align:middle;display:inline-block";
    title.appendChild(dot);
  }

  card.querySelector(".meta").textContent =
    `👤 ${thread.buyer_display_name ?? "?"}`;

  let preview = (thread.ru_client ?? "").replace(/\s+/g, " ").trim();
  if (!preview && thread.deal_brief_json) {
    try {
      const b = typeof thread.deal_brief_json === "string"
        ? JSON.parse(thread.deal_brief_json) : thread.deal_brief_json;
      if (b?.summary_ru) preview = b.summary_ru;
    } catch (e) { /* skip */ }
  }
  if (preview.length > 110) preview = preview.slice(0, 110) + "…";
  const ruEl = card.querySelector(".ru-preview");
  if (preview) ruEl.textContent = `💬 ${preview}`; else ruEl.remove();

  card.querySelector(".when").textContent = berlinTime(thread.last_event_at);

  const badges = card.querySelector(".badges");
  if (thread.is_autopilot) {
    badges.appendChild(el(`<span class="badge bg-warning text-dark">🤖</span>`));
  }
  if (thread.pending_drafts_count > 0) {
    const b = el(`<span class="badge bg-info ms-1"></span>`);
    b.textContent = `📝 ${thread.pending_drafts_count}`;
    badges.appendChild(b);
  }
  return card;
}
```

- [ ] **Step 3: Добавить функцию `updateBulkBar()`**

После функции `removeBulkBar()` добавить:

```js
function updateBulkBar() {
  if (!_bulkBar) return;
  const count = sel.ids.size;
  const countEl = _bulkBar.querySelector(".sel-count");
  if (countEl) countEl.textContent = count > 0 ? `Выбрано: ${count}` : "Ничего не выбрано";
  _bulkBar.querySelectorAll("button[data-action]").forEach(b => {
    b.disabled = count === 0;
  });
}
```

- [ ] **Step 4: Добавить функцию `renderSection()`**

Перед функцией `paint()` добавить хелпер:

```js
function renderSection(label, items, accountsById) {
  const frag = document.createDocumentFragment();
  frag.appendChild(el(`<div class="text-muted small text-uppercase mt-2 mb-1">${label}: ${items.length}</div>`));
  items.forEach(t => frag.appendChild(threadCard(t, accountsById, sel.active)));
  return frag;
}
```

- [ ] **Step 5: Полностью заменить функцию `paint()`**

```js
async function paint(mount) {
  removeBulkBar();
  let data;
  try {
    data = await api("/api/ma/pipeline");
  } catch (e) { setError(mount, e.message ?? String(e)); return; }

  const accountsById = {};
  (data.accounts ?? []).forEach(a => { accountsById[a.id] = a; });

  const container = el(`<div class="${sel.active ? "pb-5" : ""}"></div>`);

  // Хедер
  const head = el(`
    <div class="d-flex justify-content-between align-items-center mb-2">
      <h5 class="mb-0">📥 Входящие</h5>
      <div class="d-flex gap-2">
        <button class="btn btn-sm btn-outline-secondary refresh">↻</button>
        <button class="btn btn-sm sel-toggle"></button>
      </div>
    </div>`);
  const selToggle = head.querySelector(".sel-toggle");
  selToggle.textContent = sel.active ? "Отмена" : "Выбрать";
  selToggle.classList.add(sel.active ? "btn-secondary" : "btn-outline-primary");
  head.querySelector(".refresh").addEventListener("click", () => paint(mount));
  selToggle.addEventListener("click", () => {
    sel.active = !sel.active;
    sel.ids.clear();
    paint(mount);
  });
  container.appendChild(head);

  // Фильтры (только в обычном режиме)
  if (!sel.active) {
    const accBar = el(`<div class="mb-1"></div>`);
    accBar.appendChild(chip("Все аккаунты", state.account === "",
      () => { state.account = ""; paint(mount); }));
    (data.accounts ?? []).forEach(a => {
      accBar.appendChild(chip(a.name, String(state.account) === String(a.id),
        () => { state.account = a.id; paint(mount); }));
    });
    container.appendChild(accBar);

    const stBar = el(`<div class="mb-2"></div>`);
    [["all", "Все"], ["red", "🔴 ждут нас"], ["green", "🟢 ждём"], ["ap", "🤖 автопилот"]]
      .forEach(([k, lbl]) => stBar.appendChild(chip(lbl, state.status === k,
        () => { state.status = k; paint(mount); })));
    container.appendChild(stBar);
  }

  // Списки
  const pinned = (data.pinned ?? []).filter(t => matchFilter(t, "pinned"));
  const red    = (data.red   ?? []).filter(t => matchFilter(t, "red"));
  const green  = (data.green ?? []).filter(t => matchFilter(t, "green"));
  const list = el(`<div class="list-group list-group-flush"></div>`);

  if (!pinned.length && !red.length && !green.length) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-3 text-center">Нет обращений по фильтру</div>`));
  } else {
    if (pinned.length) list.appendChild(renderSection("📌 Закреплённые", pinned, accountsById));
    if (red.length)    list.appendChild(renderSection("🔴 Ждут нас", red, accountsById));
    if (green.length)  list.appendChild(renderSection("🟢 Ждём клиента", green, accountsById));
  }
  container.appendChild(list);
  mount.replaceChildren(container);

  // Bulk action bar
  if (sel.active) {
    _bulkBar = el(`
      <div class="position-fixed bottom-0 start-0 end-0 bg-white border-top p-2" style="z-index:1050">
        <div class="text-muted small text-center sel-count mb-1">Ничего не выбрано</div>
        <div class="d-flex gap-2 justify-content-center flex-wrap">
          <button class="btn btn-sm btn-outline-primary" data-action="pin">📌 Закрепить</button>
          <button class="btn btn-sm btn-outline-secondary" data-action="unpin">📌 Открепить</button>
          <button class="btn btn-sm btn-outline-success" data-action="read">✉️ Прочитано</button>
          <button class="btn btn-sm btn-outline-warning" data-action="unread">🔔 Непрочитано</button>
          <button class="btn btn-sm btn-danger" data-action="close">🗑 Убрать</button>
        </div>
      </div>
    `);
    _bulkBar.querySelectorAll("button[data-action]").forEach(btn => {
      btn.disabled = true;
      btn.addEventListener("click", async () => {
        if (!sel.ids.size) return;
        const action = btn.dataset.action;
        btn.disabled = true;
        try {
          await api("/api/ma/threads/bulk-action", {
            method: "POST",
            body: { thread_ids: [...sel.ids], action },
          });
        } catch (e) {
          alert(`Ошибка: ${e.message}`);
          return;
        } finally {
          btn.disabled = false;
        }
        sel.active = false;
        sel.ids.clear();
        paint(mount);
      });
    });
    document.body.appendChild(_bulkBar);
    updateBulkBar();
  }
}
```

- [ ] **Step 6: Обновить `matchFilter()` для pinned**

В функции `matchFilter(t, kind)` добавить обработку `kind === "pinned"`:

```js
function matchFilter(t, kind) {
  if (state.account && String(t.account_id) !== String(state.account)) return false;
  if (kind === "pinned") return true;  // закреплённые показываем всегда при фильтре аккаунта
  if (state.status === "ap") return !!t.is_autopilot;
  if (state.status === "red") return kind === "red";
  if (state.status === "green") return kind === "green";
  return true;
}
```

- [ ] **Step 7: Bump version-строки**

В `web-app/js/router.js` строка с pipeline:
```js
// было:
import * as pipeline from "./screens/pipeline.js?v=20260624-024500";
// стало:
import * as pipeline from "./screens/pipeline.js?v=20260702-000000";
```

В `web-app/js/screens/pipeline.js` первые строки:
```js
// было:
import { api } from "../api.js?v=20260624-024500";
import { el, esc, berlinTime, setLoading, setError, accountBadge } from "../utils.js?v=20260624-024500";
// стало:
import { api } from "../api.js?v=20260702-000000";
import { el, esc, berlinTime, setLoading, setError, accountBadge } from "../utils.js?v=20260702-000000";
```

- [ ] **Step 8: Задеплоить**

```bash
rsync -av --exclude='.git' --exclude='.claude' --exclude='__pycache__' --exclude='kleinanzeigen.db' --exclude='bot.db' /home/pg/kleinanzeigen-bot/ pg@192.168.88.28:/home/pg/kleinanzeigen-bot/
ssh pg@192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
```

- [ ] **Step 9: Ручной тест в браузере**

Открыть МА → Входящие:
1. Нажать «Выбрать» → появляются чекбоксы, кнопка меняется на «Отмена», фильтры скрыты
2. Тапнуть по 2-3 карточкам → чекбоксы отмечаются, снизу появляется бар «Выбрано: N»
3. Нажать «📌 Закрепить» → треды переходят в раздел 📌 Закреплённые, режим сброшен
4. Войти в режим, выбрать закреплённый → «📌 Открепить» → тред возвращается в 🔴/🟢
5. Нажать «🔔 Непрочитано» → заголовок карточки становится жирным + синяя точка
6. Выбрать и нажать «✉️ Прочитано» → жирность и точка пропадают
7. Нажать «🗑 Убрать» → трed исчезает из пайплайна
8. «Отмена» → сбрасывает режим без действий

- [ ] **Step 10: Коммит**

```bash
cd /home/pg/kleinanzeigen-bot
git add web-app/js/screens/pipeline.js web-app/js/router.js
git commit -m "feat: inbox selection mode with pin/read/unread/close bulk actions"
```
