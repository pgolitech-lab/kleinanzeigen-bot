# Telegram Mini App — Phase 4 (Compose + Autopilot + Settings + deep-link) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в MA 4 функции (compose / autopilot start-stop / settings KV editor / deep-link `/review/{msg_id}`) которые сейчас живут только в боте — это финальный шаг чтобы Phase 5 мог удалить дублирующие UI handlers из telegram_bot.py.

**Architecture:** 5 новых endpoint'ов под `/api/ma/*`. Backend использует существующие helpers: `scheduler.send_manual_compose(source_msg_id, operator_text)`, `db.start_thread_autopilot(thread_id, floor, mode, started_by)`, `db.stop_thread_autopilot(thread_id, reason)`, `telegram_bot.send_autopilot_start_notification(msg_id, floor, actor)`, `db.set_setting(key, value)`. Frontend: 2 новых form-компонента (compose, autopilot) + 1 новый screen (settings) + 1 deep-link redirector. Cache-bust до `?v=20260510-12`.

**Tech Stack:** Python 3.11 + FastAPI + asyncio.to_thread / pytest + TestClient + MagicMock / vanilla JS + Bootstrap 5.3 + Pydantic.

**Все команды на проде через ssh** (`ssh 192.168.88.28 ...`). Worktree `/home/pg/kleinanzeigen-bot-ma` на ветке `ma-phase4`.

**Spec reference:** `docs/superpowers/specs/2026-05-10-tg-mini-app-phase4-design.md`

**Backup point:** `pre-phase4-2026-05-10` (commit `9d81f1d`). DB snapshot в `/home/pg/backups/db-pre-phase4-2026-05-10.db`. Rollback procedure в `/home/pg/backups/ROLLBACK.md`.

**Worktree setup (один раз перед Task 1):**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree add /home/pg/kleinanzeigen-bot-ma -b ma-phase4 && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `78 passed`. Worktree на ветке `ma-phase4`.

---

## File Structure

**Создаются:**
- `tests/test_api_ma_compose.py` — 3 tests
- `tests/test_api_ma_autopilot.py` — 5 tests
- `tests/test_api_ma_settings.py` — 5 tests
- `web-app/js/components/compose-form.js`
- `web-app/js/components/autopilot-form.js`
- `web-app/js/screens/settings.js`
- `web-app/js/screens/review.js` (thin redirect)

**Модифицируются:**
- `web/api_ma.py` — +5 endpoints (compose / autopilot.start / autopilot.stop / settings.get / settings.post), +Pydantic models, ALLOWED_SETTING_KEYS whitelist
- `web-app/js/screens/thread.js` — `✉️ Написать` button + autopilot section (start-form / stop-button)
- `web-app/js/screens/pipeline.js` — +⚙ link to `#/settings`
- `web-app/js/router.js` — +/settings и /review/{msg_id} routes
- `web-app/index.html` — bump cache-bust до `?v=20260510-12`
- All ESM imports in `web-app/js/**/*.js` — bump query string до `?v=20260510-12`

---

## Task 1: Compose endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_compose.py`

**Background:** `scheduler.send_manual_compose(source_msg_id: int, operator_text: str) -> dict[str, Any]` — public helper. Принимает msg_id из треда (любая row), берёт thread_id + account из неё. Возвращает result-dict.

Endpoint принимает `thread_id` URL-параметром, бекенд находит latest msg_id в треде через `db.thread_history(thread_id)[-1]` (или `MAX(id) WHERE direction='in'`).

### Step 1: Write failing tests

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_compose.py`:

```python
"""TestClient тесты для POST /api/ma/threads/{thread_id}/compose."""
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
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol, \
         patch("web.api_ma.scheduler") as msched, \
         patch("web.api_ma.claude") as mclaude:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = [
            _row(id=42, direction="in", gmail_thread_id="abc", status="sent"),
        ]
        msched.send_manual_compose.return_value = {"kind": "ok", "message_id": 99}
        from web.app import app
        yield TestClient(app), mdb, msched


def test_compose_success(client):
    c, mdb, msched = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "Здравствуйте, у меня вопрос..."},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    msched.send_manual_compose.assert_called_once_with(42, "Здравствуйте, у меня вопрос...")


def test_compose_404_thread_missing(client):
    c, mdb, msched = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/compose",
                 json={"text": "test"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_compose_empty_text_422(client):
    c, mdb, msched = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_compose_smtp_failure_500(client):
    c, mdb, msched = client
    msched.send_manual_compose.return_value = {"kind": "error", "message": "SMTP timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "test"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    assert "SMTP timeout" in res.json()["detail"]


def test_compose_requires_auth(client):
    c, mdb, msched = client
    res = c.post("/api/ma/threads/abc/compose", json={"text": "test"})
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_compose.py -v'
```

Expected: 5 fail (404 endpoint).

### Step 3: Add endpoint to web/api_ma.py

Append after `ma_instruction` (Phase 3b last endpoint):

```python
class ComposeBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/threads/{thread_id}/compose")
async def ma_compose(thread_id: str, body: ComposeBody,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Operator-initiated message в тред (compose-режим)."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    # Берём последнюю row треда (любую) — send_manual_compose извлечёт thread_id и account
    source_msg_id = history[-1]["id"]

    import asyncio
    result = await asyncio.to_thread(
        scheduler.send_manual_compose, source_msg_id, body.text
    )
    if result.get("kind") == "error":
        raise HTTPException(500, result.get("message", "compose failed"))

    return {
        "ok": True,
        "sent_msg_id": result.get("message_id"),
        "thread_id": thread_id,
    }
```

### Step 4: Run tests, PASS

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_compose.py -v'
```

Expected: `5 passed`.

### Step 5: Run full suite

```bash
ssh 192.168.88.28 'python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `83 passed` (78 + 5).

### Step 6: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_compose.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): compose endpoint for operator-initiated messages"'
```

---

## Task 2: Autopilot start/stop endpoints (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_autopilot.py`

**Background:**
- `db.start_thread_autopilot(thread_id, floor_price_eur, notify_mode, started_by)` — sync
- `db.stop_thread_autopilot(thread_id, reason)` — sync
- `telegram_bot.send_autopilot_start_notification(msg_id, floor, actor)` — sync, шлёт DM-fanout (только если notify_mode='notify')
- `telegram_bot.send_autopilot_stop_notification(msg_id, reason)` — sync, reason='manual' = silent

Notification helpers требуют msg_id (последний в треде). Берём из `db.thread_history(thread_id)[-1]["id"]`.

### Step 1: Write failing tests

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_autopilot.py`:

```python
"""TestClient тесты для POST /api/ma/threads/{id}/autopilot/{start,stop}."""
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
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = [
            _row(id=42, direction="in", gmail_thread_id="abc", status="pending",
                 ad_title="Sitzbank", ad_price="1500€", ad_url=None, ad_id=None,
                 buyer_display_name="Osman", buyer_name="osman@x.com",
                 client_lang="de", de_client="", ru_client="",
                 ru_answer="", de_answer="", ru_translation="",
                 deal_brief_json=None, extra_notes=None, is_auto_ack=0,
                 account_id=1),
        ]
        mdb.thread_events.return_value = []
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mdb.get_account.return_value = _row(name="main", gmail_email="us@gmail.com")
        from web.app import app
        yield TestClient(app), mdb, mtb


def test_autopilot_start_silent(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1200, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["header"]["thread_id"] == "abc"
    mdb.start_thread_autopilot.assert_called_once()
    args, kwargs = mdb.start_thread_autopilot.call_args
    # signature: (thread_id, floor_price_eur, notify_mode, started_by=...)
    assert args[0] == "abc"
    assert args[1] == 1200
    assert args[2] == "silent"
    # Silent → notification NOT sent
    mtb.send_autopilot_start_notification.assert_not_called()


def test_autopilot_start_notify_sends_notification(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "notify"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mtb.send_autopilot_start_notification.assert_called_once()
    args, kwargs = mtb.send_autopilot_start_notification.call_args
    # signature: (msg_id, floor, actor)
    assert args[0] == 42  # latest msg_id из thread_history
    assert args[1] == 1500
    assert args[2] == "@pgtest#999"


def test_autopilot_start_invalid_floor_negative_422(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": -100, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_autopilot_start_invalid_notify_mode_422(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "loud"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_autopilot_start_404_thread_missing(client):
    c, mdb, mtb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_autopilot_stop_success(client):
    c, mdb, mtb = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/autopilot/stop",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mdb.stop_thread_autopilot.assert_called_once_with("abc", "manual")


def test_autopilot_stop_404_thread_missing(client):
    c, mdb, mtb = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/autopilot/stop",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_autopilot_endpoints_require_auth(client):
    c, mdb, mtb = client
    res = c.post("/api/ma/threads/abc/autopilot/start",
                 json={"floor_eur": 1500, "notify_mode": "silent"})
    assert res.status_code == 422
    res = c.post("/api/ma/threads/abc/autopilot/stop")
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_autopilot.py -v'
```

### Step 3: Add endpoints to web/api_ma.py

```python
class AutopilotStartBody(BaseModel):
    floor_eur: float = Field(..., gt=0, lt=100000)
    notify_mode: str

    @field_validator("notify_mode")
    @classmethod
    def _check_notify(cls, v: str) -> str:
        if v not in {"silent", "notify"}:
            raise ValueError("notify_mode must be 'silent' or 'notify'")
        return v


@router.post("/threads/{thread_id}/autopilot/start")
async def ma_autopilot_start(thread_id: str, body: AutopilotStartBody,
                              user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Запустить автопилот для треда."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    actor = actor_from_user(user)
    msg_id = history[-1]["id"]

    db.start_thread_autopilot(thread_id, body.floor_eur, body.notify_mode, actor)

    if body.notify_mode == "notify":
        try:
            telegram_bot.send_autopilot_start_notification(msg_id, body.floor_eur, actor)
        except Exception:
            pass  # best-effort

    # Возвращаем обновлённый thread payload
    return _thread_dict(thread_id)


@router.post("/threads/{thread_id}/autopilot/stop")
async def ma_autopilot_stop(thread_id: str,
                             user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Остановить автопилот (manual stop — silent)."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
    db.stop_thread_autopilot(thread_id, "manual")
    return _thread_dict(thread_id)
```

**ВНИМАНИЕ:** `_thread_dict(thread_id)` — нужен helper аналогичный `_message_review_dict(msg_id)` из Phase 3b. Если его ещё нет — extract из `ma_thread` (Phase 2 endpoint).

Add helper near `_message_review_dict` if missing:

```python
def _thread_dict(thread_id: str) -> dict[str, Any]:
    """Полный thread payload (header + events + related). Used by autopilot endpoints."""
    history = db.thread_history(thread_id)
    if not history:
        raise HTTPException(404, "thread not found")
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
        "buyer_email": last_in["buyer_name"] if "buyer_name" in last_in.keys() else None,
        "account_name": account["name"] if account is not None else None,
        "account_email": account["gmail_email"] if account is not None else None,
        "is_autopilot": is_autopilot,
    }
    events = [_event_to_api(e) for e in db.thread_events(thread_id)]
    related_matches = db.find_related_inquiries(
        last_in["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if last_in["buyer_display_name"] else []
    related = {
        "buyer_display_name": last_in["buyer_display_name"],
        "matches": [_related_match(r) for r in related_matches],
    }
    return {"header": header, "events": events, "related": related}
```

И затем `ma_thread` (Phase 2) обновить чтобы делегировал на `_thread_dict`:

```python
@router.get("/threads/{thread_id}")
async def ma_thread(thread_id: str, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _thread_dict(thread_id)
```

### Step 4-6: Run tests + commit

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_autopilot.py -v'
ssh 192.168.88.28 'python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `91 passed` (83 + 8 new autopilot tests).

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_autopilot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): autopilot start/stop endpoints + extract _thread_dict helper"'
```

---

## Task 3: Settings GET/POST endpoints (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_settings.py`

**Background:** `config.get(key)` reads from settings KV table. `db.set_setting(key, value)` writes. Whitelist allowed keys в backend; sensitive masking при GET.

### Step 1: Write failing tests

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_settings.py`:

```python
"""TestClient тесты для GET/POST /api/ma/settings."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.config") as mc2:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mc2.get.side_effect = lambda k: {
            "send_mode": "disabled",
            "gmail_poll_interval_sec": "60",
            "anthropic_api_key": "sk-ant-secret-12345",
            "telegram_bot_token": "secret-bot-token-67890",
            "polling_paused": "0",
        }.get(k, "")
        from web.app import app
        yield TestClient(app), mdb, mc2


def test_settings_get_returns_dict(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/settings", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, dict)
    assert "send_mode" in body
    assert body["send_mode"] == "disabled"


def test_settings_get_masks_secrets(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/settings", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    # Secrets must be masked
    assert "secret" not in body.get("anthropic_api_key", "").lower()
    assert "secret" not in body.get("telegram_bot_token", "").lower()
    assert body["anthropic_api_key"].startswith("•")
    assert body["telegram_bot_token"].startswith("•")


def test_settings_post_valid_update(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "send_mode", "value": "production"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    mdb.set_setting.assert_called_once_with("send_mode", "production")


def test_settings_post_invalid_key_400(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "evil_key", "value": "x"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mdb.set_setting.assert_not_called()


def test_settings_post_invalid_send_mode_value_400(client):
    c, mdb, mc2 = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/settings",
                 json={"key": "send_mode", "value": "evil"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mdb.set_setting.assert_not_called()


def test_settings_endpoints_require_auth(client):
    c, mdb, mc2 = client
    res = c.get("/api/ma/settings")
    assert res.status_code == 422
    res = c.post("/api/ma/settings", json={"key": "send_mode", "value": "disabled"})
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

### Step 3: Add endpoint to web/api_ma.py

```python
ALLOWED_SETTING_KEYS = {
    "send_mode", "debug_email", "gmail_poll_interval_sec", "gmail_from_filter",
    "inquiry_max_age_days",
    "reminders_enabled", "reminder_after_days",
    "polling_paused",
    "telegram_authorized", "telegram_operator_dm_ids",
    "max_discount_percent",
    "claude_model", "system_prompt",
    "chat_font_em", "chat_padding_v_rem", "chat_padding_h_rem",
    "chat_max_width_pct", "chat_radius_rem", "chat_row_gap_rem",
    "chat_meta_font_em", "chat_secondary_font_em",
    "api_balance_snapshot_usd", "api_balance_snapshot_at",
    "anthropic_api_key", "telegram_bot_token",
    "google_drive_credentials_json", "google_drive_folder_id", "backup_interval_hours",
    "web_port", "web_host",
}

SENSITIVE_KEYS = {
    "anthropic_api_key", "telegram_bot_token",
    "google_drive_credentials_json",
}

VALIDATORS = {
    "send_mode": lambda v: v in {"disabled", "redirect", "production"},
    "polling_paused": lambda v: v in {"0", "1"},
    "reminders_enabled": lambda v: v in {"0", "1"},
    "gmail_poll_interval_sec": lambda v: v.isdigit() and 10 <= int(v) <= 3600,
    "inquiry_max_age_days": lambda v: v.isdigit() and 1 <= int(v) <= 365,
    "max_discount_percent": lambda v: v.replace(".", "", 1).isdigit() and 0 <= float(v) <= 100,
}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•••••"
    return "•••••" + value[-4:]


class SettingPostBody(BaseModel):
    key: str
    value: str = Field(..., max_length=100000)


@router.get("/settings")
async def ma_settings_get(user: dict = Depends(verify_init_data_dep)) -> dict[str, str]:
    """Все whitelist-настройки. Sensitive ключи замаскированы."""
    out: dict[str, str] = {}
    for key in sorted(ALLOWED_SETTING_KEYS):
        raw = config.get(key) or ""
        if key in SENSITIVE_KEYS:
            out[key] = _mask_secret(raw)
        else:
            out[key] = raw
    return out


@router.post("/settings")
async def ma_settings_post(body: SettingPostBody,
                           user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Установить одно значение. Whitelist + per-key validator."""
    if body.key not in ALLOWED_SETTING_KEYS:
        raise HTTPException(400, f"key '{body.key}' is not in whitelist")
    validator = VALIDATORS.get(body.key)
    if validator and not validator(body.value):
        raise HTTPException(400, f"invalid value for {body.key}")
    db.set_setting(body.key, body.value)
    return {"ok": True, "key": body.key, "value": body.value}
```

`config` уже импортируется в api_ma.py (Phase 3). Но проверь — если нет, добавь:
```python
import config
```

### Step 4-6: Tests + commit

Expected: `97 passed` (91 + 6).

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_settings.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): settings GET/POST endpoints with whitelist"'
```

---

## Task 4: Frontend — compose-form component + thread.js wire

**Files:**
- Create: `web-app/js/components/compose-form.js`
- Modify: `web-app/js/screens/thread.js`

### Step 1: Create compose-form.js

```javascript
// Compose form для operator-initiated message в тред.

import { api } from "../api.js?v=20260510-12";
import { el } from "../utils.js?v=20260510-12";


export function buildComposeForm({threadId, onSubmitComplete, onCancel}) {
  const form = el(`
    <div class="compose-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold">✉️ Написать клиенту (на русском — переведём)</div>
      <textarea class="form-control mb-2" rows="6" placeholder="Введите текст…"></textarea>
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">📨 Отправить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const textarea = form.querySelector("textarea");
  const errEl = form.querySelector(".form-error");

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    errEl.classList.add("d-none");
    const text = textarea.value.trim();
    if (!text) {
      errEl.textContent = "Текст не может быть пустым";
      errEl.classList.remove("d-none");
      return;
    }
    if (text.length > 4000) {
      errEl.textContent = "Слишком длинный текст (макс. 4000)";
      errEl.classList.remove("d-none");
      return;
    }
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose`, {
        method: "POST",
        body: {text},
      });
      onSubmitComplete(res);
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelector(".save-btn").disabled = false;
      form.querySelector(".cancel-btn").disabled = false;
    }
  });

  return form;
}
```

### Step 2: Modify thread.js — add compose button to backToPipeline area

В `backToPipeline()` функции в thread.js (Phase 2/3 версия) — заменить на:

```javascript
function backRow(threadId, onCompose) {
  const row = el(`
    <div class="d-flex gap-2 mt-3">
      <a class="btn btn-sm btn-outline-secondary" href="#/pipeline">↩ К pipeline</a>
      <button class="btn btn-sm btn-outline-primary compose-btn">✉️ Написать клиенту</button>
    </div>
  `);
  row.querySelector(".compose-btn").addEventListener("click", () => onCompose());
  return row;
}
```

И в `renderThread` подменить `container.appendChild(backToPipeline())` на:

```javascript
container.appendChild(backRow(params.thread_id, () => {
  // Replace bottom row with compose form
  const oldRow = container.querySelector(".d-flex.mt-3");
  const composeForm = buildComposeForm({
    threadId: params.thread_id,
    onSubmitComplete: () => render(mount, params),
    onCancel: () => render(mount, params),
  });
  if (oldRow) oldRow.replaceWith(composeForm);
}));
```

Add import at top of thread.js:
```javascript
import { buildComposeForm } from "../components/compose-form.js?v=20260510-12";
```

### Step 3: Syntax check + commit

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && node --input-type=module --check < components/compose-form.js && node --input-type=module --check < screens/thread.js && echo OK'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/js/components/compose-form.js web-app/js/screens/thread.js && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): compose-form component + thread.js wire"'
```

---

## Task 5: Frontend — autopilot-form component + thread.js wire

**Files:**
- Create: `web-app/js/components/autopilot-form.js`
- Modify: `web-app/js/screens/thread.js`

### Step 1: Create autopilot-form.js

```javascript
// Autopilot start form (floor + notify mode).

import { api } from "../api.js?v=20260510-12";
import { el } from "../utils.js?v=20260510-12";


export function buildAutopilotForm({threadId, onSubmitComplete, onCancel}) {
  const form = el(`
    <div class="autopilot-form border rounded p-3 mt-3">
      <div class="fw-semibold small mb-2">🚀 Запустить автопилот</div>
      <div class="mb-2">
        <label class="form-label small mb-1">Floor цена €</label>
        <input type="number" step="10" min="0" class="form-control floor-input" placeholder="1200" />
      </div>
      <div class="mb-3">
        <div class="form-check">
          <input class="form-check-input" type="radio" name="notify_mode" id="ap_silent" value="silent" checked>
          <label class="form-check-label" for="ap_silent">🤫 Silent (тихо)</label>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="notify_mode" id="ap_notify" value="notify">
          <label class="form-check-label" for="ap_notify">🔔 Notify (пинг при каждом ответе)</label>
        </div>
      </div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">🚀 Старт</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const errEl = form.querySelector(".form-error");
  const floorInput = form.querySelector(".floor-input");

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    errEl.classList.add("d-none");
    const floor = parseFloat(floorInput.value);
    if (Number.isNaN(floor) || floor <= 0) {
      errEl.textContent = "Введите положительное число для floor";
      errEl.classList.remove("d-none");
      return;
    }
    const mode = form.querySelector("input[name=notify_mode]:checked")?.value || "silent";
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/autopilot/start`, {
        method: "POST",
        body: {floor_eur: floor, notify_mode: mode},
      });
      onSubmitComplete(res);
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelector(".save-btn").disabled = false;
      form.querySelector(".cancel-btn").disabled = false;
    }
  });

  return form;
}


export function buildAutopilotStatus({threadId, autopilotState, onStop, onStart}) {
  /**
   * autopilotState: {active, messages_sent, floor_eur, notify_mode}
   */
  const block = el(`
    <div class="autopilot-status border rounded p-2 mt-2">
      <div class="ap-info"></div>
      <div class="ap-actions mt-2"></div>
    </div>
  `);

  const info = block.querySelector(".ap-info");
  const actions = block.querySelector(".ap-actions");

  if (autopilotState?.active) {
    info.textContent = `🤖 Активен · ${autopilotState.messages_sent ?? 0}/20 · floor ${autopilotState.floor_eur}€ · ${autopilotState.notify_mode}`;
    const stopBtn = el(`<button class="btn btn-sm btn-danger">🛑 Остановить автопилот</button>`);
    stopBtn.addEventListener("click", onStop);
    actions.appendChild(stopBtn);
  } else {
    info.textContent = "🚀 Автопилот не активен";
    const startBtn = el(`<button class="btn btn-sm btn-outline-primary">🚀 Запустить</button>`);
    startBtn.addEventListener("click", onStart);
    actions.appendChild(startBtn);
  }

  return block;
}
```

### Step 2: Modify thread.js to render autopilot status

В `renderThread`, после `pendingDraftBlock` (если он рендерится) добавить:

```javascript
import { buildAutopilotForm, buildAutopilotStatus } from "../components/autopilot-form.js?v=20260510-12";

// ... в renderThread:
if (review) {
  const draftBlock = pendingDraftBlock(review);
  container.appendChild(draftBlock);
  // ... existing logic
}

// Autopilot status (independent of pending — показываем для каждого треда)
const apStatus = buildAutopilotStatus({
  threadId: params.thread_id,
  autopilotState: review?.autopilot ?? data.header.is_autopilot ? {active: data.header.is_autopilot} : null,
  onStop: async () => {
    if (!confirm("Остановить автопилот?")) return;
    try {
      await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}/autopilot/stop`, {method: "POST"});
      render(mount, params);
    } catch (e) {
      alert(`Ошибка: ${e.message}`);
    }
  },
  onStart: () => {
    const form = buildAutopilotForm({
      threadId: params.thread_id,
      onSubmitComplete: () => render(mount, params),
      onCancel: () => render(mount, params),
    });
    apStatus.replaceWith(form);
  },
});
container.appendChild(apStatus);
```

### Step 3: Syntax check + commit

Expected: 97 tests still pass.

---

## Task 6: Settings screen + router wire-up

**Files:**
- Create: `web-app/js/screens/settings.js`
- Modify: `web-app/js/router.js`
- Modify: `web-app/js/screens/pipeline.js` (add ⚙ link)

### Step 1: Create settings.js

```javascript
// Settings screen — KV editor с per-field save.

import { api } from "../api.js?v=20260510-12";
import { el, esc, setLoading, setError } from "../utils.js?v=20260510-12";


const FIELDS = [
  // [key, label, kind, options?]
  ["send_mode", "Send mode", "radio", ["disabled", "redirect", "production"]],
  ["polling_paused", "Polling paused", "checkbox"],
  ["debug_email", "Debug email (для redirect mode)", "text"],
  ["gmail_poll_interval_sec", "Gmail poll interval (сек)", "number"],
  ["gmail_from_filter", "Gmail from filter", "text"],
  ["inquiry_max_age_days", "Inquiry max age (дней)", "number"],
  ["reminders_enabled", "Reminders enabled", "checkbox"],
  ["reminder_after_days", "Reminder after (дней)", "number"],
  ["telegram_authorized", "Telegram authorized IDs (CSV)", "text"],
  ["telegram_operator_dm_ids", "Telegram DM IDs (CSV)", "text"],
  ["max_discount_percent", "Max discount %", "number"],
  ["claude_model", "Claude model", "text"],
  ["chat_font_em", "Chat font em", "text"],
  ["api_balance_snapshot_usd", "API balance snapshot $", "text"],
  ["api_balance_snapshot_at", "API balance snapshot at (ISO)", "text"],
  ["anthropic_api_key", "Anthropic API key", "secret"],
  ["telegram_bot_token", "Telegram bot token", "secret"],
];


function fieldRow(key, label, kind, value, options) {
  const wrap = el(`<div class="mb-3"></div>`);

  const labelEl = el(`<label class="form-label small fw-semibold mb-1"></label>`);
  labelEl.textContent = label;
  wrap.appendChild(labelEl);

  let input;
  if (kind === "radio") {
    const group = el(`<div></div>`);
    options.forEach(opt => {
      const choice = el(`
        <div class="form-check form-check-inline">
          <input class="form-check-input" type="radio" name="${key}" value="${opt}" id="${key}_${opt}">
          <label class="form-check-label small" for="${key}_${opt}">${opt}</label>
        </div>
      `);
      const radio = choice.querySelector("input");
      if (value === opt) radio.checked = true;
      group.appendChild(choice);
    });
    input = group;
  } else if (kind === "checkbox") {
    input = el(`<div class="form-check"><input class="form-check-input" type="checkbox" id="${key}"></div>`);
    input.querySelector("input").checked = (value === "1");
  } else if (kind === "secret") {
    const masked = el(`
      <div class="d-flex gap-2 align-items-center">
        <span class="masked-display"></span>
        <button type="button" class="btn btn-sm btn-outline-secondary replace-btn">Заменить</button>
        <input type="password" class="form-control secret-input d-none" />
      </div>
    `);
    masked.querySelector(".masked-display").textContent = value || "(не задано)";
    masked.querySelector(".replace-btn").addEventListener("click", () => {
      masked.querySelector(".masked-display").classList.add("d-none");
      masked.querySelector(".replace-btn").classList.add("d-none");
      masked.querySelector(".secret-input").classList.remove("d-none");
    });
    input = masked;
  } else {
    input = el(`<input class="form-control" type="${kind === "number" ? "number" : "text"}" />`);
    input.value = value || "";
  }
  wrap.appendChild(input);

  const saveBar = el(`
    <div class="d-flex gap-2 mt-1 align-items-center">
      <button class="btn btn-sm btn-primary save-btn">💾 Сохранить</button>
      <span class="status-text small text-muted"></span>
    </div>
  `);
  wrap.appendChild(saveBar);

  saveBar.querySelector(".save-btn").addEventListener("click", async () => {
    const status = saveBar.querySelector(".status-text");
    status.textContent = "Сохраняю…";
    status.className = "status-text small text-muted";

    let valueToSend;
    if (kind === "radio") {
      valueToSend = input.querySelector("input:checked")?.value || "";
    } else if (kind === "checkbox") {
      valueToSend = input.querySelector("input").checked ? "1" : "0";
    } else if (kind === "secret") {
      valueToSend = input.querySelector(".secret-input").value;
      if (!valueToSend) {
        status.textContent = "Поле пустое — пропустил";
        status.className = "status-text small text-warning";
        return;
      }
    } else {
      valueToSend = input.value;
    }

    try {
      await api("/api/ma/settings", {
        method: "POST",
        body: {key, value: valueToSend},
      });
      status.textContent = "✅ Сохранено";
      status.className = "status-text small text-success";
    } catch (e) {
      status.textContent = `❌ ${e.message ?? "ошибка"}`;
      status.className = "status-text small text-danger";
    }
  });

  return wrap;
}


export async function render(mount, params) {
  setLoading(mount, "Загружаю настройки…");
  try {
    const data = await api("/api/ma/settings");
    const container = el(`
      <div>
        <h5 class="mb-3">⚙ Настройки</h5>
        <div class="fields-list"></div>
        <a class="btn btn-sm btn-outline-secondary mt-3" href="#/pipeline">↩ К pipeline</a>
      </div>
    `);
    const list = container.querySelector(".fields-list");
    FIELDS.forEach(([key, label, kind, options]) => {
      list.appendChild(fieldRow(key, label, kind, data[key] ?? "", options));
    });
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

### Step 2: Update router.js — add settings + review routes

В `web-app/js/router.js`:

```javascript
import * as pipeline from "./screens/pipeline.js?v=20260510-12";
import * as thread from "./screens/thread.js?v=20260510-12";
import * as history from "./screens/history.js?v=20260510-12";
import * as settings from "./screens/settings.js?v=20260510-12";  // NEW
import * as review from "./screens/review.js?v=20260510-12";  // NEW
import { setError } from "./utils.js?v=20260510-12";
import { hideBack, showBack } from "./tg.js?v=20260510-12";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, params: () => ({}) },
  { pattern: /^#\/thread\/(.+)$/,         screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/settings\/?$/,          screen: settings, params: () => ({}) },  // NEW
  { pattern: /^#\/review\/(.+)$/,         screen: review, params: m => ({ msg_id: decodeURIComponent(m[1]) }) },  // NEW
];

// rest unchanged
```

### Step 3: Create review.js redirector

```javascript
import { api } from "../api.js?v=20260510-12";
import { setLoading, setError } from "../utils.js?v=20260510-12";

export async function render(mount, params) {
  setLoading(mount, "Открываю карточку…");
  try {
    const review = await api(`/api/ma/messages/${encodeURIComponent(params.msg_id)}`);
    if (!review.thread_id) {
      setError(mount, "Тред не найден");
      return;
    }
    location.hash = `#/thread/${encodeURIComponent(review.thread_id)}`;
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
```

### Step 4: Add ⚙ link in pipeline.js

В `web-app/js/screens/pipeline.js` функция `render` — в начале container добавить header-row с settings link. Найти `container.appendChild(refreshButton(...));` и заменить на:

```javascript
const headerRow = el(`
  <div class="d-flex justify-content-between align-items-center mb-2">
    <button class="btn btn-sm btn-outline-secondary refresh-btn">↻ Обновить</button>
    <a class="btn btn-sm btn-outline-secondary" href="#/settings">⚙</a>
  </div>
`);
headerRow.querySelector(".refresh-btn").addEventListener("click", () => render(mount, params));
container.appendChild(headerRow);
```

Удалить старую refreshButton-функцию или оставить unused.

### Step 5: Syntax check + commit

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && for f in *.js components/*.js screens/*.js; do node --input-type=module --check < "$f" && echo "$f OK"; done'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/ && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): settings screen + review redirect + router wire-up"'
```

---

## Task 7: Cache-bust + final commit

**Files:**
- Modify: ALL web-app files (cache-bust query)

### Step 1: Bump all from `?v=20260510-11` to `?v=20260510-12`

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app && grep -lr "?v=20260510-11" . | xargs sed -i "s/?v=20260510-11/?v=20260510-12/g"'
ssh 192.168.88.28 'grep -rn "?v=20260510" /home/pg/kleinanzeigen-bot-ma/web-app/ | grep -v "?v=20260510-12" | head -5'
```

Expected: empty (only `-12`).

### Step 2: Final test run

```bash
ssh 192.168.88.28 'python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `97 passed`.

### Step 3: All JS syntax check

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && for f in *.js components/*.js screens/*.js; do node --input-type=module --check < "$f" && echo "$f OK"; done'
```

Expected: all OK (10 files).

### Step 4: Commit cache-bust

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/ && git -C /home/pg/kleinanzeigen-bot-ma commit -m "chore(ma): bump cache-bust to v=20260510-12"'
```

---

## Task 8: Merge → main, deploy, E2E

- [ ] **Step 1: Verify commits**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma log --oneline 9d81f1d..HEAD'
```

Expected: ~7 commits.

- [ ] **Step 2: Merge to main**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot status -s && git -C /home/pg/kleinanzeigen-bot merge ma-phase4 --no-edit 2>&1 | tail -5'
```

- [ ] **Step 3: Restart bot**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 5 && systemctl is-active kleinanzeigen-bot && journalctl -u kleinanzeigen-bot -n 30 --no-pager --since "30 seconds ago" | grep -iE "ERROR|Traceback|Application startup" | head -10'
```

- [ ] **Step 4: Local smoke**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "compose: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/threads/abc/compose'
ssh 192.168.88.28 'curl -s -o /dev/null -w "ap-start: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/threads/abc/autopilot/start'
ssh 192.168.88.28 'curl -s -o /dev/null -w "settings-get: %{http_code}\n" http://127.0.0.1:8080/api/ma/settings'
```

Expected: все `422` (no auth header).

- [ ] **Step 5: Verify cloudflared tunnel**

```bash
curl -s -o /dev/null -w "tunnel settings: %{http_code}\n" https://choice-drunk-curriculum-effectiveness.trycloudflare.com/api/ma/settings
```

Expected: `422`. Если 000 — tunnel умер; перезапустить + обновить URL в api.js + bump cache-bust + push.

- [ ] **Step 6: Push to GitHub**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main 2>&1 | tail -3'
```

- [ ] **Step 7: Wait Pages rebuild**

```bash
until [ "$(curl -s -H "Authorization: Bearer github_pat_..." https://api.github.com/repos/pgolitech-lab/kleinanzeigen-bot/pages/builds/latest | python3 -c "import json,sys; print(json.load(sys.stdin).get('status'))" 2>/dev/null)" = "built" ]; do sleep 8; done
```

- [ ] **Step 8: Manual E2E в Telegram**

Закрой/открой MA полностью.

1. **Pipeline** — должна быть кнопка `⚙` справа сверху → tap → `#/settings` → видно список ~25 полей с per-field save
2. **Settings**:
   - send_mode radio (3 опции) → выбери одну → tap «💾 Сохранить» → «✅ Сохранено»
   - Numeric поле → введи → save → success
   - `anthropic_api_key` показано как `••••• xxxx` + кнопка «Заменить» → tap → раскрывается password input → введи новое → save
3. **Compose**:
   - Pipeline → тред → внизу кнопка `✉️ Написать клиенту` → tap → form
   - Введи русский текст → tap «📨 Отправить» → re-render thread (новый out-event в логе)
4. **Autopilot**:
   - В треде с pending → autopilot status «🚀 Автопилот не активен» + кнопка «🚀 Запустить»
   - Tap → form (floor + radio silent/notify) → tap «🚀 Старт» → re-render → теперь видно «🤖 Активен · 0/20 · floor X€ · silent»
   - Tap «🛑 Остановить» → confirm → re-render
5. **Deep-link `/review/{msg_id}`**:
   - В TG любая существующая кнопка ссылающаяся на `?tgWebAppStartParam=review_<id>` (или вручную в браузере) — должна редиректить на правильный thread
6. **Lock interaction**: открой thread с pending, всё работает как Phase 3b (action grid жив)

- [ ] **Step 9: Acceptance**

Phase 4 закрыт когда:
- ✅ Все 4 features работают в TG (compose, autopilot start/stop, settings, deep-link)
- ✅ 97 unit-тестов проходят
- ✅ Никаких ERROR/Traceback в журнале бота на `/api/ma/*`
- ✅ Phase 3b features (review actions) не сломаны

---

## Cleanup

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree remove /home/pg/kleinanzeigen-bot-ma'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot branch -d ma-phase4'
```

## Risks для Phase 4

- **`scheduler.send_manual_compose` сигнатура** — verified takes `(source_msg_id: int, operator_text: str)`. Если поведение возвращает иное чем `{kind, message_id, ...}` — план мог пропустить case. Plan task должен grep-нуть фактический return shape перед implementation.
- **Settings race condition** — два оператора одновременно меняют один ключ. Last-write-wins; не критично для personal-use.
- **Sensitive value display** — после save секрет не показываем заново. Если оператор хочет проверить — нужно cancel + re-edit. Acceptable.
- **Autopilot start без pending в треде** — `db.thread_history` возвращает любые row (in/out). Last row может быть direction='out' (наш sent). Notification helper всё равно использует msg_id любой row — должно работать. Если появятся issues — добавить filter `direction='in'`.
- **`config.get` vs `db.get_setting`** — могут возвращать None vs "". Используем `(value or "")` чтобы не None-ить frontend.
- **TG WebView ESM cache** — bump cache-bust на ВСЕ файлы.

## What deferred to Phase 6 (optional future)

- Live polling autopilot status (refresh каждые ~10 сек)
- Reminder approve в MA (offer → MA form)
- Daily summary в MA (text view)
- Live broadcast keyboard refresh (sticky issue, has guard)
