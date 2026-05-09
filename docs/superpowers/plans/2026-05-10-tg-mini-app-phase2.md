# Telegram Mini App — Phase 2 (Read-only screens) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оператор может через Mini App: (1) увидеть pipeline активных тредов с делением 🔴/🟢, (2) тапнуть тред → увидеть chat-style лог событий, (3) открыть историю клиента (все его треды) через related-warning. Никаких действий — read-only.

**Architecture:** SPA добавляет hash-router и 3 экрана (`pipeline`, `thread`, `history`). Backend получает 3 GET endpoint'а под `/api/ma/*`, размещены в новом модуле `web/api_ma.py` через `APIRouter` (выносим из `web/app.py`, попутно мигрируя существующий `/api/ma/health`). Все endpoint'ы — read-only, тонкие обёртки над существующими `db.pipeline_threads`, `db.thread_events`, `db.list_threads_for_client`, `db.find_related_inquiries`.

**Tech Stack:** Python 3.11 / FastAPI APIRouter / pytest + TestClient / vanilla JS + Preact runtime + HTM (CDN) / Bootstrap 5.3.

**Все команды на проде через ssh** (`ssh 192.168.88.28 ...`). Phase 1 удалил worktree — работаем прямо на main, бот рестартим точечно после backend-изменений.

**Spec reference:** `docs/superpowers/specs/2026-05-09-tg-mini-app-design.md`

**Phase 1 wrap-up (обязательное чтение):** `docs/superpowers/plans/2026-05-09-tg-mini-app-phase1.md` — операционная инфраструктура (cloudflared tunnel, Pages, BotFather menu) уже настроена и работает. URL Mini App: `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/`. Cloudflared Quick Tunnel на текущей итерации: `https://choice-drunk-curriculum-effectiveness.trycloudflare.com` (URL ротируется при рестарте). Если рестартанули туннель и URL поменялся — обнови `web-app/js/api.js:API_BASE` + bump cache-bust в `web-app/index.html` перед началом работы.

---

## File Structure

**Создаются:**
- `web/api_ma.py` — APIRouter с префиксом `/api/ma`, новый дом всех Mini App endpoint'ов
- `web/__init__.py` — пустой если ещё нет (нужен для `from web.api_ma import ...`)
- `tests/test_api_ma_pipeline.py` — TestClient тесты для `GET /api/ma/pipeline`
- `tests/test_api_ma_threads.py` — TestClient тесты для `GET /api/ma/threads/{id}`
- `tests/test_api_ma_clients.py` — TestClient тесты для `GET /api/ma/clients/{email}/history`
- `web-app/js/router.js` — hash-router (parsing + dispatching на screen-render функции)
- `web-app/js/screens/pipeline.js` — pipeline screen renderer
- `web-app/js/screens/thread.js` — thread detail screen renderer
- `web-app/js/screens/history.js` — client history screen renderer
- `web-app/js/utils.js` — общие хелперы (`el`, `esc`, `berlinTime`)

**Модифицируются:**
- `web/app.py` — убрать `/api/ma/health`, добавить `app.include_router(ma_router)` + импорт
- `web-app/js/app.js` — заменить inline renderHealth на router-driven mount
- `web-app/js/tg.js` — добавить helpers для BackButton / MainButton API
- `web-app/index.html` — bump cache-bust (`?v=...`)

---

## Task 1: Refactor — APIRouter + перенос /api/ma/health

**Files:**
- Create: `web/api_ma.py`
- Create: `web/__init__.py` (если не существует)
- Modify: `web/app.py`

**Background:** Phase 1 положил `/api/ma/health` в конец `web/app.py` (782 строки). С добавлением 3 новых endpoint'ов в Phase 2 файл станет неуправляемым. Splittим сейчас, через APIRouter с prefix `/api/ma`. Все Mini App endpoints живут в `web/api_ma.py`. `web/app.py` подключает их через `include_router`.

- [ ] **Step 1: Verify web/__init__.py exists**

```bash
ssh 192.168.88.28 'ls /home/pg/kleinanzeigen-bot/web/__init__.py 2>&1 || echo MISSING'
```

Если MISSING — создай пустой файл:
```bash
ssh 192.168.88.28 'touch /home/pg/kleinanzeigen-bot/web/__init__.py'
```

- [ ] **Step 2: Create web/api_ma.py — пустой router + перенос /health**

```python
"""Mini App API router.

Все endpoint'ы под префиксом /api/ma/. Тонкие обёртки над database.* и
existing scheduler.* helpers — никакой бизнес-логики.

Все endpoint'ы требуют валидной Telegram initData через verify_init_data_dep.
"""
from __future__ import annotations
from typing import Any

from fastapi import APIRouter, Depends

import database as db
from modules.tg_init_data import verify_init_data_dep


router = APIRouter(prefix="/api/ma", tags=["Mini App"])


@router.get("/health")
async def ma_health(user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Health endpoint для Mini App. Возвращает идентичность оператора."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }
```

- [ ] **Step 3: Modify web/app.py — убрать старый health endpoint, добавить include_router**

Найди в `web/app.py` блок:
```python
# ---------- Mini App API ----------

@app.get("/api/ma/health")
async def ma_health(user: dict = Depends(verify_init_data_dep)) -> dict:
    """Health endpoint для Mini App. Возвращает идентичность оператора."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }
```

Удали его целиком.

Найди импорт:
```python
from modules.tg_init_data import verify_init_data_dep
```

Замени на:
```python
from web.api_ma import router as ma_router
```

(Импорт `verify_init_data_dep` больше не нужен в `app.py` — он используется только внутри `api_ma.py`.)

После строки `app.add_middleware(CORSMiddleware, ...)` добавь:
```python
app.include_router(ma_router)
```

- [ ] **Step 4: Update test mock targets**

Тесты из Phase 1 мокают `modules.tg_init_data.config`. Endpoint переехал, но мок-цель не меняется (в `api_ma.py` через `import config` всё ещё указывает на тот же `modules.tg_init_data.config` через зависимость). Тесты должны работать без изменений.

Проверим:
```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_health_endpoint.py -v'
```

Expected: `5 passed`. Если тесты не находят app или endpoint 404 — потребуется фикс импорта в test fixture.

- [ ] **Step 5: Smoke endpoint locally**

Restart bot, проверь что endpoint всё ещё доступен:
```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 4 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/ma/health'
```

Expected: `422` (no header — auth dep complains).

Также проверь что `/api/ma/health` доступен через cloudflared:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://choice-drunk-curriculum-effectiveness.trycloudflare.com/api/ma/health
```

Expected: `422`. Если URL изменился — обнови плановый URL.

- [ ] **Step 6: Run full test suite**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/ -v'
```

Expected: `15 passed` (Phase 1 baseline).

- [ ] **Step 7: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web/api_ma.py web/__init__.py web/app.py && git -C /home/pg/kleinanzeigen-bot commit -m "refactor(ma): extract APIRouter into web/api_ma.py"'
```

---

## Task 2: GET /api/ma/pipeline endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_pipeline.py`

**Background:** Mirror `db.pipeline_threads()` для Mini App. Server разделяет threads на `red` (last_event_kind='in', ждут нас) и `green` (last_event_kind='out', ждём клиента) — frontend получает готовое деление. Сортировка внутри секции — `last_event_at ASC` (старые сверху, наиболее срочные внизу — как в боте).

Возвращаемая структура:
```json
{
  "red":   [{thread_id, msg_id, ad_title, ad_price, ad_url, buyer_display_name,
             deal_brief_json, last_event_at, last_event_kind, pending_drafts_count,
             is_autopilot}, ...],
  "green": [...]
}
```

`msg_id` — id последнего incoming row в треде (для deep-link на review карточку в Phase 3). `is_autopilot` — boolean из `thread_autopilot.active` или ложь, если row не найдена.

- [ ] **Step 1: Write failing test**

Create `/home/pg/kleinanzeigen-bot/tests/test_api_ma_pipeline.py`:

```python
"""TestClient тесты для GET /api/ma/pipeline."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**overrides):
    """Helper: создать MagicMock что ведёт себя как sqlite3.Row для конкретных колонок."""
    defaults = {
        "id": 1,
        "gmail_thread_id": "thread_abc",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://www.kleinanzeigen.de/s-anzeige/x/123",
        "buyer_display_name": "Osman",
        "deal_brief_json": '{"summary_ru":"торгуется"}',
        "last_event_at": "2026-05-10T10:00:00",
        "last_event_kind": "in",
        "pending_drafts_count": 1,
        "any_sent_count": 0,
        "real_sent_count": 0,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__.side_effect = defaults.__getitem__
    row.keys.return_value = defaults.keys()
    return row


@pytest.fixture
def client():
    """TestClient + замоканные config + db."""
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        # Default: pipeline_threads пустой; per-test переопределяет.
        mdb.pipeline_threads.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_message.return_value = None
        from web.app import app
        yield TestClient(app), mdb


def test_pipeline_empty_returns_empty_sections(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body == {"red": [], "green": []}


def test_pipeline_splits_by_last_event_kind(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row(id=1, gmail_thread_id="t1", last_event_kind="in", last_event_at="2026-05-10T10:00:00"),
        _row(id=2, gmail_thread_id="t2", last_event_kind="out", last_event_at="2026-05-10T11:00:00"),
        _row(id=3, gmail_thread_id="t3", last_event_kind="in", last_event_at="2026-05-10T09:00:00"),
    ]
    mdb.get_message.return_value = MagicMock()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert len(body["red"]) == 2
    assert len(body["green"]) == 1
    # Сортировка ASC внутри красной — t3 (09:00) перед t1 (10:00)
    assert body["red"][0]["thread_id"] == "t3"
    assert body["red"][1]["thread_id"] == "t1"
    assert body["green"][0]["thread_id"] == "t2"


def test_pipeline_serialises_required_fields(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [
        _row(id=42, gmail_thread_id="abc", ad_title="Test", ad_price="1000€",
             buyer_display_name="Jane", pending_drafts_count=2),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    item = res.json()["red"][0]
    assert item["thread_id"] == "abc"
    assert item["msg_id"] == 42
    assert item["ad_title"] == "Test"
    assert item["ad_price"] == "1000€"
    assert item["buyer_display_name"] == "Jane"
    assert item["pending_drafts_count"] == 2
    assert item["last_event_at"] == "2026-05-10T10:00:00"
    assert item["last_event_kind"] == "in"
    assert item["is_autopilot"] is False


def test_pipeline_autopilot_flag_true_when_row_active(client):
    c, mdb = client
    mdb.pipeline_threads.return_value = [_row(gmail_thread_id="ap_thread")]
    autopilot_row = MagicMock()
    autopilot_row.__getitem__.side_effect = {"active": 1}.__getitem__
    autopilot_row.keys.return_value = ["active"]
    mdb.get_thread_autopilot.return_value = autopilot_row
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/pipeline", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["red"][0]["is_autopilot"] is True


def test_pipeline_requires_auth(client):
    c, mdb = client
    res = c.get("/api/ma/pipeline")  # no header
    assert res.status_code == 422


def test_pipeline_rejects_bad_hash(client):
    c, mdb = client
    res = c.get(
        "/api/ma/pipeline",
        headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=99&hash=fake"},
    )
    assert res.status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_pipeline.py -v'
```

Expected: 6 tests fail with 404 (endpoint не существует) или collection error.

- [ ] **Step 3: Add helper db functions if missing**

`db.get_thread_autopilot(thread_id)` может уже существовать или нет. Проверь:
```bash
ssh 192.168.88.28 'grep -n "def get_thread_autopilot" /home/pg/kleinanzeigen-bot/database.py'
```

Если отсутствует — добавь в `database.py` после блока про `thread_autopilot`:

```python
def get_thread_autopilot(thread_id: str) -> Optional[sqlite3.Row]:
    """Возвращает row из thread_autopilot или None."""
    if not thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM thread_autopilot WHERE gmail_thread_id = ?",
            (thread_id,),
        ).fetchone()
```

- [ ] **Step 4: Implement GET /api/ma/pipeline in web/api_ma.py**

Append к `web/api_ma.py` после `ma_health`:

```python
def _row_to_pipeline_item(row: Any, autopilot_row: Any) -> dict[str, Any]:
    """Конвертация row из db.pipeline_threads + autopilot lookup в API-форму."""
    is_autopilot = False
    if autopilot_row is not None:
        try:
            is_autopilot = bool(autopilot_row["active"])
        except (KeyError, IndexError):
            is_autopilot = False
    return {
        "thread_id": row["gmail_thread_id"],
        "msg_id": row["id"],
        "ad_title": row["ad_title"],
        "ad_price": row["ad_price"],
        "ad_url": row["ad_url"] if "ad_url" in row.keys() else None,
        "buyer_display_name": row["buyer_display_name"],
        "deal_brief_json": row["deal_brief_json"] if "deal_brief_json" in row.keys() else None,
        "last_event_at": row["last_event_at"],
        "last_event_kind": row["last_event_kind"],
        "pending_drafts_count": row["pending_drafts_count"],
        "is_autopilot": is_autopilot,
    }


@router.get("/pipeline")
async def ma_pipeline(user: dict = Depends(verify_init_data_dep)) -> dict[str, list]:
    """Pipeline активных тредов: разделение на red (ждут нас) / green (ждём клиента).

    Сортировка внутри секции — ASC по last_event_at (старые сверху).
    """
    rows = db.pipeline_threads()
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    for row in rows:
        thread_id = row["gmail_thread_id"]
        autopilot_row = db.get_thread_autopilot(thread_id)
        item = _row_to_pipeline_item(row, autopilot_row)
        if item["last_event_kind"] == "in":
            red.append(item)
        else:
            green.append(item)
    # pipeline_threads возвращает уже отсортированный ASC по last_event_at,
    # но сплит может перемешать — пересортируем внутри секций.
    red.sort(key=lambda x: x["last_event_at"] or "")
    green.sort(key=lambda x: x["last_event_at"] or "")
    return {"red": red, "green": green}
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_pipeline.py -v'
```

Expected: `6 passed`.

- [ ] **Step 6: Restart bot + smoke**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 4 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/ma/pipeline'
```

Expected: `422`.

- [ ] **Step 7: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web/api_ma.py tests/test_api_ma_pipeline.py database.py && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): GET /api/ma/pipeline endpoint"'
```

---

## Task 3: GET /api/ma/threads/{thread_id} endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_threads.py`

**Background:** Mirror `db.thread_events()` для chat-style лога. Header формируется из row'ов треда: ad-инфо берём из самой свежей `direction='in'` row, account берём через `accounts` table по `account_id`. Related-warning блок строится из `db.find_related_inquiries`.

Возвращаемая структура:
```json
{
  "header": {
    "thread_id": "...",
    "ad_title": "...",
    "ad_price": "...",
    "ad_url": "...",
    "buyer_display_name": "...",
    "buyer_email": "...",
    "account_name": "main",
    "account_email": "us@gmail.com",
    "is_autopilot": false
  },
  "events": [
    {"ts": "...", "kind": "in", "text": "...", "ru_text": "...", "is_auto_ack": false, "msg_id": 123, "status": "sent"}
  ],
  "related": {
    "buyer_display_name": "...",
    "matches": [{"thread_id":"...", "ad_title":"...", "ad_price":"...", "last_at":"..."}]
  }
}
```

- [ ] **Step 1: Write failing tests**

Create `/home/pg/kleinanzeigen-bot/tests/test_api_ma_threads.py`:

```python
"""TestClient тесты для GET /api/ma/threads/{thread_id}."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**fields):
    row = MagicMock()
    row.__getitem__.side_effect = fields.__getitem__
    row.keys.return_value = list(fields.keys())
    return row


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = []
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        from web.app import app
        yield TestClient(app), mdb


def test_thread_not_found_returns_404(client):
    c, mdb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/nonexistent", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_thread_returns_header_events_related(client):
    c, mdb = client
    last_in = _row(
        id=42, gmail_thread_id="t1", direction="in",
        ad_title="Sitzbank", ad_price="1500€",
        ad_url="https://kleinanzeigen.de/x/123",
        buyer_display_name="Osman", buyer_name="osman@x.com",
        account_id=1,
    )
    mdb.thread_history.return_value = [last_in]
    mdb.thread_events.return_value = [
        {"ts": "2026-05-10T10:00:00", "kind": "in", "text": "Hallo",
         "ru_text": "Привет", "is_auto_ack": False,
         "row": _row(id=42, status="pending")},
        {"ts": "2026-05-10T11:00:00", "kind": "out", "text": "MfG",
         "ru_text": None, "is_auto_ack": False,
         "row": _row(id=42, status="sent")},
    ]
    mdb.find_related_inquiries.return_value = [
        _row(gmail_thread_id="t2", ad_title="Other", ad_price="500€",
             sent_at="2026-05-09T10:00:00", created_at="2026-05-09T09:00:00"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/t1", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()

    assert body["header"]["thread_id"] == "t1"
    assert body["header"]["ad_title"] == "Sitzbank"
    assert body["header"]["ad_price"] == "1500€"
    assert body["header"]["buyer_display_name"] == "Osman"
    assert body["header"]["buyer_email"] == "osman@x.com"
    assert body["header"]["account_name"] == "main"
    assert body["header"]["account_email"] == "us@gmail.com"
    assert body["header"]["is_autopilot"] is False

    assert len(body["events"]) == 2
    assert body["events"][0]["kind"] == "in"
    assert body["events"][0]["text"] == "Hallo"
    assert body["events"][0]["ru_text"] == "Привет"
    assert body["events"][1]["kind"] == "out"

    assert body["related"]["buyer_display_name"] == "Osman"
    assert len(body["related"]["matches"]) == 1
    assert body["related"]["matches"][0]["thread_id"] == "t2"


def test_thread_autopilot_flag(client):
    c, mdb = client
    mdb.thread_history.return_value = [
        _row(id=1, direction="in", buyer_display_name="X", buyer_name="x@y.com",
             ad_title="A", ad_price="100", ad_url="u", account_id=1),
    ]
    mdb.thread_events.return_value = []
    autopilot = MagicMock()
    autopilot.__getitem__.side_effect = {"active": 1}.__getitem__
    autopilot.keys.return_value = ["active"]
    mdb.get_thread_autopilot.return_value = autopilot
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/threads/t1", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["header"]["is_autopilot"] is True


def test_thread_requires_auth(client):
    c, mdb = client
    res = c.get("/api/ma/threads/abc")
    assert res.status_code == 422
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_threads.py -v'
```

Expected: tests fail (404 endpoint).

- [ ] **Step 3: Add db.get_account helper if missing**

```bash
ssh 192.168.88.28 'grep -n "def get_account" /home/pg/kleinanzeigen-bot/database.py'
```

Если нет — добавь в database.py:
```python
def get_account(account_id: Optional[int]) -> Optional[sqlite3.Row]:
    """Account row by id, or None."""
    if not account_id:
        return None
    with get_conn() as conn:
        return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
```

- [ ] **Step 4: Implement GET /api/ma/threads/{thread_id}**

Append к `web/api_ma.py`:

```python
from fastapi import HTTPException


def _event_to_api(event: dict[str, Any]) -> dict[str, Any]:
    """Конвертация event-dict из db.thread_events в API-форму."""
    row = event.get("row")
    return {
        "ts": event.get("ts"),
        "kind": event.get("kind"),
        "text": event.get("text"),
        "ru_text": event.get("ru_text"),
        "is_auto_ack": bool(event.get("is_auto_ack")),
        "msg_id": row["id"] if row is not None else None,
        "status": event.get("status") or (row["status"] if row is not None else None),
    }


def _related_match(row: Any) -> dict[str, Any]:
    return {
        "thread_id": row["gmail_thread_id"],
        "ad_title": row["ad_title"] if "ad_title" in row.keys() else None,
        "ad_price": row["ad_price"] if "ad_price" in row.keys() else None,
        "last_at": (row["sent_at"] if "sent_at" in row.keys() else None) or (
            row["created_at"] if "created_at" in row.keys() else None),
    }


@router.get("/threads/{thread_id}")
async def ma_thread(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Thread detail: header + chronological events + related-buyer block."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")

    # Header — самый свежий direction='in' (он несёт ad-инфо и buyer)
    last_in = next((r for r in reversed(history) if r["direction"] == "in"), history[-1])
    account = db.get_account(last_in["account_id"]) if "account_id" in last_in.keys() else None
    autopilot_row = db.get_thread_autopilot(thread_id)
    is_autopilot = False
    if autopilot_row is not None:
        try:
            is_autopilot = bool(autopilot_row["active"])
        except (KeyError, IndexError):
            is_autopilot = False

    header = {
        "thread_id": thread_id,
        "ad_title": last_in["ad_title"],
        "ad_price": last_in["ad_price"],
        "ad_url": last_in["ad_url"] if "ad_url" in last_in.keys() else None,
        "buyer_display_name": last_in["buyer_display_name"],
        "buyer_email": last_in["buyer_name"],
        "account_name": account["name"] if account is not None else None,
        "account_email": account["gmail_email"] if account is not None else None,
        "is_autopilot": is_autopilot,
    }

    events = [_event_to_api(e) for e in db.thread_events(thread_id)]

    related_matches = db.find_related_inquiries(
        last_in["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    )
    related = {
        "buyer_display_name": last_in["buyer_display_name"],
        "matches": [_related_match(r) for r in related_matches],
    }

    return {"header": header, "events": events, "related": related}
```

- [ ] **Step 5: Run tests, expect PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_threads.py -v'
```

Expected: `4 passed`.

- [ ] **Step 6: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web/api_ma.py tests/test_api_ma_threads.py database.py && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): GET /api/ma/threads/{thread_id} endpoint"'
```

---

## Task 4: GET /api/ma/clients/{email}/history endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_clients.py`

**Background:** Mirror `db.list_threads_for_client(buyer_email)`. URL-path содержит email с возможным `@` — FastAPI принимает `:path` тип для пропуска / в пути или просто `str` (email обычно не содержит /). Path-encoded `@` тоже работает.

Возвращаемая структура:
```json
{
  "buyer_email": "osman@x.com",
  "threads": [
    {"thread_id":"...", "ad_title":"...", "ad_id":"...", "ad_price":"...",
     "msg_count": 5, "last_at":"...", "last_status":"sent"}
  ]
}
```

- [ ] **Step 1: Write failing tests**

Create `/home/pg/kleinanzeigen-bot/tests/test_api_ma_clients.py`:

```python
"""TestClient тесты для GET /api/ma/clients/{email}/history."""
from __future__ import annotations
from unittest.mock import patch, MagicMock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**fields):
    row = MagicMock()
    row.__getitem__.side_effect = fields.__getitem__
    row.keys.return_value = list(fields.keys())
    return row


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.list_threads_for_client.return_value = []
        from web.app import app
        yield TestClient(app), mdb


def test_client_history_empty_returns_empty_threads(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('user@example.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    body = res.json()
    assert body == {"buyer_email": "user@example.com", "threads": []}


def test_client_history_returns_threads(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="123", ad_price="1500€",
             msg_count=3, last_at="2026-05-10T10:00:00", last_status="sent"),
        _row(thread_id="t2", ad_title="B", ad_id="456", ad_price="500€",
             msg_count=1, last_at="2026-05-09T10:00:00", last_status="pending"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('osman@x.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["buyer_email"] == "osman@x.com"
    assert len(body["threads"]) == 2
    assert body["threads"][0]["thread_id"] == "t1"
    assert body["threads"][0]["msg_count"] == 3
    assert body["threads"][0]["last_status"] == "sent"


def test_client_history_uses_email_as_lookup_key(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.get(
        f"/api/ma/clients/{quote('foo@bar.com')}/history",
        headers={"X-Telegram-Init-Data": init},
    )
    assert res.status_code == 200
    mdb.list_threads_for_client.assert_called_once_with("foo@bar.com")


def test_client_history_requires_auth(client):
    c, mdb = client
    res = c.get(f"/api/ma/clients/{quote('x@y.com')}/history")
    assert res.status_code == 422
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_clients.py -v'
```

- [ ] **Step 3: Implement endpoint**

Append к `web/api_ma.py`:

```python
@router.get("/clients/{email}/history")
async def ma_client_history(email: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """История тредов клиента (по buyer_email)."""
    rows = db.list_threads_for_client(email)
    threads = [
        {
            "thread_id": r["thread_id"],
            "ad_title": r["ad_title"],
            "ad_id": r["ad_id"],
            "ad_price": r["ad_price"],
            "msg_count": r["msg_count"],
            "last_at": r["last_at"],
            "last_status": r["last_status"],
        }
        for r in rows
    ]
    return {"buyer_email": email, "threads": threads}
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_api_ma_clients.py -v'
```

Expected: `4 passed`. Total суммарно: 19 tests pass (5 health + 6 pipeline + 4 threads + 4 clients).

- [ ] **Step 5: Restart bot + verify через cloudflared**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 4'
curl -s -o /dev/null -w "%{http_code}\n" https://choice-drunk-curriculum-effectiveness.trycloudflare.com/api/ma/pipeline
# expect: 422
```

- [ ] **Step 6: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web/api_ma.py tests/test_api_ma_clients.py && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): GET /api/ma/clients/{email}/history endpoint"'
```

---

## Task 5: Frontend — utils + hash router

**Files:**
- Create: `web-app/js/utils.js`
- Create: `web-app/js/router.js`
- Modify: `web-app/js/app.js`
- Modify: `web-app/js/tg.js`

**Background:** Phase 1 SPA имеет один `renderHealth` без routing. Phase 2 нужно перейти на hash-based router: при изменении `location.hash` SPA вызывает соответствующую `screens/<name>.render(mount, params)`. Routes:
- `#/pipeline` (default если hash пустой) → `pipeline.render`
- `#/thread/{thread_id}` → `thread.render`
- `#/client/{email}` → `history.render`

`utils.js` — общие хелперы: безопасный `el(html)`, `esc(s)` для XSS, `berlinTime(iso, fmt)` для конверсии UTC → Europe/Berlin.

`tg.js` — добавим helpers для `BackButton` (показать/скрыть/handler).

- [ ] **Step 1: Create web-app/js/utils.js**

```javascript
// Общие хелперы для всех экранов.

export function el(html) {
  // ВАЖНО: html здесь должен быть СТАТИЧЕСКИМ. Динамические данные —
  // через .textContent на named-узлах после el().
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html.trim();
  return tmpl.content.firstChild;
}

export function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function berlinTime(iso, fmt = "short") {
  // UTC ISO → "DD.MM HH:MM" (Europe/Berlin) для оператора.
  if (!iso) return "";
  try {
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    if (isNaN(d.getTime())) return iso;
    const opts = fmt === "full"
      ? { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }
      : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat("ru-RU", { ...opts, timeZone: "Europe/Berlin" }).format(d);
  } catch (e) {
    return iso;
  }
}

export function setLoading(mount, message = "Загрузка…") {
  mount.replaceChildren(el(`<p class="text-muted py-3">${esc(message)}</p>`));
}

export function setError(mount, message) {
  const card = el(`<div class="alert alert-danger" role="alert"><strong>Ошибка:</strong> <span class="msg"></span></div>`);
  card.querySelector(".msg").textContent = String(message ?? "");
  mount.replaceChildren(card);
}
```

Note: `setLoading` использует `${esc(message)}` — это первое использование `esc` для встраивания в HTML. Безопасно потому что `esc` уже escape-нул < > " &.

- [ ] **Step 2: Update web-app/js/tg.js — add BackButton helpers**

Replace целиком:

```javascript
// Helpers вокруг Telegram WebApp SDK.
// NOTE: telegram-web-app.js должен загружаться SYNC (no async/defer)
// в <head> до этого модуля; иначе window.Telegram.WebApp будет undefined.

export const tg = window.Telegram?.WebApp;

export function ready() {
  if (!tg) return;
  tg.ready();
  tg.expand();
}

export function initData() {
  return tg?.initData ?? "";
}

export function user() {
  return tg?.initDataUnsafe?.user ?? null;
}

export function startParam() {
  return tg?.initDataUnsafe?.start_param ?? null;
}

export function close() {
  tg?.close();
}

// BackButton API
export function showBack(handler) {
  if (!tg?.BackButton) return;
  tg.BackButton.show();
  // Снимаем старый handler если был, ставим новый
  tg.BackButton.offClick();
  tg.BackButton.onClick(handler);
}

export function hideBack() {
  if (!tg?.BackButton) return;
  tg.BackButton.offClick();
  tg.BackButton.hide();
}
```

- [ ] **Step 3: Create web-app/js/router.js**

```javascript
// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js";
import * as thread from "./screens/thread.js";
import * as history from "./screens/history.js";
import { setError } from "./utils.js";
import { hideBack, showBack } from "./tg.js";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, params: () => ({}) },
  { pattern: /^#\/thread\/(.+)$/,         screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
];

function navigateBack() {
  // Возврат к pipeline. window.history.back() мог бы работать, но если
  // оператор пришёл прямо на thread (через start_param) — back уведёт за пределы MA.
  // Поэтому всегда явно идём на /pipeline.
  if (location.hash === "#/pipeline" || !location.hash) {
    // already at root, do nothing
    return;
  }
  location.hash = "#/pipeline";
}

export function start(mount) {
  function dispatch() {
    const hash = location.hash || "#/";
    for (const r of ROUTES) {
      const m = hash.match(r.pattern);
      if (m) {
        // BackButton: показываем для всех экранов кроме pipeline (root).
        if (r.screen === pipeline) {
          hideBack();
        } else {
          showBack(navigateBack);
        }
        try {
          r.screen.render(mount, r.params(m));
        } catch (e) {
          console.error("[router] render failed:", e);
          setError(mount, e.message ?? String(e));
        }
        return;
      }
    }
    setError(mount, `Неизвестный путь: ${hash}`);
  }

  window.addEventListener("hashchange", dispatch);
  dispatch();
}
```

- [ ] **Step 4: Refactor web-app/js/app.js — drop renderHealth, use router**

Replace целиком:

```javascript
// Mini App entry point. Bootstrap → router.start.

import { ready, startParam } from "./tg.js";
import { start as startRouter } from "./router.js";

function applyStartParam() {
  // BotFather может отдать start_param через initDataUnsafe.start_param.
  // Формат: "screen_id" → переводим в hash.
  // Примеры: "review_123" → /review/123 (Phase 3),
  //          "thread_abc" → /thread/abc.
  const sp = startParam();
  if (!sp) return;
  const m = sp.match(/^([a-z]+)_(.+)$/);
  if (m) {
    const [, screen, id] = m;
    location.hash = `#/${screen}/${encodeURIComponent(id)}`;
  }
}

function main() {
  ready();
  applyStartParam();
  const mount = document.getElementById("app");
  startRouter(mount);
}

main();
```

- [ ] **Step 5: Stub the screens (will be filled in Tasks 6-8)**

Create EMPTY placeholder files so router imports work:

`web-app/js/screens/pipeline.js`:
```javascript
import { setLoading } from "../utils.js";

export function render(mount, params) {
  setLoading(mount, "Pipeline: stub (Task 6 fills this in)");
}
```

`web-app/js/screens/thread.js`:
```javascript
import { setLoading } from "../utils.js";

export function render(mount, params) {
  setLoading(mount, `Thread: stub for ${params.thread_id} (Task 7 fills this in)`);
}
```

`web-app/js/screens/history.js`:
```javascript
import { setLoading } from "../utils.js";

export function render(mount, params) {
  setLoading(mount, `History: stub for ${params.email} (Task 8 fills this in)`);
}
```

- [ ] **Step 6: Bump cache-bust**

`web-app/index.html`:
```html
<script type="module" src="js/app.js?v=20260510"></script>
```

- [ ] **Step 7: Syntax check JS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot/web-app/js && for f in *.js screens/*.js; do node --input-type=module --check < "$f" && echo "$f OK"; done'
```

Expected: каждый файл `OK`.

- [ ] **Step 8: Pytest sanity (no Python changes но проверим)**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/ -v'
```

Expected: 19 passed.

- [ ] **Step 9: Push к GitHub Pages**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/ && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): hash-router + screen stubs + utils + BackButton" && git -C /home/pg/kleinanzeigen-bot push origin main'
```

GitHub Pages пересоберёт за ~60 сек. Открой MA в Telegram — должен показать «Pipeline: stub». Тапнуть BackButton не появится (мы на root).

---

## Task 6: Pipeline screen

**Files:**
- Modify: `web-app/js/screens/pipeline.js`
- Create: `web-app/css/pipeline.css` (опционально — если нужны custom стили; иначе всё через Bootstrap inline)

**Background:** Pipeline screen — entry point. Shows red (🔴 ждут нас) сверху и green (🟢 ждём клиента) под этим. Каждый thread = карточка с ad_title, ad_price, deal_brief краткой строкой, время last_event, кнопка-link на thread detail.

Заголовок (header strip) с подсчётами секций как в боте: `🔴 ждут нас: N · 🟢 ждём клиента: N`.

Кнопка ↻ Refresh для пере-загрузки.

- [ ] **Step 1: Replace `web-app/js/screens/pipeline.js`**

```javascript
import { api } from "../api.js";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js";

function threadCard(thread) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="meta text-muted small mt-1"></div>
        </div>
        <div class="text-end small">
          <div class="when text-muted"></div>
          <div class="badges"></div>
        </div>
      </div>
    </a>
  `);

  card.href = `#/thread/${encodeURIComponent(thread.thread_id)}`;
  card.querySelector(".title").textContent =
    `${thread.ad_title ?? "(без названия)"} · ${thread.ad_price ?? "?"}`;

  const metaParts = [];
  if (thread.buyer_display_name) metaParts.push(`👤 ${thread.buyer_display_name}`);
  if (thread.deal_brief_json) {
    try {
      const brief = typeof thread.deal_brief_json === "string"
        ? JSON.parse(thread.deal_brief_json)
        : thread.deal_brief_json;
      if (brief?.summary_ru) metaParts.push(`💬 ${brief.summary_ru}`);
    } catch (e) { /* invalid JSON, skip */ }
  }
  card.querySelector(".meta").textContent = metaParts.join(" · ");

  card.querySelector(".when").textContent = berlinTime(thread.last_event_at);

  const badges = card.querySelector(".badges");
  if (thread.is_autopilot) {
    const b = el(`<span class="badge bg-warning text-dark">🤖 автопилот</span>`);
    badges.appendChild(b);
  }
  if (thread.pending_drafts_count > 0) {
    const b = el(`<span class="badge bg-info ms-1">📝 ${thread.pending_drafts_count}</span>`);
    badges.appendChild(b);
  }

  return card;
}

function sectionBlock(title, color, threads) {
  const sec = el(`
    <div class="mb-3">
      <h6 class="text-muted small text-uppercase mb-2"></h6>
      <div class="list-group list-group-flush"></div>
    </div>
  `);
  sec.querySelector("h6").textContent = `${color} ${title}: ${threads.length}`;
  const list = sec.querySelector(".list-group");
  if (threads.length === 0) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-1">пусто</div>`));
  } else {
    threads.forEach(t => list.appendChild(threadCard(t)));
  }
  return sec;
}

function refreshButton(onClick) {
  const btn = el(`<button class="btn btn-sm btn-outline-secondary mb-2">↻ Обновить</button>`);
  btn.addEventListener("click", onClick);
  return btn;
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю pipeline…");
  try {
    const data = await api("/api/ma/pipeline");
    const container = el(`<div></div>`);
    container.appendChild(refreshButton(() => render(mount, params)));
    // Сначала red (ждут нас), потом green (ждём клиента)
    container.appendChild(sectionBlock("ждут нас", "🔴", data.red ?? []));
    container.appendChild(sectionBlock("ждём клиента", "🟢", data.green ?? []));
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

- [ ] **Step 2: Bump cache-bust**

`web-app/index.html`: `?v=20260510-2`.

- [ ] **Step 3: JS syntax check**

```bash
ssh 192.168.88.28 'node --input-type=module --check < /home/pg/kleinanzeigen-bot/web-app/js/screens/pipeline.js && echo OK'
```

Expected: `OK`.

- [ ] **Step 4: Push + smoke в TG**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/ && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): pipeline screen with red/green sections"'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main'
```

Подожди ~60 сек, открой MA из TG. Должен показать секции с реальными тредами. Если backend требует рестарт (потому что Task 1-4 могут потребовать) — рестартани:

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
```

Если что-то не так — проверь Network tab в TG WebApp DevTools (на десктопе — правый клик → Inspect WebApp).

---

## Task 7: Thread detail screen

**Files:**
- Modify: `web-app/js/screens/thread.js`

**Background:** Thread detail = chat-style лог. Header сверху с ad-инфо + клиент + наш аккаунт. Затем events: каждый — bubble (incoming = слева серым, outgoing = справа синим). Auto-ack помечен 🤖. Ниже related-warning блок если matches > 0. Внизу — кнопка «↩ К pipeline» (BackButton API уже даёт системную кнопку, но для надёжности дублируем UI).

- [ ] **Step 1: Replace `web-app/js/screens/thread.js`**

```javascript
import { api } from "../api.js";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js";

function threadHeader(header) {
  const card = el(`
    <div class="border-bottom pb-2 mb-3">
      <div class="d-flex justify-content-between align-items-start">
        <div>
          <div class="fw-semibold ad-title"></div>
          <div class="text-muted small ad-price-buyer"></div>
          <div class="text-muted small account-info"></div>
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
  return card;
}

function eventBubble(event) {
  const isIn = event.kind === "in";
  const align = isIn ? "" : "ms-auto";
  const bg = isIn ? "bg-secondary-subtle" : "bg-primary-subtle";
  const tag = event.is_auto_ack ? "🤖 ack" : (isIn ? "👤" : "🏪");
  const bubble = el(`
    <div class="d-flex mb-2">
      <div class="bubble rounded p-2 ${align} ${bg}" style="max-width:80%">
        <div class="text-muted small d-flex justify-content-between mb-1">
          <span class="who"></span>
          <span class="when"></span>
        </div>
        <div class="text"></div>
        <div class="ru-text text-muted small fst-italic mt-1"></div>
      </div>
    </div>
  `);
  bubble.querySelector(".who").textContent = tag;
  bubble.querySelector(".when").textContent = berlinTime(event.ts);
  bubble.querySelector(".text").textContent = event.text ?? "";
  if (event.ru_text && event.ru_text !== event.text) {
    bubble.querySelector(".ru-text").textContent = event.ru_text;
  } else {
    bubble.querySelector(".ru-text").remove();
  }
  return bubble;
}

function relatedBlock(related) {
  if (!related?.matches?.length) return null;
  const block = el(`
    <div class="alert alert-warning small">
      <div class="mb-1"><strong>⚠️ Этот клиент уже писал по другим объявлениям:</strong></div>
      <ul class="mb-0 ps-3 matches"></ul>
    </div>
  `);
  const ul = block.querySelector(".matches");
  related.matches.forEach(m => {
    const li = el(`<li><a class="link"></a></li>`);
    const a = li.querySelector(".link");
    a.href = `#/thread/${encodeURIComponent(m.thread_id)}`;
    a.textContent = `${m.ad_title ?? "?"} · ${m.ad_price ?? "?"} · ${berlinTime(m.last_at)}`;
    ul.appendChild(li);
  });
  return block;
}

function backToPipeline() {
  const btn = el(`<a class="btn btn-sm btn-outline-secondary" href="#/pipeline">↩ К pipeline</a>`);
  return btn;
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);
    const container = el(`<div></div>`);
    container.appendChild(threadHeader(data.header));
    const related = relatedBlock(data.related);
    if (related) container.appendChild(related);
    if (data.events.length === 0) {
      container.appendChild(el(`<p class="text-muted">Событий пока нет.</p>`));
    } else {
      data.events.forEach(e => container.appendChild(eventBubble(e)));
    }
    container.appendChild(backToPipeline());
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

- [ ] **Step 2: Bump cache-bust**

`web-app/index.html`: `?v=20260510-3`.

- [ ] **Step 3: Syntax check + push**

```bash
ssh 192.168.88.28 'node --input-type=module --check < /home/pg/kleinanzeigen-bot/web-app/js/screens/thread.js && echo OK'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/ && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): thread detail screen with chat-style log"'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main'
```

Smoke в TG: pipeline → tap thread → должен показать events.

---

## Task 8: Client history screen

**Files:**
- Modify: `web-app/js/screens/history.js`

**Background:** Client history = таблица всех тредов клиента. Тап на тред → переход на `#/thread/...`. Header — email клиента крупно.

- [ ] **Step 1: Replace `web-app/js/screens/history.js`**

```javascript
import { api } from "../api.js";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js";

function threadRow(t) {
  const row = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="text-muted small meta"></div>
        </div>
        <div class="text-end small text-muted">
          <div class="when"></div>
          <div class="status badge bg-secondary"></div>
        </div>
      </div>
    </a>
  `);
  row.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  row.querySelector(".title").textContent = `${t.ad_title ?? "?"} · ${t.ad_price ?? "?"}`;
  row.querySelector(".meta").textContent = `${t.msg_count} сообщ.${t.ad_id ? ` · #${t.ad_id}` : ""}`;
  row.querySelector(".when").textContent = berlinTime(t.last_at);
  row.querySelector(".status").textContent = t.last_status ?? "?";
  return row;
}

export async function render(mount, params) {
  setLoading(mount, `Загружаю историю клиента ${params.email}…`);
  try {
    const data = await api(`/api/ma/clients/${encodeURIComponent(params.email)}/history`);
    const container = el(`
      <div>
        <h5 class="email-header"></h5>
        <div class="text-muted small total-count mb-2"></div>
        <div class="list-group list-group-flush mb-3"></div>
        <a class="btn btn-sm btn-outline-secondary" href="#/pipeline">↩ К pipeline</a>
      </div>
    `);
    container.querySelector(".email-header").textContent = data.buyer_email;
    container.querySelector(".total-count").textContent = `Всего тредов: ${data.threads.length}`;
    const list = container.querySelector(".list-group");
    if (data.threads.length === 0) {
      list.appendChild(el(`<div class="text-muted fst-italic px-2 py-1">Нет тредов.</div>`));
    } else {
      data.threads.forEach(t => list.appendChild(threadRow(t)));
    }
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

- [ ] **Step 2: Bump cache-bust**

`web-app/index.html`: `?v=20260510-4`.

- [ ] **Step 3: Syntax check + push**

```bash
ssh 192.168.88.28 'node --input-type=module --check < /home/pg/kleinanzeigen-bot/web-app/js/screens/history.js && echo OK'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/ && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): client history screen"'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main'
```

---

## Task 9: E2E smoke от Telegram

**Files:** none — это integration test.

**Background:** Phase 2 завершено когда оператор может:
1. Открыть MA из TG menu → видеть pipeline
2. Тапнуть тред → видеть thread detail
3. Из thread detail тапнуть related-buyer link → видеть thread (если есть related)
4. Через TG BackButton вернуться на pipeline
5. Открыть deep-link `https://...github.io/.../?tgWebAppStartParam=thread_<thread_id>` → сразу на thread detail

- [ ] **Step 1: Pipeline загрузка**

Открой MA в TG. Должен показать:
- Кнопку ↻ Обновить
- Секцию «🔴 ждут нас: N» с тредами (или «пусто»)
- Секцию «🟢 ждём клиента: N»
- Если есть треды — у каждого видны title, price, deal-brief краткое (если есть), время last event, бейджи 📝 N или 🤖 (если автопилот)

Если pipeline пустой — открой Telegram, отправь себе тестовое incoming-письмо в одной из связанных учёток, дождись пока бот его подхватит (1-2 мин), снова открой MA. Тред должен появиться.

- [ ] **Step 2: Thread detail**

Тапни на тред в pipeline. Должен показать:
- Header с ad_title, ценой, клиентом, нашим аккаунтом, иконкой 📎 (link на объявление)
- Related-warning если у клиента есть другие треды
- Хронологический лог событий: incoming-сообщения слева серым, наши outgoing справа синим
- Auto-ack события маркированы 🤖 ack
- Кнопка ↩ К pipeline внизу
- В Telegram появилась системная BackButton сверху

- [ ] **Step 3: Telegram BackButton работает**

Тапни системную TG BackButton (стрелка влево сверху чата) — должен вернуться на pipeline.

- [ ] **Step 4: Related → thread navigation**

Если в thread detail виден related-buyer block — тапни одну из ссылок. Должен открыть тот thread.

- [ ] **Step 5: Refresh button**

Вернись на pipeline. Тапни ↻ Обновить — pipeline должен перезагрузиться (без перезагрузки страницы).

- [ ] **Step 6: Deep-link через start_param**

В Telegram попроси кого-нибудь из контактов написать в чат с твоим ботом любую команду — это даст возможность создать inline-keyboard-кнопку с web_app + start_param. Или проще: создай в коде test-хендлер.

Альтернативно — открой URL вручную в браузере с дополнительным параметром:
`https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/?tgWebAppStartParam=thread_<существующий_thread_id>`

Только это не сработает в обычном браузере (нет initData). Тест в TG: пока нет inline-кнопок с web_app в боте — пропусти этот шаг до Phase 3.

- [ ] **Step 7: Phase 2 acceptance criteria**

Phase 2 закрыт когда:
- ✅ Pipeline показывает реальные треды
- ✅ Thread detail показывает корректный лог событий
- ✅ Client history открывается через related-warning ссылку (если есть related)
- ✅ TG BackButton возвращает на pipeline
- ✅ Никаких ошибок (HTTP 4xx/5xx) в Network tab при нормальном использовании
- ✅ Все 19 unit тестов backend проходят

---

## Operations summary (что не меняется в Phase 2)

- Cloudflared Quick Tunnel: `https://choice-drunk-curriculum-effectiveness.trycloudflare.com` (URL ротируется при рестарте `cloudflared`)
- GitHub Pages: `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/` (long URL — не меняли)
- BotFather menu button URL — тот же
- systemd unit `kleinanzeigen-bot.service` — без изменений

## Что НЕ делает Phase 2 (отложено)

- **Action endpoints** (send/skip/edit/regenerate/etc) — Phase 3
- **operator_lock рефакторинг** — Phase 3 (когда MA реально берёт lock)
- **Autopilot config screen + endpoints** — Phase 4
- **Settings screen** — Phase 4
- **Compose action** (`✉️ Написать клиенту`) — Phase 3
- **Live updates** (SSE / polling) — пока pull-to-refresh через ↻
- **Service worker / offline mode**
- **Pagination для pipeline** — пока ограничены 50-100 активными тредами максимум, пагинация не нужна
- **Search / filter** — Phase 4

## Risks

- **`sqlite3.Row` mocking в тестах** — фабрика `_row()` использует MagicMock с `__getitem__.side_effect`. Если в endpoint встречается `row.keys()` — мы возвращаем dict_keys. Если вызывается `dict(row)` — может потребоваться более полный mock. Если тест падает с `TypeError` — переключиться на `unittest.mock.MagicMock(spec=sqlite3.Row)` или сделать helper `_make_row` через namedtuple-like класс.
- **`ad_url` колонка** — в `pipeline_threads` SQL её может не быть в SELECT (надо проверить). Если падает — добавь в SELECT в `database.py:pipeline_threads`.
- **Mobile Safari hash routing quirks** — Telegram WebView на iOS иногда не triggerит `hashchange` event при программном `location.hash =`. Если будет — добавить ручной `dispatch()` вызов после `location.hash = ...`.
- **Pages cache TTL** — после `git push` старые операторы могут видеть прежнюю версию. Bump `?v=` в `index.html` каждый раз.
- **Cloudflared URL может смениться** — если рестарт tunnel-а; нужно обновить `web-app/js/api.js` + push + cache-bust.
