// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js?v=20260720-1";
import * as thread from "./screens/thread.js?v=20260720-1";
import * as client from "./screens/client.js?v=20260720-1";
import * as settings from "./screens/settings.js?v=20260720-1";
import * as review from "./screens/review.js?v=20260720-1";
import * as sales from "./screens/sales.js?v=20260720-1";
import * as detected from "./screens/detected.js?v=20260720-1";
import * as dashboard from "./screens/dashboard.js?v=20260720-1";
import * as clients from "./screens/clients.js?v=20260720-1";
import * as scout from "./screens/scout.js?v=20260720-1";
import { setError } from "./utils.js?v=20260720-1";
import { hideBack, showBack, setTitle } from "./components/backbar.js?v=20260720-1";

const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/dashboard\/?$/,         screen: dashboard, title: "Обзор", params: () => ({}) },
  { pattern: /^#\/clients\/?$/,          screen: clients, title: "Клиенты", params: () => ({}) },
  { pattern: /^#\/sales\/?$/,             screen: sales,    title: "Продажи", params: () => ({}) },
  { pattern: /^#\/scout\/?$/,             screen: scout,    title: "Рынок", params: () => ({}) },
  { pattern: /^#\/detected\/?$/,          screen: detected, title: "Проверка продаж", params: () => ({}) },
  { pattern: /^#\/thread\/([^/]+)\/msg\/(.+)$/,  screen: thread, title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]), focus_msg_id: decodeURIComponent(m[2]) }) },  // focused deep-link
  { pattern: /^#\/thread\/([^/]+)\/?$/,         screen: thread, title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: client, title: "Клиент", params: m => ({ email: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/settings\/?$/,          screen: settings, title: "Настройки", params: () => ({}) },
  { pattern: /^#\/review\/(.+)$/,         screen: review, title: "Открываю…", params: m => ({ msg_id: decodeURIComponent(m[1]) }) },
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
        if (r.screen === pipeline || r.screen === dashboard || r.screen === clients || r.screen === sales || r.screen === scout) {
          hideBack();
        } else {
          showBack(navigateBack);
        }
        setTitle(r.title);
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
