# Telegram Mini App — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Оператор открывает Mini App из Telegram-меню и видит экран «Authenticated as @username (id: 12345)». Это доказывает что (1) GitHub Pages раздаёт SPA, (2) cloudflared tunnel пускает запросы к `:8080`, (3) initData HMAC validator работает, (4) FastAPI endpoint валидирует и возвращает user info.

**Architecture:** SPA на vanilla JS + Preact runtime + HTM хостится на GitHub Pages из `/web-app/` директории. FastAPI (текущий процесс на :8080) получает префикс `/api/ma/*` с initData HMAC dependency. Cloudflared named tunnel выставляет `:8080` как HTTPS. operator_lock рефакторинг отложен до Phase 3 (когда MA будет реально брать lock).

**Tech Stack:** Python 3.11 / FastAPI / pytest / vanilla JS + HTM + Preact (CDN) / Bootstrap 5.3 (CDN) / cloudflared / GitHub Pages.

**Все команды выполняются на проде через ssh** (`ssh 192.168.88.28 ...`) если не указано иное. Локальной копии репо нет — sshfs не смонтирован, prod = source of truth.

**Spec reference:** `docs/superpowers/specs/2026-05-09-tg-mini-app-design.md`

---

## File Structure

**Создаются:**
- `pytest.ini` — pytest config
- `tests/__init__.py` — пустой
- `tests/conftest.py` — sys.path bootstrap
- `tests/test_tg_init_data.py` — тесты HMAC validator
- `modules/tg_init_data.py` — HMAC validator + FastAPI dependency
- `web-app/index.html` — SPA entry point
- `web-app/css/app.css` — минимальные стили
- `web-app/js/app.js` — hash router + mount
- `web-app/js/api.js` — fetch wrapper с initData header
- `web-app/js/tg.js` — Telegram.WebApp helpers (theme, BackButton)

**Модифицируются:**
- `requirements.txt` — добавить `pytest`
- `web/app.py` — добавить CORSMiddleware + `/api/ma/health` endpoint

**Manual ops (не git):**
- BotFather → Bot Settings → Menu Button (URL = GitHub Pages URL)
- GitHub repo Settings → Pages → Source: main / folder: `/web-app`
- Prod server → cloudflared install + named tunnel + systemd unit

---

## Task 1: Bootstrap pytest infrastructure

**Files:**
- Modify: `requirements.txt`
- Create: `pytest.ini`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest to requirements.txt**

Append на новой строке:
```
pytest
```

Команда:
```bash
ssh 192.168.88.28 'echo pytest >> /home/pg/kleinanzeigen-bot/requirements.txt && tail -3 /home/pg/kleinanzeigen-bot/requirements.txt'
```

Expected output:
```
google-auth-oauthlib
google-auth-httplib2
pytest
```

- [ ] **Step 2: Install pytest на проде**

```bash
ssh 192.168.88.28 'pip3 install --break-system-packages --ignore-installed pytest'
```

Expected: `Successfully installed pytest-X.Y.Z` (X.Y.Z может различаться).

- [ ] **Step 3: Create pytest.ini**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
```

- [ ] **Step 4: Create tests/__init__.py**

Пустой файл (обычный пакет-маркер).

- [ ] **Step 5: Create tests/conftest.py**

```python
"""Pytest config + общие фикстуры.

sys.path bootstrap чтобы тесты могли импортить modules/* и web/app.py
без устанавливаемого пакета.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
```

- [ ] **Step 6: Sanity smoke run — pytest должен стартовать без ошибок**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/ -v'
```

Expected: exit code 5 (`no tests ran`) — это OK, директория пустая. Главное — нет ImportError или ModuleNotFoundError.

- [ ] **Step 7: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add requirements.txt pytest.ini tests/ && git -C /home/pg/kleinanzeigen-bot commit -m "test: bootstrap pytest infrastructure"'
```

---

## Task 2: Telegram initData HMAC validator (TDD)

**Files:**
- Create: `tests/test_tg_init_data.py`
- Create: `modules/tg_init_data.py`

**Background:** Telegram отдаёт SPA `window.Telegram.WebApp.initData` — URL-encoded query string `query_id=...&user=%7B...%7D&auth_date=...&hash=...`. Backend верифицирует:
1. `data_check_string = "\n".join(sorted([f"{k}={v}" for k, v in fields if k != "hash"]))`
2. `secret_key = HMAC_SHA256(key=b"WebAppData", msg=BOT_TOKEN)`
3. `expected_hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hex()`
4. `assert expected_hash == received_hash`

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

- [ ] **Step 1: Write failing tests**

Create `tests/test_tg_init_data.py`:

```python
"""Тесты HMAC-валидатора Telegram initData."""
from __future__ import annotations
import hmac
import hashlib
import json
import time
from urllib.parse import urlencode
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from modules import tg_init_data


TEST_BOT_TOKEN = "123456:test_token_AbCdEfG"
TEST_USER_AUTHORIZED = {"id": 999, "first_name": "Pg", "username": "pgtest"}
TEST_USER_FOREIGN = {"id": 666, "first_name": "Stranger"}


def _make_init_data(
    user: dict,
    auth_date: int | None = None,
    bot_token: str = TEST_BOT_TOKEN,
    bad_hash: bool = False,
) -> str:
    """Сгенерировать валидный (или со сломанным hash) initData querystring."""
    if auth_date is None:
        auth_date = int(time.time())
    fields = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(auth_date)}
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if bad_hash:
        h = "0" * 64
    fields["hash"] = h
    return urlencode(fields)


@pytest.fixture
def patched_config():
    """Подменяет config.telegram_bot_token и telegram_authorized_ids."""
    with patch("modules.tg_init_data.config") as m:
        m.telegram_bot_token.return_value = TEST_BOT_TOKEN
        m.telegram_authorized_ids.return_value = {999}
        yield m


def test_valid_init_data_returns_user(patched_config):
    init = _make_init_data(TEST_USER_AUTHORIZED)
    user = tg_init_data.verify_init_data(init)
    assert user["id"] == 999
    assert user["username"] == "pgtest"


def test_bad_hash_raises_401(patched_config):
    init = _make_init_data(TEST_USER_AUTHORIZED, bad_hash=True)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
    assert "hash" in ei.value.detail.lower()


def test_expired_auth_date_raises_401(patched_config):
    expired = int(time.time()) - 3700  # >1 hour
    init = _make_init_data(TEST_USER_AUTHORIZED, auth_date=expired)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
    assert "expired" in ei.value.detail.lower()


def test_unauthorized_user_raises_403(patched_config):
    init = _make_init_data(TEST_USER_FOREIGN)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 403
    assert "auth" in ei.value.detail.lower()


def test_missing_user_field_raises_401(patched_config):
    """Гарантируем что отсутствие user в parsed данных не падает на KeyError."""
    fields = {"auth_date": str(int(time.time())), "hash": "0" * 64}
    init = urlencode(fields)
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401


def test_token_change_invalidates_old_data(patched_config):
    """Если бот-токен переустановили — старые initData невалидны."""
    init = _make_init_data(TEST_USER_AUTHORIZED, bot_token="OLD_TOKEN_XYZ")
    with pytest.raises(HTTPException) as ei:
        tg_init_data.verify_init_data(init)
    assert ei.value.status_code == 401
```

- [ ] **Step 2: Run tests, expect FAIL**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_tg_init_data.py -v'
```

Expected: ImportError на `from modules import tg_init_data` (модуль ещё не существует) → `collected 0 items / 1 error`.

- [ ] **Step 3: Implement modules/tg_init_data.py**

```python
"""Telegram WebApp initData HMAC validator + FastAPI dependency.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Используется в /api/ma/* endpoint'ах через `Depends(verify_init_data_dep)`.
Прямая функция `verify_init_data` — для unit-тестов.
"""
from __future__ import annotations
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qs

from fastapi import Header, HTTPException

import config


AUTH_DATE_MAX_AGE_SEC = 3600  # 1 час


def verify_init_data(raw: str) -> dict:
    """Валидирует initData строку, возвращает user dict.

    Raises HTTPException:
      - 401 если hash не совпал, отсутствует user, или auth_date устарел
      - 403 если user.id не в config.telegram_authorized_ids()
    """
    parsed = parse_qs(raw, strict_parsing=False, keep_blank_values=True)
    if "hash" not in parsed or "user" not in parsed or "auth_date" not in parsed:
        raise HTTPException(401, "init_data malformed: missing required field")

    received_hash = parsed.pop("hash")[0]
    try:
        auth_date = int(parsed["auth_date"][0])
    except (ValueError, IndexError):
        raise HTTPException(401, "init_data: bad auth_date")

    if time.time() - auth_date > AUTH_DATE_MAX_AGE_SEC:
        raise HTTPException(401, "init_data expired")

    # data-check-string: k=v\nk=v... отсортированных по ключу.
    data_check = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))

    bot_token = config.telegram_bot_token()
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(401, "init_data: hash mismatch")

    try:
        user = json.loads(parsed["user"][0])
    except (json.JSONDecodeError, IndexError):
        raise HTTPException(401, "init_data: cannot parse user")

    if user.get("id") not in config.telegram_authorized_ids():
        raise HTTPException(403, "user not authorized")

    return user


async def verify_init_data_dep(
    x_telegram_init_data: str = Header(..., alias="X-Telegram-Init-Data"),
) -> dict:
    """FastAPI dependency. Возвращает user dict."""
    return verify_init_data(x_telegram_init_data)
```

- [ ] **Step 4: Run tests, expect PASS**

```bash
ssh 192.168.88.28 'cd /home/pg/kleinanzeigen-bot && python3 -m pytest tests/test_tg_init_data.py -v'
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add modules/tg_init_data.py tests/test_tg_init_data.py && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): tg_init_data HMAC validator + dependency"'
```

---

## Task 3: CORS middleware + /api/ma/health endpoint

**Files:**
- Modify: `web/app.py` (две вставки: CORS импорт + добавление middleware; новый роутер для /api/ma/*)

**Background:** SPA на `https://pgolitech-lab.github.io` будет fetch-ить cloudflared URL — другой origin → нужен CORS preflight. Web-Telegram desktop клиент — origin `https://web.telegram.org`.

- [ ] **Step 1: Найти место в web/app.py где создаётся FastAPI app**

```bash
ssh 192.168.88.28 'grep -n "FastAPI(\|app = FastAPI\|^app = " /home/pg/kleinanzeigen-bot/web/app.py | head -5'
```

Expected: одна строка вида `app = FastAPI(...)` около начала файла (~ строки 20-50).

- [ ] **Step 2: Добавить импорт CORSMiddleware**

В начале `web/app.py` после существующих `from fastapi import ...` добавить:
```python
from fastapi.middleware.cors import CORSMiddleware
```

(Если такая строка уже есть — пропустить.)

- [ ] **Step 3: Добавить middleware сразу после `app = FastAPI(...)` инициализации**

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pgolitech-lab.github.io",
        "https://web.telegram.org",
        "https://*.t.me",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Telegram-Init-Data", "Content-Type"],
    max_age=600,
)
```

- [ ] **Step 4: Добавить /api/ma/health endpoint в конце файла (перед if __name__ == ... если есть, иначе в самом конце)**

В начале блока добавить импорты (если не уже импортировано):
```python
from fastapi import Depends
from modules.tg_init_data import verify_init_data_dep
```

Сам endpoint:
```python
@app.get("/api/ma/health")
async def ma_health(user: dict = Depends(verify_init_data_dep)) -> dict:
    """Health endpoint для Mini App. Возвращает идентичность оператора."""
    return {
        "ok": True,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }
```

- [ ] **Step 5: Restart bot и проверить что FastAPI стартанул**

```bash
ssh 192.168.88.28 'sudo systemctl restart kleinanzeigen-bot && sleep 3 && journalctl -u kleinanzeigen-bot -n 30 --no-pager | tail -20'
```

Expected: видно строки "Started Kleinanzeigen Bot" / uvicorn запустился без traceback. Если есть `ImportError` — починить.

- [ ] **Step 6: Smoke-test endpoint без auth (должен вернуть 422 — Header missing)**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/ma/health'
```

Expected: `422` (FastAPI ругается что отсутствует обязательный Header).

- [ ] **Step 7: Smoke-test с фейковым initData (должен вернуть 401 — hash mismatch)**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "%{http_code}\n" -H "X-Telegram-Init-Data: hash=fake&user=%7B%22id%22%3A1%7D&auth_date=$(date +%s)" http://127.0.0.1:8080/api/ma/health'
```

Expected: `401`.

- [ ] **Step 8: CORS preflight smoke-test**

```bash
ssh 192.168.88.28 'curl -s -o /dev/null -w "%{http_code} | origin: %header{access-control-allow-origin}\n" -X OPTIONS -H "Origin: https://pgolitech-lab.github.io" -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: X-Telegram-Init-Data" http://127.0.0.1:8080/api/ma/health'
```

Expected: status `200`, `access-control-allow-origin: https://pgolitech-lab.github.io`.

- [ ] **Step 9: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web/app.py && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): /api/ma/health endpoint + CORS for github.io"'
```

---

## Task 4: SPA bootstrap (web-app/)

**Files:**
- Create: `web-app/index.html`
- Create: `web-app/css/app.css`
- Create: `web-app/js/tg.js`
- Create: `web-app/js/api.js`
- Create: `web-app/js/app.js`

**Background:** GitHub Pages раздаст эти файлы как статику с https://pgolitech-lab.github.io/kleinanzeigen-bot/. Никакого build-step. ESM модули с `type="module"`.

В рамках Phase 1 SPA только бутстрапит, делает GET /api/ma/health и рендерит результат. API_BASE в js/api.js пока — placeholder, реальное значение появится после Task 5 (cloudflared) и подставится в Task 8.

- [ ] **Step 1: Create web-app/index.html**

```html
<!DOCTYPE html>
<html lang="ru" data-bs-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Kleinanzeigen Bot — Operator</title>

  <script src="https://telegram.org/js/telegram-web-app.js"></script>

  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="css/app.css">
</head>
<body>
  <main id="app" class="container py-3">
    <p class="text-muted">Загрузка...</p>
  </main>

  <script type="module" src="js/app.js?v=20260509"></script>
</body>
</html>
```

- [ ] **Step 2: Create web-app/css/app.css**

```css
:root {
  --tg-bg: var(--tg-theme-bg-color, #1a1a1a);
  --tg-text: var(--tg-theme-text-color, #e0e0e0);
  --tg-hint: var(--tg-theme-hint-color, #888);
  --tg-link: var(--tg-theme-link-color, #4a9eff);
  --tg-button: var(--tg-theme-button-color, #1d4ed8);
  --tg-button-text: var(--tg-theme-button-text-color, #ffffff);
}

body {
  background: var(--tg-bg);
  color: var(--tg-text);
  font-size: 14px;
  margin: 0;
}

.muted { color: var(--tg-hint); }
.error { color: #f87171; }
.ok { color: #34d399; }

pre.payload {
  background: rgba(255,255,255,0.05);
  padding: .75rem;
  border-radius: 6px;
  font-size: 12px;
  overflow: auto;
}
```

- [ ] **Step 3: Create web-app/js/tg.js**

```javascript
// Helpers вокруг Telegram WebApp SDK.

export const tg = window.Telegram?.WebApp;

export function ready() {
  if (!tg) return;
  tg.ready();
  tg.expand();
}

export function initData() {
  return tg?.initData ?? "";
}

export function user() {
  return tg?.initDataUnsafe?.user ?? null;
}

export function startParam() {
  return tg?.initDataUnsafe?.start_param ?? null;
}

export function close() {
  tg?.close();
}
```

- [ ] **Step 4: Create web-app/js/api.js**

```javascript
import { initData, close } from "./tg.js";

// Production: будет заменено на реальный cloudflared URL после Task 5.
// Пока — пусто; Task 8 пропишет финальный URL и сделает повторный commit.
export const API_BASE = "";

export async function api(path, { method = "GET", body = null, headers = {} } = {}) {
  const url = `${API_BASE}${path}`;
  const opts = {
    method,
    headers: {
      "X-Telegram-Init-Data": initData(),
      "Content-Type": "application/json",
      ...headers,
    },
  };
  if (body !== null) opts.body = JSON.stringify(body);

  const res = await fetch(url, opts);
  if (res.status === 401) {
    // Init data истекла или плохой hash — закрываем MA, оператор перезапустит.
    close();
    throw new Error("auth expired");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}
```

- [ ] **Step 5: Create web-app/js/app.js**

```javascript
// Mini App entry point. Phase 1: только bootstrap + /api/ma/health.

import { ready, user, startParam } from "./tg.js";
import { api, API_BASE } from "./api.js";

function el(html) {
  const tmpl = document.createElement("template");
  tmpl.innerHTML = html.trim();
  return tmpl.content.firstChild;
}

async function renderHealth(mount) {
  if (!API_BASE) {
    mount.replaceChildren(el(`
      <div>
        <h5>⚠️ API_BASE не настроен</h5>
        <p class="muted">Это ожидаемо в Phase 1 локально. После Task 5 (cloudflared) обновится.</p>
        <p>TG user (из initDataUnsafe):</p>
        <pre class="payload">${JSON.stringify(user(), null, 2)}</pre>
      </div>
    `));
    return;
  }
  try {
    mount.replaceChildren(el(`<p class="muted">Запрашиваю /api/ma/health…</p>`));
    const data = await api("/api/ma/health");
    mount.replaceChildren(el(`
      <div>
        <h5 class="ok">✅ Authenticated</h5>
        <p>${data.first_name ?? "?"} ${data.username ? `(@${data.username})` : ""}</p>
        <p class="muted">user_id: ${data.user_id}</p>
        <pre class="payload">${JSON.stringify(data, null, 2)}</pre>
      </div>
    `));
  } catch (e) {
    mount.replaceChildren(el(`
      <div>
        <h5 class="error">❌ Ошибка</h5>
        <pre class="payload">${e.message}</pre>
      </div>
    `));
  }
}

function main() {
  ready();
  const mount = document.getElementById("app");
  const sp = startParam();
  if (sp) {
    console.log("[ma] start_param:", sp);
  }
  renderHealth(mount);
}

main();
```

- [ ] **Step 6: Local smoke (без TG, чисто что HTML парсится)**

```bash
ssh 192.168.88.28 'python3 -c "
import html.parser
class P(html.parser.HTMLParser):
    def error(self, msg): raise SystemExit(msg)
P().feed(open(\"/home/pg/kleinanzeigen-bot/web-app/index.html\").read())
print(\"OK\")"'
```

Expected: `OK`. Это базовая проверка что HTML не сломан синтаксически (TG-specific тестирование — Task 8).

- [ ] **Step 7: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/ && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): SPA bootstrap (index.html + tg/api/app.js)"'
```

---

## Task 5: Cloudflared named tunnel (operations)

**Background:** Бесплатный named tunnel выдаёт стабильный URL вида `https://<name>.<acct>.cloudflareaccess.com` или (если нет cf account) — random `trycloudflare.com`. Для prod — нужен named tunnel (URL не должен меняться при рестарте).

**Этот таск не имеет TDD-формы — это ops-процедура. Каждый чекбокс = одна команда или ручное действие.**

- [ ] **Step 1: Установить cloudflared**

```bash
ssh 192.168.88.28 'curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb && sudo dpkg -i /tmp/cf.deb && cloudflared --version'
```

Expected: `cloudflared version 2024.X.Y`.

- [ ] **Step 2: Авторизоваться в Cloudflare (manual в браузере)**

```bash
ssh -t 192.168.88.28 'cloudflared tunnel login'
```

Команда покажет URL — открой в браузере, залогинься в свой Cloudflare account, согласись с access. Без своего домена — сертификат всё равно появится.

Expected: файл `~/.cloudflared/cert.pem` создан на проде.

- [ ] **Step 3: Создать named tunnel**

```bash
ssh 192.168.88.28 'cloudflared tunnel create kleinanzeigen-api'
```

Expected: `Tunnel credentials written to ~/.cloudflared/<UUID>.json`. Cохрани UUID.

- [ ] **Step 4: Получить публичный URL**

Без своего домена есть два варианта:
- (a) Назначить `<random>.trycloudflare.com` — НЕ named, не подходит для prod
- (b) Привязать к Cloudflare-managed `<sub>.cfargotunnel.com` через тот же `tunnel route dns`

Команда:
```bash
ssh 192.168.88.28 'cloudflared tunnel route dns kleinanzeigen-api kleinanzeigen-api.cfargotunnel.com'
```

Если вернёт ошибку «zone not found» — нужно купить домен и привязать его к Cloudflare (см. Task 5 alt-path ниже).

**Alt-path: купленный домен** (если выбираешь B архитектуру вместо A):
```bash
# Прежде в Cloudflare dashboard: добавь свой домен, переключи NS на cf
ssh 192.168.88.28 'cloudflared tunnel route dns kleinanzeigen-api api.<your-domain>.com'
```

Запиши итоговый URL — он понадобится в Task 8 для подстановки в `web-app/js/api.js`.

- [ ] **Step 5: Создать systemd unit**

Содержимое `/etc/systemd/system/cloudflared.service`:

```ini
[Unit]
Description=Cloudflare Tunnel for kleinanzeigen-bot API
After=network.target kleinanzeigen-bot.service

[Service]
Type=simple
User=pg
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --config /home/pg/.cloudflared/config.yml run kleinanzeigen-api
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Команда (через heredoc):
```bash
ssh 192.168.88.28 'sudo tee /etc/systemd/system/cloudflared.service > /dev/null <<"EOF"
[Unit]
Description=Cloudflare Tunnel for kleinanzeigen-bot API
After=network.target kleinanzeigen-bot.service

[Service]
Type=simple
User=pg
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --config /home/pg/.cloudflared/config.yml run kleinanzeigen-api
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF'
```

- [ ] **Step 6: Создать config.yml для tunnel**

`/home/pg/.cloudflared/config.yml`:

```yaml
tunnel: kleinanzeigen-api
credentials-file: /home/pg/.cloudflared/<UUID>.json

ingress:
  - hostname: kleinanzeigen-api.cfargotunnel.com
    service: http://127.0.0.1:8080
  - service: http_status:404
```

ЗАМЕНИ `<UUID>` на UUID из Step 3.

- [ ] **Step 7: Стартовать сервис**

```bash
ssh 192.168.88.28 'sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared && sleep 3 && systemctl status cloudflared --no-pager | head -15'
```

Expected: `active (running)`.

- [ ] **Step 8: Проверить что туннель отдаёт FastAPI снаружи**

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://kleinanzeigen-api.cfargotunnel.com/api/ma/health
```

Expected: `422` (header missing — это и есть успех, FastAPI достижим через https).

- [ ] **Step 9: Commit unit-файла в репо для документации**

Скопировать unit на /home/pg/kleinanzeigen-bot/cloudflared.service:

```bash
ssh 192.168.88.28 'sudo cp /etc/systemd/system/cloudflared.service /home/pg/kleinanzeigen-bot/cloudflared.service && sudo chown pg:pg /home/pg/kleinanzeigen-bot/cloudflared.service && git -C /home/pg/kleinanzeigen-bot add cloudflared.service && git -C /home/pg/kleinanzeigen-bot commit -m "ops: cloudflared systemd unit (named tunnel for /api/ma)"'
```

---

## Task 6: GitHub Pages config (manual)

**Этот таск выполняется руками в GitHub UI.** Один раз на репо.

- [ ] **Step 1: Открыть Settings → Pages в репо**

URL: https://github.com/pgolitech-lab/kleinanzeigen-bot/settings/pages

- [ ] **Step 2: Source**

Выбрать `Deploy from a branch`.

- [ ] **Step 3: Branch**

`main` (или `master` если так), Folder: `/web-app`. Click Save.

- [ ] **Step 4: Подождать первый деплой**

Через 30-90 секунд страница покажет «Your site is live at https://pgolitech-lab.github.io/kleinanzeigen-bot/».

- [ ] **Step 5: Smoke-test что Pages раздаёт index.html**

```bash
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" https://pgolitech-lab.github.io/kleinanzeigen-bot/
```

Expected: `200 text/html` или `200 text/html; charset=utf-8`.

```bash
curl -s https://pgolitech-lab.github.io/kleinanzeigen-bot/js/app.js | head -3
```

Expected: первые строки нашего `app.js` (импорт из ./tg.js).

---

## Task 7: BotFather menu button (manual)

- [ ] **Step 1: Открыть @BotFather**

В Telegram — найти @BotFather, написать `/mybots`.

- [ ] **Step 2: Выбрать своего бота**

Нажать на бота из списка.

- [ ] **Step 3: Bot Settings → Menu Button**

Нажать `Bot Settings` → `Menu Button` → `Configure menu button`.

- [ ] **Step 4: Ввести URL**

Send: `https://pgolitech-lab.github.io/kleinanzeigen-bot/`

- [ ] **Step 5: Ввести button text**

Send: `Operator` (или любой). Это текст pill-кнопки слева от поля ввода в чате с ботом.

- [ ] **Step 6: Smoke-test в Telegram**

Открыть DM с ботом — слева от поля ввода появилась pill-кнопка. Тапнуть — открывается WebApp.

Если страница не загружается — вернись к Task 6 и проверь URL.

---

## Task 8: End-to-end smoke + финальный API_BASE подкат

**Files:**
- Modify: `web-app/js/api.js` — подставить реальный cloudflared URL вместо пустого API_BASE

- [ ] **Step 1: Подставить API_BASE в web-app/js/api.js**

Заменить:
```javascript
export const API_BASE = "";
```
на (URL — из Task 5 step 4):
```javascript
export const API_BASE = "https://kleinanzeigen-api.cfargotunnel.com";
```

- [ ] **Step 2: Bump version в index.html (cache-bust)**

В `web-app/index.html` найти:
```html
<script type="module" src="js/app.js?v=20260509"></script>
```

И обновить `?v=` на сегодняшнюю дату или timestamp:
```html
<script type="module" src="js/app.js?v=20260509-2"></script>
```

(GitHub Pages кэширует ~10 минут — версия в URL форсит refetch.)

- [ ] **Step 3: Commit**

```bash
ssh 192.168.88.28 'git -C /home/pg/kleinanzeigen-bot add web-app/js/api.js web-app/index.html && git -C /home/pg/kleinanzeigen-bot commit -m "feat(ma): wire API_BASE to cloudflared tunnel"'
```

- [ ] **Step 4: Подождать ~60 сек пока GitHub Pages пересобирает**

- [ ] **Step 5: Открыть MA из Telegram**

Тапнуть на pill-кнопку «Operator» в DM с ботом.

Expected на экране MA:
```
✅ Authenticated
<твой first_name> (@<username>)
user_id: <твой tg id>

{
  "ok": true,
  "user_id": ...,
  ...
}
```

- [ ] **Step 6: Negative test — оператор без авторизации**

Если у тебя есть второй TG-аккаунт не входящий в `telegram_authorized` (или временно убрать своего из CSV в /settings):

Открыть MA с этого аккаунта — должно появиться `❌ Ошибка / HTTP 403: user not authorized`.

После теста — вернуть авторизованный CSV если менял.

- [ ] **Step 7: Финальная проверка журнала**

```bash
ssh 192.168.88.28 'journalctl -u kleinanzeigen-bot -n 50 --no-pager | grep -E "GET /api/ma|401|403" | tail -10'
```

Expected: видно успешные `200 OK` для своих запросов, и (если делал negative test) `403`.

- [ ] **Step 8: Phase 1 acceptance — отметить завершение**

Phase 1 закрыт когда:
- ✅ Открыл MA из TG menu
- ✅ Увидел свой first_name + username + user_id
- ✅ Negative test (403 на чужого) сработал
- ✅ Логи backend'а без ERROR/Traceback по `/api/ma/*`

---

## Operations summary (ссылка для будущих фаз)

После Phase 1 настроены:
- Cloudflared tunnel: `https://kleinanzeigen-api.cfargotunnel.com` → 127.0.0.1:8080
- GitHub Pages: https://pgolitech-lab.github.io/kleinanzeigen-bot/ ← `main:/web-app/`
- BotFather menu button: указывает на Pages URL

Phase 2-4 не трогают эту инфраструктуру — только добавляют код в `web/app.py` (новые `/api/ma/*` endpoint'ы) и в `web-app/js/screens/` (новые экраны).

## Что НЕ делает Phase 1 (откладывается)

- `modules/operator_lock.py` extraction — будет в Phase 3 когда MA реально берёт lock на review-карточку. Текущий `_THREAD_LOCKS` в telegram_bot.py остаётся как есть.
- Все кроме `/api/ma/health` endpoint'ы.
- Все экраны кроме health-check'а на главной.
- Service worker / cache strategy.
- Rate-limiting per user_id.
