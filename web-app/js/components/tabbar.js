// Нижний таб-бар: SVG-иконка + подпись, активная вкладка подсвечена.
// Вкладки: Входящие · Клиенты · Продажи · Склад · Ещё. Склад — товары с их
// объявлениями (закрывает и «Мои объявления»). Обзор влит во Входящие
// (полоса-итог), Рынок/Настройки/Аккаунты — в «Ещё».
import { el } from "../utils.js";

const ICONS = {
  inbox: '<path d="M4 13h4l2 3h4l2-3h4"/><path d="M5.5 5h13l2.5 8v5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-5z"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M18 14a6.5 6.5 0 0 1 3.5 6"/>',
  sales: '<path d="M4 19V10"/><path d="M10 19V5"/><path d="M16 19v-6"/><path d="M21 19H3"/>',
  listings: '<path d="M3 7l9-4 9 4-9 4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/>',
  more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
};

const TABS = [
  { icon: "inbox",    label: "Входящие",   hash: "#/pipeline", match: /^#\/(pipeline|thread|review|triage)/ },
  { icon: "users",    label: "Клиенты",    hash: "#/clients",  match: /^#\/(clients|client)/ },
  { icon: "sales",    label: "Продажи",    hash: "#/sales",    match: /^#\/(sales|detected)/ },
  { icon: "listings", label: "Склад",      hash: "#/stock",    match: /^#\/(stock|listings)/ },
  { icon: "more",     label: "Ещё",        hash: "#/more",     match: /^#\/(more|settings|accounts|dashboard|scout)/ },
];

export function mountTabbar() {
  if (document.getElementById("tabbar")) return;
  const nav = el(`<nav id="tabbar" aria-label="Разделы"></nav>`);
  TABS.forEach(t => {
    const a = el(`<a class="tab"><svg viewBox="0 0 24 24" aria-hidden="true"></svg><span class="tl"></span></a>`);
    a.href = t.hash;
    a.querySelector("svg").innerHTML = ICONS[t.icon];  // статичная разметка иконок
    a.querySelector(".tl").textContent = t.label;
    a.dataset.tab = t.icon;
    a.setAttribute("aria-label", t.label);  // подпись на узких экранах скрыта
    nav.appendChild(a);
  });
  (document.getElementById("shell") ?? document.body).appendChild(nav);

  function paint() {
    const h = location.hash || "#/";
    const tabs = nav.querySelectorAll(".tab");
    tabs.forEach((a, i) => {
      const isRoot = TABS[i].hash === "#/pipeline" && (h === "#/" || h === "" || !h.startsWith("#/"));
      a.classList.toggle("active", TABS[i].match.test(h) || isRoot);
    });
  }
  window.addEventListener("hashchange", paint);
  paint();
}

// Счётчик на вкладке (например «ждут ответа» на Входящих); 0/null — убрать
export function setTabBadge(tab, n) {
  const a = document.querySelector(`#tabbar .tab[data-tab="${tab}"]`);
  if (!a) return;
  let b = a.querySelector(".tb");
  if (!n) { b?.remove(); return; }
  if (!b) { b = document.createElement("span"); b.className = "tb"; a.appendChild(b); }
  b.textContent = n > 99 ? "99+" : String(n);
}
