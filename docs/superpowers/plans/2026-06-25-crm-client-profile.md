# CRM: Профиль клиента Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить минималистичный экран истории клиента полноценным CRM-профилем: теги + заметка оператора, агрегаты (обращения/продажи/сумма), deal_brief по каждому треду, кнопка «Написать».

**Architecture:** Три слоя: (1) DB — новая таблица `client_profiles` + расширенный запрос тредов; (2) API — расширить GET history, добавить POST profile; (3) Frontend — новый `client.js` заменяет `history.js`, роутер обновляется.

**Tech Stack:** Python 3.11, SQLite, FastAPI, Vanilla JS ESM, Bootstrap 5.3

## Global Constraints

- Все строки комментариев в Python — на русском
- Запуск тестов: `cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v`
- Git команды: `GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git <cmd>`
- Рабочая папка на сервере: `/home/pg/kleinanzeigen-bot`
- Разрешённые теги клиента: `["Серьёзный", "Торгуется", "Тянет время", "Мошенник"]`
- Сортировка тредов: `ORDER BY last_at DESC` (свежие сверху)
- `display_name` из последнего messages.buyer_display_name, fallback = email
- `last_active_thread_id` — первый тред (по last_at DESC) со статусом не в `{skipped, skipped_sold, archived}`

---

## File Map

| Файл | Действие |
|---|---|
| `database.py` | Modify: добавить CREATE TABLE client_profiles в `_ensure_schema` |
| `modules/db_threads.py` | Modify: расширить `list_threads_for_client` + добавить `get_client_profile`, `upsert_client_profile` |
| `web/api_ma.py` | Modify: расширить `ma_client_history`, добавить `POST .../profile` |
| `tests/test_db_client_profile.py` | Create: DB unit tests (real SQLite temp db) |
| `tests/test_api_ma_clients.py` | Modify: добавить тесты новых полей и POST endpoint |
| `web-app/js/screens/client.js` | Create: новый экран профиля |
| `web-app/js/screens/history.js` | Delete |
| `web-app/js/router.js` | Modify: заменить `history` → `client` |

---

### Task 1: DB — таблица `client_profiles` + функции get/upsert

**Files:**
- Modify: `database.py` (после последнего `CREATE TABLE`, строка ~339)
- Modify: `modules/db_threads.py` (добавить в конец файла)
- Create: `tests/test_db_client_profile.py`

**Interfaces:**
- Produces:
  - `db_threads.get_client_profile(buyer_email: str) -> sqlite3.Row | None` — возвращает row с полями `tags_json`, `note`, `updated_at`; None если нет записи
  - `db_threads.upsert_client_profile(buyer_email: str, tags: list[str], note: str) -> None`

- [ ] **Step 1: Написать падающие тесты**

Создать файл `tests/test_db_client_profile.py`:

```python
"""Unit-тесты для client_profiles: get/upsert."""
from __future__ import annotations
import json
import pytest
import database
import modules.db_threads as db_threads


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    return db_path


def test_get_client_profile_returns_none_when_missing(tmp_db):
    result = db_threads.get_client_profile("nobody@example.com")
    assert result is None


def test_upsert_creates_profile(tmp_db):
    db_threads.upsert_client_profile("buyer@test.com", ["Серьёзный"], "хороший клиент")
    row = db_threads.get_client_profile("buyer@test.com")
    assert row is not None
    assert json.loads(row["tags_json"]) == ["Серьёзный"]
    assert row["note"] == "хороший клиент"


def test_upsert_updates_existing_profile(tmp_db):
    db_threads.upsert_client_profile("buyer@test.com", ["Серьёзный"], "первая заметка")
    db_threads.upsert_client_profile("buyer@test.com", ["Торгуется", "Мошенник"], "вторая")
    row = db_threads.get_client_profile("buyer@test.com")
    assert json.loads(row["tags_json"]) == ["Торгуется", "Мошенник"]
    assert row["note"] == "вторая"


def test_upsert_empty_tags_and_note(tmp_db):
    db_threads.upsert_client_profile("x@y.com", [], "")
    row = db_threads.get_client_profile("x@y.com")
    assert json.loads(row["tags_json"]) == []
    assert row["note"] == ""
```

- [ ] **Step 2: Запустить — убедиться что падает**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_db_client_profile.py -v
```

Ожидаем: `AttributeError: module 'modules.db_threads' has no attribute 'get_client_profile'`

- [ ] **Step 3: Добавить таблицу в `database.py`**

В `database.py`, после блока `scout_corrections` (строка ~339), внутри `init_db` → `with get_conn() as conn:` добавить:

```python
        # client_profiles — теги и заметки оператора по покупателю
        conn.execute("""
            CREATE TABLE IF NOT EXISTS client_profiles (
                buyer_email TEXT PRIMARY KEY,
                tags_json   TEXT NOT NULL DEFAULT '[]',
                note        TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
```

- [ ] **Step 4: Добавить функции в `modules/db_threads.py`**

В конец файла:

```python
# --- CLIENT PROFILES ---

def get_client_profile(buyer_email: str) -> Optional[sqlite3.Row]:
    """Теги и заметка оператора по покупателю. None если профиль не создан."""
    if not buyer_email:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM client_profiles WHERE buyer_email = ?",
            (buyer_email,),
        ).fetchone()


def upsert_client_profile(buyer_email: str, tags: list[str], note: str) -> None:
    """Сохранить или обновить теги + заметку для покупателя."""
    if not buyer_email:
        return
    import json as _json
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO client_profiles (buyer_email, tags_json, note, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(buyer_email) DO UPDATE SET
                tags_json  = excluded.tags_json,
                note       = excluded.note,
                updated_at = excluded.updated_at
            """,
            (buyer_email, _json.dumps(tags, ensure_ascii=False), note),
        )
```

- [ ] **Step 5: Запустить тесты — убедиться что проходят**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_db_client_profile.py -v
```

Ожидаем: 4 PASSED

- [ ] **Step 6: Запустить полный набор тестов**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v --tb=short
```

Ожидаем: все 201+ тестов PASSED

- [ ] **Step 7: Коммит**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add \
    database.py modules/db_threads.py tests/test_db_client_profile.py
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git commit \
    -m "feat(db): таблица client_profiles + get/upsert функции"
```

---

### Task 2: DB — расширить `list_threads_for_client` с `deal_brief_json`

**Files:**
- Modify: `modules/db_threads.py:list_threads_for_client`
- Modify: `tests/test_api_ma_clients.py`

**Interfaces:**
- Consumes: `list_threads_for_client(buyer_email)` из Task 1 (уже существует)
- Produces: каждая Row из `list_threads_for_client` теперь содержит поле `deal_brief_json` (TEXT или None)

- [ ] **Step 1: Написать падающий тест**

В `tests/test_api_ma_clients.py` добавить в существующий `client()` fixture поле `deal_brief_json` и новый тест:

```python
def test_client_history_includes_deal_brief(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="800€",
             msg_count=2, last_at="2026-06-24T10:00:00", last_status="skipped_sold",
             deal_brief_json='{"summary_ru":"Договорились","negotiated_price_eur":750,"client_assessment":"серьёзный"}'),
    ]
    mdb.get_client_profile.return_value = None
    mdb.get_conn = None  # заглушка — display_name и cost придут из отдельных queries
    init = make_init_data(TEST_USER)
    # Замокать get_conn чтобы display_name/cost queries вернули пустое
    with patch("web.api_ma.db.get_conn") as mock_conn:
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = mock_cursor
        res = c.get(
            f"/api/ma/clients/{quote('osman@x.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert len(body["threads"]) == 1
    brief = body["threads"][0].get("deal_brief")
    assert brief is not None
    assert brief["summary_ru"] == "Договорились"
    assert brief["negotiated_price_eur"] == 750
```

- [ ] **Step 2: Запустить — убедиться что падает**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py::test_client_history_includes_deal_brief -v
```

Ожидаем: FAILED (KeyError или AssertionError — `deal_brief` отсутствует в ответе)

- [ ] **Step 3: Расширить SQL в `list_threads_for_client`**

В `modules/db_threads.py`, функция `list_threads_for_client`, добавить подзапрос в SELECT перед `FROM messages m`:

```python
    sql = """
        SELECT
            m.gmail_thread_id AS thread_id,
            COUNT(*) AS msg_count,
            MAX(m.created_at) AS last_at,
            (SELECT status FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
             ORDER BY s.id DESC LIMIT 1) AS last_status,
            (SELECT ad_title FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_title IS NOT NULL AND ad_title != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_title,
            (SELECT ad_id FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_id IS NOT NULL AND ad_id != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_id,
            (SELECT ad_price FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND ad_price IS NOT NULL AND ad_price != ''
             ORDER BY s.id DESC LIMIT 1) AS ad_price,
            (SELECT deal_brief_json FROM messages s
             WHERE s.gmail_thread_id = m.gmail_thread_id
               AND s.deal_brief_json IS NOT NULL AND s.deal_brief_json != ''
             ORDER BY s.id DESC LIMIT 1) AS deal_brief_json
        FROM messages m
        WHERE m.buyer_name = ?
          AND m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
        GROUP BY m.gmail_thread_id
        ORDER BY last_at DESC
    """
```

- [ ] **Step 4: Запустить тест**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py -v --tb=short
```

(Тест с deal_brief пока может падать — API ещё не возвращает поле. Проверяем что старые тесты не сломались)

- [ ] **Step 5: Запустить полный набор**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v --tb=short
```

Ожидаем: все существующие тесты PASSED (новый может падать до Task 3)

- [ ] **Step 6: Коммит**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add modules/db_threads.py tests/test_api_ma_clients.py
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git commit \
    -m "feat(db): добавить deal_brief_json в list_threads_for_client"
```

---

### Task 3: API — расширить `GET /api/ma/clients/{email}/history`

**Files:**
- Modify: `web/api_ma.py` (функция `ma_client_history`, строка ~156)
- Modify: `tests/test_api_ma_clients.py`

**Interfaces:**
- Consumes:
  - `db.list_threads_for_client(email)` → rows с `deal_brief_json` (Task 2)
  - `db.get_client_profile(email)` → Row | None (Task 1)
  - `db.get_conn()` → для display_name и total_cost подзапросов
- Produces: расширенный JSON ответ со всеми новыми полями

- [ ] **Step 1: Написать падающие тесты**

Добавить в `tests/test_api_ma_clients.py`:

```python
def _make_client(monkeypatch_fixture=None):
    """Вспомогательная фабрика fixture с полным набором моков для новых полей."""
    pass  # см. ниже — тесты используют контекстный менеджер patch напрямую


def test_client_history_returns_aggregates(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="800€",
             msg_count=3, last_at="2026-06-24T10:00:00", last_status="sent",
             deal_brief_json=None),
        _row(thread_id="t2", ad_title="B", ad_id="2", ad_price="500€",
             msg_count=1, last_at="2026-06-20T10:00:00", last_status="skipped_sold",
             deal_brief_json='{"summary_ru":"ok","negotiated_price_eur":500,"client_assessment":"серьёзный"}'),
    ]
    mdb.get_client_profile.return_value = None
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('buyer@test.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert "display_name" in body
    assert "tags" in body
    assert "note" in body
    assert "sold_count" in body
    assert "total_negotiated_eur" in body
    assert "last_active_thread_id" in body
    assert body["sold_count"] == 1
    assert body["total_negotiated_eur"] == 500
    assert body["last_active_thread_id"] == "t1"  # t1 статус sent — активный


def test_client_history_last_active_none_when_all_closed(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = [
        _row(thread_id="t1", ad_title="A", ad_id="1", ad_price="100€",
             msg_count=1, last_at="2026-06-24T10:00:00", last_status="skipped",
             deal_brief_json=None),
        _row(thread_id="t2", ad_title="B", ad_id="2", ad_price="200€",
             msg_count=1, last_at="2026-06-20T10:00:00", last_status="skipped_sold",
             deal_brief_json=None),
    ]
    mdb.get_client_profile.return_value = None
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('buyer@test.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    assert res.json()["last_active_thread_id"] is None


def test_client_history_returns_tags_from_profile(client):
    c, mdb = client
    mdb.list_threads_for_client.return_value = []
    profile_row = _row(tags_json='["Серьёзный","Торгуется"]', note="заметка теста")
    mdb.get_client_profile.return_value = profile_row
    init = make_init_data(TEST_USER)
    with patch("web.api_ma.db.get_conn") as mock_conn:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.execute.return_value.fetchone.return_value = None
        mock_conn.return_value = ctx
        res = c.get(
            f"/api/ma/clients/{quote('x@y.com')}/history",
            headers={"X-Telegram-Init-Data": init},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["tags"] == ["Серьёзный", "Торгуется"]
    assert body["note"] == "заметка теста"
```

- [ ] **Step 2: Запустить — убедиться что падают**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py::test_client_history_returns_aggregates tests/test_api_ma_clients.py::test_client_history_last_active_none_when_all_closed tests/test_api_ma_clients.py::test_client_history_returns_tags_from_profile -v
```

Ожидаем: 3 FAILED (KeyError — `display_name` отсутствует)

- [ ] **Step 3: Переписать `ma_client_history` в `web/api_ma.py`**

Заменить функцию `ma_client_history` (строка ~156):

```python
_CLOSED_STATUSES = {"skipped", "skipped_sold", "archived"}
_ALLOWED_TAGS = {"Серьёзный", "Торгуется", "Тянет время", "Мошенник"}


@router.get("/clients/{email}/history")
async def ma_client_history(email: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Профиль клиента: треды + deal_brief + теги + агрегаты."""
    rows = db.list_threads_for_client(email)

    # display_name из последнего сообщения с непустым полем
    display_name = email
    with db.get_conn() as conn:
        dn_row = conn.execute(
            "SELECT buyer_display_name FROM messages "
            "WHERE buyer_name = ? AND buyer_display_name IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (email,),
        ).fetchone()
        if dn_row:
            display_name = dn_row["buyer_display_name"]

    # total_cost_usd — сумма по всем сообщениям клиента
    with db.get_conn() as conn:
        cost_row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM messages WHERE buyer_name = ?",
            (email,),
        ).fetchone()
    total_cost_usd = round(float(cost_row["total"]), 5) if cost_row else 0.0

    # Теги и заметка из client_profiles
    profile = db.get_client_profile(email)
    tags: list[str] = []
    note: str = ""
    if profile:
        try:
            tags = json.loads(profile["tags_json"]) or []
        except (json.JSONDecodeError, TypeError):
            tags = []
        note = profile["note"] or ""

    # Собрать треды + посчитать агрегаты
    threads = []
    sold_count = 0
    total_negotiated_eur = 0
    last_active_thread_id = None
    for r in rows:
        brief = _parse_deal_brief(r["deal_brief_json"]) if "deal_brief_json" in r.keys() else None
        status = r["last_status"] or ""
        # last_active_thread_id — первый (свежий) тред не в закрытых статусах
        if last_active_thread_id is None and status not in _CLOSED_STATUSES:
            last_active_thread_id = r["thread_id"]
        # sold_count / total_negotiated_eur
        if status == "skipped_sold" and brief:
            price = brief.get("negotiated_price_eur") or 0
            if price and price > 0:
                sold_count += 1
                total_negotiated_eur += price
        threads.append({
            "thread_id": r["thread_id"],
            "ad_title": r["ad_title"],
            "ad_id": r["ad_id"],
            "ad_price": r["ad_price"],
            "msg_count": r["msg_count"],
            "last_at": r["last_at"],
            "last_status": status,
            "deal_brief": brief,
        })

    return {
        "buyer_email": email,
        "display_name": display_name,
        "total_cost_usd": total_cost_usd,
        "tags": tags,
        "note": note,
        "sold_count": sold_count,
        "total_negotiated_eur": total_negotiated_eur,
        "last_active_thread_id": last_active_thread_id,
        "threads": threads,
    }
```

- [ ] **Step 4: Запустить тесты**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py -v --tb=short
```

Ожидаем: все тесты в файле PASSED

- [ ] **Step 5: Запустить полный набор**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v --tb=short
```

Ожидаем: все тесты PASSED

- [ ] **Step 6: Коммит**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add \
    web/api_ma.py tests/test_api_ma_clients.py
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git commit \
    -m "feat(api): расширить GET /clients/{email}/history — профиль, теги, агрегаты, deal_brief"
```

---

### Task 4: API — `POST /api/ma/clients/{email}/profile`

**Files:**
- Modify: `web/api_ma.py` (добавить после `ma_client_history`)
- Modify: `tests/test_api_ma_clients.py`

**Interfaces:**
- Consumes: `db.upsert_client_profile(email, tags, note)` (Task 1)
- Produces: `POST /api/ma/clients/{email}/profile` → `{"ok": true}`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_api_ma_clients.py`:

```python
def test_client_profile_post_saves_tags_and_note(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post(
        f"/api/ma/clients/{quote('buyer@test.com')}/profile",
        headers={"X-Telegram-Init-Data": init},
        json={"tags": ["Серьёзный", "Торгуется"], "note": "важный клиент"},
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    mdb.upsert_client_profile.assert_called_once_with(
        "buyer@test.com", ["Серьёзный", "Торгуется"], "важный клиент"
    )


def test_client_profile_post_filters_invalid_tags(client):
    c, mdb = client
    init = make_init_data(TEST_USER)
    res = c.post(
        f"/api/ma/clients/{quote('x@y.com')}/profile",
        headers={"X-Telegram-Init-Data": init},
        json={"tags": ["Серьёзный", "НеизвестныйТег", "Мошенник"], "note": ""},
    )
    assert res.status_code == 200
    # НеизвестныйТег отфильтрован, сохранены только допустимые в том же порядке
    mdb.upsert_client_profile.assert_called_once_with(
        "x@y.com", ["Серьёзный", "Мошенник"], ""
    )


def test_client_profile_post_requires_auth(client):
    c, mdb = client
    res = c.post(
        f"/api/ma/clients/{quote('x@y.com')}/profile",
        json={"tags": [], "note": ""},
    )
    assert res.status_code == 422
```

- [ ] **Step 2: Запустить — убедиться что падают**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py::test_client_profile_post_saves_tags_and_note tests/test_api_ma_clients.py::test_client_profile_post_filters_invalid_tags tests/test_api_ma_clients.py::test_client_profile_post_requires_auth -v
```

Ожидаем: 3 FAILED (404 — endpoint не существует)

- [ ] **Step 3: Добавить Pydantic-модель и endpoint в `web/api_ma.py`**

После `ma_client_history` добавить:

```python
class ClientProfilePayload(BaseModel):
    tags: list[str] = Field(default_factory=list)
    note: str = Field(default="")


@router.post("/clients/{email}/profile")
async def ma_client_profile_save(
    email: str,
    payload: ClientProfilePayload,
    user: dict = Depends(verify_init_data_dep),
) -> dict[str, Any]:
    """Сохранить теги и заметку оператора для покупателя."""
    clean_tags = [t for t in payload.tags if t in _ALLOWED_TAGS]
    db.upsert_client_profile(email, clean_tags, payload.note)
    return {"ok": True}
```

- [ ] **Step 4: Запустить тесты**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/test_api_ma_clients.py -v --tb=short
```

Ожидаем: все тесты PASSED

- [ ] **Step 5: Запустить полный набор**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v --tb=short
```

Ожидаем: все тесты PASSED

- [ ] **Step 6: Коммит**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add \
    web/api_ma.py tests/test_api_ma_clients.py
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git commit \
    -m "feat(api): POST /clients/{email}/profile — сохранение тегов и заметки"
```

---

### Task 5: Frontend — `client.js` + обновление роутера

**Files:**
- Create: `web-app/js/screens/client.js`
- Delete: `web-app/js/screens/history.js`
- Modify: `web-app/js/router.js`

**Interfaces:**
- Consumes: `GET /api/ma/clients/{email}/history` (Task 3), `POST /api/ma/clients/{email}/profile` (Task 4)

- [ ] **Step 1: Создать `web-app/js/screens/client.js`**

```javascript
// 👤 Профиль клиента — CRM карточка: шапка, теги, заметка, треды с deal_brief.
import { api } from "../api.js?v=20260624-024500";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260624-024500";

const ALLOWED_TAGS = ["Серьёзный", "Торгуется", "Тянет время", "Мошенник"];

const STATUS_LABELS = {
  sent: "отправлен",
  sent_debug: "отправлен",
  pending: "ждёт",
  new: "ждёт",
  edited: "ждёт",
  approved: "ждёт",
  skipped: "пропущен",
  skipped_sold: "продан",
  archived: "архив",
};

function statusLabel(raw) {
  return STATUS_LABELS[raw] ?? raw ?? "статус";
}

function threadCard(t) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="brief text-muted small mt-1 fst-italic"></div>
        </div>
        <div class="text-end small">
          <div class="when text-muted"></div>
          <span class="badge bg-secondary status mt-1"></span>
        </div>
      </div>
    </a>
  `);
  card.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  card.querySelector(".title").textContent =
    `${t.ad_title ?? "(без названия)"} · ${t.ad_price ?? "?"}`;
  card.querySelector(".when").textContent = berlinTime(t.last_at);
  card.querySelector(".status").textContent = statusLabel(t.last_status);

  if (t.deal_brief) {
    const b = t.deal_brief;
    const parts = [];
    if (b.summary_ru) parts.push(b.summary_ru);
    if (b.client_assessment) parts.push(b.client_assessment);
    if (b.negotiated_price_eur) parts.push(`${b.negotiated_price_eur}€`);
    if (parts.length) card.querySelector(".brief").textContent = parts.join(" · ");
  }
  return card;
}

export async function render(mount, params) {
  const email = params.email;
  setLoading(mount, `Загружаю профиль ${email}…`);
  let data;
  try {
    data = await api(`/api/ma/clients/${encodeURIComponent(email)}/history`);
  } catch (e) { setError(mount, e.message ?? String(e)); return; }

  // Локальное состояние тегов (мутируется при тогглах)
  let currentTags = Array.isArray(data.tags) ? [...data.tags] : [];

  const root = el(`
    <div>
      <div class="mb-3">
        <div class="fw-bold fs-5 name"></div>
        <div class="text-muted small email-line"></div>
        <div class="d-flex gap-3 mt-2 stats text-muted small"></div>
      </div>

      <div class="mb-2 tag-buttons d-flex flex-wrap gap-1"></div>

      <textarea class="form-control form-control-sm mb-1 note-area" rows="2"
        placeholder="Заметка оператора…"></textarea>
      <div class="d-flex justify-content-end mb-3">
        <button class="btn btn-sm btn-outline-primary save-btn">💾 Сохранить</button>
      </div>

      <div class="write-wrap mb-3 d-none">
        <a class="btn btn-sm btn-outline-success write-btn">✉️ Написать</a>
      </div>

      <div class="text-muted small fw-semibold mb-1">──── Переписки ────</div>
      <div class="list-group list-group-flush thread-list"></div>
    </div>
  `);

  root.querySelector(".name").textContent = `👤 ${esc(data.display_name || email)}`;
  root.querySelector(".email-line").textContent = email;

  // Статистика
  const stats = root.querySelector(".stats");
  stats.innerHTML = `
    <span>${data.threads.length} обращ.</span>
    <span>${data.sold_count} продажа</span>
    ${data.total_negotiated_eur ? `<span>${data.total_negotiated_eur}€ итог</span>` : ""}
  `;

  // Кнопки тегов
  const tagWrap = root.querySelector(".tag-buttons");
  ALLOWED_TAGS.forEach(tag => {
    const btn = el(`<button class="btn btn-sm"></button>`);
    btn.textContent = tag;
    const active = currentTags.includes(tag);
    btn.className = `btn btn-sm ${active ? "btn-secondary" : "btn-outline-secondary"}`;
    btn.addEventListener("click", () => {
      if (currentTags.includes(tag)) {
        currentTags = currentTags.filter(t => t !== tag);
        btn.className = "btn btn-sm btn-outline-secondary";
      } else {
        currentTags.push(tag);
        btn.className = "btn btn-sm btn-secondary";
      }
    });
    tagWrap.appendChild(btn);
  });

  // Заметка
  const noteArea = root.querySelector(".note-area");
  noteArea.value = data.note || "";

  // Кнопка Сохранить
  const saveBtn = root.querySelector(".save-btn");
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "⏳…";
    try {
      await api(`/api/ma/clients/${encodeURIComponent(email)}/profile`, {
        method: "POST",
        body: { tags: currentTags, note: noteArea.value },
      });
      saveBtn.textContent = "✅ Сохранено";
    } catch (e) {
      saveBtn.textContent = "❌ Ошибка";
    } finally {
      setTimeout(() => { saveBtn.disabled = false; saveBtn.textContent = "💾 Сохранить"; }, 1500);
    }
  });

  // Кнопка Написать
  if (data.last_active_thread_id) {
    const writeWrap = root.querySelector(".write-wrap");
    writeWrap.classList.remove("d-none");
    root.querySelector(".write-btn").href =
      `#/thread/${encodeURIComponent(data.last_active_thread_id)}`;
  }

  // Список тредов
  const list = root.querySelector(".thread-list");
  if (data.threads.length === 0) {
    list.appendChild(el(`<div class="text-muted fst-italic px-2 py-2">Нет переписок.</div>`));
  } else {
    data.threads.forEach(t => list.appendChild(threadCard(t)));
  }

  mount.replaceChildren(root);
}
```

- [ ] **Step 2: Обновить `web-app/js/router.js`**

Заменить строку с импортом `history`:
```js
import * as history from "./screens/history.js?v=20260624-024500";
```
На:
```js
import * as client from "./screens/client.js?v=20260625-1";
```

Заменить строку роута:
```js
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
```
На:
```js
  { pattern: /^#\/client\/(.+)$/,         screen: client, params: m => ({ email: decodeURIComponent(m[1]) }) },
```

В блоке `hideBack` в `dispatch()` заменить:
```js
        if (r.screen === pipeline || r.screen === dashboard || r.screen === clients || r.screen === sales || r.screen === scout) {
```
На (убрать `history`, добавить `client` — `client` — это deep-экран, back нужен):
```js
        if (r.screen === pipeline || r.screen === dashboard || r.screen === clients || r.screen === sales || r.screen === scout) {
```
(В этом блоке `history` не фигурировало — back уже работает для `/client/` маршрута. Ничего менять не нужно.)

- [ ] **Step 3: Удалить `history.js`**

```bash
ssh 192.168.88.28 'rm /home/pg/kleinanzeigen-bot/web-app/js/screens/history.js'
```

- [ ] **Step 4: Проверить синтаксис JS через node**

```bash
ssh 192.168.88.28 'node --input-type=module < /home/pg/kleinanzeigen-bot/web-app/js/screens/client.js 2>&1 || true'
```

Ожидаем: сообщение об ошибке импорта (файл не в модульном окружении) — это нормально, главное что не `SyntaxError`.

- [ ] **Step 5: Запустить все тесты**

```bash
cd /home/pg/kleinanzeigen-bot && python -m pytest tests/ -v --tb=short
```

Ожидаем: все тесты PASSED

- [ ] **Step 6: Перезапустить сервис и проверить в браузере**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
ssh 192.168.88.28 'systemctl status kleinanzeigen-bot --no-pager -n 5'
```

Открыть Mini App → Клиенты → кликнуть на любого клиента → убедиться что открывается новый профиль-экран.

- [ ] **Step 7: Коммит**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git add \
    web-app/js/screens/client.js web-app/js/router.js
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git rm \
    web-app/js/screens/history.js
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git commit \
    -m "feat(ma): экран профиля клиента — теги, заметка, агрегаты, deal_brief, Написать"
```

- [ ] **Step 8: Пуш на GitHub**

```bash
GIT_DIR=/home/pg/kleinanzeigen-bot/.git GIT_WORK_TREE=/home/pg/kleinanzeigen-bot git push origin main
```

GitHub Pages пересоберётся ~60 сек.
