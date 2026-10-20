// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js";
import * as thread from "./screens/thread.js";
import * as review from "./screens/review.js";
import * as dashboard from "./screens/dashboard.js";
import * as clients from "./screens/clients.js";
import { setError, setLoading } from "./utils.js";
import { hideBack, showBack, setTitle } from "./components/backbar.js";

// Редкие экраны грузятся лениво — при первом заходе, а не при старте MA.
const client   = () => import("./screens/client.js");
const settings = () => import("./screens/settings.js");
const sales    = () => import("./screens/sales.js");
const detected = () => import("./screens/detected.js");
const scout    = () => import("./screens/scout.js");

// root: true — корневой экран вкладки (без кнопки «Назад»).
const ROUTES = [
  { pattern: /^#?\/?$/,                   screen: pipeline, root: true, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, root: true, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/dashboard\/?$/,         screen: dashboard, root: true, title: "Обзор", params: () => ({}) },
  { pattern: /^#\/clients\/?$/,           screen: clients,  root: true, title: "Клиенты", params: () => ({}) },
  { pattern: /^#\/sales\/?$/,             screen: sales,    root: true, title: "Продажи", params: () => ({}) },
  { pattern: /^#\/scout\/?$/,             screen: scout,    root: true, title: "Рынок", params: () => ({}) },
  { pattern: /^#\/detected\/?$/,          screen: detected, title: "Проверка продаж", params: () => ({}) },
  { pattern: /^#\/thread\/([^/]+)\/msg\/(.+)$/,  screen: thread, title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]), focus_msg_id: decodeURIComponent(m[2]) }) },  // focused deep-link
  { pattern: /^#\/thread\/([^/]+)\/?$/,         screen: thread, title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/client\/(.+)$/,         screen: client, title: "Клиент", params: m => ({ email: decodeURIComponent(m[1]) }) },
  { pattern: /^#\/settings\/?$/,          screen: settings, title: "Настройки", params: () => ({}) },
  { pattern: /^#\/review\/(.+)$/,         screen: review, title: "Открываю…", params: m => ({ msg_id: decodeURIComponent(m[1]) }) },
];

// Модуль экрана: уже загруженный или ленивый загрузчик () => import(...).
async function loadScreen(screen) {
  if (typeof screen !== "function") return screen;
  try {
    return await screen();
  } catch (e) {
    console.error("[router] screen load failed:", e);
    throw new Error("Не удалось загрузить экран. Проверьте интернет и откройте его ещё раз.");
  }
}

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
        if (r.root) {
          hideBack();
        } else {
          showBack(navigateBack);
        }
        setTitle(r.title);
        // Каждому переходу — свежий контейнер. Запоздавший ответ прошлого
        // экрана (опрос, медленный fetch, LLM) пишет в отцепленный от DOM div и
        // не перерисовывает чужой экран; экраны проверяют mount.isConnected,
        // чтобы остановить таймеры и не захватывать локи впустую.
        const screenMount = document.createElement("div");
        mount.replaceChildren(screenMount);
        if (typeof r.screen === "function") setLoading(screenMount);
        loadScreen(r.screen)
          .then(mod => {
            if (screenMount.isConnected) return mod.render(screenMount, r.params(m));
          })
          .catch(e => {
            console.error("[router] render failed:", e);
            if (screenMount.isConnected) setError(screenMount, e.message ?? String(e));
          });
        return;
      }
    }
    const screenMount = document.createElement("div");
    mount.replaceChildren(screenMount);
    setError(screenMount, `Неизвестный путь: ${hash}`);
  }

  window.addEventListener("hashchange", dispatch);
  dispatch();
}
