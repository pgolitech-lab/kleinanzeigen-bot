// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js";
import * as thread from "./screens/thread.js";
import * as history from "./screens/history.js";
import { setError } from "./utils.js";
import { hideBack, showBack } from "./tg.js";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, params: () => ({}) },
  { pattern: /^#\/thread\/(.+)$/,         screen: thread, params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: history, params: m => ({ email: decodeURIComponent(m[1]) }) },
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
