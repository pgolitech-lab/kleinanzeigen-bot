# Telegram Mini App — Phase 3b (Action grid + lock + broadcast) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оператор в MA берёт lock, выполняет одно из 9 действий (send/skip/sold/regenerate/edit-ru/edit-de/price/instruction), TG-карточки бота обновляются через broadcast.

**Architecture:** 11 POST endpoint'ов под `/api/ma/messages/{id}/*` (2 lock + 9 actions). Backend: тонкие обёртки над `scheduler.send_one`, `claude.regenerate_*`, `claude.translate_only`, `db.update_message`. Lock использует Phase 3a wrappers `telegram_bot._check_lock`/`_acquire_lock`/`_release_lock` (orchestration с thread_busy + drain). Broadcast — новый sync helper `telegram_bot.broadcast_after_external_action(msg_id)` через существующий `_http_post_single`. Frontend: action-grid (sticky-bottom) с inline confirm + edit-inline forms; auto-acquire lock на mount, release на unmount.

**Tech Stack:** Python 3.11 + FastAPI + asyncio.to_thread (sync work in threadpool) / pytest + TestClient + MagicMock / vanilla JS + Bootstrap 5.3.

**Все команды на проде через ssh** (`ssh 192.168.88.28 ...`). Работаем в worktree `/home/pg/kleinanzeigen-bot-ma` на ветке `ma-phase3b`. Прод-бот не трогаем до финального merge.

**Spec reference:** `docs/superpowers/specs/2026-05-10-tg-mini-app-phase3b-design.md`

**Phase 1-3a wrap-up (обязательное чтение):**
- Phase 1 plan: `docs/superpowers/plans/2026-05-09-tg-mini-app-phase1.md`
- Phase 2 plan: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase2.md`
- Phase 3a plan: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase3a.md`
- Pages URL: `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/`
- Cloudflared Quick Tunnel: `https://choice-drunk-curriculum-effectiveness.trycloudflare.com` — если не отвечает, перезапустить через `nohup cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf-tunnel.log 2>&1 &`, обновить URL в `web-app/js/api.js:API_BASE`.
- Текущий baseline: 50 unit тестов на main.

**Worktree setup (один раз перед Task 1):**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree add /home/pg/kleinanzeigen-bot-ma -b ma-phase3b && cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `50 passed`. Worktree на ветке `ma-phase3b`.

---

## File Structure

**Создаются:**
- `tests/test_api_ma_lock_actions.py` — TestClient тесты lock acquire/release (~6 tests)
- `tests/test_api_ma_actions.py` — TestClient тесты 9 action endpoints (~20 tests)
- `web-app/js/components/action-grid.js` — компонент action-grid с inline confirm state machine
- `web-app/js/components/edit-form.js` — компоненты для edit-ru/edit-de/price/instruction inline forms

**Модифицируются:**
- `modules/telegram_bot.py` — добавляем `broadcast_after_external_action(msg_id)` helper
- `web/api_ma.py` — добавляем `actor_from_user`, 2 lock endpoint'а, 9 action endpoint'ов
- `web-app/js/screens/thread.js` — расширяем render с lock acquire/release, mount action-grid, edit-form
- `web-app/js/utils.js` — без изменений (используем существующие helpers)
- `web-app/index.html` — bump cache-bust до `?v=20260510-8`
- Все ESM-импорты в `web-app/js/**/*.js` — bump query string до `?v=20260510-8`

---

## Task 1: `actor_from_user` helper + Lock endpoints (acquire/release)

**Files:**
- Modify: `web/api_ma.py` (add helper + 2 endpoints)
- Create: `tests/test_api_ma_lock_actions.py`

**Background:** Lock endpoints дёргают bot's wrappers `_check_lock` / `_acquire_lock` / `_release_lock` (Phase 3a) для orchestration с thread_busy + drain. Actor строка формируется из initData user.

### Step 1: Write failing tests

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_lock_actions.py`:

```python
"""TestClient тесты для POST /api/ma/messages/{id}/lock/{acquire,release}."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER_PG = {"id": 999, "first_name": "Pg", "username": "pgtest"}
TEST_USER_NO_NAME = {"id": 555, "first_name": "Sam"}


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
        mc.telegram_authorized_ids.return_value = {"999", "555"}
        mdb.get_message.return_value = _row(id=123, gmail_thread_id="abc")
        mtb._check_lock.return_value = None  # default: lock free
        mtb._acquire_lock.return_value = None
        mtb._release_lock.return_value = None
        mol.remaining_min.return_value = 5
        from web.app import app
        yield TestClient(app), mtb, mol, mdb


def test_actor_format_with_username():
    from web.api_ma import actor_from_user
    assert actor_from_user(TEST_USER_PG) == "@pgtest#999"


def test_actor_format_without_username():
    from web.api_ma import actor_from_user
    assert actor_from_user(TEST_USER_NO_NAME) == "Sam#555"


def test_acquire_succeeds_when_free(client):
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = None
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["holder"] == "@pgtest#999"
    assert body["remaining_min"] == 5
    mtb._acquire_lock.assert_called_once_with(123, "@pgtest#999")


def test_acquire_409_when_held_by_other(client):
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = "@other#111"
    mol.remaining_min.return_value = 4
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409
    body = res.json()
    assert body["detail"]["holder"] == "@other#111"
    assert body["detail"]["remaining_min"] == 4
    mtb._acquire_lock.assert_not_called()


def test_acquire_idempotent_when_already_self(client):
    """check_lock возвращает None если holder=self — мы re-acquire-аем (refresh ttl)."""
    c, mtb, mol, mdb = client
    mtb._check_lock.return_value = None  # None означает "свободно или мой"
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    mtb._acquire_lock.assert_called_once()


def test_acquire_404_when_msg_missing(client):
    c, mtb, mol, mdb = client
    mdb.get_message.return_value = None
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/999/lock/acquire",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_release_succeeds(client):
    c, mtb, mol, mdb = client
    init = make_init_data(TEST_USER_PG)
    res = c.post("/api/ma/messages/123/lock/release",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 204
    mtb._release_lock.assert_called_once_with(123)


def test_release_idempotent_no_holder(client):
    """Permissive — release не проверяет holder."""
    c, mtb, mol, mdb = client
    init = make_init_data(TEST_USER_NO_NAME)
    res = c.post("/api/ma/messages/123/lock/release",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 204


def test_lock_endpoints_require_auth(client):
    c, mtb, mol, mdb = client
    res = c.post("/api/ma/messages/123/lock/acquire")
    assert res.status_code == 422
    res = c.post("/api/ma/messages/123/lock/release")
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_lock_actions.py -v'
```

Expected: tests fail (endpoints don't exist; actor_from_user not exported).

### Step 3: Implement actor_from_user + lock endpoints in web/api_ma.py

(a) Add import at top of `web/api_ma.py` (после `from modules import operator_lock`):

```python
from modules import telegram_bot
```

(b) Add helper near other helpers:

```python
def actor_from_user(user: dict) -> str:
    """Формат actor для lock — соответствует тому что bot использует в callback'ах."""
    uid = user.get("id")
    name = user.get("username") or user.get("first_name") or "?"
    prefix = "@" if user.get("username") else ""
    return f"{prefix}{name}#{uid}"
```

(c) Add endpoints after `ma_message_lock_state`:

```python
@router.post("/messages/{msg_id}/lock/acquire")
async def ma_lock_acquire(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Захватить lock на review-карточку. 409 если занято другим."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign_holder = telegram_bot._check_lock(msg_id, actor)
    if foreign_holder is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "holder": foreign_holder,
                "remaining_min": operator_lock.remaining_min(msg_id),
            },
        )
    telegram_bot._acquire_lock(msg_id, actor)
    return {
        "holder": actor,
        "remaining_min": operator_lock.remaining_min(msg_id),
    }


@router.post("/messages/{msg_id}/lock/release", status_code=204)
async def ma_lock_release(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> None:
    """Освободить lock. Permissive — не проверяет holder."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    telegram_bot._release_lock(msg_id)
    return None
```

### Step 4: Run tests, expect PASS

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_lock_actions.py -v'
```

Expected: `9 passed`.

### Step 5: Run full suite

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `59 passed` (50 + 9 new).

### Step 6: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_lock_actions.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): lock acquire/release endpoints + actor helper"'
```

Verify branch is `ma-phase3b`.

---

## Task 2: `broadcast_after_external_action` helper in `telegram_bot.py`

**Files:**
- Modify: `modules/telegram_bot.py`

**Background:** После каждого MA-action нужно обновить TG-карточки в боте. Используем существующий sync `_http_post_single` (telegram_bot.py:50). Не используем async `_broadcast_card` (требует Application context).

**Простая реализация:** только обновить text карточек. Keyboard оставляем как есть — оператор увидит новый текст, при следующем взаимодействии бот пересоздаст keyboard.

### Step 1: Find existing `_format_review_text` helper

```bash
ssh 192.168.88.28 'grep -n "def _format_review_text\|def _truncate_html_safe" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -5'
```

Expected: existing functions `_format_review_text(msg_row) -> str` и `_truncate_html_safe(text, ...) -> str`.

### Step 2: Add helper

В конце `modules/telegram_bot.py` (или рядом с `_broadcast_card`) добавить:

```python
def broadcast_after_external_action(msg_id: int) -> None:
    """Sync обновление всех DM-копий review-карточки после внешнего действия (MA).

    Best-effort: log + return на любой ошибке. Обновляет только text — keyboard
    остаётся прежним (бот пересоберёт его при следующем взаимодействии).

    Используется из web/api_ma.py POST endpoint'ов.
    """
    try:
        msg = db.get_message(msg_id)
    except Exception:
        logger.exception("broadcast_after_external_action: db.get_message failed")
        return
    if not msg:
        return

    try:
        text = _format_review_text(msg)
    except Exception:
        logger.exception("broadcast_after_external_action: _format_review_text failed")
        return
    text = _truncate_html_safe(text)

    dispatches = db.list_card_dispatches(msg_id)
    if not dispatches:
        # Fallback на legacy telegram_message_id
        if msg["telegram_message_id"]:
            try:
                _http_post_single("editMessageText", {
                    "chat_id": int(config.telegram_chat_id()),
                    "message_id": msg["telegram_message_id"],
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
            except Exception:
                logger.warning("broadcast_after_external_action legacy edit failed")
        return

    for d in dispatches:
        try:
            _http_post_single("editMessageText", {
                "chat_id": d["chat_id"],
                "message_id": d["tg_msg_id"],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except Exception:
            logger.warning("broadcast_after_external_action edit failed: chat=%s msg=%s",
                           d["chat_id"], d["tg_msg_id"])
```

### Step 3: Sanity — module imports cleanly

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot; print(telegram_bot.broadcast_after_external_action)"'
```

Expected: `<function broadcast_after_external_action at 0x...>`.

### Step 4: Run full test suite (no regressions)

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `59 passed` (no new tests yet — tested via integration in subsequent tasks).

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): broadcast_after_external_action helper for sync card refresh"'
```

---

## Task 3: `POST /messages/{id}/send|skip|sold` endpoints (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_actions.py`

**Background:** 3 final-state actions. Все:
- Проверяют lock
- Запускают action (sync work через `asyncio.to_thread`)
- Зовут `broadcast_after_external_action`
- Освобождают lock (final state)

`send` — `scheduler.send_one(msg_id)` — отправляет SMTP, может вернуть `{kind: "error", message: ...}` если что-то не так.
`skip` — `db.update_message(msg_id, status="skipped")`.
`sold` — `db.update_message(msg_id, status="skipped_sold")`. Также маркируем ad sold (через `db.set_ad_sold(ad_id)` если есть; если нет helper'а — пропускаем, не критично).

### Step 1: Check `db.set_ad_sold` exists

```bash
ssh 192.168.88.28 'grep -n "def set_ad_sold\|sold_at = " /home/pg/kleinanzeigen-bot-ma/database.py | head -5'
```

If absent, `sold` endpoint просто `db.update_message(msg_id, status="skipped_sold")` — sold_at в ad_briefs обновит scheduler естественно.

### Step 2: Write failing tests

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_actions.py`:

```python
"""TestClient тесты для action endpoints под /api/ma/messages/{id}/*."""
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


def _full_msg_row(**overrides):
    defaults = {
        "id": 123,
        "gmail_thread_id": "abc",
        "status": "pending",
        "direction": "in",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://x/2812",
        "ad_id": "2812",
        "buyer_display_name": "Osman",
        "buyer_name": "osman@gmx.de",
        "client_lang": "de",
        "de_client": "Hallo",
        "ru_client": "Привет",
        "ru_answer": "ответ",
        "de_answer": "Antwort",
        "ru_translation": "ответ-back",
        "deal_brief_json": None,
        "extra_notes": None,
        "is_auto_ack": 0,
    }
    defaults.update(overrides)
    return _row(**defaults)


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
        mdb.get_message.return_value = _full_msg_row()
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mtb._check_lock.return_value = None
        mtb._acquire_lock.return_value = None
        mtb._release_lock.return_value = None
        mtb.broadcast_after_external_action.return_value = None
        mol.state.return_value = ("@pgtest#999", 1700000000.0)
        mol.remaining_min.return_value = 5
        from web.app import app
        yield TestClient(app), mdb, mtb, msched, mclaude


def test_send_success(client):
    c, mdb, mtb, msched, mclaude = client
    msched.send_one.return_value = {"kind": "ok", "message": "sent"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    msched.send_one.assert_called_once_with(123)
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    mtb._release_lock.assert_called_once_with(123)


def test_send_409_when_locked_by_other(client):
    c, mdb, mtb, msched, mclaude = client
    mtb._check_lock.return_value = "@other#222"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409
    msched.send_one.assert_not_called()


def test_send_500_on_smtp_failure(client):
    c, mdb, mtb, msched, mclaude = client
    msched.send_one.return_value = {"kind": "error", "message": "SMTP timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/send",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    body = res.json()
    assert "SMTP timeout" in body["detail"]
    # Lock не освобождаем при ошибке — оператор может попробовать ещё
    mtb._release_lock.assert_not_called()


def test_skip_success(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/skip",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["status"] == "skipped"
    mdb.update_message.assert_called_once_with(123, status="skipped")
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    mtb._release_lock.assert_called_once_with(123)


def test_sold_success(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/sold",
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "skipped_sold"
    mdb.update_message.assert_called_once_with(123, status="skipped_sold")


def test_action_endpoints_require_auth(client):
    c, mdb, mtb, msched, mclaude = client
    for path in ["/api/ma/messages/123/send", "/api/ma/messages/123/skip",
                 "/api/ma/messages/123/sold"]:
        res = c.post(path)
        assert res.status_code == 422
```

### Step 3: Run, expect FAIL

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_actions.py -v'
```

Expected: 6 tests fail (404 endpoints).

### Step 4: Implement endpoints + add scheduler/claude imports

В `web/api_ma.py` сверху (после `from modules import operator_lock, telegram_bot`):

```python
import scheduler
from modules import claude
```

После `ma_lock_release` добавить helper и 3 endpoint'а:

```python
def _check_actor_holds(msg_id: int, actor: str) -> str | None:
    """Возвращает foreign holder string если кто-то другой держит lock, иначе None.
    None означает: свободно ИЛИ holder=actor (acquire-нём ниже idempotent)."""
    return telegram_bot._check_lock(msg_id, actor)


def _ensure_lock(msg_id: int, actor: str) -> None:
    """Acquire (или re-acquire if self) — для action endpoint'ов перед мутацией."""
    telegram_bot._acquire_lock(msg_id, actor)


@router.post("/messages/{msg_id}/send")
async def ma_send(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Отправить approved/pending draft. Final action — releases lock на success."""
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(scheduler.send_one, msg_id)
    if result.get("kind") == "error":
        # Не освобождаем lock — оператор может retry
        raise HTTPException(500, result.get("message", "send failed"))

    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot._release_lock(msg_id)

    fresh = db.get_message(msg_id)
    return {"ok": True, "status": fresh["status"] if fresh else "sent"}


@router.post("/messages/{msg_id}/skip")
async def ma_skip(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Пропустить draft без отправки."""
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    db.update_message(msg_id, status="skipped")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped"}


@router.post("/messages/{msg_id}/sold")
async def ma_sold(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Помечаем что товар продан."""
    if db.get_message(msg_id) is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    db.update_message(msg_id, status="skipped_sold")
    telegram_bot.broadcast_after_external_action(msg_id)
    telegram_bot._release_lock(msg_id)
    return {"ok": True, "status": "skipped_sold"}
```

### Step 5: Run tests, expect PASS

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_actions.py -v'
```

Expected: `6 passed`.

### Step 6: Run full suite

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `65 passed` (59 + 6).

### Step 7: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_actions.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): send/skip/sold action endpoints"'
```

---

## Task 4: `POST /messages/{id}/regenerate` endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Modify: `tests/test_api_ma_actions.py` (append tests)

**Background:** Strategy whitelist: `"fest" | "harsh" | "friend" | "short" | "regen"`. (`fest` — без торга, `harsh/friend/short/regen` — TWEAK strategies). `claude.regenerate_with_strategy(msg_row, strategy, brief_text="", history=None, lessons=None)` возвращает dict с обновлёнными полями. Обновляем db. Lock keeps (intermediate action).

### Step 1: Check helper for regenerate context loading

```bash
ssh 192.168.88.28 'grep -n "def _load_regen_context\|brief_text\|_load_brief\|lessons" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py /home/pg/kleinanzeigen-bot-ma/scheduler.py 2>/dev/null | head -10'
```

If telegram_bot или scheduler уже имеют helper для load — reuse. Если нет — для Phase 3b упрощённо: вызываем regenerate с `brief_text=""`, `history=None`, `lessons=None` (Sonnet получит меньше контекста — chuting качество regenerate но not блокер для MVP). План может улучшить позже.

### Step 2: Append tests to `test_api_ma_actions.py`

```python
def test_regenerate_valid_strategy(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_strategy.return_value = {
        "ru_answer": "новый RU",
        "client_answer": "neue DE",
        "ru_translation": "новый back",
        "deal_summary_ru": "обновлено",
        "expected_next": "ждём",
        "negotiated_price_eur": 1400,
        "client_assessment": "серьёзный",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "harsh"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    # Возвращается полный review payload (как GET /messages/{id})
    assert body["msg_id"] == 123
    mclaude.regenerate_with_strategy.assert_called_once()
    # call signature: (msg_row, strategy, ...)
    args, kwargs = mclaude.regenerate_with_strategy.call_args
    assert args[1] == "harsh"
    mtb.broadcast_after_external_action.assert_called_once_with(123)
    # Lock остаётся (intermediate)
    mtb._release_lock.assert_not_called()


def test_regenerate_invalid_strategy_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "evil"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422
    mclaude.regenerate_with_strategy.assert_not_called()


def test_regenerate_409_when_locked(client):
    c, mdb, mtb, msched, mclaude = client
    mtb._check_lock.return_value = "@other#222"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/regenerate",
                 json={"strategy": "harsh"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 409


def test_regenerate_all_5_valid_strategies(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_strategy.return_value = {
        "ru_answer": "x", "client_answer": "y", "ru_translation": "z",
        "deal_summary_ru": "", "expected_next": "", "negotiated_price_eur": None,
        "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    for strategy in ["fest", "harsh", "friend", "short", "regen"]:
        res = c.post("/api/ma/messages/123/regenerate",
                     json={"strategy": strategy},
                     headers={"X-Telegram-Init-Data": init})
        assert res.status_code == 200, f"strategy={strategy} failed"
```

### Step 3: Run, expect FAIL

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_actions.py::test_regenerate_valid_strategy -v'
```

Expected: 404 (endpoint not implemented).

### Step 4: Implement endpoint

В `web/api_ma.py` after `ma_sold` add:

```python
from pydantic import BaseModel, Field, field_validator


VALID_STRATEGIES = {"fest", "harsh", "friend", "short", "regen"}


class RegenerateBody(BaseModel):
    strategy: str

    @field_validator("strategy")
    @classmethod
    def _check_strategy(cls, v: str) -> str:
        if v not in VALID_STRATEGIES:
            raise ValueError(f"strategy must be one of {sorted(VALID_STRATEGIES)}")
        return v


def _apply_regenerate_result(msg_id: int, result: dict[str, Any]) -> None:
    """Записать результат claude.regenerate_* в db и перевести status в 'edited'."""
    fields: dict[str, Any] = {"status": "edited"}
    if "ru_answer" in result:
        fields["ru_answer"] = result["ru_answer"]
    if "client_answer" in result:
        fields["de_answer"] = result["client_answer"]
    if "ru_translation" in result:
        fields["ru_translation"] = result["ru_translation"]
    # deal_brief: соберём подмножество и запишем как json
    deal: dict[str, Any] = {}
    for src in ("deal_summary_ru", "expected_next", "negotiated_price_eur", "client_assessment"):
        if src in result:
            target = "summary_ru" if src == "deal_summary_ru" else src
            deal[target] = result[src]
    if deal:
        fields["deal_brief_json"] = json.dumps(deal, ensure_ascii=False)
    db.update_message(msg_id, **fields)


def _build_review_payload(msg_id: int) -> dict[str, Any]:
    """Reuse: вернуть тот же shape что GET /messages/{id}."""
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/regenerate")
async def ma_regenerate(msg_id: int, body: RegenerateBody,
                        user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать draft под strategy. Intermediate — lock keeps."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_strategy, row, body.strategy
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)

    return _build_review_payload(msg_id)
```

**ВНИМАНИЕ:** `_message_review_dict(msg_id)` — нужна функция-extract из existing `ma_message_review`. Refactor: вытащить тело `ma_message_review` в `_message_review_dict(msg_id, user_dict=None)` (user не нужен внутри — только для auth). Вызвать его и из `ma_message_review`, и из `_build_review_payload`.

Простой вариант: `_build_review_payload` дублирует логику. Чище: extract в helper:

```python
def _message_review_dict(msg_id: int) -> dict[str, Any]:
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    thread_id = row["gmail_thread_id"]
    autopilot_row = db.get_thread_autopilot(thread_id) if thread_id else None
    lock_state = operator_lock.state(msg_id)
    lock_holder = lock_state[0] if lock_state else None
    related_matches = db.find_related_inquiries(
        row["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if row["buyer_display_name"] else []
    return {
        "msg_id": row["id"], "thread_id": thread_id, "status": row["status"],
        "ad": {
            "title": row["ad_title"], "price": row["ad_price"],
            "url": row["ad_url"] if "ad_url" in row.keys() else None,
            "id": row["ad_id"] if "ad_id" in row.keys() else None,
            "buyer_display_name": row["buyer_display_name"],
            "buyer_email": row["buyer_name"] if "buyer_name" in row.keys() else None,
        },
        "client_lang": row["client_lang"] if "client_lang" in row.keys() else None,
        "client_message": {
            "raw": row["de_client"] if "de_client" in row.keys() else None,
            "ru": row["ru_client"] if "ru_client" in row.keys() else None,
        },
        "draft": {
            "ru_answer": row["ru_answer"] if "ru_answer" in row.keys() else None,
            "de_answer": row["de_answer"] if "de_answer" in row.keys() else None,
            "ru_translation": row["ru_translation"] if "ru_translation" in row.keys() else None,
        },
        "deal_brief": _parse_deal_brief(row["deal_brief_json"] if "deal_brief_json" in row.keys() else None),
        "related": {
            "buyer_display_name": row["buyer_display_name"],
            "matches": [_related_match(r) for r in related_matches],
        },
        "lock": {"holder": lock_holder, "remaining_min": operator_lock.remaining_min(msg_id)},
        "autopilot": _autopilot_view(autopilot_row),
        "extra_notes": row["extra_notes"] if "extra_notes" in row.keys() else None,
        "is_auto_ack": bool(row["is_auto_ack"]) if "is_auto_ack" in row.keys() else False,
    }
```

И обновить `ma_message_review` чтобы он зовёт этот helper:
```python
@router.get("/messages/{msg_id}")
async def ma_message_review(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    return _message_review_dict(msg_id)
```

### Step 5: Run tests

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `69 passed` (65 + 4 new regenerate tests).

### Step 6: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_actions.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): regenerate endpoint with strategy whitelist"'
```

---

## Task 5: `POST /messages/{id}/edit-ru|edit-de` endpoints (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Modify: `tests/test_api_ma_actions.py`

**Background:** edit-ru: оператор редактирует RU-черновик → backend translate forward (target=client_lang) для DE + back-translate (target=ru) для verification. edit-de: оператор редактирует DE напрямую → store as-is, back-translate в RU. Both intermediate (lock keeps).

`claude.translate_only(text, source_lang, target_lang)` → str (по signature claude.py:504).

### Step 1: Append tests

```python
def test_edit_ru_success(client):
    c, mdb, mtb, msched, mclaude = client
    # translate_only вызывается дважды: forward (ru→de) и back (de→ru)
    mclaude.translate_only.side_effect = ["Neue DE Text", "Новый back"]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": "Новый RU текст"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["msg_id"] == 123
    assert mclaude.translate_only.call_count == 2
    # Db update должен содержать ru_answer + de_answer + ru_translation
    args, kwargs = mdb.update_message.call_args
    assert kwargs["ru_answer"] == "Новый RU текст"
    assert kwargs["de_answer"] == "Neue DE Text"
    assert kwargs["ru_translation"] == "Новый back"
    assert kwargs["status"] == "edited"
    mtb.broadcast_after_external_action.assert_called_once_with(123)


def test_edit_de_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.translate_only.return_value = "Новый back-translation"
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-de",
                 json={"text": "Neuer DE Text"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    # translate_only вызывается ОДИН раз — только back-translate
    assert mclaude.translate_only.call_count == 1
    args, kwargs = mdb.update_message.call_args
    assert kwargs["de_answer"] == "Neuer DE Text"
    assert kwargs["ru_translation"] == "Новый back-translation"
    assert "ru_answer" not in kwargs  # ru_answer (идея от GPT) НЕ трогаем
    assert kwargs["status"] == "edited"


def test_edit_ru_empty_text_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_edit_ru_too_long_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/edit-ru",
                 json={"text": "x" * 5000},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

### Step 3: Implement

В `web/api_ma.py`:

```python
class EditTextBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/messages/{msg_id}/edit-ru")
async def ma_edit_ru(msg_id: int, body: EditTextBody,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Operator edits RU answer. Forward+back translate."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    target_lang = row["client_lang"] if "client_lang" in row.keys() else "de"
    de_text = await asyncio.to_thread(
        claude.translate_only, body.text, "ru", target_lang
    )
    ru_back = await asyncio.to_thread(
        claude.translate_only, de_text, target_lang, "ru"
    )
    db.update_message(msg_id, ru_answer=body.text, de_answer=de_text,
                      ru_translation=ru_back, status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/edit-de")
async def ma_edit_de(msg_id: int, body: EditTextBody,
                     user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Operator edits DE answer directly. Back-translate for verification."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    source_lang = row["client_lang"] if "client_lang" in row.keys() else "de"
    ru_back = await asyncio.to_thread(
        claude.translate_only, body.text, source_lang, "ru"
    )
    db.update_message(msg_id, de_answer=body.text, ru_translation=ru_back,
                      status="edited")
    telegram_bot.broadcast_after_external_action(msg_id)
    return _message_review_dict(msg_id)
```

### Step 4: Run tests

Expected: `73 passed` (69 + 4).

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_actions.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): edit-ru and edit-de endpoints with back-translate"'
```

---

## Task 6: `POST /messages/{id}/price|instruction` endpoints (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Modify: `tests/test_api_ma_actions.py`

**Background:** Two custom regenerate endpoints. price — числовой input. instruction — текстовый. Оба зовут соответствующий claude helper, обновляют db, broadcast.

### Step 1: Append tests

```python
def test_price_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_price.return_value = {
        "ru_answer": "согласен на 1400",
        "client_answer": "akzeptiere 1400",
        "ru_translation": "принимаю 1400",
        "deal_summary_ru": "", "expected_next": "",
        "negotiated_price_eur": 1400, "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": 1400},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    args, kwargs = mclaude.regenerate_with_price.call_args
    # Signature: (msg_row, price_eur, ...)
    assert args[1] == 1400
    mtb.broadcast_after_external_action.assert_called_once_with(123)


def test_price_invalid_negative_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": -10},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_price_invalid_too_high_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/price",
                 json={"eur": 999999},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_instruction_success(client):
    c, mdb, mtb, msched, mclaude = client
    mclaude.regenerate_with_instruction.return_value = {
        "ru_answer": "x", "client_answer": "y", "ru_translation": "z",
        "deal_summary_ru": "", "expected_next": "",
        "negotiated_price_eur": None, "client_assessment": "",
    }
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/instruction",
                 json={"text": "Скажи что доставим завтра"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    args, kwargs = mclaude.regenerate_with_instruction.call_args
    assert args[1] == "Скажи что доставим завтра"


def test_instruction_empty_422(client):
    c, mdb, mtb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/messages/123/instruction",
                 json={"text": ""},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422
```

### Step 2: Run, expect FAIL

### Step 3: Implement

```python
class PriceBody(BaseModel):
    eur: float = Field(..., gt=0, lt=100000)


class InstructionBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


@router.post("/messages/{msg_id}/price")
async def ma_price(msg_id: int, body: PriceBody,
                   user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать с конкретной ценой от оператора."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_price, row, body.eur
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)
    return _message_review_dict(msg_id)


@router.post("/messages/{msg_id}/instruction")
async def ma_instruction(msg_id: int, body: InstructionBody,
                          user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Регенерировать со свободной инструкцией."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    actor = actor_from_user(user)
    foreign = _check_actor_holds(msg_id, actor)
    if foreign:
        raise HTTPException(409, {"holder": foreign, "remaining_min": operator_lock.remaining_min(msg_id)})
    _ensure_lock(msg_id, actor)

    import asyncio
    result = await asyncio.to_thread(
        claude.regenerate_with_instruction, row, body.text
    )
    _apply_regenerate_result(msg_id, result)
    telegram_bot.broadcast_after_external_action(msg_id)
    return _message_review_dict(msg_id)
```

### Step 4: Run tests

Expected: `78 passed` (73 + 5).

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_actions.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): price and instruction custom-regenerate endpoints"'
```

---

## Task 7: Frontend — action-grid component

**Files:**
- Create: `web-app/js/components/action-grid.js`

**Background:** Action-grid — sticky-bottom компонент. Показывает 11 кнопок в bot-mirror layout. Inline confirm: tap → морфит в `[Да][Нет]`. Хранит state локально, dispatches API calls. Edit-режим (для edit-ru/edit-de/price/instruction) — НЕ в этом компоненте, см. Task 8.

### Step 1: Create the file

```javascript
// Action grid — sticky-bottom компонент с inline confirm state machine.

import { api } from "../api.js?v=20260510-8";
import { el } from "../utils.js?v=20260510-8";

const CONFIRM_TIMEOUT_MS = 5000;

// Action key → {label, endpoint, confirm_label, kind: "final"|"intermediate"|"edit"}
const ACTIONS = {
  send:        {label: "✅ ОТПРАВИТЬ",     path: "/send",        confirm: "Отправить?",   kind: "final"},
  skip:        {label: "❌ Пропустить",   path: "/skip",        confirm: "Пропустить?",  kind: "final"},
  sold:        {label: "💰 Продано",      path: "/sold",        confirm: "Помечать продано?", kind: "final"},
  fest:        {label: "💎 Без торга",   path: "/regenerate",  body: {strategy: "fest"},  confirm: "Регенерировать?", kind: "intermediate"},
  harsh:       {label: "👊 Жёстче",      path: "/regenerate",  body: {strategy: "harsh"}, confirm: "Регенерировать?", kind: "intermediate"},
  friend:      {label: "☺️ Мягче",       path: "/regenerate",  body: {strategy: "friend"},confirm: "Регенерировать?", kind: "intermediate"},
  short:       {label: "✂️ Короче",      path: "/regenerate",  body: {strategy: "short"}, confirm: "Регенерировать?", kind: "intermediate"},
  regen:       {label: "🔁 Переформ.",   path: "/regenerate",  body: {strategy: "regen"}, confirm: "Регенерировать?", kind: "intermediate"},
  edit_ru:     {label: "✏️ Правка RU",  kind: "edit", field: "ru"},
  edit_de:     {label: "✏️ Правка DE",  kind: "edit", field: "de"},
  price:       {label: "💸 Своя цена",  kind: "edit", field: "price"},
  instruction: {label: "📝 Своя инстр.",kind: "edit", field: "instruction"},
};


export function buildActionGrid({msgId, onActionComplete, onError, onEditRequest}) {
  /**
   * onActionComplete(action_key, response_body) — после успешного API call (final or intermediate)
   * onError(action_key, message) — при HTTP error
   * onEditRequest(field) — оператор тапнул edit-* / price / instruction (UI должен показать форму)
   */
  const grid = el(`
    <div class="action-grid mt-3">
      <div class="d-grid mb-2">
        <button data-action="send" class="btn btn-primary">✅ ОТПРАВИТЬ</button>
      </div>
      <div class="row g-2">
        <div class="col-6"><button data-action="edit_ru" class="btn btn-outline-secondary w-100">✏️ Правка RU</button></div>
        <div class="col-6"><button data-action="edit_de" class="btn btn-outline-secondary w-100">✏️ Правка DE</button></div>
        <div class="col-6"><button data-action="fest" class="btn btn-outline-secondary w-100">💎 Без торга</button></div>
        <div class="col-6"><button data-action="price" class="btn btn-outline-secondary w-100">💸 Своя цена</button></div>
        <div class="col-6"><button data-action="harsh" class="btn btn-outline-secondary w-100">👊 Жёстче</button></div>
        <div class="col-6"><button data-action="friend" class="btn btn-outline-secondary w-100">☺️ Мягче</button></div>
        <div class="col-6"><button data-action="short" class="btn btn-outline-secondary w-100">✂️ Короче</button></div>
        <div class="col-6"><button data-action="regen" class="btn btn-outline-secondary w-100">🔁 Переформ.</button></div>
        <div class="col-6"><button data-action="instruction" class="btn btn-outline-secondary w-100">📝 Своя инстр.</button></div>
        <div class="col-6"><button data-action="skip" class="btn btn-outline-danger w-100">❌ Пропустить</button></div>
      </div>
      <div class="d-grid mt-2">
        <button data-action="sold" class="btn btn-danger">💰 Продано</button>
      </div>
    </div>
  `);

  let confirmTimer = null;
  let confirmingAction = null;

  function resetConfirm() {
    if (confirmTimer) {
      clearTimeout(confirmTimer);
      confirmTimer = null;
    }
    if (confirmingAction) {
      const btn = grid.querySelector(`[data-action="${confirmingAction}"]`);
      if (btn) btn.textContent = ACTIONS[confirmingAction].label;
      confirmingAction = null;
    }
    grid.querySelectorAll("button[data-action]").forEach(b => b.disabled = false);
  }

  async function fireAction(actionKey) {
    const a = ACTIONS[actionKey];
    if (!a) return;
    if (a.kind === "edit") {
      resetConfirm();
      onEditRequest(a.field);
      return;
    }
    grid.querySelectorAll("button[data-action]").forEach(b => b.disabled = true);
    try {
      const opts = {method: "POST"};
      if (a.body) opts.body = a.body;
      const res = await api(`/api/ma/messages/${msgId}${a.path}`, opts);
      onActionComplete(actionKey, res);
    } catch (e) {
      onError(actionKey, e.message ?? String(e));
      resetConfirm();
    }
  }

  function startConfirm(actionKey) {
    const a = ACTIONS[actionKey];
    confirmingAction = actionKey;
    const btn = grid.querySelector(`[data-action="${actionKey}"]`);
    btn.innerHTML = `⚠️ ${a.confirm} <span class="ms-1 text-success" data-confirm="yes">[Да]</span> <span class="ms-1 text-danger" data-confirm="no">[Нет]</span>`;
    grid.querySelectorAll("button[data-action]").forEach(b => {
      if (b !== btn) b.disabled = true;
    });
    confirmTimer = setTimeout(resetConfirm, CONFIRM_TIMEOUT_MS);
  }

  grid.addEventListener("click", (event) => {
    const target = event.target;
    const confirmHit = target.closest("[data-confirm]");
    if (confirmHit) {
      event.stopPropagation();
      const yes = confirmHit.dataset.confirm === "yes";
      const action = confirmingAction;
      resetConfirm();
      if (yes && action) fireAction(action);
      return;
    }
    const btn = target.closest("button[data-action]");
    if (!btn || btn.disabled) return;
    const actionKey = btn.dataset.action;
    if (confirmingAction && confirmingAction !== actionKey) {
      // Тап другой кнопки во время confirm — отменяем confirm, не запускаем действие
      resetConfirm();
      return;
    }
    if (confirmingAction === actionKey) return; // confirm-mode, ждём [Да]/[Нет]

    const a = ACTIONS[actionKey];
    if (a.kind === "edit") {
      fireAction(actionKey);  // edit не требует confirm
    } else {
      startConfirm(actionKey);
    }
  });

  return grid;
}
```

### Step 2: Make subdir if needed + JS syntax check

```bash
ssh 192.168.88.28 'mkdir -p /home/pg/kleinanzeigen-bot-ma/web-app/js/components && ls -la /home/pg/kleinanzeigen-bot-ma/web-app/js/components/'
```

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && node --input-type=module --check < components/action-grid.js && echo OK'
```

Expected: `OK`.

### Step 3: Pytest sanity (no regressions)

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `78 passed`.

### Step 4: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/js/components/ && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): action-grid component with inline confirm state machine"'
```

---

## Task 8: Frontend — edit-form component (textarea/input для edit-режимов)

**Files:**
- Create: `web-app/js/components/edit-form.js`

**Background:** Когда оператор тапает edit-ru/edit-de/price/instruction — action-grid скрывается и появляется form с pre-filled value + Save/Cancel. Save → POST endpoint → re-render. Cancel → close form.

### Step 1: Create file

```javascript
// Edit form component — textarea/input для edit-ru / edit-de / price / instruction.

import { api } from "../api.js?v=20260510-8";
import { el } from "../utils.js?v=20260510-8";


const FIELD_CONFIG = {
  ru: {
    label: "Правка RU (наша инструкция)",
    path: "/edit-ru",
    inputType: "textarea",
    placeholder: "Введите текст…",
    valueFrom: r => r.draft?.ru_answer ?? "",
    bodyKey: "text",
  },
  de: {
    label: "Правка DE (текст для клиента)",
    path: "/edit-de",
    inputType: "textarea",
    placeholder: "Введите текст…",
    valueFrom: r => r.draft?.de_answer ?? "",
    bodyKey: "text",
  },
  price: {
    label: "Своя цена €",
    path: "/price",
    inputType: "number",
    placeholder: "1400",
    valueFrom: () => "",
    bodyKey: "eur",
    transform: v => parseFloat(v),
  },
  instruction: {
    label: "Своя инструкция",
    path: "/instruction",
    inputType: "textarea",
    placeholder: "Скажи что доставим в субботу...",
    valueFrom: () => "",
    bodyKey: "text",
  },
};


export function buildEditForm({msgId, field, review, onSubmitComplete, onCancel, onError}) {
  const cfg = FIELD_CONFIG[field];
  if (!cfg) return null;

  const form = el(`
    <div class="edit-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold form-label"></div>
      <div class="input-wrap mb-2"></div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">💾 Сохранить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);
  form.querySelector(".form-label").textContent = cfg.label;

  const wrap = form.querySelector(".input-wrap");
  let input;
  if (cfg.inputType === "textarea") {
    input = el(`<textarea class="form-control" rows="6"></textarea>`);
    input.value = cfg.valueFrom(review) || "";
  } else {
    input = el(`<input class="form-control" type="number" step="10" min="0" />`);
    input.placeholder = cfg.placeholder;
  }
  wrap.appendChild(input);

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    const errEl = form.querySelector(".form-error");
    errEl.classList.add("d-none");
    const raw = input.value;
    if (!raw || (typeof raw === "string" && !raw.trim())) {
      errEl.textContent = "Поле не может быть пустым";
      errEl.classList.remove("d-none");
      return;
    }
    const value = cfg.transform ? cfg.transform(raw) : raw;
    if (cfg.transform && (Number.isNaN(value) || value <= 0)) {
      errEl.textContent = "Введите положительное число";
      errEl.classList.remove("d-none");
      return;
    }
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/messages/${msgId}${cfg.path}`, {
        method: "POST",
        body: {[cfg.bodyKey]: value},
      });
      onSubmitComplete(field, res);
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

### Step 2: Syntax check

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && node --input-type=module --check < components/edit-form.js && echo OK'
```

Expected: `OK`.

### Step 3: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/js/components/edit-form.js && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): edit-form component for textarea/numeric inputs"'
```

---

## Task 9: Frontend — wire action-grid + edit-form в `thread.js` + lifecycle (auto-acquire/release lock + 409 UX)

**Files:**
- Modify: `web-app/js/screens/thread.js`
- Modify: `web-app/index.html` (cache-bust)
- Modify: `web-app/js/**/*.js` (cache-bust queries)

### Step 1: Replace `web-app/js/screens/thread.js`

Read current Phase 3a thread.js first:
```bash
ssh 192.168.88.28 'cat /home/pg/kleinanzeigen-bot-ma/web-app/js/screens/thread.js'
```

Replace ENTIRE content with:

```javascript
import { api } from "../api.js?v=20260510-8";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260510-8";
import { buildActionGrid } from "../components/action-grid.js?v=20260510-8";
import { buildEditForm } from "../components/edit-form.js?v=20260510-8";

const PENDING_STATUSES = new Set(["pending", "new", "edited", "approved"]);

// Module-state для lock release at unmount.
let _heldMsgId = null;

function findLatestPending(events) {
  let candidate = null;
  for (const ev of events) {
    if (ev.kind === "in" && ev.status && PENDING_STATUSES.has(ev.status) && ev.msg_id) {
      candidate = ev.msg_id;
    }
  }
  return candidate;
}

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

function eventBubble(event, latestPendingMsgId) {
  const isIn = event.kind === "in";
  const align = isIn ? "" : "ms-auto";
  const bg = isIn ? "bg-secondary-subtle" : "bg-primary-subtle";
  const tag = event.is_auto_ack ? "🤖 ack" : (isIn ? "👤" : "🏪");
  const isPending = isIn && event.msg_id === latestPendingMsgId &&
                    PENDING_STATUSES.has(event.status);
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
  bubble.querySelector(".who").textContent = isPending ? `${tag} 📝` : tag;
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

function pendingDraftBlock(review) {
  const block = el(`
    <div class="border-top pt-3 mt-3 pending-draft">
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

function lockedByOtherBanner(lock, onRetry) {
  const block = el(`
    <div class="alert alert-warning mt-3">
      <div class="mb-2"><strong>⚠️ Карточка занята оператором <span class="holder"></span>.</strong></div>
      <div class="small text-muted mb-2">Действия недоступны (осталось ~<span class="mins"></span> мин).</div>
      <button class="btn btn-sm btn-outline-primary retry-btn">↻ Проверить снова</button>
    </div>
  `);
  block.querySelector(".holder").textContent = lock.holder ?? "?";
  block.querySelector(".mins").textContent = String(lock.remaining_min ?? 0);
  block.querySelector(".retry-btn").addEventListener("click", onRetry);
  return block;
}

function backToPipeline() {
  return el(`<a class="btn btn-sm btn-outline-secondary mt-3" href="#/pipeline">↩ К pipeline</a>`);
}


async function tryReleaseLock(msgId) {
  if (msgId === null) return;
  try {
    await api(`/api/ma/messages/${msgId}/lock/release`, {method: "POST"});
  } catch (e) {
    console.warn("[thread] lock release failed:", e);
  }
}


export async function render(mount, params) {
  // Cleanup: release предыдущий lock (если был на другом thread)
  if (_heldMsgId !== null) {
    await tryReleaseLock(_heldMsgId);
    _heldMsgId = null;
  }

  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);
    const latestPendingMsgId = findLatestPending(data.events);

    let review = null;
    let acquired = false;
    let acquireError = null;

    if (latestPendingMsgId !== null) {
      try {
        review = await api(`/api/ma/messages/${latestPendingMsgId}`);
      } catch (e) {
        console.warn("[thread] review fetch failed:", e);
      }

      // Auto-acquire lock
      try {
        const lockRes = await api(`/api/ma/messages/${latestPendingMsgId}/lock/acquire`, {method: "POST"});
        acquired = true;
        _heldMsgId = latestPendingMsgId;
        if (review?.lock) {
          review.lock = lockRes;  // обновляем lock-state в review payload
        }
      } catch (e) {
        if (e.message && e.message.includes("HTTP 409")) {
          acquireError = "locked";
        } else {
          acquireError = "network";
          console.warn("[thread] lock acquire failed:", e);
        }
      }
    }

    renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError);

  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}


function renderThread(mount, params, data, latestPendingMsgId, review, acquired, acquireError) {
  const container = el(`<div></div>`);

  // Lock for header: используем lock-state из review (после acquire-call)
  container.appendChild(threadHeader(data.header, review?.lock ?? null));
  const related = relatedBlock(data.related);
  if (related) container.appendChild(related);
  if (data.events.length === 0) {
    container.appendChild(el(`<p class="text-muted">Событий пока нет.</p>`));
  } else {
    data.events.forEach(e => container.appendChild(eventBubble(e, latestPendingMsgId)));
  }

  if (review) {
    const draftBlock = pendingDraftBlock(review);
    container.appendChild(draftBlock);

    if (acquireError === "locked" && review.lock) {
      // 409 — read-only с retry
      container.appendChild(lockedByOtherBanner(review.lock, () => render(mount, params)));
    } else if (acquired) {
      // Render action-grid
      const grid = buildActionGrid({
        msgId: latestPendingMsgId,
        onActionComplete: (action, res) => {
          // After any successful action — re-render thread
          render(mount, params);
        },
        onError: (action, message) => {
          alert(`Ошибка: ${message}`);
        },
        onEditRequest: (field) => {
          // Replace action-grid with edit-form
          const oldGrid = container.querySelector(".action-grid");
          const form = buildEditForm({
            msgId: latestPendingMsgId,
            field,
            review,
            onSubmitComplete: () => render(mount, params),
            onCancel: () => render(mount, params),
            onError: (msg) => alert(`Ошибка: ${msg}`),
          });
          if (oldGrid && form) oldGrid.replaceWith(form);
        },
      });
      container.appendChild(grid);
    } else if (acquireError === "network") {
      container.appendChild(el(`<p class="text-warning small mt-3">⚠️ Не удалось взять lock — действия недоступны. <a href="#" onclick="event.preventDefault(); location.reload();">Перезагрузить</a></p>`));
    }
  }

  container.appendChild(backToPipeline());
  mount.replaceChildren(container);
}


// Lock release on navigation away
window.addEventListener("hashchange", () => {
  if (_heldMsgId !== null && !location.hash.startsWith("#/thread/")) {
    const msgId = _heldMsgId;
    _heldMsgId = null;
    tryReleaseLock(msgId);
  }
});

// Lock release on TG WebApp close / page hide
window.addEventListener("pagehide", () => {
  if (_heldMsgId !== null) {
    // sendBeacon — fire-and-forget на page-close
    const url = `${location.origin === "null" ? "" : ""}/api/ma/messages/${_heldMsgId}/lock/release`;
    navigator.sendBeacon?.(url);  // best-effort; backup — auto-expire 5мин
  }
});
```

⚠️ `sendBeacon` отправит без auth header'а — backup unreliable. Главная защита — auto-expire через 5 мин (Phase 3a operator_lock).

### Step 2: Bump cache-bust

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && grep -lr "?v=20260510-7" . | xargs sed -i "s/?v=20260510-7/?v=20260510-8/g"'
ssh 192.168.88.28 'sed -i "s/?v=20260510-7/?v=20260510-8/g" /home/pg/kleinanzeigen-bot-ma/web-app/index.html'
```

Verify:
```bash
ssh 192.168.88.28 'grep -rn "?v=20260510" /home/pg/kleinanzeigen-bot-ma/web-app/'
```
Expected: все `?v=20260510-8`.

### Step 3: Syntax check all files

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && for f in *.js components/*.js screens/*.js; do node --input-type=module --check < "$f" && echo "$f OK"; done'
```

Expected: all `OK`.

### Step 4: Pytest sanity

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `78 passed`.

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/ && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): wire action-grid + edit-form + lock lifecycle into thread.js"'
```

---

## Task 10: Merge to main, deploy, E2E smoke

**Files:** none (deployment task).

- [ ] **Step 1: Verify commits на ветке**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma log --oneline e65eda6..HEAD'
```

Expected: 9 commits (Tasks 1-9).

- [ ] **Step 2: Verify main clean**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot status -s'
```

Expected: empty. Если untracked — clean.

- [ ] **Step 3: Merge ma-phase3b → main**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot merge ma-phase3b --no-edit 2>&1 | tail -5'
```

Expected: Fast-forward.

- [ ] **Step 4: Restart bot**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 5 && systemctl is-active kleinanzeigen-bot && journalctl -u kleinanzeigen-bot -n 20 --no-pager --since "30 seconds ago" | grep -iE "ERROR|Traceback|Application startup" | head -10'
```

Expected: `active` + `Application startup complete`. Никаких ImportError.

- [ ] **Step 5: Local smoke endpoints**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "send: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/messages/1/send'
ssh 192.168.88.28 'curl -s -o /dev/null -w "lock acquire: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/messages/1/lock/acquire'
ssh 192.168.88.28 'curl -s -o /dev/null -w "lock release: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/messages/1/lock/release'
```

Expected: все `422` (no header).

- [ ] **Step 6: Verify cloudflared tunnel**

```bash
curl -s -o /dev/null -w "tunnel send: %{http_code}\n" -X POST https://choice-drunk-curriculum-effectiveness.trycloudflare.com/api/ma/messages/1/send
```

Expected: `422`. Если 000/timeout — tunnel умер, перезапустить:
```bash
ssh 192.168.88.28 'pkill cloudflared; nohup cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf-tunnel.log 2>&1 &'
sleep 6
ssh 192.168.88.28 'grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf-tunnel.log | head -1'
```
Если URL поменялся — обновить в `web-app/js/api.js:API_BASE`, bump cache-bust ещё раз, push.

- [ ] **Step 7: Push to GitHub**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main 2>&1 | tail -3'
```

- [ ] **Step 8: Wait for Pages rebuild**

```bash
until [ "$(curl -s -H "Authorization: Bearer github_pat_..." https://api.github.com/repos/pgolitech-lab/kleinanzeigen-bot/pages/builds/latest | python3 -c "import json,sys; print(json.load(sys.stdin).get('status'))" 2>/dev/null)" = "built" ]; do sleep 8; done
```

(Use the saved PAT from previous phases.)

Verify Pages serves new content:
```bash
curl -s -H "Cache-Control: no-cache" https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/js/screens/thread.js | grep -E "buildActionGrid|buildEditForm" | head -3
```

Expected: imports visible.

- [ ] **Step 9: E2E smoke в Telegram (manual)**

Закрой/открой MA в TG (на десктопе — полностью clear cache; ESM модули кэшируются агрессивно).

Pipeline → тред с pending → должен показать:
- header + events + pending-draft block (как Phase 3a)
- + Action-grid снизу: 11 кнопок в bot-mirror layout
- Если другой оператор взял lock в боте → banner вместо action-grid + retry

Tap `❌ Пропустить` → кнопка должна морфнуть в `⚠️ Пропустить? [Да] [Нет]`. Tap `[Да]` → status меняется на `skipped`, screen re-rendится. В TG-боте карточка обновляется (broadcast).

Tap `✏️ Правка RU` → action-grid скрывается, появляется textarea с pre-filled ru_answer + Save/Cancel. Save → POST, re-render с обновлёнными de_answer и ru_translation.

Tap `💸 Своя цена` → numeric input + Save. Введи `1300` → Save → regenerate с ценой 1300€.

Tap `👊 Жёстче` → confirm → Yes → новый draft.

Tap `✅ ОТПРАВИТЬ` → confirm → Yes → email уходит (если `send_mode=production`) или skipped/sent_debug.

- [ ] **Step 10: Phase 3b acceptance**

Phase 3b закрыт когда:
- ✅ Открываешь MA → tread с pending → видны action-grid (11 кнопок)
- ✅ Confirm pattern работает (tap → морф → Да/Нет)
- ✅ Edit-inline работает (textarea pre-filled, Save → re-render)
- ✅ Регенерации меняют draft + deal_brief
- ✅ Send/skip/sold завершают action + бот видит обновление через broadcast
- ✅ Если другой оператор работает в боте → MA показывает 409-banner
- ✅ Все 78 unit-тестов проходят
- ✅ Никаких ERROR/Traceback в журнале бота по `/api/ma/*`

---

## Cleanup

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree remove /home/pg/kleinanzeigen-bot-ma'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot branch -d ma-phase3b'
```

## Risks

- **`broadcast_after_external_action` без keyboard** — TG-карточка после MA-action имеет старый keyboard. Оператор может нажать кнопку которая уже невалидна (например ✅ Send когда уже sent). Phase 3a guard в bot уже отработает («status уже не actionable») — будет error message в TG, не падение. Acceptable для Phase 3b. Phase 4 может усилить если потребуется.
- **`scheduler.send_one` synchronous + блокирует event loop** — мы зовём через `asyncio.to_thread`, хорошо. Но sync вызов держит SMTP connection ~3 сек. FastAPI threadpool default ~40 workers — не блокер при наших нагрузках.
- **Lock на edit-mode** — оператор открыл edit-RU, печатает 2 минуты, lock auto-expire (5 мин) — потенциально другой возьмёт. Решение: не критично для MVP, оператор Save → если 409, видит ошибку. Phase 4 может добавить heartbeat.
- **`broadcast_after_external_action` reads bot-token from config.telegram_bot_token()** — должно работать. Если token пустой → exception → log + no broadcast (action всё равно завершился). OK.
- **`navigator.sendBeacon` без auth** — release lock на page-close могут не пройти. Backup: auto-expire 5 мин.
- **Cloudflared Quick Tunnel** — может умереть. Step 6 of Task 10 ловит и перезапускает.
