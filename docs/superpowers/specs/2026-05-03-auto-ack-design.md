# Auto-ack: автоответ на первое сообщение клиента

**Дата:** 2026-05-03
**Цель:** накрутить метрику Kleinanzeigen «antwortet typisch innerhalb X Stunden» за счёт мгновенной отправки короткого приветствия-заглушки на первый inquiry в каждом новом треде.

## Мотивация

Текущий flow: входящее → Sonnet генерит черновик → оператор нажимает ✅ через Telegram → SMTP. Время от inquiry до отправки ≈ от секунд (если оператор у телефона) до часов. Kleinanzeigen рассчитывает badge «отвечает в течение X часов» по этому интервалу — для продаж важно держать его низким.

Решение: при получении первого письма в треде бот сразу (в фоне, до нажатия оператора) отправляет короткий ack-text вида *«Guten Tag Hans! Bin gerade unterwegs, melde mich gleich mit Details.»* — уложиться в 3–5 секунд от получения. Реальный полноценный ответ через нормальный operator-review flow приходит позже.

## Что в скоупе

- Per-account toggle: `accounts.auto_ack_enabled` (default 0).
- Триггер: первое incoming-сообщение в новом `gmail_thread_id`.
- Генерация: Haiku-4-5 — детект языка + приветствие с учётом времени суток + имя клиента + рандомный «повод» из заранее заготовленного списка.
- Отправка: через тот же `gmail.send_reply` + `_send_reply` flow, уважает `send_mode` (disabled / redirect / production).
- Запись в БД: row в `messages` с `direction='out'`, `is_auto_ack=1`.
- Отображение: маркер 🤖 в блоке «История» Telegram-карточки + badge в web-морде. Sonnet видит ack как `[Продавец]:` turn в history → не дублирует приветствие.
- Глобальный on/off: НЕТ. Только per-account. Если хочется глобально — выключаешь во всех аккаунтах.

## Что НЕ в скоупе

- Кастомизация текста ack через UI (текст полностью генерится Haiku).
- Список «поводов» через UI — module-level константа в `scheduler.py`.
- Отдельный `auto_ack_send_mode` (используем общий `send_mode`).
- Отдельная Telegram-нотификация про факт ack (показываем только в основной карточке ревью).

---

## Архитектура

### Поток обработки в `_process_incoming` (scheduler.py)

```
1. Defense-in-depth фильтры (noreply, age, junk, classifier, sold) — без изменений
2. Извлечь ad_id, ad_url, очистить body
3. INSERT messages (direction='in', status='new')

4. ── Auto-ack ──
   Условие:
     account.auto_ack_enabled = 1
     AND это первое incoming-сообщение треда (sum(direction='in')==1)
     AND ни одного ack-row в треде ещё нет (защита от reprocess)
     AND send_mode != 'disabled'
   а) Haiku: claude.generate_auto_ack(buyer_display_name, body, hour_local, excuse)
      excuse = random.choice(AUTO_ACK_EXCUSES)
   б) INSERT messages (direction='out', is_auto_ack=1, status='approved',
                        de_answer=ack_text, client_lang=detected_lang, ...)
   в) _send_reply(ack_row) → SMTP → status='sent'/'sent_debug'/'error_send_failed'
   Любая ошибка → log.warning, продолжаем без ack.

5. Playwright parse_ad
6. Brief generation (cached per ad_id)
7. Sonnet generate_reply (history теперь содержит ack-row → не дублирует greeting)
8. summarize_thread
9. send_for_review в Telegram (карточка покажет 🤖 Auto-ack в блоке «История»)
```

Auto-ack вставляется **до** Playwright, потому что Playwright медленный (~10–30с), а цель ack — уложиться в первые секунды для метрики.

### Изменения схемы БД

Идемпотентные миграции через `_add_column_if_missing` в `init_db`:

```python
# accounts: per-account toggle
_add_column_if_missing(conn, "accounts", "auto_ack_enabled", "INTEGER NOT NULL DEFAULT 0")

# messages: пометка row как авто-приветствия
_add_column_if_missing(conn, "messages", "is_auto_ack", "INTEGER")
```

`auto_ack_enabled` default `0` — старые аккаунты после миграции ничего не шлют пока галку не поставишь.

### Новая функция `claude.generate_auto_ack`

```python
def generate_auto_ack(
    buyer_display_name: str,   # "Hans Müller" или ""
    body: str,                  # тело письма (для детекта языка)
    hour_local: int,            # 0..23, час в Europe/Berlin
    excuse_hint: str,           # одна из заготовок RU
) -> dict[str, Any]:
    """Возвращает {client_lang, ack_text, tokens_in, tokens_out, cost_usd}."""
```

**JSON-схема ответа:**
```python
{
    "type": "object",
    "properties": {
        "client_lang": {"type": "string"},
        "ack_text": {"type": "string"},
    },
    "required": ["client_lang", "ack_text"],
    "additionalProperties": False,
}
```

**Системный промпт:**
> «Ты вежливый продавец на Kleinanzeigen.de. Пишешь короткое приветствие-заглушку покупателю когда не можешь ответить подробно сразу.»

**User промпт (template):**
```
Покупатель прислал:
"<body[:500]>"

Имя: <buyer_display_name>  (или "не указано")
Время в Берлине: HH:00

Сгенерируй очень короткий (1–2 предложения) автоответ-заглушку:
- Поздоровайся с учётом времени суток (Guten Morgen/Tag/Abend / аналог на нужном языке)
- Обратись по имени, если оно реальное (не email/служебный текст); иначе обычное "Hallo!"
- Скажи кратко повод почему не можешь сразу: "<excuse_hint>"
- Пообещай ответить подробно скоро (в течение часа / сегодня)
- БЕЗ конкретики по товару — это заглушка
- На том же языке, что написал покупатель (определи язык)

Поля: client_lang (ISO 639-1), ack_text.
```

**Модель:** `claude-haiku-4-5` (без `effort`/`thinking` — Haiku их не поддерживает). Цена ≈ $0.0006 за вызов.

### Список «поводов» в `scheduler.py`

Module-level константа:
```python
AUTO_ACK_EXCUSES = [
    "сейчас за рулём, не могу подробно отвечать",
    "сейчас на встрече",
    "сейчас на телефоне с другим клиентом",
    "сейчас вне офиса",
    "сейчас занят, освобожусь в течение часа",
    "сейчас в дороге",
]
```

Random pick на каждый вызов даёт реальную вариативность; Haiku переводит/адаптирует на язык покупателя.

### Новая функция `scheduler._send_auto_ack`

```python
def _send_auto_ack(account, in_row, body, buyer_display_name) -> Optional[int]:
    """Сгенерить + послать ack. Возвращает ack-row id или None при skip/error."""
    if config.send_mode() == "disabled":
        logger.info("auto-ack skipped (mode=disabled), msg=%s", in_row["id"])
        return None
    if _ack_already_sent_for_thread(in_row["gmail_thread_id"]):
        logger.info("auto-ack: уже был для thread=%s, skip", in_row["gmail_thread_id"])
        return None

    berlin_hour = datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).hour
    excuse = random.choice(AUTO_ACK_EXCUSES)

    try:
        ack = claude.generate_auto_ack(
            buyer_display_name=buyer_display_name or "",
            body=body, hour_local=berlin_hour, excuse_hint=excuse,
        )
    except Exception:
        logger.exception("auto-ack: Haiku упал, пропускаю")
        return None

    ack_id = db.add_message(
        account_id=account["id"], direction="out",
        gmail_thread_id=in_row["gmail_thread_id"],
        ad_url=in_row["ad_url"], ad_title=in_row["ad_title"],
        ad_id=in_row["ad_id"], ad_price=in_row["ad_price"],
        seller_name=in_row["seller_name"], buyer_name=in_row["buyer_name"],
        buyer_display_name=buyer_display_name,
        email_subject=in_row["email_subject"],
        client_lang=ack["client_lang"], answer_lang=ack["client_lang"],
        de_answer=ack["ack_text"],
        tokens_in=ack["tokens_in"], tokens_out=ack["tokens_out"],
        cost_usd=ack["cost_usd"],
        is_auto_ack=1, status="approved",
    )
    ack_row = db.get_message(ack_id)
    result = _send_reply(ack_row)
    if result["kind"] == "error":
        logger.warning("auto-ack SMTP failed for in_msg=%s: %s",
                       in_row["id"], result["message"])
    else:
        logger.info("auto-ack sent for in_msg=%s, mode=%s",
                    in_row["id"], config.send_mode())
    return ack_id


def _ack_already_sent_for_thread(thread_id: Optional[str]) -> bool:
    if not thread_id:
        return False
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE gmail_thread_id = ? "
            "AND COALESCE(is_auto_ack, 0) = 1 LIMIT 1",
            (thread_id,),
        ).fetchone()
    return bool(row)
```

### Маленькая правка `_send_reply`

Fallback subject когда `ad_title` пуст (Playwright ещё не отработал в момент ack-отправки):

```python
subject = "Re: Anfrage"
if msg["ad_title"]:
    subject = f"Re: {msg['ad_title']}"
elif msg["email_subject"]:
    es = msg["email_subject"]
    subject = es if es.lower().startswith("re:") else f"Re: {es}"
```

Польза не только для ack — для любого случая когда Playwright упал.

### Условие в `_process_incoming`

После `inserted_id = db.add_message(direction='in', ...)`:

```python
thread_id = email.get("gmail_thread_id") or ""
if account["auto_ack_enabled"] and thread_id:
    thread_msgs = db.thread_history(thread_id)
    is_first_inquiry = (
        sum(1 for r in thread_msgs if r["direction"] == "in") == 1
        and not any(_safe_get(r, "is_auto_ack") for r in thread_msgs)
    )
    if is_first_inquiry:
        in_row = next(r for r in thread_msgs if r["id"] == inserted_id)
        try:
            _send_auto_ack(
                account, in_row, body,
                buyer_display_name=_clean_display_name(email.get("from_name") or "") or None,
            )
        except Exception:
            logger.exception("auto-ack pipeline crash, продолжаю основной flow")
```

`_safe_get` нужно либо протащить из telegram_bot (или продублировать локально, или просто использовать try/except KeyError).

### Изменения отображения

#### `modules/telegram_bot.py::_format_thread_history`

Маркер 🤖 у ack-rows:
```python
if r["de_answer"] and r["status"] in ("sent", "sent_debug", "edited", "approved"):
    is_ack = _safe_get(r, "is_auto_ack")
    label = "🤖 Auto-ack" if is_ack else "Мы"
    our_text = r["ru_answer"] or r["de_answer"] or ""
    lines.append(f"○ <b>{label} [{r['status']}]:</b> {_html(_truncate(our_text, 250))}")
```

То же в `_format_client_history` (полная история клиента в callback).

#### `web/templates/thread_detail.html` и `client_detail.html`

Рядом с status-badge для исходящих:
```jinja
{% if m.is_auto_ack %}<span class="badge text-bg-info">🤖 ack</span>{% endif %}
```

#### `web/templates/account_form.html`

Чекбокс рядом с `is_active`:
```html
<div class="form-check mb-3">
    <input type="hidden" name="auto_ack_enabled" value="0">
    <input type="checkbox" name="auto_ack_enabled" value="1" id="auto_ack_enabled"
           class="form-check-input"
           {% if account.auto_ack_enabled %}checked{% endif %}>
    <label class="form-check-label" for="auto_ack_enabled">
        🤖 Автоответ-приветствие на первое сообщение
    </label>
    <div class="form-text">
        При первом письме в новом треде бот отправляет короткое приветствие через Haiku
        (≈$0.0006 за раз) на языке клиента. Цель — крутить метрику Kleinanzeigen
        «отвечает в течение X часов». Уважает <code>send_mode</code>.
    </div>
</div>
```

#### `web/templates/accounts.html`

Опционально — колонка «Auto-ack» в таблице со списком аккаунтов: галочка/прочерк, чтобы видеть включена ли крутилка.

#### `database.py::update_account` — расширить allowlist

```python
allowed = {"name", "gmail_email", "gmail_app_password",
           "kleinanzeigen_email", "kleinanzeigen_password",
           "is_active", "auto_ack_enabled"}
```

#### `web/app.py` — обработчики

В `post_account_new` и `post_account_edit`:
```python
auto_ack_enabled: str = Form(""),
...
fields["auto_ack_enabled"] = 1 if auto_ack_enabled else 0
```

(паттерн скрытого hidden=0 в HTML уже стоит для unchecked-state)

### Изменение `find_reminder_candidates`

Исключить ack-rows из кандидатов на пинг:
```sql
AND COALESCE(is_auto_ack, 0) = 0
```

Иначе случай «бот послал ack, оператор ещё не одобрил реальный ответ → reminder сработал → пингуем клиента "не забывайте", хотя по делу мы ему не написали ни разу» — embarrassing.

---

## Edge cases и защиты

| Случай | Поведение |
|---|---|
| `send_mode=disabled` | log + skip (ack-row не создаётся) |
| `account.auto_ack_enabled=0` | skip (no log) |
| Первое сообщение в треде, но в БД уже есть ack для этого thread_id (reprocess) | log + skip |
| Не первое incoming в треде (есть прошлые in-rows) | skip silently |
| Haiku падает (5xx, timeout) | `logger.exception`, return None, основной flow продолжается |
| SMTP падает | ack-row остаётся со status `error_send_failed`, основной flow продолжается |
| `buyer_display_name` пуст / похож на email | передаём пустую строку, Haiku сам решает обращаться по имени или нет |
| `body` слишком короткий / нетекстовой | Haiku всё равно даёт client_lang (default 'de' если не уверен) и ack_text |
| Reminder hourly job через день после ack без реального reply | `is_auto_ack=1` исключает row из reminder candidates → ничего не предлагает |

---

## Цена эксплуатации

- **+1 Haiku-вызов на первое сообщение треда:** ≈ 150 in + 80 out tokens = **$0.00055**.
- Главная нагрузка существующего pipeline — Sonnet generate_reply ≈ $0.005–$0.02 за inquiry — порядки больше. Auto-ack ≈ +3% к стоимости одного inquiry.

---

## План ручного тестирования

(Автотестов в проекте нет, тестируем через `/debug/reprocess`.)

1. **`send_mode=disabled` + checkbox=on** на тестовом аккаунте → `/debug/reprocess 1` → в логе `auto-ack skipped (mode=disabled)`, ack-row не создаётся.
2. **`send_mode=redirect` + `debug_email=admin@interconnect.gr` + checkbox=on** → `/debug/reprocess 1` → ack приходит на debug_email с банером `[DEBUG → buyer]`, основной reply Sonnet тоже туда. В Telegram-карточке блок `🤖 Auto-ack [sent_debug]: ...`. Sonnet reply не повторяет greeting.
3. **`send_mode=production` + checkbox=off** → ack НЕ шлётся (per-account toggle off), в логе ничего про ack.
4. **Двойной reprocess того же письма** → второй ack НЕ дублируется (guard на `_ack_already_sent_for_thread`).
5. **Reminder не пингует ack-only тред:** включить `reminders_enabled=1`, `reminder_after_days=0`, прогнать `/debug/check-reminders-now` сразу после ack — кандидатов нет.
6. **Приветствие по времени суток:** прогнать reprocess в разное время дня, проверить что Haiku генерит «Guten Morgen» / «Guten Tag» / «Guten Abend» соответственно.
7. **Имя клиента в ack:** проверить что нормальный `display_name` встраивается, а email-вид (`hans.muller@gmail.com`) — игнорируется и используется обобщённое «Hallo!».
8. **Не-немецкий язык:** прислать тестовый inquiry на английском/русском/польском (через личный gmail-relay) → ack приходит на том же языке.

---

## Список затронутых файлов

| Файл | Изменения |
|---|---|
| `database.py` | Миграции `auto_ack_enabled`, `is_auto_ack`. Расширение allowlist `update_account`. SQL-фильтр в `find_reminder_candidates`. |
| `modules/claude.py` | Новая функция `generate_auto_ack`. |
| `scheduler.py` | Новые: `AUTO_ACK_EXCUSES`, `_send_auto_ack`, `_ack_already_sent_for_thread`. Вставка вызова в `_process_incoming` после INSERT incoming. Маленькая правка fallback-subject в `_send_reply`. |
| `modules/telegram_bot.py` | Маркер 🤖 в `_format_thread_history` и `_format_client_history`. |
| `web/app.py` | Принимать `auto_ack_enabled` в POST handlers `accounts/new` и `accounts/{id}/edit`. |
| `web/templates/account_form.html` | Чекбокс. |
| `web/templates/accounts.html` | Колонка «Auto-ack» (опционально). |
| `web/templates/thread_detail.html` | `🤖 ack` badge. |
| `web/templates/client_detail.html` | `🤖 ack` badge. |
