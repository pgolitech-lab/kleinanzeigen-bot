import { initData } from "./tg.js?v=20260722-2";

// Cloudflare Quick Tunnel — URL ротируется при рестарте `cloudflared tunnel --url ...`.
// Обнови эту константу + bump cache-bust в index.html если tunnel поменялся.
//
// NB: backend (FastAPI на :8080) должен отдавать Access-Control-Allow-Origin для
// github.io origin'а — CORSMiddleware уже сконфигурирован в Phase 1.
// X-Telegram-Init-Data — non-simple header, всегда триггерит preflight.
export const API_BASE = "https://makes-helps-instance-marker.trycloudflare.com";

// Понятная инструкция вместо технической ошибки/вечной загрузки, когда
// Telegram не передал сессию (initData пустой) или сервер её отверг.
const SESSION_LOST_MSG =
  "Сессия Telegram потеряна. Закройте мини-приложение и откройте заново " +
  "через кнопку меню в боте.";

export async function api(path, { method = "GET", body = null, headers = {} } = {}) {
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

  const res = await fetch(url, opts);
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
