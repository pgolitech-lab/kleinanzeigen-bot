// 🗺 Рынок — хаб с тремя вкладками в одном экране:
//   «Сиденья» (по умолчанию) — рынок по НАШИМ сиденьям без ввода: тепловая карта, цены, конкуренты;
//   «Карта» — разведка scout по землям/городам (машины / запчасти / запросы);
//   «Поиск» — ручной поиск любого товара.
// Вкладка живёт в адресе (#/scout, #/scout/map, #/scout/search), переключение — replaceState,
// чтобы «Назад» уводил с «Рынка», а не листал вкладки.
import { el } from "../utils.js";

const TABS = [
  { key: "seats",  hash: "#/scout",        label: "🪑 Сиденья", load: () => import("./market-seats.js") },
  { key: "map",    hash: "#/scout/map",    label: "🗺 Карта",   load: () => import("./scout.js") },
  { key: "search", hash: "#/scout/search", label: "🔎 Поиск",   load: () => import("./market-search.js") },
];

export async function render(mount, params) {
  const root = el(`
    <div class="mhub">
      <nav class="mh-tabs" role="tablist" aria-label="Рынок"></nav>
      <div class="mh-body"></div>
    </div>`);
  const nav = root.querySelector(".mh-tabs");
  const body = root.querySelector(".mh-body");
  const btns = TABS.map(t => {
    const b = el(`<button type="button" class="mh-tab" role="tab"></button>`);
    b.textContent = t.label;
    b.dataset.key = t.key;
    b.addEventListener("click", () => open(t.key, true));
    nav.appendChild(b);
    return b;
  });
  mount.replaceChildren(root);

  let seq = 0;
  async function open(key, user) {
    const t = TABS.find(x => x.key === key) ?? TABS[0];
    if (user && body.dataset.tab === t.key) return;
    btns.forEach(b => {
      const on = b.dataset.key === t.key;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (user && location.hash !== t.hash) {
      try { history.replaceState(history.state, "", t.hash); } catch (e) { /* без History API — вкладка не запомнится */ }
    }
    body.dataset.tab = t.key;
    // свежий контейнер на каждую вкладку: запоздавший ответ прошлой вкладки увидит isConnected=false
    const box = el(`<div class="mh-pane"></div>`);
    body.replaceChildren(box);
    const my = ++seq;
    let mod;
    try {
      mod = await t.load();
    } catch (e) {
      if (my === seq && box.isConnected) box.replaceChildren(el(`<div class="ms-note ms-err"><p>Не удалось загрузить вкладку. Проверьте интернет и откройте её ещё раз.</p></div>`));
      return;
    }
    if (my !== seq || !box.isConnected) return;
    await mod.render(box, {});
  }

  await open(params?.tab ?? "seats", false);
}
