# Phase 5 — TG bot cleanup (slim bot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Удалить из `modules/telegram_bot.py` все UI handlers и state machine которые покрыты Phase 1-4 в MA. Превратить бот в pipeline + notifications surface, всё actionable перенести в MA через web_app deep-link buttons.

**Architecture:** Additive changes сначала (новый mini-card builder, переписать send_for_review и pipeline rendering под web_app buttons), потом bulk-delete legacy кода (callback handlers, _PENDING_INPUTS, _format_review_text variants, _review_keyboard, NEEDS_CONFIRM, confirm-gate). После каждого destructive task — sanity check что бот импортируется.

**Tech Stack:** Python 3.11 + python-telegram-bot 22+ (для InlineKeyboardMarkup helper) / pytest 9.0.

**Все команды на проде через ssh** (`ssh 192.168.88.28 ...`). Worktree `/home/pg/kleinanzeigen-bot-ma` на ветке `ma-phase5`. Backup: tag `pre-phase4-2026-05-10` + DB snapshot.

**Spec reference:** `docs/superpowers/specs/2026-05-10-tg-bot-phase5-design.md`

**Worktree setup (один раз):**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree remove /home/pg/kleinanzeigen-bot-ma 2>&1; git -C /home/pg/kleinanzeigen-bot branch -d ma-phase4 2>&1; git -C /home/pg/kleinanzeigen-bot worktree add /home/pg/kleinanzeigen-bot-ma -b ma-phase5 && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `97 passed`. Worktree на `ma-phase5`.

**Telegram Mini App URL для deep-link:** `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/` (current Pages URL).

---

## File Structure

**Modified (heavily):**
- `modules/telegram_bot.py` — additive changes (Tasks 1-3) + bulk delete (Tasks 4-7). Net: 3507 → ~1000-1200 LOC.

**Tests:**
- Все 97 unit-тестов должны пройти без изменений.
- Опциональный smoke-test в Task 8: `python3 -c "from modules import telegram_bot"` без ошибок.

---

## Task 1: Add web_app deep-link helper + rewrite `send_for_review`

**Files:**
- Modify: `modules/telegram_bot.py`

**Background:** `send_for_review(message_id)` сейчас (строка ~1235) шлёт full review-карточку с keyboard в DM-fanout. Нужно превратить в мини-карточку с одной web_app-кнопкой. Существующая инфраструктура DM-fanout через `_http_post` — оставляем.

### Step 1: Inspect current send_for_review

```bash
ssh 192.168.88.28 'sed -n "1235,1340p" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

Запомни структуру: какие функции она зовёт, как формируется text, как добавляется в `db.add_card_dispatch`.

### Step 2: Find a good place to add MA URL constant + helper

Add near top of file (after existing module imports/constants), before any function definitions. Find good location:

```bash
ssh 192.168.88.28 'grep -n "^TELEGRAM_API\|^logger = \|^PIPELINE_BUTTON_LABEL" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -5'
```

Insert after these constants (e.g., right before `_http_post_single` definition):

```python
# Mini App URL для deep-link buttons. Pages serves SPA из main:/web-app/.
MA_BASE_URL = "https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/"


def _ma_deep_link(start_param: str) -> str:
    """Сформировать URL для web_app кнопки. start_param формата 'review_<msg_id>' / 'thread_<id>'."""
    return f"{MA_BASE_URL}?tgWebAppStartParam={start_param}"


def _minicard_text(msg: Any) -> str:
    """Компактный текст для bot-уведомления о новом draft / обновления после MA-action.

    Замена _format_review_text для slim-bot. ~5 строк вместо 30.
    """
    buyer = msg["buyer_display_name"] if "buyer_display_name" in msg.keys() else "?"
    ad_title = msg["ad_title"] if "ad_title" in msg.keys() else "(без названия)"
    ad_price = msg["ad_price"] if "ad_price" in msg.keys() else "?"
    de_client = msg["de_client"] if "de_client" in msg.keys() else ""
    status = msg["status"] if "status" in msg.keys() else ""

    status_emoji = {
        "pending": "📨", "new": "📨", "edited": "✏️", "approved": "👌",
        "sent": "✅", "sent_debug": "🟨", "skipped": "❌", "skipped_sold": "💰",
    }.get(status, "📨")

    snippet = (de_client or "")[:160].replace("\n", " ").strip()
    if len(de_client or "") > 160:
        snippet += "…"

    text = f"{status_emoji} <b>{_html(buyer)}</b>\n"
    text += f"🏷 {_html(ad_title)} · {_html(ad_price)}\n"
    if snippet:
        text += f"💬 {_html(snippet)}"
    return text


def _ma_review_keyboard(msg_id: int) -> dict[str, Any]:
    """Single-button keyboard для bot-уведомления — открывает MA review screen."""
    return {
        "inline_keyboard": [[{
            "text": "📋 Открыть в MA",
            "web_app": {"url": _ma_deep_link(f"review_{msg_id}")},
        }]]
    }
```

### Step 3: Rewrite `send_for_review`

Replace existing `send_for_review(message_id)` body (line ~1235-1340 — ~100 LOC) with slim version:

```python
def send_for_review(message_id: int) -> Optional[int]:
    """Отправить мини-уведомление о draft в DM-fanout. Single button → MA review."""
    msg = db.get_message(message_id)
    if not msg:
        logger.warning("send_for_review: msg %s not found", message_id)
        return None

    # send_for_review НЕ сбрасывает status в pending если row уже не 'new'
    # (защита от sent → pending corrупции)
    if msg["status"] == "new":
        db.update_message(message_id, status="pending")

    # Очистить предыдущие dispatches (если карточка пересылается)
    db.clear_card_dispatches(message_id)

    text = _minicard_text(msg)
    text = _truncate_html_safe(text)
    keyboard = _ma_review_keyboard(message_id)

    primary_chat_id = config.telegram_chat_id()
    payload = {
        "chat_id": int(primary_chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": keyboard,
    }

    # _http_post автоматически делает DM-fanout если operator_dm_ids непустой.
    try:
        result = _http_post("sendMessage", payload)
    except Exception as e:
        logger.exception("send_for_review failed: %s", e)
        return None

    # Track dispatch'ы для broadcast_after_external_action
    dm_ids = config.telegram_operator_dm_ids()
    if dm_ids:
        # _http_post внутри fan'аутил — нам нужно собрать message_ids всех dispatches.
        # Поскольку _http_post возвращает только first_result, для DM-fanout мы
        # вызвали отдельные _http_post_single per DM внутри _http_post. card_dispatches
        # обновляется через _track_msg → но _track_msg в _http_post пишет в _BOT_TRACKED_MSGS.
        # Для card_dispatches (broadcast) нам нужно явно записать в db:
        for dm_id in dm_ids:
            # Найдём message_id из _BOT_TRACKED_MSGS — там последние tracked msg_ids per chat.
            # Проще: повторно отправить per DM if не сработал fanout.
            pass  # _http_post уже отправил всем, но db.add_card_dispatch не зовётся
    else:
        # Single-recipient: result is the sent message
        if isinstance(result, dict) and result.get("message_id"):
            db.add_card_dispatch(message_id, int(primary_chat_id), result["message_id"])

    return result.get("message_id") if isinstance(result, dict) else None
```

⚠️ **Внимание (concerns to surface to controller):**

Существующий `send_for_review` использует `_broadcast_card` или подобный механизм для DM-fanout с tracking. Slim version выше может потерять tracking в `card_dispatches`. Нужно:
1. Прочитать ОРИГИНАЛЬНЫЙ `send_for_review` тщательно (Step 1) и понять как он добавляет card_dispatches.
2. Воспроизвести ту же логику но с НОВЫМ text + keyboard.

Если оригинал сложный — implementer должен **сохранить tracking-логику** и заменить ТОЛЬКО text + keyboard вычисление. Иначе broadcast_after_external_action перестанет находить dispatches.

Альтернатива: оставить большую часть send_for_review как есть, но заменить:
- `text = _format_review_text(msg)` → `text = _minicard_text(msg)`
- `kb = _review_keyboard(...)` → `kb = _ma_review_keyboard(message_id)`

И этого достаточно — tracking остаётся.

**Implementer должен выбрать минимально-инвазивный подход. Если send_for_review сложный, surface concerns и просить помощи.**

### Step 4: Verify bot still imports

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot; print(telegram_bot.send_for_review)"'
```

Expected: `<function send_for_review at 0x...>`. NO ImportError, NO syntax error.

### Step 5: Run tests

```bash
ssh 192.168.88.28 'python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `97 passed`.

### Step 6: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(bot): mini-card review notification with web_app button"'
```

---

## Task 2: Rewrite pipeline rendering — web_app buttons

**Files:**
- Modify: `modules/telegram_bot.py`

**Background:** Pipeline-карточки рендерятся в `_on_pipeline` или `_render_pipeline` (поищи). Каждый тред — кнопка с `callback_data: "pipe:N"` → handler → шлёт thread-detail. После Phase 5 каждая кнопка должна быть `web_app: {url: ...?tgWebAppStartParam=thread_<thread_id>}`.

### Step 1: Find pipeline rendering function

```bash
ssh 192.168.88.28 'grep -n "_on_pipeline\|_render_pipeline\|def.*pipeline\|pipe:" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -20'
```

### Step 2: Read pipeline rendering code

```bash
ssh 192.168.88.28 'sed -n "<line>,+80p" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

(Use line number from Step 1.)

### Step 3: Modify pipeline-card builder

Find the part that builds inline_keyboard for each thread. Look for something like:

```python
"reply_markup": {"inline_keyboard": [[{"text": "...", "callback_data": f"pipe:{msg_id}"}]]}
```

Replace `callback_data: "pipe:N"` patterns with `web_app: {url: ...}`. Use the `_ma_deep_link` helper from Task 1:

```python
"reply_markup": {"inline_keyboard": [[{
    "text": "📋 Открыть в MA",
    "web_app": {"url": _ma_deep_link(f"thread_{thread_id}")},
}]]}
```

Note: `thread_id` here is the gmail_thread_id (string, not msg_id integer). Confirm in code.

If pipeline-card has multiple buttons (e.g., status indicators) — replace ALL `callback_data` references with web_app. Most likely only one tap-target per card.

### Step 4: Verify imports + tests

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot; print(\"OK\")" && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: OK + 97 passed.

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "feat(bot): pipeline cards use web_app buttons → MA"'
```

---

## Task 3: Smoke test after additive changes

**Files:** none (verification task).

**Background:** After Tasks 1-2 — additive changes done. Bot has new mini-card and web_app pipeline buttons. Old callback handlers still exist (will be deleted in Tasks 4-7). Need to verify everything works before bulk-delete.

### Step 1: Restart prod bot temporarily — checkpoint

⚠️ **Warning:** prod bot runs from main, not worktree. We're testing on worktree only here, no prod impact.

Verify bot module imports cleanly:

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "
from modules import telegram_bot
# Verify new helpers exist
assert callable(telegram_bot._ma_deep_link), \"_ma_deep_link missing\"
assert callable(telegram_bot._minicard_text), \"_minicard_text missing\"
assert callable(telegram_bot._ma_review_keyboard), \"_ma_review_keyboard missing\"
# Verify legacy still exists (will delete in next tasks)
assert callable(telegram_bot.send_for_review)
print(\"all helpers present, ready for delete tasks\")
"'
```

Expected: all helpers present.

### Step 2: Test deep-link URL formation

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "
from modules import telegram_bot
url = telegram_bot._ma_deep_link(\"review_42\")
assert \"tgWebAppStartParam=review_42\" in url
assert url.startswith(\"https://\")
print(url)
"'
```

Expected: `https://pgolitech-lab.github.io/kleinanzeigen-bot/web-app/?tgWebAppStartParam=review_42`.

### Step 3: Run all tests

```bash
ssh 192.168.88.28 'python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: `97 passed`.

No commit — this is a checkpoint task.

---

## Task 4: Delete `_on_callback` action handlers

**Files:**
- Modify: `modules/telegram_bot.py` (delete sections inside `_on_callback`)

**Background:** `_on_callback` is at line ~2414 with ~700 LOC. It has many `if action == "..."` branches. We need to delete branches for actions now in MA, keep only:
- `back` → no-op (operator may have old TG card with `back:N` callback) — answer query, no rendering
- `pipe:N` → no-op (legacy, replaced by web_app buttons)
- Reminder approve/skip handlers (`rem:approve:N`, `rem:skip:N`, `rem:snooze:*:N` if exist) — keep
- `cancel`, `inputcancel`, `noop`, `locked_noop` — keep as graceful no-ops (if used)

**Delete branches for:**
- `send`, `skip`, `sold`, `editru`, `editde`, `price`, `instr`
- `q:*` (q:fest, q:minus5, q:minus10, q:ask, q:meet)
- `t:*` (t:harsh, t:friend, t:short, t:regen)
- `compose`
- `apstart`, `apconfirm`, `apstop`
- `clienthist`
- `opencard`
- `confirm:<original>:N` handlers (was used for confirm-gate)

### Step 1: Read _on_callback fully

```bash
ssh 192.168.88.28 'sed -n "2414,3149p" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | wc -l'
ssh 192.168.88.28 'sed -n "2414,3149p" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -200'
```

Identify all `if action == "..."` branches and confirm which to delete.

### Step 2: Delete branches via Edit tool

For each action to delete, find the `if action == "<name>":` block and `if action.startswith("q:") or action.startswith("t:"):` blocks. Use Edit with old_string=full block, new_string="". Verify each deletion doesn't break adjacent code.

After deletions, the `_on_callback` should be much shorter — ~100 LOC instead of 700. Should still have:
- Initial parsing of callback_data
- Authorization check
- Confirm-gate check (keep for now — deleted in Task 7)
- Lock check (keep)
- `back` / `pipe` / reminder handlers
- Default fallthrough: `await query.answer(text="Открой карточку в MA", show_alert=False)` for unknown actions (graceful)

### Step 3: Verify imports + tests

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot" && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: 97 passed.

### Step 4: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "refactor(bot): delete callback action handlers (moved to MA)"'
```

---

## Task 5: Delete `_PENDING_INPUTS` state machine + input mode handlers

**Files:**
- Modify: `modules/telegram_bot.py`

**Background:** `_PENDING_INPUTS` (line ~1362) + helpers `_enter_input_mode`, `_exit_input_mode` + branches in `_on_text` (line ~3149) для input modes. Все эти input modes теперь в MA edit-form.js.

### Step 1: Locate state machine

```bash
ssh 192.168.88.28 'grep -nE "^_PENDING_INPUTS\b|^def _enter_input_mode|^def _exit_input_mode|_PENDING_INPUTS\[" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -15'
```

### Step 2: Delete state dict + helpers

Find and delete:
- `_PENDING_INPUTS: dict[...] = {}` definition
- `_enter_input_mode(...)` function
- `_exit_input_mode(...)` function
- Module-level state related to input mode

### Step 3: Trim `_on_text` handler

Read `_on_text` (line ~3149). Delete branches that handle:
- `pending["action"] == "edit_ru"` / `"edit_de"` / `"price"` / `"instr"` / `"compose"` / `"ap_floor"`
- Any cleanup logic for `_PENDING_INPUTS`

Keep:
- Pipeline button handler (`text == PIPELINE_BUTTON_LABEL`)
- Standard slash commands (`/help`, `/stats`, etc.)
- Reminder text response if any

### Step 4: Verify imports + tests

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot" && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "refactor(bot): delete _PENDING_INPUTS state machine + input mode handlers"'
```

---

## Task 6: Delete review-card render functions

**Files:**
- Modify: `modules/telegram_bot.py`

**Background:** `_format_review_text` (line ~470, ~190 LOC) и `_review_keyboard` / `_review_keyboard_obj` / `_review_keyboard_for_msg` (lines ~656-810, ~150 LOC) — больше не нужны. Mini-card text builder в Task 1 заменил их.

### Step 1: Verify no remaining usages

```bash
ssh 192.168.88.28 'grep -nE "_format_review_text|_review_keyboard\b|_review_keyboard_obj|_review_keyboard_for_msg" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -20'
```

If `broadcast_after_external_action` (Phase 3b helper) uses `_format_review_text` — change to use `_minicard_text` instead. Edit:
```python
text = _format_review_text(msg)
```
→
```python
text = _minicard_text(msg)
```

If `send_for_review` (rewritten in Task 1) doesn't use them — they should be unused now.

### Step 2: Delete the functions

Use Edit to delete:
- `_format_review_text(msg)` body
- `_review_keyboard(message_id, status)` body
- `_review_keyboard_obj(message_id, status)` body
- `_review_keyboard_for_msg(msg)` body

### Step 3: Verify

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot" && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

### Step 4: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "refactor(bot): delete _format_review_text + _review_keyboard helpers"'
```

---

## Task 7: Delete confirm-gate + cleanup unused

**Files:**
- Modify: `modules/telegram_bot.py`

### Step 1: Delete `NEEDS_CONFIRM` set

```bash
ssh 192.168.88.28 'grep -nE "^NEEDS_CONFIRM\b|NEEDS_CONFIRM\[" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py | head -5'
```

Delete:
- `NEEDS_CONFIRM: set[str] = {...}` definition (line ~815)
- Confirm-gate check in `_on_callback` (the `if action_key in NEEDS_CONFIRM and not confirm_prefix:` block)
- `_render_confirm_preview(...)` function if exists

### Step 2: Identify other unused helpers

```bash
ssh 192.168.88.28 'grep -nE "^def _" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

For each helper function, check if it's used:

```bash
ssh 192.168.88.28 'grep -c "<helper_name>" /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

If only definition exists (count=1) — function unused, can delete.

Likely now-unused candidates:
- Confirm-preview builders
- Review-card text formatters for specific states
- Edit-mode UI builders

DO NOT delete (still used):
- `_http_post` / `_http_post_single` / `_truncate_html_safe`
- `mark_thread_busy` / `clear_thread_busy` / `thread_is_busy` / `_THREAD_BUSY` / `_msg_thread_id`
- `_acquire_lock` / `_release_lock` / `_check_lock` / `_lock_remaining_min`
- `_html` (HTML escape)
- `broadcast_after_external_action`
- Pipeline rendering helpers
- Reminder helpers (`send_reminder_offer` + ассоциированные)
- Autopilot notifications (`send_autopilot_*`)
- `build_application`
- `send_daily_summary`
- Hourly error monitor

### Step 3: Verify tests + imports

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot-ma && python3 -c "from modules import telegram_bot" && python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

### Step 4: Final wc

```bash
ssh 192.168.88.28 'wc -l /home/pg/kleinanzeigen-bot-ma/modules/telegram_bot.py'
```

Expected: ~1000-1200 lines (vs 3507 before).

### Step 5: Commit

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma add modules/telegram_bot.py && git -C /home/pg/kleinanzeigen-bot-ma commit -m "refactor(bot): delete confirm-gate + cleanup unused helpers"'
```

---

## Task 8: Merge → main, restart, deploy, E2E

- [ ] **Step 1: Verify commits**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot-ma log --oneline 45cf83c..HEAD'
```

Expected: ~7 commits.

- [ ] **Step 2: Verify main clean + final test**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot status -s; python3 -m pytest /home/pg/kleinanzeigen-bot-ma/tests/ -c /home/pg/kleinanzeigen-bot-ma/pytest.ini 2>&1 | tail -3'
```

Expected: empty status, 97 passed.

- [ ] **Step 3: Merge to main**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot merge ma-phase5 --no-edit 2>&1 | tail -5'
```

- [ ] **Step 4: Restart bot — CRITICAL CHECKPOINT**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 6 && systemctl is-active kleinanzeigen-bot && journalctl -u kleinanzeigen-bot -n 30 --no-pager --since "60 seconds ago" | grep -iE "ERROR|Traceback|Application startup|Application started" | head -15'
```

Expected: `active` + `Application startup complete` + `Application started`. **NO Traceback/ImportError**.

⚠️ Если бот не стартует — IMMEDIATE ROLLBACK:
```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot reset --hard pre-phase4-2026-05-10'
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot'
```

- [ ] **Step 5: Local smoke endpoints**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "health: %{http_code}\n" http://127.0.0.1:8080/api/ma/health'
ssh 192.168.88.28 'curl -s -o /dev/null -w "pipeline: %{http_code}\n" http://127.0.0.1:8080/api/ma/pipeline'
ssh 192.168.88.28 'curl -s -o /dev/null -w "send: %{http_code}\n" -X POST http://127.0.0.1:8080/api/ma/messages/1/send'
```

Expected: все `422`.

- [ ] **Step 6: Push to GitHub**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push origin main 2>&1 | tail -3'
```

- [ ] **Step 7: E2E in Telegram (manual)**

Закрой/открой TG. Открой DM с ботом.

1. **Pipeline-карточка** — каждый тред теперь имеет одну `[📋 Открыть в MA]` web_app кнопку. Tap → MA открывается на thread screen.
2. **Notification на новый incoming** — мини-карточка `📨 Buyer · Title · 1500€` + одна кнопка `[📋 Открыть в MA]`. Tap → MA review screen с pending-draft + action-grid.
3. **Action в MA** (например send) → бот видит обновление текста мини-карточки (через `broadcast_after_external_action`). Карточка показывает `✅ Buyer · Title · 1500€` (status emoji обновился).
4. **Старая carcточка в чате** (если оператор листал назад) — кнопки `q:fest` / `send` / etc. — клик → bot отвечает «Открой в MA» (graceful no-op).
5. **`/pipeline` команда** работает.
6. **Persistent reply keyboard** «🔄 Обновить» работает.
7. **Reminders** (если есть pending) — offer flow в боте всё ещё работает.

- [ ] **Step 8: Phase 5 acceptance**

Phase 5 закрыт когда:
- ✅ telegram_bot.py reduced ~3507 → ~1000-1200 LOC
- ✅ Бот стартует без ошибок
- ✅ 97 unit-тестов проходят
- ✅ Pipeline кнопки → MA работают
- ✅ Новый draft → мини-карточка с web_app кнопкой → MA review
- ✅ MA action → broadcast обновляет мини-карточку в боте
- ✅ Старые callback'и в чате → graceful no-op
- ✅ Reminders + daily summary + hourly errors не сломаны

---

## Cleanup

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot worktree remove /home/pg/kleinanzeigen-bot-ma'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot branch -d ma-phase5'
```

## Rollback

If anything breaks at Step 4 of Task 8:
```bash
ssh 192.168.88.28 'sudo systemctl stop kleinanzeigen-bot'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot reset --hard pre-phase4-2026-05-10'
ssh 192.168.88.28 'cp /home/pg/backups/db-pre-phase4-2026-05-10.db /home/pg/kleinanzeigen-bot/kleinanzeigen.db'
ssh 192.168.88.28 'rm -f /home/pg/kleinanzeigen-bot/kleinanzeigen.db-wal /home/pg/kleinanzeigen-bot/kleinanzeigen.db-shm'
ssh 192.168.88.28 'sudo systemctl start kleinanzeigen-bot'
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot push --force-with-lease origin main'
```

## Risks

- **Refactor scale** — touching one 3507-line file with 2000+ LOC removal. High chance of leaving dangling reference. Sanity-check `from modules import telegram_bot` after EACH task.
- **send_for_review tracking** — original may have specific card_dispatches logic that we must preserve. Implementer Task 1 must read original carefully.
- **Reminder handlers** — must NOT delete `rem:approve:N` callbacks. Verify before each Task 4 deletion.
- **Backwards compat** — old TG cards in chat may have callback_data referring to deleted actions. Default fallthrough в `_on_callback` должен show graceful message instead of error.
- **scheduler.py dependencies** — Tasks 4-7 могут случайно удалить функцию которую scheduler зовёт. Each task verifies via `from modules import telegram_bot` import + pytest.

## What deferred to Phase 6 (optional)

- Reminder approve/skip flow в MA
- Daily summary view в MA
- Settings: account-edit (gmail accounts CRUD)
