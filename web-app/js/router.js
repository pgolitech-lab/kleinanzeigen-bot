// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js?v=20260623-100509";
import * as thread from "./screens/thread.js?v=20260623-100509";
import * as history from "./screens/history.js?v=20260623-100509";
import * as settings from "./screens/settings.js?v=20260623-100509";
import * as review from "./screens/review.js?v=20260623-100509";
import * as sales from "./screens/sales.js?v=20260623-100509";
import * as detected from "./screens/detected.js?v=20260623-100509";
import { setError } from "./utils.js?v=20260623-100509";
import { hideBack, showBack } from "./tg.js?v=20260623-100509";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, params: () => ({}) },
  { pattern: /^#\/sales\/?$/,             screen: sales,    params: () => ({}) },
  { pattern: /^#\/detected\/?$/,          screen: detected, params: () => ({}) },
  { pattern: /^#\/thread\/([^/]+)\/msg\/(.+)$/,  screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]), focus_msg_id: decodeURIComponent(m[2]) }) },  // focused deep-link
  { pattern: /^#\/thread\/([^/]+)\/?$/,         screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/settings\/?$/,          screen: settings, params: () => ({}) },
  { pattern: /^#\/review\/(.+)$/,         screen: review, params: m => ({ msg_id: decodeURIComponent(m[1]) }) },
];

function navigateBack() {
  // Используем browser history если есть куда — это сохраняет «откуда пришёл»
  // (например из /detected открыл тред → back возвращает в /detected, не /pipeline).
  // Если истории нет — fallback на /pipeline.
  if (window.history.length > 1) {
    const before = location.hash;
    window.history.back();
    // Если через 100мс hash не изменился (history.back ничего не сделал) — fallback
    setTimeout(() => {
      if (location.hash === before) {
        location.hash = "#/pipeline";
      }
    }, 100);
    return;
  }
  if (location.hash !== "#/pipeline" && location.hash) {
    location.hash = "#/pipeline";
  }
}

export function start(mount) {
  function dispatch() {
    let hash = location.hash || "#/";
    // Telegram WebApp подкладывает #tgWebAppData=... в URL при запуске.
    // Это не наш роут — считаем за root (pipeline).
    if (!hash.startsWith("#/")) {
      hash = "#/";
    }
    for (const r of ROUTES) {
      const m = hash.match(r.pattern);
      if (m) {
        if (r.screen === pipeline) {
          hideBack();
        } else {
          showBack(navigateBack);
        }
        try {
          r.screen.render(mount, r.params(m));
        } catch (e) {
          console.error("[router] render failed:", e);
          setError(mount, e.message ?? String(e));
        }
        return;
      }
    }
    setError(mount, `Неизвестный путь: ${hash}`);
  }

  window.addEventListener("hashchange", dispatch);
  dispatch();
}
