# CRM: Профиль клиента в Mini App

**Дата:** 2026-06-25
**Маршрут:** `#/client/<email>`
**Заменяет:** `web-app/js/screens/history.js` (минимальный список тредов)

## Цель

Превратить страницу клиента из простого списка тредов в полноценный CRM-профиль:
- глобальный итог по клиенту (обращения, продажи, сумма)
- теги + заметка оператора
- deal_brief по каждому треду
- кнопка «Написать» → последний активный тред

## Данные

### Новая таблица `client_profiles`

```sql
CREATE TABLE IF NOT EXISTS client_profiles (
    buyer_email TEXT PRIMARY KEY,
    tags_json   TEXT NOT NULL DEFAULT '[]',  -- JSON array: ["Серьёзный", ...]
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Создаётся в `database._ensure_schema` через `CREATE TABLE IF NOT EXISTS`.

### Расширение `list_threads_for_client` (`modules/db_threads.py`)

Добавить в SELECT подзапрос для `deal_brief_json` — из последнего сообщения треда где поле не NULL:

```sql
(SELECT deal_brief_json FROM messages s
 WHERE s.gmail_thread_id = m.gmail_thread_id
   AND s.deal_brief_json IS NOT NULL AND s.deal_brief_json != ''
 ORDER BY s.id DESC LIMIT 1) AS deal_brief_json
```

Сортировка остаётся `ORDER BY last_at DESC` (свежие сверху).

### Новые DB-функции в `modules/db_threads.py`

```python
def get_client_profile(buyer_email: str) -> sqlite3.Row | None
def upsert_client_profile(buyer_email: str, tags: list[str], note: str) -> None
```

## API

### `GET /api/ma/clients/{email}/history` — расширение

Добавить в ответ:
```json
{
  "buyer_email": "...",
  "display_name": "Иван Петров",
  "total_cost_usd": 0.042,
  "tags": ["Серьёзный"],
  "note": "Приедет в пятницу",
  "sold_count": 1,
  "total_negotiated_eur": 750,
  "last_active_thread_id": "thread-xyz",
  "threads": [
    {
      "thread_id": "...",
      "ad_title": "Seat Peugeot",
      "ad_price": "800€",
      "msg_count": 5,
      "last_at": "2026-06-24T10:00:00",
      "last_status": "sent",
      "deal_brief": {
        "summary_ru": "Договорились на 750€",
        "negotiated_price_eur": 750,
        "client_assessment": "серьёзный покупатель"
      }
    }
  ]
}
```

**`last_active_thread_id`** — последний тред по `last_at` со статусом не в `{skipped, skipped_sold, archived}`. Если таких нет — `null`.

**`sold_count`** — количество тредов с `deal_brief.negotiated_price_eur > 0` и статусом `skipped_sold`.

**`total_negotiated_eur`** — сумма `deal_brief.negotiated_price_eur` по тредам со статусом `skipped_sold`.

**`display_name`** — из последнего сообщения клиента с непустым `buyer_display_name`.

### `POST /api/ma/clients/{email}/profile`

```json
{ "tags": ["Серьёзный", "Торгуется"], "note": "текст заметки" }
```

Вызывает `db.upsert_client_profile`. Возвращает `{"ok": true}`.

Валидация тегов: разрешённые значения `["Серьёзный", "Торгуется", "Тянет время", "Мошенник"]`. Остальные игнорируются.

## Frontend

### Файл: `web-app/js/screens/client.js` (новый, заменяет `history.js`)

Регистрируется в `router.js` вместо `history.js`:
```js
import * as client from "./screens/client.js?v=...";
// { pattern: /^#\/client\/(.+)$/, screen: client, ... }
```

`history.js` удаляется.

### Макет экрана

```
👤 Иван Петров
ivan@gmail.com

3 обращ.   1 продажа   750€ итог

[ Серьёзный ]  [ Торгуется ]  [ Тянет время ]  [ Мошенник ]
(Bootstrap btn-outline-secondary, btn-sm, toggle — активный = btn-secondary)

┌──────────────────────────────────────────────┐
│ Заметка оператора...                         │
└──────────────────────────────────────────────┘
                                    [💾 Сохранить]

[✉️ Написать]   (скрыт если last_active_thread_id == null)

──── Переписки ────

Seat Peugeot · 800€               отправлен  2д назад
Договорились на 750€ · серьёзный покупатель

Sitzbank · 1000€                   пропущен  5д назад
```

### Локализация статусов

| raw | отображение |
|---|---|
| `sent` / `sent_debug` | отправлен |
| `pending` / `new` / `edited` / `approved` | ждёт |
| `skipped` | пропущен |
| `skipped_sold` | продан |
| `archived` | архив |
| прочее | статус |

### Кнопка «Написать»

`href="#/thread/<last_active_thread_id>"` — навигация на экран треда, где оператор использует существующий compose.

### Deal brief под тредом

Показывается только если `deal_brief` непустой. Формат:
```
<summary_ru> · <client_assessment>
```
Если `negotiated_price_eur` есть — добавляется `· <price>€`.

## Тесты

В `tests/test_db_threads.py`:
- `test_client_profile_upsert` — create + update, проверить tags и note
- `test_list_threads_includes_deal_brief` — deal_brief_json подтягивается из последнего сообщения
- `test_last_active_thread_id_logic` — скрытие при all-skipped, корректный выбор при mixed статусах

В `tests/test_api_ma_pipeline.py`:
- `test_client_history_returns_profile` — tags/note/aggregates присутствуют
- `test_client_profile_post` — сохранение + повторное чтение

## Что не меняется

- Маршрут `#/client/<email>` — тот же
- `screens/clients.js` (список клиентов) — не трогаем
- `backbar.js` — `showBack` уже вызывается для `/client/` маршрута в router.js
