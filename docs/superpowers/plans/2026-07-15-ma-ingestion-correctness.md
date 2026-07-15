# MA Ingestion & Manual-Flow Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five correctness bugs in the Kleinanzeigen bot's incoming-email / manual-flow path (Track B, Increment 1): reminders pinging autopilot threads, sold threads silently reopening, client cross-thread matching by namesake, relay-duplicate re-processing, and system emails bypassing the inquiry classifier.

**Architecture:** All five fixes are additive — new pure/DB-helper functions plus small wiring changes in `modules/incoming.py` and `web/api_ma.py`. **No schema changes** (no new tables/columns), so no DB migration and no `kleinanzeigen.db` backup are required for this plan. Each fix is independently testable at the DB/parser layer following the existing `tmp_db` test pattern.

**Tech Stack:** Python 3 (system python, no venv), SQLite (WAL), pytest. FastAPI for the Mini App API. Playwright/Anthropic are not exercised by these tests.

## Global Constraints

- **Do NOT change `send_mode`** or send any real email; these tasks touch no send path. (`AGENTS.md`)
- **No schema migration in this plan.** If a task seems to need a new column/table, stop — it belongs in Plan 2, not here.
- **Run `python3 -m pytest -q` and confirm green before every commit.** Baseline is 234–236 passing; each task adds tests and must keep the whole suite green.
- **Comments and log messages in Russian**, matching the surrounding code. (`CLAUDE.md`)
- **Work on branch `track-b-ma-negotiation`** (already created; the spec commit `df84fc7` lives there). Do not commit to `main`.
- **Commit message trailer** (every commit): `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Tests use a per-file `tmp_db` fixture that monkeypatches `database.DB_PATH` to a `tmp_path` file and calls `init_db()` (copy the pattern from `tests/test_mark_thread_sold.py`).
- Repo root on the server: `~/kleinanzeigen-bot`. All paths below are relative to it.

---

## File Structure

- `modules/db_threads.py` — **Modify.** Add `find_reminder_candidates` autopilot exclusion; add `thread_close_reason`, `should_reopen_closed_thread`, `has_recent_identical_incoming`, `_normalize_body`; change `find_related_inquiries` to match by email. Add `import re`.
- `modules/parser.py` — **Modify.** Add `SYSTEM_BODY_PATTERNS` + `is_system_message_body`.
- `modules/incoming.py` — **Modify.** Wire content-dedup, classifier tightening, sold-reopen guard.
- `web/api_ma.py` — **Modify.** Update both `find_related_inquiries` callers (lines 151, 339) to pass email.
- `database.py` — **Modify.** Add `thread_close_reason`, `should_reopen_closed_thread`, `has_recent_identical_incoming` to the `modules.db_threads` re-export block (lines 725–748).
- `tests/test_reminder_autopilot_exclusion.py` — **Create.**
- `tests/test_sold_reopen_guard.py` — **Create.**
- `tests/test_related_by_email.py` — **Create.**
- `tests/test_content_dedup.py` — **Create.**
- `tests/test_classifier_system_bypass.py` — **Create.**

---

## Task 1: Reminders exclude active-autopilot threads (Bug 10)

**Files:**
- Modify: `modules/db_threads.py` (`find_reminder_candidates`, ~lines 233–272)
- Test: `tests/test_reminder_autopilot_exclusion.py`

**Interfaces:**
- Consumes: existing `find_reminder_candidates(after_days: float) -> list[sqlite3.Row]` (signature unchanged).
- Produces: same signature; result now excludes any thread with a row in `thread_autopilot` where `active = 1`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reminder_autopilot_exclusion.py`:

```python
"""find_reminder_candidates must not offer a follow-up ping on a thread that
autopilot is actively driving (Bug 10)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _ensure_account(db):
    with db.get_conn() as conn:
        if conn.execute("SELECT id FROM accounts WHERE id=1").fetchone():
            return
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'test', 't@x', 'p', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )


def _insert_answered_incoming(db, thread_id):
    """One in-row that already has our sent reply, sent 2 days ago (last event=out)."""
    created = (datetime.utcnow() - timedelta(days=3)).isoformat()
    sent = (datetime.utcnow() - timedelta(days=2)).isoformat()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "de_client, de_answer, created_at, sent_at, reminder_state, is_reminder, "
            "is_auto_ack) VALUES (1, 'in', 'sent', ?, 'Hallo?', 'Antwort.', ?, ?, "
            "'none', 0, 0)",
            (thread_id, created, sent),
        )
        return cur.lastrowid


def _activate_autopilot(db, thread_id):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO thread_autopilot (gmail_thread_id, active, floor_price_eur, "
            "notify_mode, messages_sent, started_at) VALUES (?, 1, 1000, 'silent', 0, ?)",
            (thread_id, datetime.utcnow().isoformat()),
        )


def test_reminder_candidate_without_autopilot(tmp_db):
    _ensure_account(tmp_db)
    _insert_answered_incoming(tmp_db, "thread-R")
    ids = [r["gmail_thread_id"] for r in tmp_db.find_reminder_candidates(after_days=1)]
    assert "thread-R" in ids


def test_reminder_excludes_active_autopilot(tmp_db):
    _ensure_account(tmp_db)
    _insert_answered_incoming(tmp_db, "thread-R")
    _activate_autopilot(tmp_db, "thread-R")
    ids = [r["gmail_thread_id"] for r in tmp_db.find_reminder_candidates(after_days=1)]
    assert "thread-R" not in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_reminder_autopilot_exclusion.py -q`
Expected: `test_reminder_excludes_active_autopilot` FAILS (thread still returned); `test_reminder_candidate_without_autopilot` PASSES.

- [ ] **Step 3: Add the exclusion to the SQL**

In `modules/db_threads.py`, inside `find_reminder_candidates`, find the closing `closed_threads` guard and the `ORDER BY`:

```python
          -- Закрытые оператором треды не пингуем
          AND NOT EXISTS (
              SELECT 1 FROM closed_threads ct
               WHERE ct.gmail_thread_id = m.gmail_thread_id
          )
        ORDER BY le.at_time ASC
```

Replace it with (adds the autopilot exclusion before `ORDER BY`):

```python
          -- Закрытые оператором треды не пингуем
          AND NOT EXISTS (
              SELECT 1 FROM closed_threads ct
               WHERE ct.gmail_thread_id = m.gmail_thread_id
          )
          -- Треды под активным автопилотом не пингуем — автопилот сам ведёт диалог
          AND NOT EXISTS (
              SELECT 1 FROM thread_autopilot ta
               WHERE ta.gmail_thread_id = m.gmail_thread_id
                 AND ta.active = 1
          )
        ORDER BY le.at_time ASC
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_reminder_autopilot_exclusion.py -q`
Expected: both tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green (baseline + 2 new).

- [ ] **Step 6: Commit**

```bash
git add modules/db_threads.py tests/test_reminder_autopilot_exclusion.py
git commit -m "fix(reminders): exclude active-autopilot threads from follow-up pings (Bug 10)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Sold threads must not silently reopen (Bug 6)

**Files:**
- Modify: `modules/db_threads.py` (add helpers near the closed-threads section, ~after line 50)
- Modify: `database.py` (re-export block, lines 725–748)
- Modify: `modules/incoming.py` (reopen block, lines 485–491)
- Test: `tests/test_sold_reopen_guard.py`

**Interfaces:**
- Produces: `thread_close_reason(gmail_thread_id: str) -> Optional[str]` — returns `closed_by` or `None`.
- Produces: `should_reopen_closed_thread(gmail_thread_id: str) -> bool` — `False` when the thread was closed as a sale (`closed_by` in `{"sold", "detected-sale"}`) or is not closed; `True` otherwise.
- Consumes (incoming.py): the two above via `db.` re-export, plus existing `db.is_thread_closed`, `db.reopen_thread`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sold_reopen_guard.py`:

```python
"""A new email into a SOLD thread must not auto-reopen it (Bug 6)."""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def test_close_reason_none_when_open(tmp_db):
    assert tmp_db.thread_close_reason("nope") is None


def test_close_reason_returns_closed_by(tmp_db):
    tmp_db.close_thread("t-sold", closed_by="sold")
    assert tmp_db.thread_close_reason("t-sold") == "sold"


def test_should_not_reopen_sold(tmp_db):
    tmp_db.close_thread("t-sold", closed_by="sold")
    assert tmp_db.should_reopen_closed_thread("t-sold") is False


def test_should_not_reopen_detected_sale(tmp_db):
    tmp_db.close_thread("t-det", closed_by="detected-sale")
    assert tmp_db.should_reopen_closed_thread("t-det") is False


def test_should_reopen_operator_closed(tmp_db):
    tmp_db.close_thread("t-op", closed_by="@user#1")
    assert tmp_db.should_reopen_closed_thread("t-op") is True


def test_should_not_reopen_when_not_closed(tmp_db):
    assert tmp_db.should_reopen_closed_thread("t-open") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sold_reopen_guard.py -q`
Expected: FAIL with `AttributeError: module 'database' has no attribute 'thread_close_reason'`.

- [ ] **Step 3: Add the helpers to `modules/db_threads.py`**

Insert immediately after `is_thread_waiting` (after line 90), before the `# --- THREAD FLAGS ---` comment:

```python
# --- SOLD-REOPEN GUARD ---

# closed_by значения, означающие продажу: новое письмо в такой тред НЕ должно
# авто-реоткрывать его (не воскрешать pipeline/автопилот). См. Bug 6.
SALE_CLOSE_REASONS = {"sold", "detected-sale"}


def thread_close_reason(gmail_thread_id: str) -> Optional[str]:
    """Причина закрытия треда (closed_by) или None, если тред не закрыт."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT closed_by FROM closed_threads WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()
    return row["closed_by"] if row else None


def should_reopen_closed_thread(gmail_thread_id: str) -> bool:
    """Можно ли авто-реоткрывать закрытый тред при новом incoming.

    False, если тред закрыт как продажа (sold / detected-sale) — такое письмо
    уйдёт оператору на ревью, но тред остаётся закрытым и автопилот не воскресает.
    False также если тред вообще не закрыт (реоткрывать нечего).
    """
    reason = thread_close_reason(gmail_thread_id)
    if reason is None:
        return False
    return reason not in SALE_CLOSE_REASONS
```

- [ ] **Step 4: Re-export the helpers in `database.py`**

In the `from modules.db_threads import (` block (lines 725–748), add three names. Change:

```python
    is_thread_waiting,
    mark_processed,
```

to:

```python
    is_thread_waiting,
    thread_close_reason,
    should_reopen_closed_thread,
    mark_processed,
```

- [ ] **Step 5: Run the helper test to verify it passes**

Run: `python3 -m pytest tests/test_sold_reopen_guard.py -q`
Expected: all 6 PASS.

- [ ] **Step 6: Wire the guard into `modules/incoming.py`**

Replace the reopen block (lines 485–491):

```python
    inbound_thread = email.get("gmail_thread_id") or ""
    if inbound_thread and db.is_thread_closed(inbound_thread):
        db.reopen_thread(inbound_thread)
        logger.info(
            "Reopened closed thread %s — клиент написал в архивный тред",
            inbound_thread,
        )
```

with:

```python
    inbound_thread = email.get("gmail_thread_id") or ""
    if inbound_thread and db.is_thread_closed(inbound_thread):
        if db.should_reopen_closed_thread(inbound_thread):
            db.reopen_thread(inbound_thread)
            logger.info(
                "Reopened closed thread %s — клиент написал в архивный тред",
                inbound_thread,
            )
        else:
            # Тред закрыт как продажа — не воскрешаем pipeline/автопилот.
            # Письмо всё равно уйдёт оператору через send_for_review ниже.
            logger.info(
                "Sold thread %s получил новое письмо — оставляем закрытым, "
                "уйдёт оператору на ревью, автопилот не воскрешаем",
                inbound_thread,
            )
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add modules/db_threads.py database.py modules/incoming.py tests/test_sold_reopen_guard.py
git commit -m "fix(threads): don't auto-reopen sold threads on new incoming (Bug 6)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Related-inquiries match by email, not namesake (Bug 8)

**Files:**
- Modify: `modules/db_threads.py` (`find_related_inquiries`, lines 375–412)
- Modify: `web/api_ma.py` (callers at lines 151–153 and 339–341)
- Test: `tests/test_related_by_email.py`

**Interfaces:**
- Produces (changed signature): `find_related_inquiries(buyer_email: Optional[str], exclude_thread_id: Optional[str] = None, limit: int = 10) -> list[sqlite3.Row]` — matches other threads where `messages.buyer_name = buyer_email` (the sender email), returning one last-incoming row per thread.
- Consumes (api_ma.py): passes the thread's `buyer_name` (email) instead of `buyer_display_name`.
- Note: test files that mock `db.find_related_inquiries` (e.g. `tests/test_api_ma_threads.py`) set `.return_value` and are unaffected by the signature change.

- [ ] **Step 1: Write the failing test**

Create `tests/test_related_by_email.py`:

```python
"""find_related_inquiries must group by email, not display_name (Bug 8):
two different buyers who happen to share a first name must NOT be merged."""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _ensure_account(db):
    with db.get_conn() as conn:
        if conn.execute("SELECT id FROM accounts WHERE id=1").fetchone():
            return
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'test', 't@x', 'p', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )


def _in(db, thread_id, email, display):
    now = datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "buyer_name, buyer_display_name, de_client, created_at) "
            "VALUES (1, 'in', 'sent', ?, ?, ?, 'x', ?)",
            (thread_id, email, display, now),
        )


def test_same_email_is_related(tmp_db):
    _ensure_account(tmp_db)
    _in(tmp_db, "t1", "hans@relay.de", "Hans")
    _in(tmp_db, "t2", "hans@relay.de", "Hans")
    rows = tmp_db.find_related_inquiries("hans@relay.de", exclude_thread_id="t1")
    assert [r["gmail_thread_id"] for r in rows] == ["t2"]


def test_same_name_different_email_not_related(tmp_db):
    _ensure_account(tmp_db)
    _in(tmp_db, "t1", "hans1@relay.de", "Hans")
    _in(tmp_db, "t2", "hans2@relay.de", "Hans")  # namesake, different person
    rows = tmp_db.find_related_inquiries("hans1@relay.de", exclude_thread_id="t1")
    assert rows == []


def test_empty_email_returns_empty(tmp_db):
    assert tmp_db.find_related_inquiries("") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_related_by_email.py -q`
Expected: `test_same_name_different_email_not_related` FAILS (old code matches by display_name → returns t2).

- [ ] **Step 3: Rewrite `find_related_inquiries` to match by email**

Replace the whole function (lines 375–412) with:

```python
def find_related_inquiries(
    buyer_email: Optional[str],
    exclude_thread_id: Optional[str] = None,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Найти ДРУГИЕ треды того же клиента — по email (buyer_name).

    Раньше матчили по buyer_display_name, из-за чего однофамильцы
    («Hans», «Peter») склеивались в одного человека (Bug 8). Email
    relay-адреса — надёжный идентификатор конкретного покупателя.

    Возвращает по одной row на gmail_thread_id (последний incoming в треде),
    отсортировано DESC по времени последнего события.
    """
    if not buyer_email or not buyer_email.strip():
        return []
    sql = """
    WITH last_in AS (
        SELECT m.* FROM messages m
        WHERE m.direction = 'in'
          AND m.buyer_name = ?
          AND m.gmail_thread_id IS NOT NULL AND m.gmail_thread_id != ''
          AND m.id = (
              SELECT MAX(m2.id) FROM messages m2
              WHERE m2.gmail_thread_id = m.gmail_thread_id
                AND m2.direction = 'in'
          )
    )
    SELECT * FROM last_in
    WHERE 1=1
    """
    params: list[Any] = [buyer_email.strip()]
    if exclude_thread_id:
        sql += " AND gmail_thread_id != ?"
        params.append(exclude_thread_id)
    sql += " ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_related_by_email.py -q`
Expected: all 3 PASS.

- [ ] **Step 5: Update caller in `_thread_dict` (`web/api_ma.py` lines 151–153)**

Replace:

```python
    related_matches = db.find_related_inquiries(
        last_in["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if last_in["buyer_display_name"] else []
```

with (pass email; keep display_name in the returned dict for the UI label):

```python
    _buyer_email = last_in["buyer_name"] if "buyer_name" in last_in.keys() else None
    related_matches = db.find_related_inquiries(
        _buyer_email, exclude_thread_id=thread_id, limit=10,
    ) if _buyer_email else []
```

- [ ] **Step 6: Update caller in `_message_review_dict` (`web/api_ma.py` lines 339–341)**

Replace:

```python
    related_matches = db.find_related_inquiries(
        row["buyer_display_name"], exclude_thread_id=thread_id, limit=10,
    ) if row["buyer_display_name"] else []
```

with:

```python
    _buyer_email = row["buyer_name"] if "buyer_name" in row.keys() else None
    related_matches = db.find_related_inquiries(
        _buyer_email, exclude_thread_id=thread_id, limit=10,
    ) if _buyer_email else []
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green (the `test_api_ma_*` mocks are unaffected).

- [ ] **Step 8: Commit**

```bash
git add modules/db_threads.py web/api_ma.py tests/test_related_by_email.py
git commit -m "fix(clients): relate inquiries by email, not namesake display_name (Bug 8)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Content-level dedup for relay re-sends (Bug 9)

**Files:**
- Modify: `modules/db_threads.py` (add `import re` at top; add helpers)
- Modify: `database.py` (re-export block)
- Modify: `modules/incoming.py` (insert check after the purchase-side skip, before the classifier block)
- Test: `tests/test_content_dedup.py`

**Interfaces:**
- Produces: `has_recent_identical_incoming(gmail_thread_id: str, buyer_email: str, de_client: str, within_hours: float = 12.0) -> bool` — True if an in-row with the same normalized body from the same sender exists in the same thread within the window.
- Produces (internal): `_normalize_body(text: Optional[str]) -> str` — collapse whitespace, strip, lowercase.

- [ ] **Step 1: Write the failing test**

Create `tests/test_content_dedup.py`:

```python
"""Message-ID dedup misses Kleinanzeigen relay re-sends (new Message-ID, same
body). Content-level dedup catches them (Bug 9)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _in(db, thread_id, email, body, created=None):
    created = created or datetime.utcnow().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (account_id, direction, status, gmail_thread_id, "
            "buyer_name, de_client, created_at) VALUES (1, 'in', 'new', ?, ?, ?, ?)",
            (thread_id, email, body, created),
        )


def test_detects_identical_body_same_thread(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo, ist das noch verfügbar?")
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Hallo,   ist das noch  verfügbar?\n"
    ) is True


def test_different_body_not_duplicate(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo, ist das noch verfügbar?")
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Was ist der letzte Preis?"
    ) is False


def test_other_thread_not_duplicate(tmp_db):
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo?")
    assert tmp_db.has_recent_identical_incoming("t2", "buyer@relay.de", "Hallo?") is False


def test_outside_window_not_duplicate(tmp_db):
    old = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    _in(tmp_db, "t1", "buyer@relay.de", "Hallo?", created=old)
    assert tmp_db.has_recent_identical_incoming(
        "t1", "buyer@relay.de", "Hallo?", within_hours=12
    ) is False


def test_empty_inputs_false(tmp_db):
    assert tmp_db.has_recent_identical_incoming("", "x@y", "body") is False
    assert tmp_db.has_recent_identical_incoming("t1", "", "body") is False
    assert tmp_db.has_recent_identical_incoming("t1", "x@y", "") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_content_dedup.py -q`
Expected: FAIL with `AttributeError: module 'database' has no attribute 'has_recent_identical_incoming'`.

- [ ] **Step 3: Add `import re` and the helpers to `modules/db_threads.py`**

At the top of `modules/db_threads.py`, change:

```python
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
```

to:

```python
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional
```

Then add, immediately after `find_by_gmail_message_id` (after line 215):

```python
def _normalize_body(text: Optional[str]) -> str:
    """Нормализовать тело письма для сравнения: collapse whitespace, strip, lower."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def has_recent_identical_incoming(
    gmail_thread_id: str,
    buyer_email: str,
    de_client: str,
    within_hours: float = 12.0,
) -> bool:
    """Есть ли уже недавнее incoming с идентичным телом в этом треде от этого
    отправителя — защита от relay-повторов Kleinanzeigen с новым Message-ID (Bug 9).

    Сравнение по нормализованному тексту (collapse whitespace / lower). Окно
    within_hours ограничивает совпадение свежими повторами, чтобы легитимные
    одинаковые короткие сообщения («Danke») спустя дни не глотались.
    """
    if not (gmail_thread_id and buyer_email and de_client):
        return False
    norm = _normalize_body(de_client)
    if not norm:
        return False
    cutoff = (datetime.utcnow() - timedelta(hours=float(within_hours))).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT de_client FROM messages "
            "WHERE gmail_thread_id = ? AND buyer_name = ? AND direction = 'in' "
            "AND created_at > ? AND de_client IS NOT NULL",
            (gmail_thread_id, buyer_email, cutoff),
        ).fetchall()
    return any(_normalize_body(r["de_client"]) == norm for r in rows)
```

- [ ] **Step 4: Re-export in `database.py`**

In the `from modules.db_threads import (` block, add `has_recent_identical_incoming` after `find_by_gmail_message_id,`:

```python
    find_by_gmail_message_id,
    has_recent_identical_incoming,
    find_reminder_candidates,
```

- [ ] **Step 5: Run the helper test to verify it passes**

Run: `python3 -m pytest tests/test_content_dedup.py -q`
Expected: all 5 PASS.

- [ ] **Step 6: Wire into `modules/incoming.py`**

In `_process_incoming`, find the end of the purchase-side skip block (line 317) and the start of the classifier comment (line 319: `# AI-классификатор (Haiku): финальный gate...`). Insert this block **between** them:

```python
    # Контент-дедуп: тот же текст в том же треде от того же отправителя за окно —
    # это relay-повтор Kleinanzeigen с новым Message-ID (Message-ID-дедуп его не ловит, Bug 9).
    inbound_thread_dedup = (email.get("gmail_thread_id") or "").strip()
    from_email_dedup = (email.get("from_email") or "").strip()
    if (not force and inbound_thread_dedup and from_email_dedup
            and db.has_recent_identical_incoming(inbound_thread_dedup, from_email_dedup, body)):
        logger.info(
            "Skip: контент-дубликат incoming в треде %s от %s (relay-повтор)",
            inbound_thread_dedup, from_email_dedup,
        )
        _skip_email(account, email, "skipped_dedup")
        return
```

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add modules/db_threads.py database.py modules/incoming.py tests/test_content_dedup.py
git commit -m "fix(ingest): content-level dedup for relay re-sends (Bug 9)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: System emails must not bypass the classifier (Bug 7)

**Files:**
- Modify: `modules/parser.py` (add `SYSTEM_BODY_PATTERNS` + `is_system_message_body`, after `is_real_inquiry_subject`, ~line 140)
- Modify: `modules/incoming.py` (classifier-bypass block, lines 335–340)
- Test: `tests/test_classifier_system_bypass.py`

**Interfaces:**
- Produces: `is_system_message_body(body: str) -> bool` — True if the body carries automated/system-email markers.
- Consumes (incoming.py): `parser.is_system_message_body(body)` gates the follow-up classifier bypass.

- [ ] **Step 1: Write the failing test**

Create `tests/test_classifier_system_bypass.py`:

```python
"""A KZ system email landing in an already-active thread must still be gated by
the inquiry classifier — is_system_message_body detects it (Bug 7)."""
from __future__ import annotations

from modules import parser


def test_detects_automatic_email():
    assert parser.is_system_message_body(
        "Diese E-Mail wurde automatisch generiert. Bitte antworten Sie nicht darauf."
    ) is True


def test_detects_no_reply_note():
    assert parser.is_system_message_body(
        "Bitte antworten Sie nicht auf diese E-Mail."
    ) is True


def test_normal_buyer_message_is_not_system():
    assert parser.is_system_message_body(
        "Hallo, ist der Sitz noch verfügbar? Wäre 200 Euro möglich?"
    ) is False


def test_empty_is_not_system():
    assert parser.is_system_message_body("") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_classifier_system_bypass.py -q`
Expected: FAIL with `AttributeError: module 'modules.parser' has no attribute 'is_system_message_body'`.

- [ ] **Step 3: Add the detector to `modules/parser.py`**

Insert after `is_real_inquiry_subject` (after line 139):

```python
# Маркеры автоматических/системных писем Kleinanzeigen в ТЕЛЕ (не в subject).
# Используются чтобы системка, залетевшая в активный тред, не прошла через
# classifier-bypass follow-up'ов (Bug 7).
SYSTEM_BODY_PATTERNS: list[re.Pattern] = [
    re.compile(r'automatisch\s+(?:generierte?|erstellte?|versendete?|erzeugte?)\s+(?:E-?Mail|Nachricht)', re.IGNORECASE),
    re.compile(r'automatisch\s+generiert', re.IGNORECASE),
    re.compile(r'Diese\s+(?:E-?Mail|Nachricht)\s+wurde\s+automatisch', re.IGNORECASE),
    re.compile(r'Bitte\s+(?:antworten\s+Sie\s+)?nicht\s+(?:direkt\s+)?auf\s+diese\s+E-?Mail', re.IGNORECASE),
    re.compile(r'Antworten\s+Sie\s+nicht\s+auf\s+diese', re.IGNORECASE),
    re.compile(r'\bno-?reply\b', re.IGNORECASE),
]


def is_system_message_body(body: str) -> bool:
    """Похоже ли тело письма на автоматическое/системное сообщение Kleinanzeigen.

    Дешёвая эвристика по маркерам («automatisch generierte E-Mail», «bitte nicht
    auf diese E-Mail antworten»). Применяется в classifier-bypass follow-up'ов,
    чтобы системка не прошла в Claude/оператора без проверки Haiku.
    """
    if not body:
        return False
    return any(p.search(body) for p in SYSTEM_BODY_PATTERNS)
```

- [ ] **Step 4: Run parser test to verify it passes**

Run: `python3 -m pytest tests/test_classifier_system_bypass.py -q`
Expected: all 4 PASS.

- [ ] **Step 5: Gate the bypass in `modules/incoming.py`**

Replace the bypass assignment (lines 335–340):

```python
        if prior:
            skip_classifier = True
            logger.info(
                "Classifier bypass: thread %s уже имеет inquiry, follow-up принят без проверки",
                inbound_thread_id,
            )
```

with:

```python
        if prior and not parser.is_system_message_body(body):
            skip_classifier = True
            logger.info(
                "Classifier bypass: thread %s уже имеет inquiry, follow-up принят без проверки",
                inbound_thread_id,
            )
        elif prior:
            logger.info(
                "Classifier NOT bypassed для thread %s — тело похоже на системное "
                "письмо, прогоняем Haiku несмотря на follow-up",
                inbound_thread_id,
            )
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add modules/parser.py modules/incoming.py tests/test_classifier_system_bypass.py
git commit -m "fix(ingest): system emails don't bypass inquiry classifier in active threads (Bug 7)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Run the complete suite one more time**

Run: `python3 -m pytest -q`
Expected: all green, including the 5 new test files (~20 new tests) on top of the 234–236 baseline.

- [ ] **Restart the service and confirm a clean start (per AGENTS.md)**

Run: `sudo systemctl restart kleinanzeigen-bot && sleep 5 && systemctl is-active kleinanzeigen-bot && journalctl -u kleinanzeigen-bot -n 30 --no-pager`
Expected: `active`, no traceback on startup.

---

## Self-review notes (author)

- **Spec coverage:** Bugs 6, 7, 8, 9, 10 from the spec's §3.5 each map to Tasks 2, 5, 3, 4, 1 respectively. Guardrails G1–G6, `negotiation_state`, and autopilot rework are explicitly out of scope (Plans 2 & 3).
- **No schema changes:** confirmed — only new functions and SQL predicates; no `CREATE TABLE` / `ADD COLUMN`. No migration/backup needed.
- **Type/name consistency:** `thread_close_reason`, `should_reopen_closed_thread`, `has_recent_identical_incoming`, `_normalize_body`, `is_system_message_body`, `SALE_CLOSE_REASONS`, `SYSTEM_BODY_PATTERNS` are defined once and referenced consistently; the three DB helpers are re-exported in `database.py` and called as `db.<name>`.
- **Caller coverage:** both `find_related_inquiries` callers (`api_ma.py:151`, `:339`) are updated; `find_reminder_candidates` (scheduler.py:167) and `reopen_thread` (incoming.py:487) signatures are unchanged or handled. Test mocks of `find_related_inquiries` set `return_value` and are signature-agnostic.
- **Placeholder scan:** none — every step shows exact code/commands.
