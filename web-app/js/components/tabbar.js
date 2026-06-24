// Нижний таб-бар (как в нормальном приложении). Монтируется один раз в body,
// подсвечивает активную вкладку по location.hash.
import { el } from "../utils.js?v=20260624-020000";

const TABS = [
  { label: "📥 Входящие", hash: "#/pipeline", match: /^#\/(pipeline|thread|review)/ },
  { label: "👥 Клиенты",  hash: "#/clients",  match: /^#\/(clients|client)/ },
  { label: "💰 Продажи",  hash: "#/sales",    match: /^#\/sales/ },
  { label: "🔎 Рынок",    hash: "#/scout",    match: /^#\/scout/ },
  { label: "📊 Обзор",    hash: "#/dashboard", match: /^#\/dashboard/ },
];

export function mountTabbar() {
  if (document.getElementById("tabbar")) return;
  const nav = el(`<nav id="tabbar"></nav>`);
  TABS.forEach(t => {
    const a = el(`<a class="tab"></a>`);
    a.href = t.hash;
    a.textContent = t.label;
    nav.appendChild(a);
  });
  document.body.appendChild(nav);

  function paint() {
    const h = location.hash || "#/";
    const tabs = nav.querySelectorAll(".tab");
    tabs.forEach((a, i) => {
      const isRoot = TABS[i].hash === "#/pipeline" && (h === "#/" || h === "");
      a.classList.toggle("active", TABS[i].match.test(h) || isRoot);
    });
  }
  window.addEventListener("hashchange", paint);
  paint();
}
