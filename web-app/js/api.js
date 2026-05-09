import { initData, close } from "./tg.js";

// Production: будет заменено на реальный cloudflared URL после Task 5.
// Пока — пусто; Task 8 пропишет финальный URL и сделает повторный commit.
//
// NB: когда API_BASE станет абсолютным URL, backend ДОЛЖЕН отдавать
// Access-Control-Allow-Origin для нашего github.io origin'а
// (CORSMiddleware уже сконфигурирован в Phase 1).
// X-Telegram-Init-Data — non-simple header, всегда триггерит preflight.
export const API_BASE = "";

export async function api(path, { method = "GET", body = null, headers = {} } = {}) {
  const url = `${API_BASE}${path}`;
  const reqHeaders = {
    "X-Telegram-Init-Data": initData(),
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
