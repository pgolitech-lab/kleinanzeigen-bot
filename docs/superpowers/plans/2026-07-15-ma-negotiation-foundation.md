# MA Negotiation Foundation (Increment 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the additive foundation for the "code owns the numbers" negotiation redesign (Track B, Increment 2): a `negotiation_state` table, autopilot config keys, and a pure deterministic-guardrails module — WITHOUT touching any live send/reply path and WITHOUT changing any runtime behavior (autopilot is off).

**Architecture:** Three self-contained additive units: (1) a new SQLite table + DB helpers, (2) two new config keys with getters/whitelist/validators, (3) a new pure-function module `modules/guardrails.py`. Nothing is wired into the incoming/outgoing/autopilot execution paths in this increment — that wiring is Increment 3. Everything here is unit/DB-testable in isolation.

**Tech Stack:** Python 3 (system python, no venv), SQLite (WAL), pytest, FastAPI (for the settings whitelist/validators in `web/api_ma.py`).

## Global Constraints

- **Do NOT change `send_mode`, do NOT enable autopilot, do NOT modify any send/reply execution path** (`modules/outgoing.py`, `modules/incoming.py` dispatch, `modules/claude.py` generation). This increment only adds a table, config keys, and a standalone module.
- **Schema change is additive only:** one new `CREATE TABLE IF NOT EXISTS negotiation_state`. No `ALTER`/`DROP`/`DELETE` on existing tables. A production DB backup already exists (`backups/kleinanzeigen-20260715-234236.db`).
- **Run `python3 -m pytest -q` and confirm green before every commit.** Baseline is 256 passing; each task adds tests and must keep the whole suite green.
- **Comments and docstrings in Russian**, matching surrounding code.
- **Work on branch `track-b-increment-2`** (created from `main` — see setup). Do not commit to `main`.
- **Commit message trailer** (every commit): `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Tests use a per-file `tmp_db` fixture that monkeypatches `database.DB_PATH` to a `tmp_path` file and calls `init_db()` (copy the pattern from `tests/test_mark_thread_sold.py`).
- Repo root on the server: `~/kleinanzeigen-bot`.

---

## File Structure

- `database.py` — **Modify.** Add the `negotiation_state` CREATE TABLE inside `init_db()` (after the `thread_autopilot` block, ~line 199). Add the new `db_threads` helper names to the re-export block (~lines 725–748).
- `modules/db_threads.py` — **Modify.** Add `get_negotiation_state`, `upsert_negotiation_state`, `record_our_offer` (+ `NEGOTIATION_PHASES`/`NEGOTIATION_MODES` constants).
- `config.py` — **Modify.** Add `autopilot_message_cap` and `autopilot_shadow_mode` to `DEFAULTS` and add two typed getters.
- `web/api_ma.py` — **Modify.** Add both keys to `ALLOWED_SETTING_KEYS` and both validators to `VALIDATORS`.
- `modules/guardrails.py` — **Create.** Pure deterministic guardrail functions.
- `tests/test_negotiation_state.py` — **Create.**
- `tests/test_autopilot_config.py` — **Create.**
- `tests/test_guardrails.py` — **Create.**

---

## Setup (do once, before Task 1)

Create the branch from `main` on the server:

```bash
ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && git checkout main && git checkout -b track-b-increment-2 && git rev-parse --abbrev-ref HEAD'
```
Expected: `track-b-increment-2`.

---

## Task 1: `negotiation_state` table + DB helpers

**Files:**
- Modify: `database.py` (`init_db()` after the `thread_autopilot` block ~line 199; re-export block ~lines 725–748)
- Modify: `modules/db_threads.py` (add helpers + constants)
- Test: `tests/test_negotiation_state.py`

**Interfaces:**
- Produces: `get_negotiation_state(gmail_thread_id: str) -> Optional[sqlite3.Row]` — the state row or None.
- Produces: `upsert_negotiation_state(gmail_thread_id: str, **fields) -> None` — insert-or-update the given columns (only known columns accepted) + `updated_at`.
- Produces: `record_our_offer(gmail_thread_id: str, offer_eur: float) -> None` — set `our_last_offer_eur` (the reliable ratchet anchor, written by code) + `updated_at`.
- Produces (constants): `NEGOTIATION_PHASES = {"opening","negotiating","closing","escalated","stopped"}`, `NEGOTIATION_MODES = {"manual","auto"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_negotiation_state.py`:

```python
"""negotiation_state table + helpers (Track B Increment 2 foundation)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def test_get_missing_returns_none(tmp_db):
    assert tmp_db.get_negotiation_state("nope") is None


def test_upsert_creates_then_updates(tmp_db):
    tmp_db.upsert_negotiation_state("t1", floor_eur=1000.0, list_price_eur=1500.0)
    row = tmp_db.get_negotiation_state("t1")
    assert row is not None
    assert row["floor_eur"] == 1000.0
    assert row["list_price_eur"] == 1500.0
    assert row["phase"] == "opening"       # default
    assert row["mode"] == "manual"         # default
    assert row["updated_at"]               # set

    tmp_db.upsert_negotiation_state("t1", phase="negotiating")
    row2 = tmp_db.get_negotiation_state("t1")
    assert row2["phase"] == "negotiating"
    assert row2["floor_eur"] == 1000.0     # unchanged by partial upsert


def test_upsert_ignores_unknown_fields(tmp_db):
    # Unknown columns must not raise / must be silently ignored.
    tmp_db.upsert_negotiation_state("t2", floor_eur=500.0, bogus_col=1)
    assert tmp_db.get_negotiation_state("t2")["floor_eur"] == 500.0


def test_record_our_offer(tmp_db):
    tmp_db.record_our_offer("t3", 1420.0)
    assert tmp_db.get_negotiation_state("t3")["our_last_offer_eur"] == 1420.0
    tmp_db.record_our_offer("t3", 1400.0)
    assert tmp_db.get_negotiation_state("t3")["our_last_offer_eur"] == 1400.0


def test_empty_thread_id_is_noop(tmp_db):
    tmp_db.upsert_negotiation_state("", floor_eur=1.0)
    tmp_db.record_our_offer("", 1.0)
    assert tmp_db.get_negotiation_state("") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_negotiation_state.py -q'`
Expected: FAIL with `AttributeError: module 'database' has no attribute 'get_negotiation_state'`.

- [ ] **Step 3: Add the table to `database.py`**

In `init_db()`, immediately after the `thread_autopilot` CREATE TABLE block (which ends `)` around line 199), add:

```python
        # negotiation_state — структурное состояние сделки per-thread (Track B).
        # «Код владеет числами»: our_last_offer_eur пишется ТОЛЬКО кодом при
        # реальной отправке оферты — надёжный якорь ratchet вместо чтения из
        # неоднозначного deal_brief.negotiated_price_eur. Заполняется/используется
        # в Инкременте 3; здесь только схема + базовые CRUD-хелперы.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS negotiation_state (
                gmail_thread_id TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'opening',
                mode TEXT NOT NULL DEFAULT 'manual',
                list_price_eur REAL,
                floor_eur REAL,
                our_last_offer_eur REAL,
                buyer_last_offer_eur REAL,
                escalation_reason TEXT,
                updated_at TEXT NOT NULL
            )
        """)
```

- [ ] **Step 4: Add the helpers to `modules/db_threads.py`**

Insert after the `should_reopen_closed_thread` function (added in Increment 1, in the `# --- SOLD-REOPEN GUARD ---` section), before `# --- THREAD FLAGS ---`:

```python
# --- NEGOTIATION STATE (Track B) ---

NEGOTIATION_PHASES = {"opening", "negotiating", "closing", "escalated", "stopped"}
NEGOTIATION_MODES = {"manual", "auto"}

# Колонки negotiation_state, которые разрешено писать через upsert (кроме PK/updated_at).
_NEG_STATE_COLS = {
    "phase", "mode", "list_price_eur", "floor_eur",
    "our_last_offer_eur", "buyer_last_offer_eur", "escalation_reason",
}


def get_negotiation_state(gmail_thread_id: str) -> Optional[sqlite3.Row]:
    """Состояние сделки треда или None, если ещё не создано."""
    if not gmail_thread_id:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM negotiation_state WHERE gmail_thread_id = ?",
            (gmail_thread_id,),
        ).fetchone()


def upsert_negotiation_state(gmail_thread_id: str, **fields: Any) -> None:
    """Создать/обновить состояние сделки. Пишутся только известные колонки
    (`_NEG_STATE_COLS`); неизвестные ключи молча игнорируются. Всегда обновляет
    updated_at. Пустой thread_id — no-op."""
    if not gmail_thread_id:
        return
    known = {k: v for k, v in fields.items() if k in _NEG_STATE_COLS}
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO negotiation_state (gmail_thread_id, updated_at) "
            "VALUES (?, ?)",
            (gmail_thread_id, now),
        )
        for col, val in known.items():
            conn.execute(
                f"UPDATE negotiation_state SET {col} = ?, updated_at = ? "
                "WHERE gmail_thread_id = ?",
                (val, now, gmail_thread_id),
            )
        if not known:
            conn.execute(
                "UPDATE negotiation_state SET updated_at = ? WHERE gmail_thread_id = ?",
                (now, gmail_thread_id),
            )


def record_our_offer(gmail_thread_id: str, offer_eur: float) -> None:
    """Зафиксировать последнюю цену, которую озвучили МЫ (якорь ratchet).
    Пишется кодом при реальной отправке оферты."""
    if not gmail_thread_id:
        return
    upsert_negotiation_state(gmail_thread_id, our_last_offer_eur=float(offer_eur))
```

(Note: `_NEG_STATE_COLS` are interpolated into SQL only from this fixed literal set — never from caller input — so there is no injection surface.)

- [ ] **Step 5: Re-export in `database.py`**

In the `from modules.db_threads import (` block (~lines 725–748), add the three names after `should_reopen_closed_thread,`:

```python
    should_reopen_closed_thread,
    get_negotiation_state,
    upsert_negotiation_state,
    record_our_offer,
    mark_processed,
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_negotiation_state.py -q'`
Expected: all 5 PASS.

- [ ] **Step 7: Run the full suite**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest -q 2>&1 | tail -5'`
Expected: all green (256 + 5).

- [ ] **Step 8: Commit**

```bash
ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && git add database.py modules/db_threads.py tests/test_negotiation_state.py && git commit -m "feat(negotiation): add negotiation_state table + CRUD helpers (Track B Inc2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"'
```

---

## Task 2: Autopilot config keys (`autopilot_message_cap`, `autopilot_shadow_mode`)

**Files:**
- Modify: `config.py` (`DEFAULTS` dict; add two getters near `max_discount_percent`/`reminders_enabled`)
- Modify: `web/api_ma.py` (`ALLOWED_SETTING_KEYS` ~1191–1206; `VALIDATORS` ~1213–1220)
- Test: `tests/test_autopilot_config.py`

**Interfaces:**
- Produces: `config.autopilot_message_cap() -> int` (default 20).
- Produces: `config.autopilot_shadow_mode() -> bool` (default True).
- Both keys are settable via `POST /api/ma/settings` (whitelisted + validated).

- [ ] **Step 1: Write the failing test**

Create `tests/test_autopilot_config.py`:

```python
"""Autopilot config keys: message cap + shadow mode (Track B Increment 2)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def test_message_cap_default(tmp_db):
    import config
    assert config.autopilot_message_cap() == 20


def test_message_cap_override(tmp_db):
    import config
    tmp_db.set_setting("autopilot_message_cap", "5")
    assert config.autopilot_message_cap() == 5


def test_shadow_mode_default_true(tmp_db):
    import config
    assert config.autopilot_shadow_mode() is True


def test_shadow_mode_off(tmp_db):
    import config
    tmp_db.set_setting("autopilot_shadow_mode", "0")
    assert config.autopilot_shadow_mode() is False


def test_keys_whitelisted_and_validated():
    from web import api_ma
    assert "autopilot_message_cap" in api_ma.ALLOWED_SETTING_KEYS
    assert "autopilot_shadow_mode" in api_ma.ALLOWED_SETTING_KEYS
    assert api_ma.VALIDATORS["autopilot_message_cap"]("20") is True
    assert api_ma.VALIDATORS["autopilot_message_cap"]("1001") is False
    assert api_ma.VALIDATORS["autopilot_message_cap"]("abc") is False
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("1") is True
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("0") is True
    assert api_ma.VALIDATORS["autopilot_shadow_mode"]("2") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_autopilot_config.py -q'`
Expected: FAIL with `AttributeError: module 'config' has no attribute 'autopilot_message_cap'`.

- [ ] **Step 3: Add the keys to `config.py` DEFAULTS**

In the `DEFAULTS` dict (lines 22–92), add two entries near `"max_discount_percent": "10",`:

```python
    "autopilot_message_cap": "20",
    "autopilot_shadow_mode": "1",
```

- [ ] **Step 4: Add the getters to `config.py`**

Near the `max_discount_percent` getter (~line 204), add:

```python
def autopilot_message_cap() -> int:
    """Максимум авто-отправок автопилота на тред (персистентный cap)."""
    return get_int("autopilot_message_cap", 20)


def autopilot_shadow_mode() -> bool:
    """Shadow-режим автопилота: генерировать и логировать, но НЕ отправлять.
    По умолчанию включён (безопасно) — снимается явно при раскатке в Инкременте 3."""
    return (get("autopilot_shadow_mode") or "1").strip() == "1"
```

- [ ] **Step 5: Whitelist + validate in `web/api_ma.py`**

In `ALLOWED_SETTING_KEYS` (the set literal ~lines 1191–1206), add both key strings:

```python
    "autopilot_message_cap",
    "autopilot_shadow_mode",
```

In `VALIDATORS` (the dict ~lines 1213–1220), add:

```python
    "autopilot_message_cap": lambda v: v.isdigit() and 0 <= int(v) <= 1000,
    "autopilot_shadow_mode": lambda v: v in {"0", "1"},
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_autopilot_config.py -q'`
Expected: all 5 PASS.

- [ ] **Step 7: Run the full suite**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest -q 2>&1 | tail -5'`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && git add config.py web/api_ma.py tests/test_autopilot_config.py && git commit -m "feat(config): autopilot_message_cap + autopilot_shadow_mode keys (Track B Inc2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"'
```

---

## Task 3: Pure deterministic guardrails module

**Files:**
- Create: `modules/guardrails.py`
- Test: `tests/test_guardrails.py`

**Interfaces:**
- Produces: `reconcile_floor(ad_min_eur: Optional[float], operator_floor_eur: Optional[float]) -> Optional[float]` — the single effective floor = max of the two positive values (operator may only tighten); None if neither is a positive number.
- Produces: `floor_violation(proposed_eur: Optional[float], floor_eur: Optional[float]) -> bool` — True iff proposed < floor (None on either side → False).
- Produces: `outgoing_language_ok(text: str, client_lang: Optional[str]) -> bool` — alphabet sanity: Cyrillic allowed only when `client_lang=="ru"`; ru requires Cyrillic; empty text → False.
- Produces: `extract_json_object(text_blocks: list[str]) -> dict` — robustly pull the JSON object from model text blocks (scan from last to first; balanced-brace extraction; tolerates surrounding narrative); raises `RuntimeError` if none.

- [ ] **Step 1: Write the failing test**

Create `tests/test_guardrails.py`:

```python
"""Pure deterministic guardrails (Track B Increment 2). No side effects."""
from __future__ import annotations

import pytest

from modules import guardrails as g


# --- reconcile_floor ---
def test_reconcile_floor_takes_max():
    assert g.reconcile_floor(1000.0, 1200.0) == 1200.0
    assert g.reconcile_floor(1400.0, 1200.0) == 1400.0

def test_reconcile_floor_handles_none_and_zero():
    assert g.reconcile_floor(None, 900.0) == 900.0
    assert g.reconcile_floor(900.0, None) == 900.0
    assert g.reconcile_floor(None, None) is None
    assert g.reconcile_floor(0, 0) is None


# --- floor_violation ---
def test_floor_violation():
    assert g.floor_violation(999.0, 1000.0) is True
    assert g.floor_violation(1000.0, 1000.0) is False
    assert g.floor_violation(1500.0, 1000.0) is False
    assert g.floor_violation(None, 1000.0) is False
    assert g.floor_violation(999.0, None) is False


# --- outgoing_language_ok ---
def test_language_ru_requires_cyrillic():
    assert g.outgoing_language_ok("Здравствуйте, цена окончательная.", "ru") is True
    assert g.outgoing_language_ok("Hello there", "ru") is False

def test_language_non_ru_rejects_cyrillic():
    # The catastrophic bug: Russian draft leaked to a German buyer.
    assert g.outgoing_language_ok("Здравствуйте", "de") is False
    assert g.outgoing_language_ok("Hallo, der Preis ist fest.", "de") is True
    assert g.outgoing_language_ok("Bonjour, c'est possible.", "fr") is True

def test_language_empty_is_not_ok():
    assert g.outgoing_language_ok("", "de") is False
    assert g.outgoing_language_ok("   ", "ru") is False


# --- extract_json_object ---
def test_extract_plain_json():
    assert g.extract_json_object(['{"a": 1, "b": "x"}']) == {"a": 1, "b": "x"}

def test_extract_json_with_trailing_narrative():
    block = 'Here is the result: {"a": 1} — hope that helps!'
    assert g.extract_json_object([block]) == {"a": 1}

def test_extract_prefers_a_valid_json_block_from_the_end():
    blocks = ['searching the web...', '{"final": true, "price": 1500}']
    assert g.extract_json_object(blocks) == {"final": True, "price": 1500}

def test_extract_skips_nonjson_last_block():
    blocks = ['{"answer": "ok"}', 'no json here']
    assert g.extract_json_object(blocks) == {"answer": "ok"}

def test_extract_nested_braces_and_strings():
    block = '{"msg": "he said {hi}", "d": {"x": 1}}'
    assert g.extract_json_object([block]) == {"msg": "he said {hi}", "d": {"x": 1}}

def test_extract_raises_when_no_json():
    with pytest.raises(RuntimeError):
        g.extract_json_object(['nothing', 'still nothing'])
    with pytest.raises(RuntimeError):
        g.extract_json_object([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_guardrails.py -q'`
Expected: FAIL with `ModuleNotFoundError: No module named 'modules.guardrails'` (or ImportError).

- [ ] **Step 3: Create `modules/guardrails.py`**

```python
# Детерминированные guardrails для негоциации — «код владеет числами».
# Чистые функции без побочных эффектов и без обращений к БД/сети. Вешаются на
# авто-отправку автопилота в Инкременте 3; здесь — только сами примитивы + тесты.

import json
import re
from typing import Any, Optional


def reconcile_floor(
    ad_min_eur: Optional[float],
    operator_floor_eur: Optional[float],
) -> Optional[float]:
    """Единый эффективный пол = максимум из ad_brief-минимума и операторского
    floor (оператор может только УЖЕСТОЧИТЬ, не опустить ниже ad-min).
    None, если ни одно значение не является положительным числом."""
    vals = [float(v) for v in (ad_min_eur, operator_floor_eur)
            if v is not None and float(v) > 0]
    return max(vals) if vals else None


def floor_violation(
    proposed_eur: Optional[float],
    floor_eur: Optional[float],
) -> bool:
    """True, если предложенная цена ниже пола (нарушение). Если любая из величин
    None — сравнивать нечего, нарушения нет."""
    if proposed_eur is None or floor_eur is None:
        return False
    return float(proposed_eur) < float(floor_eur)


_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def outgoing_language_ok(text: str, client_lang: Optional[str]) -> bool:
    """Алфавитная санити-проверка исходящего клиенту текста.

    Ловит катастрофу — когда вместо переведённого client_answer уходит русский
    черновик ru_answer (или наоборот). Кириллица допустима ТОЛЬКО при
    client_lang='ru'; для не-ru языков кириллица = провал, а для 'ru' —
    наоборот требуем кириллицу. Пустой текст → провал (нечего слать)."""
    if not text or not text.strip():
        return False
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    lang = (client_lang or "").strip().lower()
    if lang == "ru":
        return has_cyrillic
    return not has_cyrillic


def extract_json_object(text_blocks: list[str]) -> dict[str, Any]:
    """Устойчиво достать JSON-объект из текстовых блоков ответа модели.

    С web_search модель может вернуть блоки вида [нарратив, tool, JSON, хвост].
    Идём по блокам С КОНЦА; в каждом вырезаем первый сбалансированный {...} и
    пробуем json.loads. Возвращаем первый распарсенный dict. RuntimeError, если
    валидного JSON-объекта нет ни в одном блоке."""
    for block in reversed(list(text_blocks)):
        if not block:
            continue
        candidate = _find_json_object(block)
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("no JSON object found in model response")


def _find_json_object(text: str) -> Optional[str]:
    """Вырезать первый сбалансированный {...}-объект из текста, корректно
    учитывая вложенность и строковые литералы с экранированием."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest tests/test_guardrails.py -q'`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest -q 2>&1 | tail -5'`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && git add modules/guardrails.py tests/test_guardrails.py && git commit -m "feat(guardrails): pure floor/language/json-extraction primitives (Track B Inc2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"'
```

---

## Final verification

- [ ] **Run the complete suite**

Run: `ssh -o RemoteCommand=none pg@100.81.154.93 'cd ~/kleinanzeigen-bot && python3 -m pytest -q 2>&1 | tail -5'`
Expected: all green, ~271 passed (256 + 15 new).

- [ ] **Confirm no runtime path changed (sanity):** `git diff --stat main..HEAD` should show only `database.py`, `modules/db_threads.py`, `config.py`, `web/api_ma.py`, the new `modules/guardrails.py`, and the three new test files — and NO changes to `modules/outgoing.py`, `modules/incoming.py`, or `modules/claude.py`.

---

## Self-review notes (author)

- **Spec coverage:** This increment delivers the additive foundation of spec §3.1 (negotiation_state model), §3.2 primitives (G5 `reconcile_floor`, G1 `floor_violation`, G2 `outgoing_language_ok`, G4 `extract_json_object`), and the config cap (spec §3.3, replacing the hardcoded 20) + shadow-mode flag (spec §3.6 rollout ladder). Wiring these into the autopilot dispatch, populating state live, and the ratchet/G6 + activation ladder are explicitly Increment 3 (they change the execution path and require the owner's go).
- **No live-path change:** confirmed — no edits to outgoing/incoming/claude execution code; autopilot remains off; `send_mode` untouched. The only schema change is one additive CREATE TABLE.
- **Injection safety:** `_NEG_STATE_COLS` is a fixed literal allowlist; column names interpolated into SQL come only from it, never from caller input.
- **Type/name consistency:** `get_negotiation_state` / `upsert_negotiation_state` / `record_our_offer` defined once, re-exported in `database.py`, referenced as `db.<name>` in tests. `reconcile_floor` / `floor_violation` / `outgoing_language_ok` / `extract_json_object` defined once in `modules/guardrails.py`.
- **Placeholder scan:** none — every step has exact code/commands.
