# Базовый прогон QA — 2026-10-20 (до слияний)

Ветка `opt/qa` = main `30fb4f4`. Тестировщики: основная сессия (харнесс по устройствам), агенты `qa` (тесты и сервис),
`ux-lazy` (сценарии ленивого оператора). Код приложения не правился, письма не отправлялись, режим отправки не трогали.

## Итог в 5 строк
1. **pytest 390/390 зелёные**, сервис живой, JS-ошибок и горизонтальной прокрутки на 7 размерах экранов — 0.
2. **Алерты об ошибках в Telegram не работают вообще** (`monitor_errors_job` фильтрует `-p warning`, а все строки идут с
   priority info) — 36 ERROR за неделю прошли незамеченными. Чинить первым (kab-ops / kab-mail).
3. **Отправка не туда / ложное «готово»** в Mini App: шит отправки остаётся открытым поверх другого экрана (B-01), черновик
   в ленте выглядит как отправленный ответ (B-02).
4. **Набранный текст теряется молча** при «←», системном «назад» и свайпе (B-03, B-04) — на всех устройствах.
5. Внешние экраны: на 344 px от названия объявления во Входящих остаются 1–2 буквы (B-09); на Flex Window и 280 px поле
   ввода закрыто доком и таб-баром при открытой клавиатуре (B-17, B-18).

## Как гоняли
- **Данные — синтетика**, не прод: доступ к валидному initData (токен бота) и копии прод-БД закрыт политикой безопасности
  сессии. `docs/qa/harness/seed.py` строит БД (5 аккаунтов, треды DE/FR/PL, черновики, отправленные, закреплённый,
  детекция продажи); `serve.py` поднимает тот же FastAPI-app на ней (без планировщика, LLM отключён, фейковый токен).
- **Браузер**: `docs/qa/harness/run.py` — headless Chromium (один), Telegram SDK подменён заглушкой с валидным initData;
  GET → прокси на QA-бэкенд, **любой не-GET → фейк 200** и запись в отчёт. Повтор: `python3 docs/qa/harness/run.py <out>`,
  сравнение: `python3 docs/qa/harness/report.py base.json new.json`.
- **Клавиатура** эмулируется урезанием высоты вьюпорта до 55 % при фокусе в поле (реальная экранная клавиатура Samsung
  может быть выше) — результаты B-17/B-18 скорее оптимистичны.
- Сырые результаты: `docs/qa/baseline-results.json`; скриншоты: `docs/qa/shots/<устройство>/<экран>.png`.

### Устройства (CSS-пиксели вьюпорта Mini App)
| id | Устройство | Вьюпорт | Откуда |
|---|---|---|---|
| fold-cover-412 | Z Fold 8 Ultra, внешний 6,5″ 1080×2520 | 412×860 | 1080/2,625; высота минус статус-бар и шапка Telegram |
| fold-cover-344 | тот же, крупный масштаб интерфейса | 344×760 | |
| fold-cover-280 | худший случай | 280×620 | |
| fold-inner-port | Z Fold 8 Ultra, внутренний 8″ 2256×2504, портрет | 860×880 | 2256/2,625 |
| fold-inner-land | то же, ландшафт | 954×780 | |
| flip5-main | Z Flip 5, основной 6,7″ 1080×2640 | 412×915 | |
| flip5-flex | Z Flip 5, Flex Window 3,4″ 720×748 | 306×318 | 720/2,35 |

Размеры Fold 8 Ultra — по предрелизным/магазинным спецификациям (2504×2256 внутренний, 2520×1080 внешний), Flip 5 Flex Window
720×748 — официальная спецификация Samsung. Точный DPR на устройствах не замерен — при первой возможности снять
`innerWidth×innerHeight` на живом телефоне (открыть MA, экран Настройки) и поправить таблицу.

## Сервис и тесты (агент `qa`)
| Проверка | Итог | Что упало / замечания |
|---|---|---|
| `pytest -q` | **OK: 390 passed**, 37 с | 140 `DeprecationWarning: utcnow()`. В CLAUDE.md «201 тест» — устарело |
| `systemctl is-active` | bot active, cloudflared active, tunnel-url-sync inactive (oneshot, success) | — |
| `/health` | OK, `polluted_incoming: 0` | — |
| Подписка Claude | loggedIn, claude.ai | — |
| Журнал 24 ч: ERROR / Traceback | 0 / 0 | рестарты 10:19, 10:25, 11:15 |
| Таймауты IMAP 24 ч | WARN: 35, все на попытке 1/2 | `fetch_new` 20, `find_orphan_seen_uids` 15 |
| «maximum number of running instances» 24 ч | **FAIL: 2130**, только `poll_all_accounts` | интервал 15 с, цикл: медиана 10 с, p90 33 с, макс 215 с; ~15 % журнала — этот шум |
| LLM через подписку | OK, выборка 4 вызова | Sonnet 1,6–1,9 с, Haiku 1,9–2,7 с |
| Журнал 7 дней | 36 ERROR / 6 Traceback | 27× DNS при IMAP (15.10 ~05:00), 6× orphan-recovery, 1× `AUTHENTICATIONFAILED` 13.10 |
| **monitor_errors_job** | **FAIL: слепой** | `journalctl -p warning` → 0 строк, т.к. все строки priority 6 → алертов нет, сторож `polluted_incoming` тоже не работает |
| Логи отправок в Telegram | **нет** | ни send_for_review, ни сводки/напоминания не пишут «отправлено» — шум бота по журналу не посчитать |

## Mini App по устройствам

Формат ячейки: горизонтальная прокрутка px / элементов за правым краем / тап-целей < 44 px / элементов с обрезанным текстом.
Обрезка в списках по дизайну (многоточие) — считается, но не вся является багом; см. B-09.

| экран | fold-cover-412 | fold-cover-344 | fold-cover-280 | fold-inner-port | fold-inner-land | flip5-main | flip5-flex |
|---|---|---|---|---|---|---|---|
| pipeline | 0/0/12/10 | 0/0/12/12 | **0/1/12/6** | 0/0/12/0 | 0/0/12/0 | 0/0/12/10 | **0/1/12/9** |
| dashboard | 0/0/2/8 | 0/0/2/10 | 0/0/2/5 | 0/0/2/0 | 0/0/2/0 | 0/0/2/8 | 0/0/2/7 |
| clients | 0/0/0/6 | 0/0/0/6 | 0/0/0/6 | 0/0/0/0 | 0/0/0/0 | 0/0/0/6 | 0/0/0/6 |
| sales | 0/0/5/0 | 0/0/5/0 | 0/0/3/0 | 0/0/9/0 | 0/0/9/0 | 0/0/5/0 | 0/0/3/0 |
| scout | 0/0/5/0 | 0/0/1/0 | 0/0/1/0 | 0/0/5/0 | 0/0/5/0 | 0/0/5/0 | 0/0/1/0 |
| detected | 0/0/5/0 | 0/0/2/0 | 0/0/2/0 | 0/0/5/0 | 0/0/5/0 | 0/0/5/0 | 0/0/2/0 |
| settings | 0/0/25/0 | 0/0/25/0 | 0/0/25/0 | 0/0/25/0 | 0/0/25/0 | 0/0/25/0 | 0/0/25/0 |
| thread-draft | 0/0/8/2 | 0/0/2/2 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/2 | 0/0/2/2 |
| thread-long | 0/0/8/1 | 0/0/2/1 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/1 | 0/0/2/1 |
| thread-pl | 0/0/8/1 | 0/0/2/1 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/1 | 0/0/2/2 |
| thread-new | 0/0/8/1 | 0/0/2/1 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/1 | 0/0/2/2 |
| thread-answered | 0/0/2/1 | 0/0/2/1 | 0/0/2/2 | 0/0/5/0 | 0/0/5/0 | 0/0/2/1 | 0/0/2/2 |
| client | 0/0/7/1 | 0/0/7/1 | 0/0/7/1 | 0/0/7/0 | 0/0/7/0 | 0/0/7/1 | 0/0/7/1 |
| review | 0/0/8/2 | 0/0/2/2 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/2 | 0/0/2/2 |
| send-sheet-1 | 0/0/12/2 | 0/0/6/2 | 0/0/5/2 | 0/0/12/0 | 0/0/12/0 | 0/0/12/2 | 0/0/5/2 |
| send-sheet-final | 0/0/12/2 | 0/0/6/2 | 0/0/6/2 | 0/0/12/0 | 0/0/12/0 | 0/0/12/2 | 0/0/6/2 |
| sheet-after-nav | 0/0/16/10 | 0/0/16/12 | **0/1/15/6** | 0/0/16/0 | 0/0/16/0 | 0/0/16/10 | **0/1/16/9** |
| more-menu | 0/0/8/2 | 0/0/2/2 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/2 | 0/0/2/2 |
| keyboard-thread | 0/0/8/2 | 0/0/2/2 | 0/0/2/2 | 0/0/8/0 | 0/0/8/0 | 0/0/8/2 | 0/0/2/2 |
| compose-sheet | 0/0/4/1 | 0/0/4/1 | 0/0/4/2 | 0/0/7/0 | 0/0/7/0 | 0/0/4/1 | 0/0/4/2 |
| keyboard-compose | 0/0/4/1 | 0/0/4/1 | 0/0/4/2 | 0/0/7/0 | 0/0/7/0 | 0/0/4/1 | 0/0/4/2 |
| sold-sheet | 0/0/10/2 | 0/0/4/2 | 0/0/2/2 | 0/0/11/0 | 0/0/11/0 | 0/0/10/2 | 0/0/2/2 |

### Флоу (одинаково на всех устройствах — значит, не вёрстка, а логика)

| проверка | fold-cover-412 | fold-cover-344 | fold-cover-280 | fold-inner-port | fold-inner-land | flip5-main | flip5-flex |
|---|---|---|---|---|---|---|---|
| unsaved_kept | False | False | False | False | False | False | False |
| sheet_open_after_nav | True | True | True | True | True | True | True |
| kb_textarea_visible | True | True | True | True | True | True | True |
| kb_send_visible | True | True | True | True | True | True | True |
| kb_compose_primary_visible | True | True | False | True | True | True | False |
| compose_kept_after_back | False | False | False | False | False | False | False |
| flow_errors | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

### Ошибки JS / HTTP / не-GET

- **fold-cover-412**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 25: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **fold-cover-344**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 25: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **fold-cover-280**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 25: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **fold-inner-port**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 27: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **fold-inner-land**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 25: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **flip5-main**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 27: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release
- **flip5-flex**: JS 0, HTTP 6, диалогов 0, не-GET (фейк 200) 25: POST /api/ma/messages/1/lock/acquire, POST /api/ma/messages/1/lock/release, POST /api/ma/messages/2/lock/acquire, POST /api/ma/messages/2/lock/release, POST /api/ma/messages/3/lock/acquire, POST /api/ma/messages/3/lock/release, POST /api/ma/messages/6/lock/acquire, POST /api/ma/messages/6/lock/release

Пояснения к флоу:
- `unsaved_kept = False` — правка черновика → «←» → вернулся: правки нет, предупреждения не было (B-03).
- `sheet_open_after_nav = True` — шит отправки остаётся после ухода с треда (B-01).
- `kb_compose_primary_visible = False` (280, Flex) — кнопки «Написать» под клавиатурой (B-18).
- `compose_kept_after_back = False` и `flow_errors = 1` — одна и та же причина: после ухода с открытым «Написать» шит
  остался открытым и перекрыл кнопку при возврате (B-01); сохранность текста compose в этом прогоне **не измерена**.
- HTTP 6 на устройство — это `sendBeacon` снятия лока на неверный адрес (B-12); 4xx/5xx от API — 0.
- Не-GET за прогон (всё фейк 200): только `lock/acquire` / `lock/release` при открытии/закрытии треда. Ни одного
  `send` / `skip` / `sold` / `autopilot` — харнесс до финальной отправки не доходит.
- Deep-link `review_1` и `thread_qa-t1`: открываются, «←» возвращает во Входящие, петли нет.

## Находки (базовый прогон, main `30fb4f4`)

Нумерация `B-NN` — чтобы ссылаться в повторных прогонах (`run-<дата>-<что>.md`).

### P1 — теряем покупателя / ложное «готово» / отправка не туда

| # | Экран | Что видно | Ожидание | Где в коде | Кому |
|---|---|---|---|---|---|
| B-01 | Тред → «📨 Отправить» → «Назад»/таб | Шит «Подготовка к отправке» (текст Klaus, кнопка «📨 Отправить») **остаётся открытым поверх Входящих** после ухода с экрана (`sheet_open_after_nav = true` на всех устройствах, скрин `sheet-after-nav.png`). Тап «Отправить» отправит Klaus, глядя на чужой список | Любая смена экрана закрывает шит | `components/sheet.js` — нет `closeSheet()` на `hashchange`; `router.js dispatch()` | kab-webapp |
| B-02 | Тред с черновиком | Неотправленный черновик рисуется **в ленте зелёным пузырём «наш ответ» со временем**, как уже отправленный (скрин `thread-draft.png`, пузырь 20.10 10:51). Ниже — тот же текст в карточке «Черновик #1» | Черновик — только в карточке черновика, в ленте только `sent/sent_debug` | `modules/db_threads.py` `thread_events`: `SENT_LIKE` включает `pending/new/edited/approved`; `thread.js eventBubble` не различает статус out-события | kab-db + kab-webapp |
| B-03 | Тред: правка черновика → «←» | Набранная правка теряется молча (`unsaved.kept = false`, предупреждения нет). То же для «✉️ Написать»: текст не сохраняется после ухода (`compose_kept_after_back = false`) | Черновик правки хранится (sessionStorage по msg_id) или confirm «Выйти без сохранения?» | `thread.js` (нет dirty-флага), `compose-form.js` | kab-webapp |
| B-04 | Весь MA | Нативная `Telegram.WebApp.BackButton` не подключена (только «←» в шапке, `tg.showBack` нигде не вызывается), `enableClosingConfirmation` нет → системный «назад» Android / свайп вниз **закрывает MA целиком** вместе с набранным текстом | BackButton = тот же `navigateBack`; closing confirmation, пока есть несохранённый текст | `router.js:62-66`, `components/backbar.js`, `tg.js:57` | kab-webapp |

### P2 — путает / лишние нажатия

| # | Экран | Что видно | Предложение | Где | Кому |
|---|---|---|---|---|---|
| B-05 | Шит «Отправка — что делаем?» | Главная (зелёная) кнопка — «🌐 Перевести на DE и **отправить**», хотя черновик уже на DE; и она **не отправляет**, только переводит (по CLAUDE.md). «Отправить как есть» ведёт ещё на шаг «Подготовка», потом финал → **4 нажатия в треде** при готовом черновике | Если язык черновика = язык клиента — главная кнопка «Отправить» сразу на финальный шаг; подпись перевода — «Перевести на DE» | `thread.js:513-600` | kab-webapp |
| B-06 | Тред без черновика (`status=new`, черновик ещё не сгенерирован) | Карточка «Черновик #6 · ждёт проверки» с **пустым** полем и активной «📨 Отправить»; «✉️ Написать» в доке нет | Пустой черновик → «Черновик готовится…» + «✍️ Написать самому» | `thread.js:871-886` | kab-webapp |
| B-07 | Тред покупателя на FR/PL | Вкладка «**DE** → клиенту», хотя клиент пишет по-французски/польски | Подпись по `client_lang`: «FR → клиенту» | `thread.js:131` | kab-webapp |
| B-08 | Входящие vs Обзор | Обзор: «Ждут нас **4**», Входящие: «Ждут нас · **3**» (+1 в «Закреплённые») | Одинаковое определение на обоих экранах (или «3 + 1 закреплённый») | `dashboard.js`, `pipeline.js` | kab-webapp |
| B-09 | Входящие (412 px и уже) | Название объявления — главное — режется до 8–10 символов на 412 px («Sitzbank C…») и до **1–2 букв на 344 px** («R…», «K…»), а цена и дата занимают полстроки; превью письма тоже в одну строку | Название в 2 строки, дата под ценой мелко | `css/app.css .tc-t` | kab-webapp |
| B-10 | «Продано» | Цена пустая, placeholder «1300» при цене объявления 450 € и договорённой 400 € | Предзаполнить `negotiated_price_eur` / цену объявления | `thread.js:206` | kab-webapp |
| B-11 | Обзор | «Баланс API —» (LLM по подписке, баланса нет), «ДРАФТЫ», «АВТОПИЛОТ 0» — жаргон | Убрать баланс; «Черновики готовы» | `dashboard.js` | kab-webapp |
| B-12 | Тред: закрытие MA | `sendBeacon` снятия лока бьёт относительным путём `/api/ma/messages/{id}/lock/release-beacon` → уходит на origin страницы (GitHub Pages), а не на `API_BASE` (в прогоне: `net::ERR_ABORTED` на 127.0.0.1:8799). Лок висит 5 мин, второй оператор видит «занято» | `${API_BASE}/api/ma/…` | `thread.js:912` | kab-webapp |
| B-13 | Глобально | Mini App при каждом открытии треда шлёт `lock/acquire`, при уходе `lock/release` (в прогоне 20+ не-GET на устройство) — ок, но при deep-link `review_1` лок берётся дважды | — | `thread.js`, `review.js` | kab-webapp |

| B-17 | Тред с открытой клавиатурой, 280 px и Flip 5 Flex | Док «Вариант / Отправить» и таб-бар остаются над клавиатурой и **закрывают поле черновика** (`dock_covers_textarea = true`); на Flex от поля не видно ничего (скрин `flip5-flex/keyboard-thread.png`) | При фокусе в поле прятать таб-бар (и док — или делать его 1 строкой) | `css/app.css .dock`, `components/tabbar.js` | kab-webapp |
| B-18 | «✉️ Написать» с клавиатурой, 280 px и Flex | Кнопки «Перевести / Отмена» под клавиатурой — чтобы нажать, надо сначала её закрыть; на Flex и само поле уходит за край | Кнопки шита — над полем или sticky внизу видимой области (`visualViewport`) | `components/compose-form.js`, `sheet.js` | kab-webapp |
| B-19 | Все экраны | Тап-цели < 44 px на всех устройствах: «←» Назад **33×35**, чипы аккаунтов и фильтров **30 px** высотой (12 шт. во Входящих), вкладки черновика 31 px, «↗» объявление **15×26**, галочка «Закрыть остальные» 18 px | Минимум 44×44 (чипы — 36 + отступы) | `css/app.css` | kab-webapp |
| B-20 | Fold внутренний (860–954 px) | Одна колонка на всю ширину: пузыри по 800 px, список и тред не видны одновременно | ≥ 800 px — две панели: список слева, тред справа | `css/app.css`, `router.js` | kab-webapp |

### P3 — мелочи

| # | Экран | Что видно |
|---|---|---|
| B-14 | Шит «Написать клиенту» | Заголовок продублирован: «✉️ Написать клиенту» и строкой ниже «✉️ Написать клиенту (на русском — переведём)» |
| B-15 | Входящие | Чип «драфт»; фильтр «Ждём» рядом с «Ждут нас» — путаются |
| B-21 | Входящие, 280 px и Flex | Точка «непрочитано» вылезает за правый край (right = 327 при ширине 306) |
| B-16 | Settings | 25 тап-целей < 44 px; технические ключи (`Send mode`, `Polling paused`) по-английски — экран для админа, оператору не нужен |

## Сценарии ленивого оператора (агент `ux-lazy`)

_(дописывается после возврата агента)_

## Чинить первым
1. **monitor_errors** — алерты об ошибках (kab-ops/kab-mail; по заметкам оркестратора в main уже есть `8eabb46` «алерт об
   ошибках работает» — проверю в повторном прогоне).
2. **B-01** закрывать шит при смене экрана; **B-02** черновик не в ленте (kab-webapp + kab-db).
3. **B-03/B-04** BackButton Telegram + защита несохранённого текста (kab-webapp).
4. Бот: авторизация в поллере (`docs/qa/telegram-bot.md`, P1-1) (kab-api).
5. `poll_all_accounts` каждые 15 с → 2130 пропусков в сутки (kab-mail).
6. B-09, B-17, B-18, B-19 — внешние экраны (kab-webapp, редизайн).
