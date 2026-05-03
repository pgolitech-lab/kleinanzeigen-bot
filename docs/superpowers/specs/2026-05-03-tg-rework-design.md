# Telegram bot rework: keyboard cleanup + edit split + custom price/instruction

**Дата:** 2026-05-03
**Цель:** убрать UI-боль текущей карточки ревью — нечитаемые кнопки, некомфортный edit-flow без переноса драфта, отсутствие ввода произвольной цены/инструкции.

## Что в скоупе

1. Новая раскладка inline-клавиатуры — 7 рядов × 2 столбца (одна full-width).
2. `✏️ Изменить` → разделение на `✏️ Правка RU` (text → Claude → client_lang) и `✏️ Правка DE` (text → save as-is + back-translate в RU для зеркала).
3. При клике любой `✏️` или `📝 Своя инструкция` или `💸 Своя цена` — текущий драфт показывается в `<pre>` блоке для tap-and-hold копирования + force_reply сообщение чтобы открыть reply-input автоматически.
4. Удалены `🔻 -5%`, `🔻 -10%`, `🤝 Встреча`, `❓ Уточнить` — заменены либо `💸 Своя цена`/`📝 Своя инструкция`, либо просто убраны как малоиспользуемые.
5. Унифицированный state-machine `_PENDING_INPUTS` (вместо `_PENDING_EDITS`) — поддерживает 4 типа ввода.
6. Новые функции `claude.regenerate_with_price` и `claude.regenerate_with_instruction` + расширение `translate_only` чтобы принимал `source_lang`.

## Что НЕ в скоупе

- Не трогаем reminder/snooze/ack-карточки.
- Не трогаем reminder-flow (висящий клиент → пинг).
- Не меняем веб-морду.
- Не меняем обработку входящих писем.

---

## Финальная раскладка

```
✏️ Правка RU       │ ✏️ Правка DE
💎 Без торга        │ 💸 Своя цена
📝 Своя инструкция │ ❌ Пропустить
✅ ОТПРАВИТЬ (full-width)            ← row 4, центр карточки
👊 Жёстче           │ ☺️ Мягче
✂️ Короче           │ 🔁 Переформулировать
💰 Товар продан    │ 📋 История клиента
```

**Удалены:** `🔻 -5%`, `🔻 -10%`, `🤝 Встреча`, `❓ Уточнить`.
**Переименованы:** `Friendlier` → `Мягче`, `Re-gen` → `Переформулировать`, `Изменить` → разделено на `Правка RU` / `Правка DE`.

13 кнопок в 7 рядах. `✅ Отправить` — full-width в центре (row 4 из 7), главное primary-действие, заметно крупнее остальных. Сверху от него — действия «изменить ответ перед отправкой» (правка/цена/инструкция/skip), снизу — preset-тон + мета.

## Confirmation step

Клик на любую кнопку **кроме `✏️ Правка RU` и `✏️ Правка DE`** не выполняет действие сразу — показывает explanation + `[▶️ Продолжить]` `[❌ Отменить]`. Только после подтверждения выполняется.

**Зачем:** защита от случайных кликов, особенно на платных операциях (regeneration ~$0.005-0.015) и финальных действиях (отправка, skip, sold).

**Поток:**
1. User clicks `👊 Жёстче` (callback `t:harsh:N`)
2. Bot edits card — body НЕ меняется (драфт + история остаются видимы), но в конце добавляется блок:
   ```
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   ⚠️ Подтверждение действия
   👊 Жёстче — Claude перепишет драфт жёстче (твёрдо, без сюсюканий).
   ~$0.005
   ```
   Клавиатура заменяется на `[▶️ Продолжить]` `[❌ Отменить]`
3. User clicks `▶️ Продолжить` (callback `confirm:t:harsh:N`):
   - Bot выполняет действие (regenerate / send / etc)
   - Клавиатура восстанавливается к полной (либо progress-state как сейчас)
4. User clicks `❌ Отменить` (callback `cancel:N`):
   - Bot восстанавливает карточку без addendum, полная клавиатура

**Encoding:** callback_data использует префикс `confirm:` перед оригинальным callback. Например `t:harsh:42` → confirmation callback = `confirm:t:harsh:42`. Парсер callback splits первое `confirm:` и обрабатывает остаток как обычный action (bypass-флаг). Cancel = просто `cancel:N`.

**Action explanations:**

| Action | Explanation |
|---|---|
| `send` | ✅ Отправит черновик клиенту через SMTP. Финальное действие. |
| `skip` | ❌ Пометит сообщение `skipped`, клиент НЕ получит ответ. |
| `q:fest` | 💎 Без торга: вежливый твёрдый отказ от торга. ~$0.005 |
| `price` | 💸 Введёшь конкретную цену числом — Claude перепишет под неё. ~$0.005 |
| `instr` | 📝 Введёшь свободный промпт — Claude перепишет по нему. ~$0.005-0.01 |
| `t:harsh` | 👊 Жёстче: тон твёрже, без сюсюканий. ~$0.005 |
| `t:friend` | ☺️ Мягче: тон теплее, человечнее. ~$0.005 |
| `t:short` | ✂️ Короче: сократит в 2 раза. ~$0.005 |
| `t:regen` | 🔁 Переформулировать: тот же смысл, другие слова. ~$0.005 |
| `sold` | 💰 Пометит объявление продано, будущие inquiries по нему авто-skip. |
| `clienthist` | 📋 Покажет полную историю переписки с этим клиентом. |

**Без confirmation (direct action):** `editru`, `editde` — потому что они открывают input-режим, где оператор печатает (ошибочно ввести нельзя без сознательного действия), и cancel внутри input-режима тоже доступен.

## Callback names

| Callback | Действие |
|---|---|
| `send:N` | Отправить (без изменений) |
| `skip:N` | Пропустить (без изменений) |
| `editru:N` | Войти в режим Edit RU (новое — заменяет `edit:N`) |
| `editde:N` | Войти в режим Edit DE (новое) |
| `q:fest:N` | Цена fest (без изменений) |
| `price:N` | Войти в режим Своя цена (новое) |
| `instr:N` | Войти в режим Своя инструкция (новое) |
| `t:harsh:N` | Жёстче (без изменений) |
| `t:friend:N` | Мягче (был Friendlier) |
| `t:short:N` | Короче (без изменений) |
| `t:regen:N` | Переформулировать (был Re-gen) |
| `sold:N` | Товар продан (без изменений) |
| `clienthist:N` | История клиента (без изменений) |
| `inputcancel:N` | Отменить любой input-режим (заменяет `editcancel:N`) |

Старые callback names `edit:N` и `editcancel:N` → удалить (фрешный rework).

## State-machine

Заменить:
```python
_PENDING_EDITS: dict[tuple[int, int], tuple[int, int]] = {}
```

На:
```python
# (chat_id, user_id) → {"action": ..., "msg_id": int, "tg_msg_id": int}
# action ∈ {"edit_ru", "edit_de", "price", "instruction"}
_PENDING_INPUTS: dict[tuple[int, int], dict[str, Any]] = {}
```

Per-(chat_id, user_id) — в группе сообщения одного оператора не перехватят сессию другого (как сейчас).

## Поток для каждого input-action

Общий шаблон:
1. Click button → callback handler
2. State stored: `_PENDING_INPUTS[(chat_id, user_id)] = {action, msg_id, tg_msg_id}`
3. Bot **edit_message** (та же карточка): кнопки заменены на `[❌ Отменить]`, в конце текста добавлен:
   - Заголовок «✏️ Жду ... от @actor»
   - Подсказка по копированию
   - `<pre>{current_draft}</pre>` блок (для edit_ru/edit_de — с текущим ru_answer/de_answer; для price/instruction — без блока, только подсказка)
4. Bot **send_message** force_reply stub: «↑ Скопируй и отправь правку реплаем» с `input_field_placeholder` (плейсхолдер в input)
5. Оператор: tap-and-hold pre-блок → Copy → reply-UI уже открыт → paste → правит → send
6. `_on_text` ловит, диспатчит по action:
   - **edit_ru**: `translate_only(text, target_lang=msg.client_lang)` → save `ru_answer=text`, `de_answer=translation`. Lesson сохраняется.
   - **edit_de**: save `de_answer=text` напрямую; `translate_only(text, source_lang=msg.client_lang, target_lang="ru")` → save `ru_answer=back_translation`. Lesson сохраняется.
   - **price**: `re.search(r'\d+', text)` → `regenerate_with_price(msg, price)` → save new draft, status=edited.
   - **instruction**: `regenerate_with_instruction(msg, text)` → save new draft, status=edited.
7. Bot edit_message: восстановить полную клавиатуру + новый draft.
8. Stub-сообщение force_reply удалить (cleanup) — можно опционально, но для аккуратности.

## Cancel

`inputcancel:N` callback:
- pop `_PENDING_INPUTS[(chat_id, user_id)]`
- edit карточки back в normal mode (полная клавиатура восстановлена)

## Новые функции `modules/claude.py`

### `regenerate_with_price(msg_row, price_eur, brief_text, history, lessons) -> dict`

```python
def regenerate_with_price(
    msg_row: Any,
    price_eur: float,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Перегенерить драфт с конкретной ценой от оператора."""
    instruction = (
        f"Согласись на цену ровно {price_eur}€. Преподнеси как лучшее предложение, "
        f"кратко обоснуй (например: «это моё последнее предложение», «учитывая состояние», "
        f"«ради быстрой сделки»). 1-2 предложения. НЕ объясняй математику скидки."
    )
    return _regenerate_core(msg_row, instruction, brief_text, history, lessons)
```

### `regenerate_with_instruction(msg_row, instruction, brief_text, history, lessons) -> dict`

```python
def regenerate_with_instruction(
    msg_row: Any,
    instruction: str,
    brief_text: str = "",
    history: Optional[list[dict[str, Any]]] = None,
    lessons: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Перегенерить драфт по операторской свободной инструкции."""
    full_instruction = (
        f"Инструкция от оператора: {instruction.strip()}\n"
        f"Следуй этой инструкции при перегенерации ответа. Если инструкция противоречит "
        f"общим правилам (например, требует обещать невыполнимое) — следуй здравому смыслу "
        f"и подсветь конфликт в ответе на русском (поле ru_answer)."
    )
    return _regenerate_core(msg_row, full_instruction, brief_text, history, lessons)
```

### `_regenerate_core(msg_row, instruction, ...)` — refactor

Вынести из `regenerate_with_strategy` общий core (prompt building + Claude call + parse). `regenerate_with_strategy` оставить как тонкую обёртку над `_regenerate_core(msg_row, QUICK_STRATEGIES[strategy] or TWEAK_INSTRUCTIONS[strategy], ...)`.

### `translate_only` — расширить

Текущая сигнатура:
```python
def translate_only(text, direction="ru_to_de", target_lang=None) -> dict
```

Новая:
```python
def translate_only(text, direction="ru_to_de", target_lang=None, source_lang=None) -> dict
```

Если `source_lang` задан — используется для prompt; иначе fallback на текущую логику. Нужно для back-translate из произвольного client_lang в RU при Edit DE.

## Telegram-bot изменения

Файл `modules/telegram_bot.py`:

- `_review_keyboard` — новая раскладка (7 рядов × 2 столбца).
- `_review_keyboard_obj` — синхронизировать.
- `_PENDING_EDITS` → `_PENDING_INPUTS` (новая структура).
- `_cancel_keyboard` → `_input_cancel_keyboard` (callback `inputcancel:N`).
- `_on_callback` — новые actions: `editru`, `editde`, `price`, `instr`, `inputcancel`. Удалить старые `edit`, `editcancel`. Переименовать `t:friend` оставить как есть в коде (только UI label поменять).
- `_on_text` — диспатч по `action` из `_PENDING_INPUTS`. Реализовать каждую ветку.
- Новый helper `_format_input_prompt(msg, action, draft_text)` — форматирует addendum к карточке для input-режима.
- Новый helper `_send_force_reply_stub(chat_id, reply_to_msg_id, action, placeholder)` — посылает stub-сообщение с force_reply.

## Edge cases

| Случай | Поведение |
|---|---|
| Оператор ввёл «не цифру» в режиме Своя цена | `re.search(\\d+)` миссит → бот шлёт «❌ не нашёл цены, отменяю» + сбрасывает state, восстанавливает карточку |
| Оператор ввёл цену вне разумных границ (0, 999999) | warning «странная цена X €, всё равно регенерирую» — продолжаем, оператор сам решит |
| Edit DE: client_lang = 'ru' (клиент пишет на русском) | back-translate → пропускаем (текст и так RU); save `ru_answer=text`, `de_answer=text` |
| Reprocess во время input-сессии | callback всё равно сработает на актуальный draft из БД (re-fetch перед регенерацией) |
| Сессия залипла (оператор не дописал) | TTL не делаем — пользователь нажмёт `❌ Отменить` или начнёт другое действие |
| Force_reply stub-сообщение остаётся в чате после обработки | Удаляем после успешной обработки (опционально) |

## План тестирования

1. **Раскладка** — отправить тестовую карточку в TG, проверить на iOS/Android/Desktop что все надписи помещаются.
2. **Edit RU** — клик → pre-блок виден → tap-and-hold копирует → reply-input открыт → отправляю правку → карточка обновлена с новым ru_answer + переведённым de_answer.
3. **Edit DE** — то же, но текст печатаю на немецком → сохраняется как de_answer + ru_answer = back-translation.
4. **Своя цена** — клик → ввожу «2300» → Claude регенерит с этой ценой → карточка обновлена.
5. **Своя цена ошибка** — ввожу «не цифры» → бот ошибка + сброс state.
6. **Своя инструкция** — клик → ввожу «ответь жёстче и упомяни самовывоз» → регенерация.
7. **Cancel** — в любом input-режиме клик `❌ Отменить` → state сброшен, полная клавиатура восстановлена.
8. **Group-mode** — два юзера в одном чате одновременно жмут разные input-кнопки → не перехватывают друг друга (per-user state).
9. **Сообщения от других callback кнопок (send/skip/sold/etc)** — работают без изменений.
10. **Уроки сохраняются** при ручной правке (RU и DE).

## Список затронутых файлов

| Файл | Изменения |
|---|---|
| `modules/telegram_bot.py` | Новая раскладка, новые callbacks, state-machine, dispatch |
| `modules/claude.py` | `regenerate_with_price`, `regenerate_with_instruction`, `_regenerate_core` (refactor), `translate_only` (+source_lang) |
| `scheduler.py` | `regenerate_draft` — добавить варианты для price/instruction (или новые wrapper-ы `regenerate_price`, `regenerate_instruction`) |

Веб-морда не трогается.

## Цена изменений

- Refactor + UI cleanup: бесплатно.
- Edit DE back-translate: +$0.001 за правку.
- Своя цена / Своя инструкция: +$0.005-0.015 за регенерацию (как у любой регенерации).
