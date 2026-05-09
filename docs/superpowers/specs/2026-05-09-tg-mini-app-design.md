# Telegram Mini App — design

**Status:** approved (2026-05-09)
**Scope:** parallel UI surface к существующему Telegram-боту. Покрывает весь функционал оператора. Бот остаётся как есть.

## Цель

У текущего бота есть pain-points в нескольких местах:
- Thread-detail длинных переговоров режется на чанки (Telegram 4096-char лимит), кнопки только на последнем чанке
- Autopilot config — три последовательных input-step'а (floor → notify → start)
- Edit DE без живого preview back-translate
- Review-карточка с 9 кнопками — плотно, легко промахнуться на телефоне
- Settings и client-history живут в отдельной web-морде на :8080 — оператор должен открывать браузер

Mini App закрывает эти места, не ломая то что уже работает (one-tap actions ✅/❌ в боте остаются — они и так быстры).

## Non-goals

- Замена бота. Бот продолжает выполнять все функции — pipeline, review, edit, autopilot, compose.
- Web-sockets / live-updates в первой итерации (pull-to-refresh достаточно).
- Multi-tenancy / роли. Все авторизованные операторы имеют одинаковые права (как в боте сейчас).

## Архитектура

```
TG client
  │
  ├──► open https://pgolitech-lab.github.io/kleinanzeigen-bot/
  │      (GitHub Pages serves /web-app/ from main branch)
  │      [HTML + JS load — no build step]
  │
  └──► fetch /api/ma/* with X-Telegram-Init-Data header
         │
         ▼
  cloudflared tunnel  https://<named>.workers.dev
         │  (named tunnel — стабильный URL)
         ▼
  prod 192.168.88.28:8080  (FastAPI — текущий процесс)
         │
         ▼
  SQLite + scheduler.* helpers + telegram_bot._broadcast_card
```

**Ключевое:** SPA — статика на GitHub Pages. Backend — тот же FastAPI на :8080, плюс новый префикс `/api/ma/*` с initData-валидацией. Бот и MA живут в одном Python-процессе, шарят in-memory state через module-level dict.

## Repo layout

```
kleinanzeigen-bot/
├─ web-app/                           ← НОВОЕ: SPA для GitHub Pages
│  ├─ index.html
│  ├─ css/app.css
│  ├─ js/
│  │  ├─ app.js                       ← hash router, mount
│  │  ├─ api.js                       ← fetch wrapper + initData header
│  │  ├─ tg.js                        ← Telegram.WebApp helpers
│  │  └─ screens/
│  │     ├─ pipeline.js
│  │     ├─ review.js
│  │     ├─ thread.js
│  │     ├─ autopilot.js
│  │     ├─ history.js
│  │     └─ settings.js
│  └─ assets/
├─ web/app.py                         ← +/api/ma/* routes
├─ modules/
│  ├─ operator_lock.py                ← НОВОЕ: общий lock dict + helpers
│  ├─ tg_init_data.py                 ← НОВОЕ: HMAC validator
│  └─ ...
├─ scheduler.py                       ← без изменений
└─ ...
```

GitHub Pages config: Settings → Pages → Source: `Deploy from a branch` → Branch: `main`, Folder: `/web-app`. URL: `https://pgolitech-lab.github.io/kleinanzeigen-bot/`. Deploy = `git push`.

## Tech stack

- **Frontend:** vanilla JS (ES modules) + [HTM](https://github.com/developit/htm) + Preact runtime (~3KB) для рендера. Bootstrap 5.3 dark от CDN. Никакого build-step.
- **Backend:** Python 3.11 + FastAPI (существующий). Новые модули `operator_lock.py`, `tg_init_data.py`.
- **HTTPS:** cloudflared named tunnel (бесплатно, не требует домена). Стабильный URL.
- **Auth:** Telegram WebApp `initData` + HMAC валидация на backend (нет sessions, нет cookies).

## Auth flow

Telegram передаёт `window.Telegram.WebApp.initData` — URL-encoded query string с `user`, `auth_date`, `hash`. SPA шлёт его в каждом запросе как `X-Telegram-Init-Data` header.

**Backend dependency** (`modules/tg_init_data.py`):

```python
async def verify_tg_init_data(x_telegram_init_data: str = Header(...)) -> dict:
    parsed = parse_qs(x_telegram_init_data, strict_parsing=True)
    received_hash = parsed.pop("hash", [""])[0]
    auth_date = int(parsed.get("auth_date", ["0"])[0])

    if time.time() - auth_date > 3600:
        raise HTTPException(401, "init_data expired")

    data_check = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", config.telegram_bot_token().encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "bad hash")

    user = json.loads(parsed["user"][0])
    if user["id"] not in config.telegram_authorized_ids():
        raise HTTPException(403, "not authorized")
    return user
```

Применяется ко всем `/api/ma/*` через `Depends(verify_tg_init_data)`.

**CORS:** `web/app.py` добавит `CORSMiddleware(allow_origins=["https://pgolitech-lab.github.io", "https://web.telegram.org"], allow_methods=["GET","POST"], allow_headers=["X-Telegram-Init-Data","Content-Type"])`.

**Authorization:** один источник правды — `config.telegram_authorized_ids()`. Тот же список что для bot-callback'ов. Добавить оператора → CSV в settings → оба интерфейса подхватили.

**Replay protection:** `auth_date > 1 hour` → 401. Клиент при таком ответе вызывает `Telegram.WebApp.close()` → пользователь перезапускает MA → свежий initData.

## Bot ↔ MA синхронизация

Бот, scheduler и FastAPI работают в одном Python-процессе (`main.py`). Это позволяет шарить in-memory state без БД-таблиц.

**Lock** (новое): выносим из `modules/telegram_bot._THREAD_LOCKS` в `modules/operator_lock.py`:

```python
# modules/operator_lock.py
_LOCKS: dict[int, tuple[str, float]] = {}
LOCK_TIMEOUT_SEC = 300

def acquire(msg_id: int, actor: str) -> bool: ...
def release(msg_id: int) -> None: ...
def state(msg_id: int) -> tuple[str, float] | None: ...
def all_locked() -> dict[int, tuple[str, float]]: ...
```

`modules/telegram_bot.py` импортирует и использует. `web/app.py` MA-endpoint'ы тоже импортируют. Один источник правды.

**Actor identifier:** строка вида `@username` или `first_name#user_id`. Lock не знает где оператор работает — бот или MA. Можно начать в боте, продолжить в MA с тем же tg user_id.

**Broadcast в TG-карточки:** все MA action-endpoint'ы после успеха зовут `telegram_bot._broadcast_card(msg_id)` — обновляет ВСЕ DM-копии review-карточки. Бот при тех же действиях зовёт `_broadcast_card` уже сейчас. MA не дублирует логику — переиспользует.

**Pending input** (`_PENDING_INPUTS` в боте): чисто бот-овая фича. MA не трогает — у него HTML textarea с native submit. Если оператор начал «✏️ Правка RU» в боте и переключился в MA — MA увидит lock с собой как actor, может продолжить или отпустить.

**Live updates в MA:** v1 — pull-to-refresh + manual «↻» кнопка в header. SSE/WebSockets — если pull окажется неудобным, добавим во v2 (бэк уже мульти-thread, добавить broadcast несложно).

## API surface

Все endpoint'ы под `/api/ma/*` с `Depends(verify_tg_init_data)`. Все JSON. Errors: 401 (auth), 403 (not authorized), 404 (msg not found), 409 (lock conflict), 422 (validation), 500 (internal).

### Pipeline & threads

- `GET /api/ma/pipeline` → `{red: [thread,...], green: [thread,...]}` где `thread = {id, msg_id, ad_title, ad_price, status_txt, last_event_at, deal_brief, autopilot}`. Mirrors `_build_pipeline_data` в боте.
- `GET /api/ma/threads/{thread_id}` → `{header: {...}, events: [...], related: {...}, autopilot: {...}}`. Events — chronological через `db.thread_events()`.

### Review (msg-level)

- `GET /api/ma/messages/{id}` → review payload: `{de_client, ru_client, ru_answer, de_answer, ru_translation, deal_brief, related_warning, lock_state, autopilot_state, status}`.
- `POST /api/ma/messages/{id}/send` → вызывает `scheduler.send_one(id)`, на успех — `_broadcast_card(id)`.
- `POST /api/ma/messages/{id}/skip`
- `POST /api/ma/messages/{id}/sold` — мирится с `ad_briefs.sold_at`.
- `POST /api/ma/messages/{id}/regenerate {strategy: "harder"|"softer"|"shorter"|"rephrase"|"no_haggle"}` → `claude.regenerate_with_strategy`.
- `POST /api/ma/messages/{id}/edit-ru {text}` → `claude.translate_only(target=client_lang)` + `db.update_message`.
- `POST /api/ma/messages/{id}/edit-de {text}` → store as-is + back-translate в `ru_translation`.
- `POST /api/ma/messages/{id}/price {eur}` → `claude.regenerate_with_price`.
- `POST /api/ma/messages/{id}/instruction {text}` → `claude.regenerate_with_instruction`.

Все mutating actions: после успеха `_broadcast_card(msg_id)` обновляет TG-копии.

### Lock

- `POST /api/ma/messages/{id}/lock/acquire` → 200 / 409 `{actor, acquired_at}`.
- `POST /api/ma/messages/{id}/lock/release` → 200.

MA при открытии review-экрана автоматически делает `acquire`. При закрытии (BackButton) — `release`.

### Autopilot

- `POST /api/ma/threads/{thread_id}/autopilot/start {floor_eur, notify_mode}` → `db.start_thread_autopilot`.
- `POST /api/ma/threads/{thread_id}/autopilot/stop` → `db.stop_thread_autopilot(reason="manual")`.

### Compose / history / settings

- `POST /api/ma/threads/{thread_id}/compose {text}` → `scheduler.send_manual_compose`.
- `GET /api/ma/clients/{email}/history` → `db.find_related_inquiries`.
- `GET /api/ma/settings` → текущие KV (без секретов: bot_token / api_key замаскированы).
- `POST /api/ma/settings {key, value}` → `config.set(key, value)` (whitelist допустимых ключей).

## Экраны (6 в MVP)

### 1. Pipeline (`#/pipeline`)
Entry-point. Список тредов сверху-вниз: 🔴 «ждут нас» сверху, 🟢 «ждём клиента» ниже. Каждая строка — карточка с ad_title, ценой, deal-brief, статусом, временем. Tap → review (если есть pending draft) или thread.

### 2. Review (`#/review/{msg_id}`)
Главный экран действий. Вёрстка mobile-first:
- Header: msg_id, ad_title, цена, статус-стрип (🔴/🟢), aктор lock-а если занято, наш аккаунт
- Tabs: Review ↔ Thread (мгновенное переключение)
- Related-warning (компактная одна строка с deep-link)
- Deal brief блок
- Сообщение клиента (collapsed, tap to expand)
- DRAFT блок side-by-side: RU (наша инструкция) | DE (текст для клиента) с inline edit
- Action grid 2-колонки: ✅ ОТПРАВИТЬ | 🚀 Автопилот (top), затем 8 preset-стратегий, внизу ❌ Пропустить | 💰 Продано
- TG MainButton API в самом низу подвязана к ✅ ОТПРАВИТЬ для жирной accent-кнопки

Edit RU/DE: inline textarea, на blur — POST. Никаких _PENDING_INPUTS.

### 3. Thread (`#/thread/{gmail_thread_id}`)
Chat-style лог через `db.thread_events()`. Бесконечный scroll, нет чанкинга. В шапке — header thread'а (ad, цена, клиент, наш аккаунт). Внизу sticky bar с действиями: «✉️ Написать», «📋 Открыть review», «↩ Pipeline».

### 4. Autopilot (`#/autopilot/{thread_id}`)
- Если не активен: форма {floor_eur, notify_mode (silent/notify)} + кнопка «🚀 Запустить»
- Если активен: счётчик `N/20`, текущий floor, режим, стоп-кнопка, журнал автоответов
- Stop-reason history (если был стоп раньше) — список с timestamps

### 5. Client history (`#/client/{email}`)
Список всех тредов клиента таблицей. Sortable по дате, фильтр по статусу. Tap → переход в Pipeline или Thread этого треда.

### 6. Settings (`#/settings`)
Mobile KV editor. Группы:
- send_mode (radio: disabled/redirect/production)
- gmail polling: interval, from_filter, max_age_days
- reminders: enabled, after_days
- Telegram authorized list, operator_dm_ids
- API balance snapshot
- Chat style settings (font/padding/etc)

Секреты (bot_token, anthropic_api_key, app_password) показываются маскированно `••••• 1234` с кнопкой «Заменить» (открывает confirmation modal).

## Cloudflared setup

Один раз на проде:
```bash
sudo apt install cloudflared
cloudflared tunnel login                       # OAuth → cf account (free)
cloudflared tunnel create kleinanzeigen-api    # выдаёт TUNNEL_ID
# Без своего домена — используем .workers.dev sub:
cloudflared tunnel route dns kleinanzeigen-api kleinanzeigen-api.<your-cf>.workers.dev
```

Systemd unit `/etc/systemd/system/cloudflared.service`:
```ini
[Unit]
Description=Cloudflare Tunnel for kleinanzeigen-bot API
After=network.target kleinanzeigen-bot.service

[Service]
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token <TOKEN> --url http://127.0.0.1:8080
Restart=always
RestartSec=5
User=pg

[Install]
WantedBy=multi-user.target
```

**API_BASE** в `web-app/js/api.js`:
```js
export const API_BASE = "https://kleinanzeigen-api.<your-cf>.workers.dev";
```

**FastAPI CORS** (`web/app.py`):
```python
app.add_middleware(CORSMiddleware,
    allow_origins=["https://pgolitech-lab.github.io", "https://web.telegram.org"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data", "Content-Type"])
```

## BotFather setup

Один раз для бота:
- `/mybots` → выбрать бота → Bot Settings → Menu Button → Configure menu button → URL: `https://pgolitech-lab.github.io/kleinanzeigen-bot/`
- Это даёт «Open App» pill-кнопку рядом с persistent reply keyboard
- Альтернативно: inline-button с `web_app: {url: ...}` в любом сообщении бота для deep-link на конкретный экран

**Deep-link через `tgWebAppStartParam`:**
- Бот шлёт inline-button: `{text: "📋 Открыть карточку", web_app: {url: "https://pgolitech-lab.github.io/kleinanzeigen-bot/?tgWebAppStartParam=review_123"}}`
- В SPA `app.js` при mount читает `Telegram.WebApp.initDataUnsafe.start_param` (= `"review_123"`) и роутит на `#/review/123`
- Формат: `<screen>_<id>` — `review_123`, `thread_abc`, `autopilot_abc`, `client_email%40x.com`. URL-encode для email/spec-chars.
- Если start_param пустой → дефолтная страница `#/pipeline`

## Deploy & dev workflow

**Dev SPA:**
- Локально: `python -m http.server 8000 --directory web-app/` + ngrok / cloudflared для HTTPS preview в TG
- Tester bot (отдельный): можно настроить второй BotFather-бот для testing menu button

**Production deploy:**
- SPA: `git commit && git push origin main` → GitHub Pages пересобирает за ~30-60 сек
- Backend: `git push` → ssh в прод → `git pull` (или sshfs save сразу) → `sudo systemctl restart kleinanzeigen-bot`
- Cloudflared: один раз настроен, работает как daemon

## Phase / план релизов

**Phase 1 — Foundation** (без screen работы):
- `modules/operator_lock.py` (вынести `_THREAD_LOCKS` из telegram_bot)
- `modules/tg_init_data.py` (HMAC validator)
- `web/app.py` — CORS middleware, `/api/ma/health` ping endpoint, dependency
- `web-app/index.html` + `js/api.js` + `js/tg.js` — bootstrap, проверка auth
- Cloudflared tunnel
- BotFather menu button
- **Acceptance:** открыл MA, увидел `{"user_id": 123, "username": "pg"}` от health endpoint

**Phase 2 — Read-only screens:**
- `GET /api/ma/pipeline` + screen pipeline.js
- `GET /api/ma/threads/{id}` + screen thread.js
- `GET /api/ma/clients/{email}/history` + screen history.js
- **Acceptance:** оператор может листать активные треды, читать историю, но не действовать

**Phase 3 — Review actions:**
- Все `/api/ma/messages/*` endpoint'ы
- Screen review.js с edit-inline и action grid
- Lock acquire/release
- **Acceptance:** оператор делает send/skip/edit/regenerate в MA; бот видит обновления через `_broadcast_card`

**Phase 4 — Autopilot + compose + settings:**
- Autopilot screen
- Compose action
- Settings screen с whitelist
- **Acceptance:** полный паритет с ботом

## Risks / open questions

- **Cloudflared latency** — добавляет 50-100ms к каждому request. Pipeline-screen с 20 тредами должен fetched одним endpoint, не 20-ю. Аналогично review.
- **GitHub Pages cache TTL** ~10 минут — после `git push` старые операторы могут видеть прежнюю версию до hard refresh. Решается version-suffix в `index.html` (`<script src="js/app.js?v=20260509">`) или service worker (overkill).
- **Lock conflict UX** — если оператор открыл review в MA, а другой кликнул в боте — MA получит 409 на acquire. Показывать «🟥 в работе у @other», ждать или принудительно перехватить (force-acquire с broadcast warning?). Решение: для MVP — просто read-only mode при foreign lock, кнопка «↻ Проверить» для retry.
- **Cookie / сторадж в TG WebView** — Telegram WebView в зависимости от платформы (iOS/Android/Desktop) имеет разные cookie-политики. initData каждый раз свежий — мы не зависим от cookie. Но если захотим persistent state (recent searches, draft) — возможно SessionStorage. Не блокер для MVP.
- **Mobile keyboard в textarea** — при edit RU/DE textarea fokus → keyboard up → viewport shrinks → надо чтобы submit-кнопка не уезжала за keyboard. Решение: `Telegram.WebApp.expand()` + sticky-bottom input form. Ниже — стандартная mobile-web вёрстка, без сюрпризов.
- **Service-account / DB access** — backend и сейчас работает с SQLite, новых ACL не вводим. Risk: если cloudflared скомпрометирован — backend exposed. Mitigation: middleware жёстко требует валидную initData; rate-limit per user_id (можно добавить).

## Что НЕ делаем в MVP

- Web-sockets / SSE
- Service worker / offline mode
- Push-нотификации MA-side (бот покрывает)
- Multi-language UI (только русский)
- Темная/светлая тема (используем TG theme variables — автоматически)
- Аналитика usage / a/b testing
- Pin to home-screen / standalone PWA mode

## References

- Telegram WebApp docs: https://core.telegram.org/bots/webapps
- initData validation reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
- HTM (htm.dev): https://github.com/developit/htm
- Cloudflared named tunnels: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
- Существующие спеки этого проекта:
  - `docs/superpowers/specs/2026-05-03-tg-rework-design.md` — текущая раскладка бот-карточки
  - `docs/superpowers/specs/2026-05-03-autopilot-design.md` — autopilot который MA должен покрыть
  - `docs/superpowers/specs/2026-05-03-auto-ack-design.md` — auto-ack контекст
