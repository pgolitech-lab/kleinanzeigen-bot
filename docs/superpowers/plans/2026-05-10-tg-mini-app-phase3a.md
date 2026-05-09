# Telegram Mini App — Phase 3a (Read-only review card) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оператор открывает тред в MA → видит pending-draft блок (RU + DE + back-translation + deal_brief) и lock-state в header. Данные read-only — никаких action-кнопок ещё нет (это Phase 3b).

**Architecture:** Выносим `_THREAD_LOCKS` dict из `modules/telegram_bot.py` в отдельный модуль `modules/operator_lock.py` (без поведенческих изменений). Добавляем 2 GET endpoint'а в `web/api_ma.py`: `/messages/{id}` (полный review payload) и `/messages/{id}/lock` (poll endpoint). Дополняем frontend `web-app/js/screens/thread.js` — fetch второго запроса для свежего pending msg_id, рендер lock-badge в header и pending-draft блока.

**Tech Stack:** Python 3.11 / FastAPI APIRouter / pytest + TestClient + MagicMock / vanilla JS + Preact runtime + HTM (CDN) / Bootstrap 5.3.

**Все команды на проде через ssh** (`ssh 192.168.88.28 ...`). Работаем в worktree `/home/pg/kleinanzeigen-bot-ma` на ветке `ma-phase3a`. Прод-бот не трогаем до финального merge.

**Spec reference:** `docs/superpowers/specs/2026-05-10-tg-mini-app-phase3a-design.md`

**Phase 1-2 wrap-up (обязательное чтение):**
- Phase 1 docs: `docs/superpowers/plans/2026-05-09-tg-mini-app-phase1.md`
- Phase 2 docs: `docs/superpowers/plans/2026-05-10-tg-mini-app-phase2.md`
- Pages URL: `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/`
- Cloudflared Quick Tunnel (URL ротируется): `https://choice-drunk-curriculum-effectiveness.trycloudflare.com` — если процесс не жив, перезапустить через `nohup cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf-tunnel.log 2>&1 &`, достать URL grep'ом, обновить `web-app/js/api.js:API_BASE`.
- Текущий baseline на main: 29 unit тестов, после Phase 3a будет ~40.

**Worktree setup (выполняется один раз перед Task 1):**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree add /home/pg/kleinanzeigen-bot-ma -b ma-phase3a && cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `29 passed`. Worktree на ветке `ma-phase3a`, prod-бот продолжает работать от main.

---

## File Structure

**Создаются:**
- `modules/operator_lock.py` — primitives, ~50 строк (state/remember/forget/remaining_min)
- `tests/test_operator_lock.py` — unit тесты примитивов (6 тестов)
- `tests/test_api_ma_messages.py` — TestClient тесты двух новых endpoint'ов (9 тестов)

**Модифицируются:**
- `modules/telegram_bot.py:858-960` — заменяем модульный `_THREAD_LOCKS` на импорт из `operator_lock`, переписываем wrappers через примитивы (поведение не меняем)
- `web/api_ma.py` — добавляем `GET /messages/{id}` и `GET /messages/{id}/lock`
- `web-app/js/screens/thread.js` — расширяем `render` чтобы fetch'ить review payload, добавляем `findLatestPending`, `pendingDraftBlock`, обновляем `threadHeader` под опциональный `lock` параметр
- `web-app/js/utils.js` — без изменений (используем существующие `el`, `esc`, `setLoading`, `setError`)
- `web-app/index.html` — bump cache-bust до `?v=20260510-7`
- Все ESM-импорты в `web-app/js/**/*.js` — bump query string до `?v=20260510-7`

**Не трогаем (явно):**
- `modules/telegram_bot.py` outside lock section (~158k остальных строк)
- `web-app/js/screens/pipeline.js`, `web-app/js/screens/history.js`
- `web-app/js/router.js`, `tg.js`, `app.js`, `api.js`
- Все Phase 1-2 endpoint'ы в `web/api_ma.py`

---

## Task 1: `operator_lock` module (TDD)

**Files:**
- Create: `tests/test_operator_lock.py`
- Create: `modules/operator_lock.py`

**Background:** Pure primitives для shared in-memory lock на review-карточку. Используется и `telegram_bot.py` (через wrappers) и `web/api_ma.py` (Phase 3a — только `state()` для отображения; Phase 3b будет `remember`/`forget` через POST endpoints).

- [ ] **Step 1: Write failing tests**

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_operator_lock.py`:

```python
"""Тесты примитивов operator_lock module."""
from __future__ import annotations
from unittest.mock import patch

import pytest

from modules import operator_lock


@pytest.fixture(autouse=True)
def _reset_locks():
    """Каждый тест начинается с пустого dict."""
    operator_lock._LOCKS.clear()
    yield
    operator_lock._LOCKS.clear()


def test_state_returns_none_when_empty():
    assert operator_lock.state(123) is None


def test_remember_and_state_roundtrip():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    st = operator_lock.state(123)
    assert st is not None
    actor, acquired_at = st
    assert actor == "@alice"
    assert acquired_at == 1000.0


def test_state_returns_none_after_timeout():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    # 5 минут + 1 сек
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 301):
        assert operator_lock.state(123) is None


def test_state_purges_expired_entry_on_read():
    """После first state-call с expired locks, dict должен быть очищен."""
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 301):
        operator_lock.state(123)
        # После state-вызова entry должен быть удалён
        assert 123 not in operator_lock._LOCKS


def test_forget_idempotent():
    operator_lock.forget(123)  # не существует — не падает
    operator_lock.remember(123, "@alice")
    operator_lock.forget(123)
    operator_lock.forget(123)  # повторный — не падает
    assert operator_lock.state(123) is None


def test_remaining_min_decreases_over_time():
    with patch("modules.operator_lock.time.time", return_value=1000.0):
        operator_lock.remember(123, "@alice")
    # Сразу после remember — почти полные 5 минут (4 + 1 = 5)
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 1):
        assert operator_lock.remaining_min(123) == 5
    # Через 100 сек — осталось ~3+1=4
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 100):
        assert operator_lock.remaining_min(123) == 4
    # Через 200 сек — ~1+1=2
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 200):
        assert operator_lock.remaining_min(123) == 2
    # После expiry — 0
    with patch("modules.operator_lock.time.time", return_value=1000.0 + 400):
        assert operator_lock.remaining_min(123) == 0


def test_remaining_min_zero_when_no_lock():
    assert operator_lock.remaining_min(123) == 0
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_operator_lock.py -v'
```

Expected: ImportError на `from modules import operator_lock` → `collected 0 items / 1 error`.

- [ ] **Step 3: Implement `modules/operator_lock.py`**

Create file with:

```python
"""Shared in-memory lock на review-карточку (msg_id).

Используется и telegram_bot, и web/api_ma. Bot wrappers в telegram_bot.py
(_acquire_lock/_release_lock/_check_lock/_lock_remaining_min) держат
orchestration с thread_busy + drain_deferred — здесь только примитивы.
"""
from __future__ import annotations
import time

LOCK_TIMEOUT_SEC = 300

# msg_id -> (actor_str, acquired_at_unix)
_LOCKS: dict[int, tuple[str, float]] = {}


def state(msg_id: int) -> tuple[str, float] | None:
    """Текущий holder, None если свободно или auto-expired.

    На read обнаруженный expired-lock автоматически удаляется из dict.
    """
    e = _LOCKS.get(msg_id)
    if e is None:
        return None
    if time.time() - e[1] > LOCK_TIMEOUT_SEC:
        _LOCKS.pop(msg_id, None)
        return None
    return e


def remember(msg_id: int, actor: str) -> None:
    """Низкоуровневый set. Caller отвечает за thread_busy + drain orchestration."""
    _LOCKS[msg_id] = (actor, time.time())


def forget(msg_id: int) -> None:
    """Низкоуровневый del. No-op если key отсутствует."""
    _LOCKS.pop(msg_id, None)


def remaining_min(msg_id: int) -> int:
    """Минут до auto-release (для UI). 0 если нет lock или expired."""
    e = state(msg_id)
    if e is None:
        return 0
    age = time.time() - e[1]
    return max(0, int((LOCK_TIMEOUT_SEC - age) // 60) + 1)
```

- [ ] **Step 4: Run, expect PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_operator_lock.py -v'
```

Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/operator_lock.py tests/test_operator_lock.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(lock): operator_lock module — primitives + tests"'
```

Verify branch:
```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma branch --show-current'
```
Expected: `ma-phase3a`.

---

## Task 2: Refactor `telegram_bot.py` to use operator_lock

**Files:**
- Modify: `modules/telegram_bot.py:858-960` (lock section)

**Background:** Сохраняем поведение existing wrappers — те же сигнатуры `_acquire_lock(msg_id, actor)`, `_release_lock(msg_id)`, `_check_lock(msg_id, actor) -> Optional[str]`, `_lock_remaining_min(msg_id) -> int`. Внутри они теперь вызывают `operator_lock.{remember,forget,state,remaining_min}` плюс существующая orchestration (thread_busy, drain_deferred).

30+ call-sites НЕ меняем.

- [ ] **Step 1: Read current lock section to know exact lines**

```bash
ssh 192.168.88.28 'sed -n "858,960p" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

Expected: видим `_THREAD_LOCKS: dict... = {}` (line 861), `LOCK_TIMEOUT_SEC = 300` (line 862), функции `_check_lock`, `_acquire_lock`, `_release_lock`, `_lock_remaining_min`, и связанные хелперы `_msg_thread_id`, `mark_thread_busy`, `clear_thread_busy`, `thread_is_busy`, `_THREAD_BUSY`.

**Внимание:** оставляем нетронутыми: `_THREAD_BUSY`, `thread_is_busy`, `mark_thread_busy`, `clear_thread_busy`, `_msg_thread_id`. Меняем только: `_THREAD_LOCKS` (удаляем), `LOCK_TIMEOUT_SEC` (удаляем), `_check_lock`, `_acquire_lock`, `_release_lock`, `_lock_remaining_min`.

- [ ] **Step 2: Find imports section in telegram_bot.py**

```bash
ssh 192.168.88.28 'grep -n "^from modules\|^import modules" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -10'
```

Expected: видим существующие `from modules import ...`. Запомни последний line number — туда добавим новый import.

- [ ] **Step 3: Add `from modules import operator_lock` to imports**

В начале файла, рядом с другими `from modules import ...`, добавить новую строку:
```python
from modules import operator_lock
```

Если уже есть `from modules import X, Y` — можно расширить: `from modules import X, Y, operator_lock`. Но проще новой строкой.

- [ ] **Step 4: Replace lock-section bodies**

Используя Edit tool, найди и замени блок (точные старые строки прямо из файла; ниже — что должно стать после замены):

**Удаляем (строки ~861-862):**
```python
_THREAD_LOCKS: dict[int, tuple[str, datetime]] = {}
LOCK_TIMEOUT_SEC = 300  # 5 минут
```

(Комментарий перед ними тоже можно удалить или обновить — не критично.)

**Заменяем `_check_lock`:**

Старое (примерно):
```python
def _check_lock(msg_id: int, actor: str) -> Optional[str]:
    """Возвращает имя текущего владельца если лок занят ДРУГИМ. None если свободно/мой."""
    e = _THREAD_LOCKS.get(msg_id)
    if not e:
        return None
    owner, acquired = e
    if owner == actor:
        return None
    age = (datetime.utcnow() - acquired).total_seconds()
    if age < LOCK_TIMEOUT_SEC:
        return owner
    # Auto-expire
    del _THREAD_LOCKS[msg_id]
    clear_thread_busy(_msg_thread_id(msg_id), "operator")
    return None
```

Новое:
```python
def _check_lock(msg_id: int, actor: str) -> Optional[str]:
    """Возвращает имя текущего владельца если лок занят ДРУГИМ. None если свободно/мой."""
    st = operator_lock.state(msg_id)
    if st is None:
        # Lock мог только что auto-expire-нуть внутри state() — синхронизируем busy-flag.
        clear_thread_busy(_msg_thread_id(msg_id), "operator")
        return None
    owner, _ = st
    if owner == actor:
        return None
    return owner
```

**Заменяем `_acquire_lock`:**

Старое:
```python
def _acquire_lock(msg_id: int, actor: str) -> None:
    _THREAD_LOCKS[msg_id] = (actor, datetime.utcnow())
    mark_thread_busy(_msg_thread_id(msg_id), "operator", actor)
```

Новое:
```python
def _acquire_lock(msg_id: int, actor: str) -> None:
    operator_lock.remember(msg_id, actor)
    mark_thread_busy(_msg_thread_id(msg_id), "operator", actor)
```

**Заменяем `_release_lock`:**

Старое:
```python
def _release_lock(msg_id: int) -> None:
    _THREAD_LOCKS.pop(msg_id, None)
    thread_id = _msg_thread_id(msg_id)
    clear_thread_busy(thread_id, "operator")
    # После освобождения — попробуем поднять отложенные карточки.
    if thread_id:
        try:
            import scheduler as _sched
            _sched.drain_deferred_thread(thread_id)
        except Exception:
            logger.exception("drain_deferred_thread fail")
```

Новое:
```python
def _release_lock(msg_id: int) -> None:
    operator_lock.forget(msg_id)
    thread_id = _msg_thread_id(msg_id)
    clear_thread_busy(thread_id, "operator")
    # После освобождения — попробуем поднять отложенные карточки.
    if thread_id:
        try:
            import scheduler as _sched
            _sched.drain_deferred_thread(thread_id)
        except Exception:
            logger.exception("drain_deferred_thread fail")
```

**Заменяем `_lock_remaining_min`:**

Старое:
```python
def _lock_remaining_min(msg_id: int) -> int:
    """Минут до auto-release. 0 если нет лока или истёк."""
    e = _THREAD_LOCKS.get(msg_id)
    if not e:
        return 0
    age = (datetime.utcnow() - e[1]).total_seconds()
    remaining = LOCK_TIMEOUT_SEC - age
    return max(0, int(remaining // 60) + 1)
```

Новое:
```python
def _lock_remaining_min(msg_id: int) -> int:
    """Минут до auto-release. 0 если нет лока или истёк."""
    return operator_lock.remaining_min(msg_id)
```

- [ ] **Step 5: Verify file imports clean (sanity)**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot; print(\"OK\")"'
```

Expected: `OK`. Если ImportError или другие ошибки — fix перед коммитом.

- [ ] **Step 6: Verify all usages of removed names are gone**

```bash
ssh 192.168.88.28 'grep -n "_THREAD_LOCKS\|LOCK_TIMEOUT_SEC" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

Expected: пусто. Если что-то найдено — это позабытые ссылки, поправить.

- [ ] **Step 7: Run full test suite — operator_lock tests + Phase 1-2 (29) + new ones**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ -v 2>&1 | tail -10'
```

Expected: `36 passed` (29 + 7 new operator_lock tests).

- [ ] **Step 8: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "refactor(lock): telegram_bot delegates to operator_lock module"'
```

---

## Task 3: `GET /api/ma/messages/{id}` endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Create: `tests/test_api_ma_messages.py`

**Background:** Полный review payload — frontend дёргает для рендера pending-draft секции. Тонкая обёртка над `db.get_message(id)` + `db.find_related_inquiries` + `db.get_thread_autopilot` + `operator_lock.state` + `json.loads(deal_brief_json)`.

- [ ] **Step 1: Write failing tests**

Create `/home/pg/kleinanzeigen-bot-ma/tests/test_api_ma_messages.py`:

```python
"""TestClient тесты для GET /api/ma/messages/{id} и /lock."""
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
         patch("web.api_ma.operator_lock") as mol:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.get_message.return_value = None
        mdb.find_related_inquiries.return_value = []
        mdb.get_thread_autopilot.return_value = None
        mol.state.return_value = None
        mol.remaining_min.return_value = 0
        from web.app import app
        yield TestClient(app), mdb, mol


def _full_msg_row(**overrides):
    """Helper для создания row с реалистичным набором колонок review-карточки."""
    defaults = {
        "id": 123,
        "gmail_thread_id": "abc",
        "status": "pending",
        "direction": "in",
        "ad_title": "Sitzbank",
        "ad_price": "1500€",
        "ad_url": "https://kleinanzeigen.de/x/2812",
        "ad_id": "2812",
        "buyer_display_name": "Osman",
        "buyer_name": "osman@gmx.de",
        "client_lang": "de",
        "de_client": "Können Sie 1300?",
        "ru_client": "Можете уступить до 1300?",
        "ru_answer": "Минимум 1400, при самовывозе...",
        "de_answer": "Mindestens 1400€... MfG",
        "ru_translation": "Минимум 1400€... С уважением",
        "deal_brief_json": '{"summary_ru":"торгуется","negotiated_price_eur":1300,"client_assessment":"серьёзный","expected_next":"ждём ответ"}',
        "extra_notes": None,
        "is_auto_ack": 0,
    }
    defaults.update(overrides)
    return _row(**defaults)


def test_message_review_404_when_missing(client):
    c, mdb, mol = client
    mdb.get_message.return_value = None
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/999", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_message_review_returns_full_payload(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["msg_id"] == 123
    assert body["thread_id"] == "abc"
    assert body["status"] == "pending"
    assert body["ad"]["title"] == "Sitzbank"
    assert body["ad"]["price"] == "1500€"
    assert body["ad"]["buyer_email"] == "osman@gmx.de"
    assert body["client_lang"] == "de"
    assert body["client_message"]["raw"] == "Können Sie 1300?"
    assert body["client_message"]["ru"] == "Можете уступить до 1300?"
    assert body["draft"]["ru_answer"].startswith("Минимум 1400")
    assert body["draft"]["de_answer"].startswith("Mindestens")
    assert body["draft"]["ru_translation"].startswith("Минимум 1400€")
    assert body["lock"]["holder"] is None
    assert body["lock"]["remaining_min"] == 0
    assert body["autopilot"]["active"] is False


def test_message_review_deal_brief_parses_json(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    brief = res.json()["deal_brief"]
    assert brief["summary_ru"] == "торгуется"
    assert brief["negotiated_price_eur"] == 1300
    assert brief["client_assessment"] == "серьёзный"
    assert brief["expected_next"] == "ждём ответ"


def test_message_review_invalid_deal_brief_json_returns_null(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row(deal_brief_json="{not_valid_json")
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["deal_brief"] is None


def test_message_review_null_deal_brief_returns_null(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row(deal_brief_json=None)
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    assert res.json()["deal_brief"] is None


def test_message_review_lock_holder_when_held(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mol.state.return_value = ("@other#111", 1700000000.0)
    mol.remaining_min.return_value = 4
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["lock"]["holder"] == "@other#111"
    assert body["lock"]["remaining_min"] == 4


def test_message_review_autopilot_active(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    autopilot = MagicMock()
    autopilot.__getitem__.side_effect = {
        "active": 1, "messages_sent": 5, "floor_eur": 1200, "notify_mode": "silent",
    }.__getitem__
    autopilot.keys.return_value = ["active", "messages_sent", "floor_eur", "notify_mode"]
    mdb.get_thread_autopilot.return_value = autopilot
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["autopilot"]["active"] is True
    assert body["autopilot"]["messages_sent"] == 5
    assert body["autopilot"]["floor_eur"] == 1200
    assert body["autopilot"]["notify_mode"] == "silent"


def test_message_review_includes_related(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mdb.find_related_inquiries.return_value = [
        _row(gmail_thread_id="t2", ad_title="Other", ad_price="500€",
             sent_at="2026-05-09T10:00:00", created_at="2026-05-09T09:00:00"),
    ]
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    related = res.json()["related"]
    assert related["buyer_display_name"] == "Osman"
    assert len(related["matches"]) == 1
    assert related["matches"][0]["thread_id"] == "t2"


def test_message_review_requires_auth(client):
    c, mdb, mol = client
    res = c.get("/api/ma/messages/123")
    assert res.status_code == 422


def test_message_review_rejects_bad_hash(client):
    c, mdb, mol = client
    res = c.get(
        "/api/ma/messages/123",
        headers={"X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=99&hash=fake"},
    )
    assert res.status_code == 401
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_messages.py -v'
```

Expected: ImportError on `web.api_ma.operator_lock` (т.к. `web/api_ma.py` ещё не импортирует `operator_lock`) или 404 на endpoint.

- [ ] **Step 3: Implement endpoint**

В `/home/pg/kleinanzeigen-bot-ma/web/api_ma.py`:

(a) Добавить импорт сверху рядом с другими (после `from fastapi import APIRouter, Depends`):

```python
import json

from modules import operator_lock
```

(`from fastapi import HTTPException` уже должен быть после Phase 2 task 3 — если нет, добавь.)

(b) Добавить хелпер `_parse_deal_brief` где-то возле других `_*_to_*` helpers:

```python
def _parse_deal_brief(raw: str | None) -> dict[str, Any] | None:
    """Parse messages.deal_brief_json. None при NULL/empty/invalid JSON."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _autopilot_view(autopilot_row: Any) -> dict[str, Any]:
    """Standard autopilot dict для frontend. Defaults если row=None."""
    if autopilot_row is None:
        return {"active": False, "messages_sent": 0, "floor_eur": None, "notify_mode": None}
    try:
        return {
            "active": bool(autopilot_row["active"]),
            "messages_sent": autopilot_row["messages_sent"] if "messages_sent" in autopilot_row.keys() else 0,
            "floor_eur": autopilot_row["floor_eur"] if "floor_eur" in autopilot_row.keys() else None,
            "notify_mode": autopilot_row["notify_mode"] if "notify_mode" in autopilot_row.keys() else None,
        }
    except (KeyError, IndexError):
        return {"active": False, "messages_sent": 0, "floor_eur": None, "notify_mode": None}
```

(c) Добавить endpoint после существующих (`ma_pipeline`, `ma_thread`, `ma_client_history`):

```python
@router.get("/messages/{msg_id}")
async def ma_message_review(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Полный review payload: ad meta + client message + draft + deal_brief + related + lock + autopilot."""
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
        "msg_id": row["id"],
        "thread_id": thread_id,
        "status": row["status"],
        "ad": {
            "title": row["ad_title"],
            "price": row["ad_price"],
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
        "lock": {
            "holder": lock_holder,
            "remaining_min": operator_lock.remaining_min(msg_id),
        },
        "autopilot": _autopilot_view(autopilot_row),
        "extra_notes": row["extra_notes"] if "extra_notes" in row.keys() else None,
        "is_auto_ack": bool(row["is_auto_ack"]) if "is_auto_ack" in row.keys() else False,
    }
```

- [ ] **Step 4: Run tests**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_messages.py -v'
```

Expected: 9 from this file + 1 lock test (next task) — но мы в этой task имеем 10 функций, из них 9 для review (один test_message_lock_state мы напишем в Task 4). Здесь только 9 review-related tests. Они должны passed.

Точнее: после step 3 имплементации, тесты `test_message_review_*` (8 шт) + `test_message_review_requires_auth` + `test_message_review_rejects_bad_hash` = 10 → expected 10 passed.

Если что-то падает на типе аргументов (msg_id как str вместо int) — добавь `Path(...)` typing (FastAPI авто-конвертит из URL).

- [ ] **Step 5: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_messages.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): GET /api/ma/messages/{id} review payload endpoint"'
```

---

## Task 4: `GET /api/ma/messages/{id}/lock` endpoint (TDD)

**Files:**
- Modify: `web/api_ma.py`
- Modify: `tests/test_api_ma_messages.py` (append 3 tests)

**Background:** Лёгкий poll endpoint — только lock state. Phase 3b будет polling каждые ~5 сек чтобы операторы видели актуальный lock-state. В 3a — endpoint существует но frontend пока зовёт его раз при render-е.

- [ ] **Step 1: Append failing tests to `tests/test_api_ma_messages.py`**

В конец файла добавить:

```python
def test_lock_state_returns_holder(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mol.state.return_value = ("@alice#100", 1700000000.0)
    mol.remaining_min.return_value = 3
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123/lock", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["holder"] == "@alice#100"
    assert body["remaining_min"] == 3


def test_lock_state_returns_null_when_free(client):
    c, mdb, mol = client
    mdb.get_message.return_value = _full_msg_row()
    mol.state.return_value = None
    mol.remaining_min.return_value = 0
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/123/lock", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["holder"] is None
    assert body["remaining_min"] == 0


def test_lock_404_when_msg_missing(client):
    c, mdb, mol = client
    mdb.get_message.return_value = None
    init = make_init_data(TEST_USER)
    res = c.get("/api/ma/messages/999/lock", headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_lock_endpoint_requires_auth(client):
    c, mdb, mol = client
    res = c.get("/api/ma/messages/123/lock")
    assert res.status_code == 422
```

- [ ] **Step 2: Run, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_messages.py -v 2>&1 | tail -15'
```

Expected: 4 new tests fail with 404 (no endpoint), остальные 10 — pass.

- [ ] **Step 3: Add endpoint to `web/api_ma.py`**

После `ma_message_review` добавить:

```python
@router.get("/messages/{msg_id}/lock")
async def ma_message_lock_state(msg_id: int, user: dict = Depends(verify_init_data_dep)) -> dict[str, Any]:
    """Lock-state poll endpoint — лёгкий, без full review payload."""
    row = db.get_message(msg_id)
    if row is None:
        raise HTTPException(404, "message not found")
    st = operator_lock.state(msg_id)
    return {
        "holder": st[0] if st else None,
        "remaining_min": operator_lock.remaining_min(msg_id),
    }
```

- [ ] **Step 4: Run tests, expect all PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/test_api_ma_messages.py -v'
```

Expected: `14 passed` (10 review + 4 lock).

- [ ] **Step 5: Run full suite (no regressions)**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `50 passed` (29 baseline + 7 operator_lock + 14 messages).

Если выходит больше — кто-то добавил тесты по дороге, не критично. Если меньше — баг в imports или mock.

- [ ] **Step 6: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web/api_ma.py tests/test_api_ma_messages.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): GET /api/ma/messages/{id}/lock poll endpoint"'
```

---

## Task 5: Frontend — thread.js дополнения

**Files:**
- Modify: `web-app/js/screens/thread.js`
- Modify: `web-app/index.html` (cache-bust)
- Modify: `web-app/js/app.js`, `router.js`, `api.js`, `utils.js`, `tg.js`, `screens/pipeline.js`, `screens/history.js` (только cache-bust в импортах)

**Background:** Расширяем Phase 2 thread.js минимально — добавляем `findLatestPending`, `pendingDraftBlock`, обновляем `threadHeader` под опциональный `lock` параметр, в `render` параллельно fetch'им review payload для свежего pending msg_id.

- [ ] **Step 1: Read current thread.js**

```bash
ssh 192.168.88.28 'cat /home/pg/kleinanzeigen-bot-ma/web-app/js/screens/thread.js'
```

Это base — Phase 2 версия (~106 строк).

- [ ] **Step 2: Replace thread.js целиком**

Write `/home/pg/kleinanzeigen-bot-ma/web-app/js/screens/thread.js` с новым содержимым:

```javascript
import { api } from "../api.js?v=20260510-7";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260510-7";

const PENDING_STATUSES = new Set(["pending", "new", "edited", "approved"]);


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


function backToPipeline() {
  return el(`<a class="btn btn-sm btn-outline-secondary" href="#/pipeline">↩ К pipeline</a>`);
}


export async function render(mount, params) {
  setLoading(mount, "Загружаю тред…");
  try {
    const data = await api(`/api/ma/threads/${encodeURIComponent(params.thread_id)}`);
    const latestPendingMsgId = findLatestPending(data.events);

    let review = null;
    if (latestPendingMsgId !== null) {
      try {
        review = await api(`/api/ma/messages/${latestPendingMsgId}`);
      } catch (e) {
        console.warn("[thread] review fetch failed:", e);
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

- [ ] **Step 3: Bump cache-bust в остальных JS-файлах**

Заменить во всех 6 файлах под `web-app/js/` префикс старой версии на `?v=20260510-7`.

Run:
```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && grep -lr "?v=20260510-6" . | xargs sed -i "s/?v=20260510-6/?v=20260510-7/g"'
```

- [ ] **Step 4: Bump cache-bust в `web-app/index.html`**

```bash
ssh 192.168.88.28 'sed -i "s/?v=20260510-6/?v=20260510-7/g" /home/pg/kleinanzeigen-bot-ma/web-app/index.html'
```

- [ ] **Step 5: Verify version updates**

```bash
ssh 192.168.88.28 'grep -rn "?v=20260510" /home/pg/kleinanzeigen-bot-ma/web-app/'
```

Expected: ВСЕ строки имеют `?v=20260510-7`. Никаких `-6` или старых номеров.

- [ ] **Step 6: JS syntax check**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma/web-app/js && for f in *.js screens/*.js; do node --input-type=module --check < "$f" && echo "$f OK"; done'
```

Expected: каждый файл `OK`.

- [ ] **Step 7: Pytest sanity (не должно сломаться)**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -m pytest tests/ 2>&1 | tail -3'
```

Expected: `50 passed`.

- [ ] **Step 8: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add web-app/ && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(ma): thread.js — pending-draft block + lock badge in header"'
```

---

## Task 6: Merge to main, deploy, E2E smoke

**Files:** none (deployment task).

**Background:** Sync ma-phase3a → main. Push to GitHub Pages. Restart prod bot чтобы /api/ma/messages/* endpoint появились в живом FastAPI.

- [ ] **Step 1: Verify все коммиты на ветке**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma log --oneline a216368..HEAD'
```

Expected: 5 коммитов:
1. feat(lock): operator_lock module — primitives + tests
2. refactor(lock): telegram_bot delegates to operator_lock module
3. feat(ma): GET /api/ma/messages/{id} review payload endpoint
4. feat(ma): GET /api/ma/messages/{id}/lock poll endpoint
5. feat(ma): thread.js — pending-draft block + lock badge in header

(SHA-ы будут конкретные, важен набор.)

- [ ] **Step 2: Verify нет untracked в основном checkout**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot status -s'
```

Expected: пусто. Если есть untracked file — удали или закоммить (см. Phase 2 опыт с web/__init__.py).

- [ ] **Step 3: Merge ma-phase3a → main**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot merge ma-phase3a --no-edit 2>&1 | tail -5'
```

Expected: `Fast-forward` или mergeommit с указанием изменённых файлов. Никаких conflict-ов (мы единолично работаем).

- [ ] **Step 4: Restart bot — pick up endpoints**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 4 && systemctl is-active kleinanzeigen-bot && journalctl -u kleinanzeigen-bot -n 30 --no-pager --since "30 seconds ago" | grep -iE "ERROR|Traceback|started|Application startup" | head -10'
```

Expected: `active` + видим `Application startup complete`. Никаких ImportError или Traceback.

- [ ] **Step 5: Local smoke — endpoint доступен**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "messages: %{http_code}\n" http://127.0.0.1:8080/api/ma/messages/1; curl -s -o /dev/null -w "lock: %{http_code}\n" http://127.0.0.1:8080/api/ma/messages/1/lock'
```

Expected: оба `422` (no header — auth dep complains).

- [ ] **Step 6: Verify Cloudflared tunnel жив**

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://choice-drunk-curriculum-effectiveness.trycloudflare.com/api/ma/messages/1
```

Expected: `422`. Если 000/404 — Quick Tunnel умер, перезапустить:
```bash
ssh 192.168.88.28 'pkill cloudflared; nohup cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf-tunnel.log 2>&1 &'
sleep 6
ssh 192.168.88.28 'grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf-tunnel.log | head -1'
```

Если URL поменялся — обнови `web-app/js/api.js:API_BASE` соответствующе, bump cache-bust ещё раз, commit, push.

- [ ] **Step 7: Push to GitHub**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main 2>&1 | tail -3'
```

Expected: `<old>..<new> main -> main`.

- [ ] **Step 8: Wait for Pages rebuild**

GitHub Pages CDN обычно 30-90 секунд. Проверить:
```bash
sleep 90
curl -s -H "Cache-Control: no-cache" "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/js/screens/thread.js" | grep -E "pendingDraftBlock|findLatestPending" | head -3
```

Expected: видим `function pendingDraftBlock` или `findLatestPending` — Pages обновлён.

- [ ] **Step 9: E2E в Telegram (manual)**

Открыть Telegram → DM с ботом → tap pill-кнопку «Operator».

Должно показать pipeline (как Phase 2). Тапнуть тред с `📝` бейджем (pending draft есть).

В open thread проверить:

✅ Если у треда есть pending-draft:
- Под events visible новый блок «📝 Наш ответ #N (черновик · pending)»
- Видно RU answer (наша инструкция)
- Видно DE answer (текст для клиента)
- Видно RU обратный перевод (italic)
- Видно строку с deal_brief: `💬 ... · 💰 торг: 1300€ · 🏷 серьёзный`

✅ Если тред занят другим оператором (lock acquired в боте):
- Под header виден `🟥 в работе у @other (Nмин)`

❌ Тред БЕЗ pending — выглядит как Phase 2 (нет нового блока, нет lock-badge).

❌ В DevTools (правый клик WebApp на десктопе → Inspect) — нет ошибок в Console.

- [ ] **Step 10: Phase 3a acceptance**

Phase 3a закрыт когда:
- ✅ Open MA из TG → pipeline → tap thread с pending → видно pending-draft block
- ✅ В треде с lock-acquired (можно вручную через бот: тапнуть `✏️ Правка RU` → не отменять) — видно lock-badge на MA-стороне
- ✅ Тред без pending — без визуальных регрессий
- ✅ Все 50 unit-тестов проходят
- ✅ В журнале бота нет ERROR/Traceback по `/api/ma/*` после restart

---

## Cleanup (после Phase 3a acceptance)

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree remove /home/pg/kleinanzeigen-bot-ma'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot branch -d ma-phase3a'
```

## Что делает Phase 3b (контекст для будущей работы)

- POST endpoints: send / skip / sold / regenerate {strategy} / edit-ru / edit-de / price / instruction
- Lock endpoints: POST acquire / POST release с правильным thread_busy + drain orchestration (зеркало bot wrappers)
- Frontend: action-grid под pending-draft block, edit-inline pattern (textarea inline + submit)
- Backend: после mutating action — вызов `telegram_bot._broadcast_card(msg_id)` чтобы синхронизировать TG-карточки
- Auto-acquire lock при открытии review-screen (с 409 на конфликт)
- Live polling lock-state через `/messages/{id}/lock` каждые 5 сек

## Risks для 3a

- **`telegram_bot.py` lock refactor может сломать live bot** — wrappers сохраняют сигнатуры, поведение не меняем, но 30+ call-sites не тестируются automatically. Smoke-test после restart (Step 4 of Task 6) — must verify journalctl.
- **`messages.deal_brief_json` для старых row может быть NULL** — `_parse_deal_brief` handle.
- **`messages.ru_translation` для старых auto-ack или manual-compose row** может быть NULL — frontend handle через `?? ""`.
- **`db.get_message` SELECT возвращает не все колонки на legacy row** (pre-Phase-1 migrations) — endpoint защищён `"X" in row.keys()` гардами.
- **Cloudflared Quick Tunnel URL может поменяться между Phase 2 и Phase 3a** — Step 6 of Task 6 ловит и инструктирует обновить.
- **Telegram WebView ESM cache** — bump cache-bust на ВСЕ файлы, иначе старая версия thread.js кэшируется и pending-draft block не появится.
