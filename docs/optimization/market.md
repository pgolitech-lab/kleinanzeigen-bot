# Модуль «Рынок-советник» (сессия market, ветка opt/market)

Ценовой советник, позиция наших объявлений в выдаче, подсказки перепоста с готовым текстом,
советы по заметности и предупреждение о дублях между аккаунтами. **Перепост делает человек**:
автопостинга и логина нет (риск бана). На kleinanzeigen.de — только публичный поиск, как у scout.

## Файлы
| Файл | Что |
|---|---|
| `modules/db_market.py` | таблицы `market_*`, миграция `ensure_market_schema(conn)` (+ ленивая `ensure_schema()`) |
| `modules/market_advisor.py` | логика и функции для api / бота |
| `modules/scout.py` | правка: строки выдачи несут `page` / `pos` / `is_top`; `parse_top_flags(html)`; после каждого part-запроса `_record_market` пишет прогон в `market_*` (ошибка — только `logger.exception`, прогон scout не падает) |
| `scripts/market_report.py` | отчёт по копии БД: `python3 scripts/market_report.py --db <путь> [--limit 10] [--llm] [--out f.md]` — источник открывается read-only и копируется во временный файл |
| `tests/test_market_advisor.py` | 36 тестов |

## Таблицы (`ensure_market_schema`)
| Таблица | Что |
|---|---|
| `market_price_history` | ad_id, price_eur, price_raw, seen_at — строка только при **изменении** цены (scout upsert цену перезаписывает) |
| `market_runs` | прогон одного запроса: source scout/market, keywords, pages, cards, error. `pages=0` — запрос не загрузился (не путать с «нас нет в выдаче») |
| `market_serp` | место каждого объявления в прогоне: page, pos (сквозное с 1), organic_pos (без платных TOP), is_top, title, price, plz, city. Хранится 120 дней |
| `market_queries` | наши поисковые запросы для позиций («sitzbank traveller», «tisch traveller») — заводятся автоматически по живым товарам, если scout такой не ищет; операторские не перезаписываются |
| `market_ad_features` | признаки объявления рынка (категория, кожа, VIP, мест, состояние, модели) — Haiku один раз на объявление, кэш по хэшу текста; при ошибке Haiku — regex (`method='regex'`), позже повтор |
| `market_llm_cache` | кэш LLM-советов (заметность — 14 дн., тексты перепоста — 7 дн.) |

## Определения
- **Наше объявление**: ad_id из `messages` / `ad_briefs` / `inv_listings`, **или** объявление рынка с нашим **PLZ**
  (не городом: в Köln/Aachen полно конкурентов; PLZ берутся из выдачи по нашим ad_id + настройка `market_own_plz` CSV)
  с тем же `match_key` заголовка или ядром заголовка, совпадающим ≥ 80 %. Из рынка такие объявления выкидываются;
  в дублях они идут как `uncertain` и одни «опасно» не дают.
- **Товар**: со складом (inv_* в БД и `modules/inventory` в main) — `inventory.list_product_profiles()` (себестоимость,
  остаток, min); без склада — группа наших объявлений с одинаковым `match_key`. `item_id` = `p:<product_id>` | `k:<hash>`.
- **Живое объявление**: `seen` — видели в выдаче за 3 дня; `probable` — письма/склад за 21 день и не «Gelöscht»/продано;
  `gone`; `unknown`.
- **Позиция**: выдача по умолчанию — «Neueste» (проверено 20.10 на живой странице), т. е. место = свежесть; перепост
  поднимает наверх. Статусы: `top` (стр. 1, ≤ 10), `page1` (стр. 1, ≤ 15), `sinking` (ниже), `missing` (полный прогон
  ≥ 3 стр. или выдача кончилась — а нас нет), `stale` (последний замер старше 2 дн.), `no_data`. Запрос относится к
  товару, если все его слова есть в заголовке. Платные TOP не считаются в `organic_pos`; если флаг TOP распознан
  < 90 % карточек — `organic_pos=None` и ERROR в лог (смена вёрстки).
- **Сопоставимые для цены**: запчасти рынка (verified_kind или kind = part), не наши, цена ≥ 20 €, видели за 60 дней,
  та же категория, совпадают «мест», VIP, кожа/не кожа (если известно), дефект. Меньше 3 — «мало данных», цифр нет.
- **«Минимум»** = max(себестоимость × (1 + `market_min_margin_pct`/100, по умолчанию 20 %), P25).
- **Окно перепоста**: вс–ср 16–19 ч по Берлину (запросы за 6 мес., ideas.md п. 1.4).

## Функции для api / бота (`from modules import market_advisor as ma`)
Все возвращают JSON-сериализуемые dict/list; полный формат — в docstring каждой.

| Вызов | Для чего | Ключевые поля ответа |
|---|---|---|
| `ma.advise_price(product_id=None, ad_id=None, title=None, price_eur=None, description=None, item_id=None)` | блок «Рынок» в карточке товара/треда, предзаполнение min | `status` ok/few_data/not_found, `p25`/`median`/`p75`, `recommended_eur`, `min_eur`+`min_basis`, `delta_pct`, `verdict` above/below/in_market, `verdict_ru`, `similar[≤5]` {ad_id,title,price_eur,city,url,active,price_change}, `n`, `excluded_own` |
| `ma.rank_report(product_id=None, ad_id=None, item_id=None, days=14)` | экран «Позиции» | `items[]` {item_id, title, status, status_ru, best{keywords,page,pos,organic_pos,ad_id,at}, queries[{keywords, latest, trend, history[]}], days_quiet, live_ads}, `checked_at`, `last_error` {at,keywords,error}\|None |
| `ma.repost_suggestions(limit=5, with_text=True, use_llm=True)` | утро / «Склад» / экран «Перепост» | `[]` {item_id, title, priority, action repost/remove_duplicates, reasons_ru[], old_ad, window{now,start,end,label_ru}, text{title,description,price_eur,changes_ru,source}, price{current,suggested,advice_ru}, duplicates, steps_ru[]} |
| `ma.visibility_tips(product_id=None, ad_id=None, item_id=None, use_llm=True)` | кнопка «Как стать заметнее» | `missing_terms[]` {term, share, is_model, in_our_description}, `leaders.above[]`/`leaders.fast[]`, `title_len`, `leader_first_lines[]`, `suggestions{titles[], first_lines[], notes_ru[], source llm/cache/rules}`, `warning` |
| `ma.duplicates_report()` | предупреждение о дублях (бот + экран) | `groups[]` {item_id, title, level danger/warn, score, n_live, accounts[], reasons_ru[], keep, remove[], message_ru «опасно: снимите 2 из 3 — …», uncertain[]}, `danger`, `warn`, `ads_to_remove` |
| `ma.summary_lines()` | 0–3 строки в утреннюю сводку / итог дня | `list[str]` (без HTML — экранировать на стороне бота); первой строкой — «Рынок: данные устарели…», если так |
| `ma.data_health()` | плашка «данные устарели» на экранах | `str` (что устарело) или `None` |
| `ma.own_items()` | список товаров для выбора на экране | `[]` {item_id, product_id, title, category, price_eur, min_price_eur, unit_cost_eur, stock_qty, live_ads, ads[]} |

LLM: `visibility_tips` и `repost_text` — Sonnet через `claude_utils.call_json`, кэш; признаки — Haiku пачками по 10.
При недоступности LLM — варианты по правилам и `warning`/`source='rules'`, ошибка в лог WARNING (все пачки Haiku
мимо — ERROR). **`text.warning` показывать оператору всегда** (нет описания, текст почти как старый, LLM недоступен).
Тексты по правилам ничего не выдумывают: добавляются только слова, которые есть в нашем же заголовке/описании.

## Что нужно от других модулей (точные вызовы)
**db** (`database.py`): в конце `init_db()` —
`from modules.db_market import ensure_market_schema` → `ensure_market_schema(conn)` (без этого таблицы создаются лениво
при первом part-прогоне scout). Миграция только добавляет таблицы `market_*` (и колонку `market_runs.top_known`);
перед первым рестартом с ней — бэкап БД по правилу AGENTS.md.

**mail / планировщик** (`scheduler.py`):
- джоб `market_rank` — `market_advisor.market_rank_job()` по cron **07:30 и 14:30 Europe/Berlin** (2 раза в сутки, до вечернего
  окна). Сам внутри не чаще раза в 10 ч, ≤ 12 запросов × ≤ 3 стр., пауза 4–8 с — ≈ 70 страниц в сутки. Выключатель —
  настройка `market_rank_enabled` (`1` по умолчанию). Ошибки прогона — `logger.error` → попадут в hourly-монитор.
- джоб `market_features` — `market_advisor.market_features_job()` каждые 6 ч (после scout), ≤ 60 объявлений за запуск.

**api** (`web/api_ma.py`), все под `verify_init_data_dep`:
- `GET /api/ma/market/items` → `ma.own_items()` (без `ads[].description`)
- `GET /api/ma/market/price?item_id=&product_id=&ad_id=` → `ma.advise_price(...)`
- `GET /api/ma/market/rank?item_id=&days=14` → `ma.rank_report(...)`
- `GET /api/ma/market/repost?limit=5` → `ma.repost_suggestions(limit)` (LLM-текст может занять до ~20 с на товар при
  холодном кэше — вызывать в threadpool; для сводки бота `with_text=False`)
- `GET /api/ma/market/visibility?item_id=` → `ma.visibility_tips(...)`
- `GET /api/ma/market/duplicates` → `ma.duplicates_report()`
- бот, утренняя сводка: `lines = ma.summary_lines()` — добавить строки как есть (экранировать `_html`).
- настройки в whitelist `/api/ma/settings`: `market_rank_enabled` (0/1), `market_min_margin_pct` (число), `market_own_plz` (CSV).

**webapp**: блок «Рынок» в карточке товара/треда — `verdict_ru`, диапазон P25–P75, «выставить за» / «минимум», 3–5
ссылок `similar`; экран «Перепост» — карточки с кнопками «Скопировать заголовок / текст / цену», окно `window.label_ru`,
шаги `steps_ru`; плашка «Дубли: опасно» с `message_ru` и списком `remove` (ссылки). Фото не сравниваем — в выдаче их нет.

**stock**: `inventory.list_product_profiles()` / `product_profile` (контракт v1) уже используются, если модуль в main.
Предложение: `min_price_eur` товара предзаполнять из `advise_price(product_id)["min_eur"]`, если пусто.

## Ограничения
- Позиции появятся только после первых прогонов (scout part-запросы + `market_rank`): до этого `rank_report` → `no_data`,
  перепост — только по сигналу «нет писем 5+ дней».
- `run_rank_check` добавляет в `scout_listings` только **новые** для scout объявления (уже известные не трогает); такие
  новые не попадут в дайджест scout «новые объявления».
- Снимки выдачи чистятся сами (раз в сутки при записи, > 120 дней) — DELETE только по своим таблицам `market_*`.
- «Быстро ушло» (≤ 7 дней в выдаче) ≠ «продано»: перепосты конкурентов (тот же заголовок + PLZ) отсекаются, остальное — прокси.
- Фото для дублей не сравниваются; аккаунт объявления без писем угадывается по PLZ нашего объявления с известным аккаунтом.
