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
