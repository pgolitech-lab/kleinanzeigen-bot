# Модуль «Склад» (сессия stock, ветка opt/stock)

## Зачем
Раньше всё держалось на `ad_id`: `ad_briefs.sold_at`, «Gelöscht/Reserviert» в промптах, авто-пропуск
писем и остановка автопилотов при продаже. Но у владельца один товар живёт во многих объявлениях
(удаляет и перепощивает ради поднятия, держит одно и то же на 2 аккаунтах), а «продано» — это
одна штука из запаса. Склад отделяет **товар** от **объявления**.

## Модель (modules/db_inventory.py, миграция `ensure_inventory_schema(conn)`)
| Таблица | Что | Ключевые поля |
|---|---|---|
| `inv_products` | товар | title, category, condition, unit_cost_eur (оценка себестоимости), list_price_eur, min_price_eur, note, match_key, **stock_tracked** (0 — остаток не внесён), archived_at, merged_into |
| `inv_listings` | объявление → товар | ad_id PK, product_id, account_id, title (чистый), price_eur, state active/deleted/reserved, link_method exact/normalized/llm/operator/new/seed, first/last_seen_at |
| `inv_movements` | движения | kind receipt/sale/writeoff/adjust, qty со знаком, unit_cost_eur (приход), unit_price_eur (продажа), source thread/detected/manual/operator/seed, gmail_thread_id (**одна продажа на тред** — уникальный индекс), occurred_at, voided_at |
| `inv_link_questions` | «это тот же товар?» | ad_id, product_id (где сейчас), suggested_product_id, score, reason, resolution same/different |

Остаток = `SUM(qty)` невоидированных движений. Пока `stock_tracked=0` остаток **неизвестен** — из
этого нельзя делать вывод «нет в наличии». Приход/инвентаризация включают учёт.

## Логика (modules/inventory.py)
- **Заголовки**: из `messages.ad_title` убираются «Nutzer-Anfrage zu deiner Anzeige "…"», «Gelöscht •»,
  «Reserviert •», промо «… bis zu 25 % schneller verkaufen*»; темы рассылок без цены/описания — мусор.
- **Автопривязка** `link_listing`: тот же нормализованный заголовок (порядок слов, пробелы, Citroën/Citroen,
  «3-er»/«3er», кириллические «а») → тот же товар. Похожее (та же категория, ядро заголовка без моделей
  авто + цена, score ≥ 0.55) → Haiku (`use_llm=True`) привязывает только при `confidence=high`, иначе
  новый товар + вопрос оператору. Ошибка Haiku → вопрос оператору (WARNING в лог).
- **Продажи**: `record_sale` = −qty, идемпотентно по треду. `sync_sales` переносит все продажи из
  `messages` (status `skipped_sold` с ценой) и воидит те, что оператор откатил.
- **Отчёт** `report(from, to)`: FIFO по приходам — остаток в шт. и по себестоимости, ожидаемая
  выручка/прибыль по цене объявления и по минимуму, продажи за период (выручка, себестоимость,
  прибыль), средний срок до продажи, возраст старейшей единицы, «залежалось > 60 дн.», минусовые
  остатки, открытые вопросы, продажи без товара. Неизвестные деньги = `None`, не 0.
- `summary_text()` — 3–6 строк для ежедневной сводки.

## Начальное заполнение (scripts/inventory_seed.py)
Dry-run на копии прод-БД (20.10): **182** объявления с названием (12 ad_id — рассылки, не заведены) →
**126 товаров**, из них 30 перепощенных; **45 вопросов** «тот же товар?»; 47 исторических продаж
перенесены движениями, **6 ручных продаж без товара** (свободный текст: «2+1 старые 3 шт и 2+1 новые
2 шт», «Поляк Peter» …). Запуск на проде — после ответа владельца на вопросы 4–5 ниже:
`python3 scripts/inventory_seed.py --apply` (бэкап БД перед этим).

## Что нужно от других модулей (точные вызовы)
**db** (`database.py`, `modules/db_sales.py`)
- `init_db()`: `from modules.db_inventory import ensure_inventory_schema` → `ensure_inventory_schema(conn)`
  в конце миграций (склад и без этого создаёт таблицы лениво, но так надёжнее).
- `mark_thread_sold(...)`: после транзакции → `inventory.record_sale(price_eur=sold_price_eur,
  gmail_thread_id=thread_id, ad_id=ad_id, message_id=msg_id, source="thread")`, вернуть его
  `qty_after`/`stock_tracked` наверх. Ошибка склада не должна откатывать продажу (`logger.exception`) —
  джоб `sync_all` догонит.
- `close_other_threads_for_ad`: закрывать другие треды только если после продажи
  `inventory.ad_context(ad_id)["stock"] == "out_of_stock"` (иначе товар ещё есть).
- `mark_ad_sold` / `is_ad_sold` / `ad_briefs.sold_at`: больше не источник правды; 3 вечные отметки
  (3391997887, 3378070882, 3398862667) снять.

**mail** (`scheduler.py`, `modules/incoming.py`)
- Джоб `inventory_sync` каждые 30 мин + при старте: `inventory.sync_all()` (без LLM).
- После сохранения входящего с `ad_id` (и после `parse_ad`):
  `inventory.link_listing(ad_id, title=ad_title, price=ad_price, account_id=account["id"], url=ad_url,
  description=ad_description, seen_at=created_at)` в `try/except: logger.exception(...)`.
- Убрать авто-пропуск `if ad_id and db.is_ad_sold(ad_id)` (`incoming.py:886`). Вместо него:
  `ctx = inventory.ad_context_safe(ad_id)`; письмо обрабатывается всегда; при `ctx["stock"] ==
  "out_of_stock"` — пометка в уведомлении оператору «по складу 0 шт.».
- `_autopilot_dispatch`: при `out_of_stock` — не автоответ, а `send_for_review`.

**llm** (`modules/claude.py`, автопилот)
- `ad_state_for(msg_row)`: `raw = "sold" if ad_briefs.sold_at else detect_ad_state(свежий title)`;
  вернуть `inventory.effective_ad_state(ad_id, raw)` → `'' | 'deleted' | 'reserved' | 'out_of_stock'`.
  Для `out_of_stock` текст в промпте: «по учёту товара нет — не обещай наличие, скажи, что уточнишь».
  Слова «продано» в промпте больше нет (отметка объявления ≠ товар).
- В системный промпт черновиков и автопилота добавить `inventory.prompt_note(ad_id)` (пустая строка —
  ничего не добавлять). Там же можно взять `ad_context_safe(ad_id)["min_price_eur"]` как floor.
- Автопилот не должен сам говорить «продано / нет в наличии», пока `stock != "out_of_stock"`.

**api** (`web/api_ma.py`) — эндпоинты `/api/ma/stock/*` (все с `verify_init_data_dep`, `def`):
| Метод | Путь | Вызов |
|---|---|---|
| GET | `/stock/report?from&to` | `inventory.report(from, to)` |
| GET | `/stock/products` | `db_inventory.list_products()` |
| GET | `/stock/products/{id}` | `get_product` + `list_listings(id)` + `list_movements(id)` + `ad_context` |
| POST | `/stock/products` | `db_inventory.create_product(title=…, category, condition, unit_cost_eur, list_price_eur, min_price_eur, note)` |
| PATCH | `/stock/products/{id}` | `db_inventory.update_product(id, **fields)` |
| POST | `/stock/products/{id}/receipt` | `inventory.receive(id, qty, unit_cost_eur, occurred_at=, note=, created_by=actor)` |
| POST | `/stock/products/{id}/writeoff` | `inventory.write_off(id, qty, note=, created_by=actor)` |
| POST | `/stock/products/{id}/count` | `inventory.set_stock(id, qty, created_by=actor)` |
| POST | `/stock/products/{id}/archive` | `db_inventory.archive_product(id, archived)` |
| POST | `/stock/merge` | `db_inventory.merge_products(src_id, dst_id)` |
| POST | `/stock/listings/{ad_id}/relink` | `db_inventory.relink_listing(ad_id, product_id)` |
| GET | `/stock/questions` | `db_inventory.list_open_questions()` |
| POST | `/stock/questions/{id}` | `inventory.answer_question(id, same: bool)` |
| GET | `/stock/unlinked-sales` | `inventory.unlinked_sales()` |
| POST | `/stock/unlinked-sales/{thread_id}` | `inventory.record_sale(product_id=, gmail_thread_id=thread_id, price_eur=, qty=, source="thread")` |
- `POST /threads/{id}/sold` (и manual-sale, apply detection): в ответе `stock: {qty_after, stock_tracked}`
  из `record_sale`. «Сегодня продано» в Обзоре — через `list_sales`, как и сейчас.
- `ValueError` из склада → 400 с текстом.

**webapp** — экран «Склад»: список товаров (остаток / «не внесён», цена, себестоимость, висит дн.,
залежалые сверху), карточка товара (объявления, движения, кнопки Приход / Списание / Инвентаризация /
Изменить / Слить), очередь «тот же товар?» (два заголовка + цены, Да / Нет), «Продажи без товара».
В шите «Продано»: «На складе останется N» и выбор количества. Строка склада в Обзоре.

**ops** — в ежедневную сводку `inventory.summary_text()`.

## Вопросы владельцу (по одному на пункт)
1. Как сейчас ведётся учёт закупки — тетрадь, таблица, нигде? Можно ли её выгрузить?
2. Вносить себестоимость задним числом для уже проданного (появится прибыль за прошлые месяцы) или
   считать только с сегодняшнего дня?
3. Себестоимость детали с разобранной машины: делить цену машины по деталям (как?) или ставить
   оценку на каждую позицию вручную?
4. Скамья 2+1 «для Traveller» и «для SpaceTourer/ProAce/Zafira» — это одна позиция склада (подходит
   ко всем, просто разные заголовки) или разные товары? От ответа зависят 45 сомнительных пар.
5. Одинаковое объявление на двух аккаунтах одновременно — это одна и та же вещь или два экземпляра?
6. Бывают ли комплекты (Komplettsatz: скамья + сиденья), которые продаются и целиком, и по частям?
   Списывать ли при продаже комплекта составляющие?
7. Сколько сейчас реально лежит по основным позициям (скамьи 2+1, одиночные/VIP, водитель+пассажир,
   столы, фаркопы, …)? — для первой инвентаризации.
8. Минимальная цена — на каждый товар отдельно или общий процент от цены объявления?
9. Когда остаток стал 0, а пишут по старому объявлению: отвечать «уточню», вежливо отказывать сразу
   или пропускать письмо?
10. Объявления не про запчасти (гараж в аренду, дом, шкаф, личные вещи) — вести на складе или скрыть?
