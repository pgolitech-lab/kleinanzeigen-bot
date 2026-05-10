import { initData, close } from "./tg.js?v=20260510-21";

// Cloudflare Quick Tunnel — URL ротируется при рестарте `cloudflared tunnel --url ...`.
// Обнови эту константу + bump cache-bust в index.html если tunnel поменялся.
//
// NB: backend (FastAPI на :8080) должен отдавать Access-Control-Allow-Origin для
// github.io origin'а — CORSMiddleware уже сконфигурирован в Phase 1.
// X-Telegram-Init-Data — non-simple header, всегда триггерит preflight.
export const API_BASE = "https://choice-drunk-curriculum-effectiveness.trycloudflare.com";

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
