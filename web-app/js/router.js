// Hash-router. Listens to location.hash и вызывает соответствующий screen.render().

import * as pipeline from "./screens/pipeline.js";
import * as thread from "./screens/thread.js";
import * as review from "./screens/review.js";
import * as dashboard from "./screens/dashboard.js";
import * as clients from "./screens/clients.js";
import { setError, setLoading } from "./utils.js";
import { hideBack, showBack, setTitle } from "./components/backbar.js";
import { closeSheet, isSheetOpen, requestCloseSheet } from "./components/sheet.js";
import { isSplit, layout, onLayoutChange } from "./layout.js";

// Редкие экраны грузятся лениво — при первом заходе, а не при старте MA.
const client   = () => import("./screens/client.js");
const settings = () => import("./screens/settings.js");
const sales    = () => import("./screens/sales.js");
const detected = () => import("./screens/detected.js");
const scout    = () => import("./screens/scout.js");

const more     = () => import("./screens/more.js");
const triage   = () => import("./screens/triage.js");
const stock    = () => import("./screens/stock.js");
const accounts = () => import("./screens/accounts.js");

// root: true — корневой экран вкладки (без кнопки «Назад»).
// list — на широком экране (Fold внутренний) слева держится этот список,
//        а экран маршрута открывается справа; key(params) — какой элемент
//        списка подсветить. На телефоне list игнорируется.
const ROUTES = [
  // flex — экран для Flip Flex Window («карман»): вместо списка — Разбор
  { pattern: /^#?\/?$/,                   screen: pipeline, root: true, list: "pipeline", flex: triage, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/pipeline\/?$/,          screen: pipeline, root: true, list: "pipeline", flex: triage, title: "Входящие", params: () => ({}) },
  { pattern: /^#\/triage\/?$/,            screen: triage, title: "Разбор", params: () => ({}) },
  { pattern: /^#\/dashboard\/?$/,         screen: dashboard, title: "Обзор", params: () => ({}) },
  { pattern: /^#\/clients\/?$/,           screen: clients,  root: true, list: "clients", title: "Клиенты", params: () => ({}) },
  { pattern: /^#\/sales\/?$/,             screen: sales,    root: true, title: "Продажи", params: () => ({}) },
  { pattern: /^#\/(?:stock|listings)\/?$/, screen: stock,  root: true, list: "stock", title: "Склад", params: () => ({}) },
  { pattern: /^#\/stock\/questions\/?$/,  screen: stock,  title: "Тот же товар?", params: () => ({ view: "questions" }) },
  { pattern: /^#\/stock\/types\/?$/,      screen: stock,  title: "Типы и себестоимость", params: () => ({ view: "types" }) },
  { pattern: /^#\/stock\/month\/?$/,      screen: stock,  title: "Отчёт за месяц", params: () => ({ view: "month" }) },
  { pattern: /^#\/stock\/(\d+)\/?$/,       screen: stock,  list: "stock", title: "Товар", params: m => ({ id: m[1] }), key: p => p.id },
  { pattern: /^#\/scout\/?$/,             screen: scout,    title: "Рынок", params: () => ({}) },
  { pattern: /^#\/more\/?$/,              screen: more,     root: true, title: "Ещё", params: () => ({}) },
  { pattern: /^#\/detected\/?$/,          screen: detected, title: "Найденные продажи", params: () => ({}) },
  { pattern: /^#\/thread\/([^/]+)\/msg\/(.+)$/,  screen: thread, list: "pipeline", title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]), focus_msg_id: decodeURIComponent(m[2]) }), key: p => p.thread_id },  // focused deep-link
  { pattern: /^#\/thread\/([^/]+)\/?$/,         screen: thread, list: "pipeline", title: "Переписка", params: m => ({ thread_id: decodeURIComponent(m[1]) }), key: p => p.thread_id },
  { pattern: /^#\/client\/(.+)$/,         screen: client, list: "clients", title: "Клиент", params: m => ({ email: decodeURIComponent(m[1]) }), key: p => p.email },
  { pattern: /^#\/accounts\/?$/,          screen: accounts, title: "Аккаунты", params: () => ({}) },
  { pattern: /^#\/settings\/?$/,          screen: settings, title: "Настройки", params: () => ({}) },
  { pattern: /^#\/review\/(.+)$/,         screen: review, list: "pipeline", title: "Открываю…", params: m => ({ msg_id: decodeURIComponent(m[1]) }) },
];

// Экраны-списки для левой панели (широкий экран)
const LISTS = { pipeline, clients, stock };

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
  // Открыт шит (отправка, правка, меню) — «Назад» сначала закрывает его,
  // а не уводит с экрана вместе с набранным текстом.
  if (isSheetOpen()) {
    requestCloseSheet();
    return;
  }
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

// Заглушка правой панели, когда слева список, а ничего не открыто
function emptyDetail(list) {
  const box = document.createElement("div");
  box.className = "pane-empty";
  const ic = document.createElement("div");
  ic.className = "pe-ic";
  ic.textContent = { clients: "👤", stock: "📦" }[list] ?? "💬";
  const t = document.createElement("div");
  t.textContent = { clients: "Выберите клиента слева", stock: "Выберите товар слева" }[list] ?? "Выберите переписку слева";
  box.append(ic, t);
  return box;
}

export function start(app) {
  const listPane = app.querySelector(".pane-list");
  const mainPane = app.querySelector(".pane-main");
  // Левая панель живёт между переходами (опрос входящих не перезапускается
  // при открытии треда); пересоздаётся только при смене списка.
  let listState = { name: null, mount: null };
  let route = null, routeParams = null, routeMatch = null;

  function renderInto(mount, screen, params) {
    if (typeof screen === "function") setLoading(mount);
    return loadScreen(screen)
      .then(mod => {
        if (mount.isConnected) return mod.render(mount, params);
      })
      .catch(e => {
        console.error("[router] render failed:", e);
        if (mount.isConnected) setError(mount, e.message ?? String(e));
      });
  }

  // Левая панель: показать/спрятать список под текущую раскладку и маршрут
  function syncList() {
    const want = isSplit() && route?.list ? route.list : null;
    app.classList.toggle("has-list", !!want);
    if (!want) {
      if (listState.mount) { listPane.replaceChildren(); listState = { name: null, mount: null }; }
      return;
    }
    if (listState.name !== want || !listState.mount?.isConnected) {
      const mount = document.createElement("div");
      listPane.replaceChildren(mount);
      listState = { name: want, mount };
      renderInto(mount, LISTS[want], {});
    }
    markSelected();
  }

  // Подсветить в списке открытый элемент (data-key = thread_id / email)
  function markSelected() {
    if (!listState.mount) return;
    const key = route?.key ? route.key(routeParams) : null;
    listState.mount.querySelectorAll("[data-key]").forEach(n => {
      n.classList.toggle("sel", key != null && n.dataset.key === String(key));
    });
  }

  function dispatch() {
    let hash = location.hash || "#/";
    // Telegram WebApp подкладывает #tgWebAppData=... в URL при запуске.
    // Это не наш роут — считаем за root (pipeline).
    if (!hash.startsWith("#/")) {
      hash = "#/";
    }
    // Шит прошлого экрана не должен висеть поверх нового (B-01)
    closeSheet();
    const r = ROUTES.find(x => x.pattern.test(hash));
    // Каждому переходу — свежий контейнер. Запоздавший ответ прошлого
    // экрана (опрос, медленный fetch, LLM) пишет в отцепленный от DOM div и
    // не перерисовывает чужой экран; экраны проверяют mount.isConnected,
    // чтобы остановить таймеры и не захватывать локи впустую.
    const screenMount = document.createElement("div");
    mainPane.replaceChildren(screenMount);
    mainPane.scrollTop = 0;
    if (!r) {
      route = null;
      syncList();
      setError(screenMount, `Неизвестный путь: ${hash}`);
      return;
    }
    route = r;
    routeMatch = hash.match(r.pattern);
    routeParams = r.params(routeMatch);
    syncList();
    applyChrome();
    setTitle(r.title);
    // На широком экране список уже слева — корневой экран списка справа не дублируем
    if (isSplit() && r.list && r.screen === LISTS[r.list] && r.root) {
      screenMount.appendChild(emptyDetail(r.list));
      return;
    }
    const flex = layout() === "flex" && r.flex;
    if (flex) setTitle("Разбор");
    renderInto(screenMount, flex ? r.flex : r.screen, routeParams);
  }

  // «Назад» в шапке: на корневых вкладках нет; на широком экране не нужен,
  // если открытое и так рядом со своим списком.
  function applyChrome() {
    if (!route || route.root || (isSplit() && route.list)) hideBack();
    else showBack(navigateBack);
  }

  // Сложили/разложили Fold, повернули: перестраиваем панели БЕЗ перерисовки
  // открытого экрана — набранный текст и прокрутка треда сохраняются.
  onLayoutChange((next, prev) => {
    // вошли в Flex Window / вышли из него — у маршрута свой экран для «кармана»
    if (route?.flex && (next === "flex" || prev === "flex")) { dispatch(); return; }
    const wasSplit = prev === "split2" || prev === "split3";
    const nowSplit = isSplit();
    if (route && route.list && route.root && wasSplit !== nowSplit) {
      // корневой экран списка: на телефоне он справа, на Fold — слева
      dispatch();
      return;
    }
    syncList();
    applyChrome();
  });

  // Список рисуется асинхронно (и обновляется опросом) — подсветку
  // открытого элемента восстанавливаем после каждой перерисовки.
  new MutationObserver(() => markSelected()).observe(listPane, { childList: true, subtree: true });

  window.addEventListener("hashchange", dispatch);
  dispatch();
}
