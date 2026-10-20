# Чтение переписки с сайта Kleinanzeigen (m-nachrichten) — только чтение.
#
# Зачем: владелец часто отвечает покупателям прямо в приложении/на сайте KZ, мимо бота.
# В почту эти ответы не попадают (в уведомлениях только реплика покупателя), поэтому
# бот считает такие треды неотвеченными. Здесь — доступ к переписке на сайте:
#
#   * вход: Playwright (Auth0 Universal Login на login.kleinanzeigen.de: e-mail → «Weiter»
#     → пароль → «Weiter»), сессия хранится в storage_state
#     ~/.local/state/kleinanzeigen-bot/ka_session_<account>.json (0600);
#   * данные: JSON-API, который вызывает сам веб-клиент сообщений —
#     GET https://gateway.kleinanzeigen.de/messagebox/api/users/{userId}/conversations/{id}
#     с Bearer-токеном. Токен и userId не вычисляем сами, а берём из запроса, который
#     делает официальный клиент на странице m-nachrichten (перехват request), и повторяем
#     запрос fetch'ем ИЗНУТРИ страницы — тот же TLS-отпечаток/куки, что у браузера
#     (www и gateway за Akamai: голый httpx/curl получает 403).
#   * conversationId на сайте == заголовок X-Conversation-ID писем-уведомлений
#     (вид «49sml:2cxlxdm:2pxg5bw9w»; в письме есть ссылка
#     m-nachrichten.html?conversationId=<тот же id>).
#
# Защита аккаунта: на сайте НИЧЕГО не нажимаем, кроме согласия на cookie, входа и
# навигации. Капча / 2FA / неверный пароль → стоп, аккаунт блокируется в state-файле
# (без повторных попыток входа), оператору уходит уведомление (делает ka_site_sync).
# Параллельных браузеров нет (fcntl-лок в ka_site_sync), паузы между запросами.

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

WWW = "https://www.kleinanzeigen.de"
MESSAGES_URL = f"{WWW}/m-nachrichten.html"
GATEWAY = "https://gateway.kleinanzeigen.de"
LOGIN_HOST = "login.kleinanzeigen.de"

# UA собираем из версии реального Chromium Playwright (major), чтобы UA не расходился
# с sec-ch-ua / userAgentData; «HeadlessChrome» в UA не светим.
UA_TEMPLATE = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def ua_for(browser_version: str) -> str:
    major = (browser_version or "").split(".")[0]
    return UA_TEMPLATE.format(major=major if major.isdigit() else "129")

STATE_DIR = Path(os.path.expanduser("~/.local/state/kleinanzeigen-bot"))

# id переписки: base36-группы через двоеточие. Строгая проверка — id попадает в URL.
CONV_ID_RE = re.compile(r"^[0-9a-z]{2,20}(?::[0-9a-z]{2,20}){1,4}$", re.I)
_GW_USER_RE = re.compile(r"/messagebox/api/users/([^/?#]+)/")

NAV_TIMEOUT_MS = 45_000
# Основная кнопка Auth0. Первая button[name=action] на странице — скрытая ловушка
# (opacity:0, pointer-events:none): нестрогий селектор кликнул бы её и упал по таймауту.
SUBMIT_BTN = "button[data-action-button-primary=true]"
# Видимые виджеты капчи (iframe или div — Friendly Captcha рисуется без iframe).
_CAPTCHA_JS = """() => {
  const sel = 'iframe[src*="arkoselabs"], iframe[src*="recaptcha"], iframe[src*="hcaptcha"],'
    + ' iframe[src*="challenges.cloudflare"], iframe[src*="friendlycaptcha"],'
    + ' .frc-captcha, .cf-turnstile, .h-captcha, .g-recaptcha, [data-captcha-provider] iframe';
  return [...document.querySelectorAll(sel)].some(e => {
    const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 20 && r.height > 20 && st.visibility !== 'hidden' && st.display !== 'none'
      && parseFloat(st.opacity || '1') > 0.1;
  });
}"""
TOKEN_WAIT_MS = 15_000


# --- Ошибки ---------------------------------------------------------------------------

class KaSiteError(Exception):
    """Базовая ошибка доступа к сайту."""


class KaBlocked(KaSiteError):
    """Сайт требует действия человека — дальше НЕ пробуем, пока оператор не разберётся.

    kind: 'captcha' | 'mfa' | 'bad_credentials' | 'no_credentials' | 'login_failed'.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class KaFormatError(KaSiteError):
    """Ответ сайта не похож на ожидаемый — формат поменялся, разбирать вслепую нельзя."""


class KaNotFound(KaSiteError):
    """Переписки с таким id нет (удалена / чужой аккаунт)."""


# --- Чистые функции (тестируются без браузера) ------------------------------------------

def is_valid_conversation_id(conv_id: Optional[str]) -> bool:
    return bool(conv_id) and bool(CONV_ID_RE.match(conv_id or ""))


def session_path(account_id: int) -> Path:
    return STATE_DIR / f"ka_session_{int(account_id)}.json"


def _to_utc_iso(value: Any) -> str:
    """Время сайта → naive UTC ISO (как created_at/sent_at в БД). Падает на мусоре."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = value / 1000.0 if value > 1e11 else float(value)  # мс или с
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(value, str) and value.strip():
        s = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Без зоны — сайт отдаёт берлинское время.
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
    else:
        raise ValueError(f"нет времени: {value!r}")
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _is_ours(msg: dict[str, Any]) -> Optional[bool]:
    """True — реплика нашего аккаунта, False — собеседника, None — не понять."""
    b = _first(msg, "boundness", "direction")
    if isinstance(b, str):
        b = b.strip().upper()
        if b in ("OUTBOUND", "OUT", "SENT", "OUTGOING"):
            return True
        if b in ("INBOUND", "IN", "RECEIVED", "INCOMING"):
            return False
    for k in ("outbound", "isOutbound", "own", "isOwn", "mine"):
        if isinstance(msg.get(k), bool):
            return msg[k]
    return None


def parse_conversation(data: Any, conversation_id: str) -> list[dict[str, Any]]:
    """JSON переписки → [{sender, ours, text, sent_at, external_id}] по времени.

    sender: 'seller' | 'buyer' (с учётом нашей роли в переписке; почти всегда мы — продавец),
    ours: реплика нашего аккаунта. external_id = '<conversationId>:<messageId>' —
    уникален глобально, ключ идемпотентности add_external_reply.
    Сообщения без текста (только фото, системные карточки) пропускаются.
    Формат не узнан (нет messages, у сообщений нет id/направления/времени) → KaFormatError:
    молча вернуть [] нельзя — тогда тред навсегда «неотвечен» без сигнала.
    """
    if not isinstance(data, dict):
        raise KaFormatError(f"ответ не объект: {type(data).__name__}")
    msgs = data.get("messages")
    if not isinstance(msgs, list):
        raise KaFormatError(f"нет списка messages (ключи: {sorted(data)[:15]})")
    role = str(data.get("role") or "").strip().lower()
    we_are_buyer = role == "buyer"
    out: list[dict[str, Any]] = []
    textless_plain = 0  # без текста И без вложений — подозрение, что поле текста переименовали
    for m in msgs:
        if not isinstance(m, dict):
            raise KaFormatError("сообщение не объект")
        mtype = str(m.get("type") or "MESSAGE").upper()
        if mtype not in ("MESSAGE", "TEXT", "TEXT_MESSAGE"):
            # Системные карточки (оплата, доставка, «reserviert» …) — не реплики.
            continue
        mid = _first(m, "messageId", "id")
        ours = _is_ours(m)
        when = _first(m, "receivedDate", "sentDate", "createdAt", "date", "timestamp")
        if mid is None or ours is None or when is None:
            raise KaFormatError(
                f"сообщение без id/направления/времени (ключи: {sorted(m)[:15]})")
        try:
            sent_at = _to_utc_iso(when)
        except ValueError as e:
            raise KaFormatError(f"время не разобрано: {when!r}") from e
        text = _first(m, "text", "textShort", "body")
        if not isinstance(text, str) or not text.strip():
            if not m.get("attachments"):
                textless_plain += 1
            continue
        if ours:
            sender = "buyer" if we_are_buyer else "seller"
        else:
            sender = "seller" if we_are_buyer else "buyer"
        out.append({
            "sender": sender,
            "ours": ours,
            "text": text.strip(),
            "sent_at": sent_at,
            "external_id": f"{conversation_id}:{mid}",
        })
    if not out and textless_plain:
        # Ни у одного сообщения не нашли текст — скорее всего, сменилось имя поля.
        raise KaFormatError(f"ни у одного из {len(msgs)} сообщений нет текста "
                            f"(ключи: {sorted(msgs[0])[:15]})")
    out.sort(key=lambda r: r["sent_at"])
    return out


def user_id_from_token(auth: str) -> Optional[str]:
    """userId из payload JWT (запасной путь, если не поймали URL клиента)."""
    tok = auth.split()[-1] if auth else ""
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    for k in ("userId", "user_id", "uid", "preferred_username", "sub"):
        v = payload.get(k)
        if isinstance(v, (str, int)) and str(v).strip() and "@" not in str(v):
            v = str(v).strip()
            # Auth0-подобный sub «auth0|12345» → берём хвост
            return v.split("|")[-1]
    return None


def detect_login_obstacle(url: str, html: str) -> Optional[str]:
    """Что мешает войти на странице логина: 'captcha' | 'mfa' | 'bad_credentials' | None."""
    u = (url or "").lower()
    h = (html or "").lower()
    if "/u/mfa" in u or "email-verification" in u or "/u/login/passwordless" in u \
            or re.search(r'<input[^>]+(?:name|id)="code"', h):
        return "mfa"
    # Капча именно показана, а не упомянута в шаблоне Auth0 (строки «captcha-*» есть на
    # любой странице входа): ищем отрисованный iframe провайдера.
    if re.search(r'<iframe[^>]+src="[^"]*(?:arkoselabs|recaptcha|hcaptcha|'
                 r'challenges\.cloudflare|friendlycaptcha|auth0\.com/captcha)', h):
        return "captcha"
    if re.search(r'id="error-element-(?:password|username)"', h) \
            or "falsches passwort" in h or "wrong password" in h or "wrong email or password" in h:
        return "bad_credentials"
    return None


# --- Браузерная сессия -----------------------------------------------------------------

def _pause(lo: float = 1.5, hi: float = 4.0) -> None:
    time.sleep(random.uniform(lo, hi))


class KaSession:
    """Одна сессия Playwright на аккаунт. Использовать как context manager, последовательно.

    get_conversation(conv_id) → JSON переписки. Вход — только если storage_state
    протух и заданы креды; препятствие → KaBlocked (без повторов).
    """

    def __init__(self, account_id: int, email: Optional[str] = None,
                 password: Optional[str] = None, headless: bool = True,
                 mfa_code_cb: Optional[Callable[[], str]] = None) -> None:
        self.account_id = int(account_id)
        self.email = (email or "").strip()
        self.password = password or ""
        self.headless = headless
        self.mfa_code_cb = mfa_code_cb  # только интерактивный скрипт входа
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._auth: Optional[str] = None
        self._user_id: Optional[str] = None
        self.logged_in_now = False  # был ли вход паролем в этой сессии

    # -- lifecycle
    def __enter__(self) -> "KaSession":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=self.headless)
            sp = session_path(self.account_id)
            kwargs: dict[str, Any] = dict(user_agent=ua_for(self._browser.version), locale="de-DE",
                                          timezone_id="Europe/Berlin",
                                          viewport={"width": 1366, "height": 900})
            if sp.exists():
                kwargs["storage_state"] = str(sp)
            self._ctx = self._browser.new_context(**kwargs)
            self._page = self._ctx.new_page()
            self._page.on("request", self._sniff)
        except BaseException:
            # __exit__ не вызовется — иначе драйвер Playwright останется висеть в процессе бота.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    def _sniff(self, req: Any) -> None:
        """Перехват запроса официального клиента к messagebox: Bearer + userId."""
        url = req.url
        if not url.startswith(GATEWAY) or "/messagebox/" not in url:
            return
        auth = req.headers.get("authorization")
        m = _GW_USER_RE.search(url)
        if auth:
            self._auth = auth
        if m:
            self._user_id = m.group(1)

    def save_state(self) -> None:
        sp = session_path(self.account_id)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        tmp = sp.with_suffix(".tmp")
        # Создаём сразу с 0600: куки сессии = доступ к аккаунту.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._ctx.storage_state(), f)
        os.replace(tmp, sp)
        os.chmod(sp, 0o600)

    # -- навигация / вход
    def _accept_cookies(self) -> None:
        for sel in ("#gdpr-banner-accept", "button:has-text('Alle akzeptieren')",
                    "button:has-text('Akzeptieren')"):
            try:
                b = self._page.query_selector(sel)
                if b and b.is_visible():
                    b.click(timeout=3000)
                    self._page.wait_for_timeout(600)
                    return
            except Exception:  # баннер необязателен; не нашли — идём дальше
                logger.debug("ka_site: cookie-баннер %s не кликнулся", sel, exc_info=True)

    def _on_login_page(self) -> bool:
        return LOGIN_HOST in (self._page.url or "")

    def open_messages(self) -> None:
        """Открыть m-nachrichten, при необходимости войти; дождаться токена клиента."""
        self._page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        _pause(1.0, 2.5)
        self._accept_cookies()
        if self._on_login_page() or "m-einloggen" in (self._page.url or ""):
            self._login()
        self._wait_token()

    def _check_obstacle(self) -> None:
        kind = detect_login_obstacle(self._page.url, self._page.content())
        if kind is None:
            try:
                if self._page.evaluate(_CAPTCHA_JS):
                    kind = "captcha"
            except Exception:
                logger.debug("ka_site: проверка видимой капчи не удалась", exc_info=True)
        if kind == "mfa" and self.mfa_code_cb is not None:
            return
        if kind:
            raise KaBlocked(kind, f"страница {self._page.url.split('?')[0]}")

    def _login(self) -> None:
        try:
            self._login_steps()
        except KaBlocked:
            raise
        except Exception as e:
            # Таймаут/селектор (сменилась вёрстка Auth0): НЕ пробовать снова каждые 45 мин —
            # блок login_failed (см. ka_site_sync.BLOCK_HOURS) + уведомление оператору.
            raise KaBlocked("login_failed", f"{type(e).__name__}: {str(e)[:160]}") from e

    def _login_steps(self) -> None:
        if not (self.email and self.password):
            raise KaBlocked("no_credentials", "сессия протухла, логин/пароль Kleinanzeigen не заданы")
        p = self._page
        if not self._on_login_page():
            # Сессия протухла без редиректа — сами открываем вход (уводит на Auth0).
            if "m-einloggen" not in (p.url or ""):
                p.goto(f"{WWW}/m-einloggen.html?targetUrl=/m-nachrichten.html",
                       wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            p.wait_for_url(f"**{LOGIN_HOST}/**", timeout=NAV_TIMEOUT_MS)
        self._check_obstacle()
        p.wait_for_selector("input#username", timeout=NAV_TIMEOUT_MS)
        _pause()
        p.fill("input#username", self.email)
        _pause(0.8, 2.0)
        p.click(SUBMIT_BTN)
        try:
            p.wait_for_selector("input#password, input[name=password]", state="visible",
                                timeout=20_000)
        except Exception as e:
            self._check_obstacle()
            raise KaBlocked("login_failed", "не появилось поле пароля") from e
        self._check_obstacle()
        _pause()
        p.fill("input#password, input[name=password]", self.password)
        _pause(0.8, 2.0)
        p.click(SUBMIT_BTN)
        try:
            p.wait_for_url(f"{WWW}/**", timeout=30_000)
        except Exception as e:
            self_kind = None
            try:
                self._check_obstacle()
            except KaBlocked as b:
                self_kind = b.kind
            kind = self_kind or detect_login_obstacle(p.url, p.content())
            if kind == "mfa" and self.mfa_code_cb is not None:
                self._enter_mfa_code()
            else:
                raise KaBlocked(kind or "login_failed", "после пароля остались на странице входа") from e
        self.logged_in_now = True
        if not p.url.startswith(MESSAGES_URL):
            _pause()
            p.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if self._on_login_page():
            raise KaBlocked("login_failed", "после входа снова форма логина")

    def _enter_mfa_code(self) -> None:
        """Код из письма/SMS — только из интерактивного скрипта (оператор вводит руками)."""
        p = self._page
        code = (self.mfa_code_cb() if self.mfa_code_cb else "").strip()
        if not code:
            raise KaBlocked("mfa", "код подтверждения не введён")
        p.fill("input#code, input[name=code]", code)
        p.click(SUBMIT_BTN)
        try:
            p.wait_for_url(f"{WWW}/**", timeout=30_000)
        except Exception as e:
            raise KaBlocked("mfa", "код не принят") from e

    def _wait_token(self) -> None:
        deadline = time.monotonic() + TOKEN_WAIT_MS / 1000
        while time.monotonic() < deadline and not (self._auth and self._user_id):
            self._page.wait_for_timeout(500)
        if self._on_login_page():
            raise KaBlocked("login_failed", "сайт снова просит вход")
        if not self._auth:
            # Запасной путь: тот же эндпоинт, что дёргает клиент для токена.
            r = self._fetch(f"{WWW}/m-access-token.json", auth=None)
            if r["status"] in (401, 403):
                # Сессия протухла без редиректа на форму входа.
                if self.logged_in_now:
                    raise KaBlocked("login_failed", f"после входа токен не выдан (HTTP {r['status']})")
                self._login()
                self._wait_token()
                return
            if r["status"] != 200:
                raise KaSiteError(f"m-access-token.json: HTTP {r['status']}")
            self._auth = r["headers"].get("authorization") or None
        if self._auth and not self._user_id:
            self._user_id = user_id_from_token(self._auth)
        if not (self._auth and self._user_id):
            raise KaFormatError("не нашли Bearer-токен/userId клиента сообщений "
                                "(клиент m-nachrichten изменился?)")

    def _fetch(self, url: str, auth: Optional[str]) -> dict[str, Any]:
        """fetch изнутри страницы → {status, body, headers}."""
        return self._page.evaluate(
            """async ([url, auth]) => {
                const h = {accept: 'application/json'};
                if (auth) h.authorization = auth;
                const r = await fetch(url, {headers: h, credentials: 'include'});
                const headers = {};
                r.headers.forEach((v, k) => { headers[k] = v; });
                return {status: r.status, body: await r.text(), headers};
            }""",
            [url, auth],
        )

    # -- API
    def get_conversation(self, conversation_id: str) -> Any:
        if not is_valid_conversation_id(conversation_id):
            raise ValueError(f"плохой conversationId: {conversation_id!r}")
        if not self._auth:
            self.open_messages()
        url = (f"{GATEWAY}/messagebox/api/users/{self._user_id}/conversations/"
               f"{conversation_id}?contentWarnings=true")
        r = self._fetch(url, self._auth)
        if r["status"] == 401:
            # Токен короткоживущий — перезагрузить клиент один раз и повторить.
            self._auth = None
            self.open_messages()
            r = self._fetch(url, self._auth)
        if r["status"] == 404:
            raise KaNotFound(f"{conversation_id}: HTTP 404")
        if r["status"] == 403:
            # Akamai/отозванный токен/ограничение аккаунта — не «переписки нет».
            raise KaSiteError(f"{conversation_id}: HTTP 403 (доступ запрещён)")
        if r["status"] != 200:
            raise KaSiteError(f"{conversation_id}: HTTP {r['status']}")
        try:
            return json.loads(r["body"])
        except json.JSONDecodeError as e:
            raise KaFormatError(f"{conversation_id}: не JSON ({r['body'][:80]!r})") from e


def cookies_to_storage_state(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Экспорт кук из браузера (Cookie-Editor / EditThisCookie JSON) → storage_state.

    Аварийный путь, если вход упирается в капчу: оператор входит на своём компьютере
    и выгружает куки kleinanzeigen.de."""
    out = []
    for c in cookies:
        dom = c.get("domain") or ""
        if "kleinanzeigen.de" not in dom:
            continue
        exp = c.get("expirationDate", c.get("expires", -1))
        same = str(c.get("sameSite") or "Lax").lower()
        same = {"no_restriction": "None", "none": "None", "strict": "Strict"}.get(same, "Lax")
        out.append({
            "name": c["name"], "value": c["value"], "domain": dom,
            "path": c.get("path") or "/",
            "expires": float(exp) if exp not in (None, "") else -1,
            "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure", True)),
            "sameSite": same,
        })
    if not out:
        raise ValueError("в файле нет кук kleinanzeigen.de")
    return {"cookies": out, "origins": []}
