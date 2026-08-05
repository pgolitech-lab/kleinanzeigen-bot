import { initData } from "./tg.js?v=20260805-052102";

// Cloudflare Quick Tunnel — URL ротируется при рестарте `cloudflared tunnel --url ...`.
// Обнови эту константу + bump cache-bust в index.html если tunnel поменялся.
//
// NB: backend (FastAPI на :8080) должен отдавать Access-Control-Allow-Origin для
// github.io origin'а — CORSMiddleware уже сконфигурирован в Phase 1.
// X-Telegram-Init-Data — non-simple header, всегда триггерит preflight.
export const API_BASE = "https://toilet-fabulous-childrens-yours.trycloudflare.com";

// Понятная инструкция вместо технической ошибки/вечной загрузки, когда
// Telegram не передал сессию (initData пустой) или сервер её отверг.
const SESSION_LOST_MSG =
  "Сессия Telegram потеряна. Закройте мини-приложение и откройте заново " +
  "через кнопку меню в боте.";

// Таймаут по умолчанию. Покрывает и загрузку экранов (GET, <1с при живой сети),
// и LLM-вызовы (Haiku/Sonnet, обычно <25с). Без него запрос, не дошедший до
// сервера (сеть клиента моргнула / старый кэш стучится в никуда), висел вечно —
// экран залипал на «Загрузка…»/«Открываю карточку…».
const DEFAULT_TIMEOUT_MS = 30000;
// Для LLM-вызовов (генерация ответа Sonnet, автопилот-превью с веб-поиском) —
// они легитимно идут дольше 30с. Передаётся явно в такие call-sites.
export const LLM_TIMEOUT_MS = 120000;

export async function api(path, { method = "GET", body = null, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const url = `${API_BASE}${path}`;
  const id = initData();
  // initData пустой — Telegram потерял контекст WebApp (обычно при смене сети
  // или пробуждении устройства). Запрос ушёл бы с пустым заголовком, Cloudflare
  // его отбрасывает, сервер отвечает 422 → экран вис на «Загрузка». Не шлём.
  if (!id) {
    throw new Error(SESSION_LOST_MSG);
  }
  const reqHeaders = {
    "X-Telegram-Init-Data": id,
    ...headers,
  };
  // Content-Type только когда есть body (RFC 7231: GET без body не должен иметь
  // Content-Type — некоторые строгие сервера ругаются, плюс лишний preflight).
  if (body !== null) {
    reqHeaders["Content-Type"] = "application/json";
  }
  const opts = { method, headers: reqHeaders };
  if (body !== null) opts.body = JSON.stringify(body);

  // Таймаут: без него мёртвый/недоступный запрос висит бесконечно.
  const ctrl = new AbortController();
  opts.signal = ctrl.signal;
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error(
        "Сервер не ответил вовремя. Проверьте связь и переоткройте " +
        "мини-приложение через кнопку меню в боте.");
    }
    // Сетевая ошибка (CORS, offline, мёртвый тоннель) — fetch реджектит TypeError
    throw new Error(
      "Не удалось связаться с сервером. Проверьте интернет и переоткройте " +
      "мини-приложение через кнопку меню в боте.");
  } finally {
    clearTimeout(timer);
  }
  // 401 — initData истекла/плохой hash; 422 — заголовок не дошёл (пустой →
  // срезан прокси). Оба = проблема сессии: показываем инструкцию, а НЕ
  // закрываем МА резко (иначе на гонке при старте окно мигает и закрывается).
  if (res.status === 401 || res.status === 422) {
    throw new Error(SESSION_LOST_MSG);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  // 204 No Content (например /lock/release, /reject) — нет JSON body
  if (res.status === 204 || res.headers.get("content-length") === "0") {
    return null;
  }
  return res.json();
}
