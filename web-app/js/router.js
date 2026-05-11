// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js?v=20260511-4";
import * as thread from "./screens/thread.js?v=20260511-4";
import * as history from "./screens/history.js?v=20260511-4";
import * as settings from "./screens/settings.js?v=20260511-4";
import * as review from "./screens/review.js?v=20260511-4";
import * as sales from "./screens/sales.js?v=20260511-4";
import { setError } from "./utils.js?v=20260511-4";
import { hideBack, showBack } from "./tg.js?v=20260511-4";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, params: () => ({}) },
  { pattern: /^#\/sales\/?$/,             screen: sales,    params: () => ({}) },
  { pattern: /^#\/thread\/([^/]+)\/msg\/(.+)$/,  screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]), focus_msg_id: decodeURIComponent(m[2]) }) },  // focused deep-link
  { pattern: /^#\/thread\/([^/]+)\/?$/,         screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/settings\/?$/,          screen: settings, params: () => ({}) },
  { pattern: /^#\/review\/(.+)$/,         screen: review, params: m => ({ msg_id: decodeURIComponent(m[1]) }) },
];

function navigateBack() {
  if (location.hash === "#/pipeline" || !location.hash) {
    return;
  }
  location.hash = "#/pipeline";
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
