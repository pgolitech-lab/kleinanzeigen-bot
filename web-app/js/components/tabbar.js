// Нижний таб-бар: иконка + подпись, активная вкладка подсвечена акцентом.
import { el } from "../utils.js?v=20260716-164626";

const TABS = [
  { icon: "📥", label: "Входящие", hash: "#/pipeline", match: /^#\/(pipeline|thread|review)/ },
  { icon: "👥", label: "Клиенты",  hash: "#/clients",  match: /^#\/(clients|client)/ },
  { icon: "💰", label: "Продажи",  hash: "#/sales",    match: /^#\/(sales|detected)/ },
  { icon: "🔎", label: "Рынок",    hash: "#/scout",    match: /^#\/scout/ },
  { icon: "📊", label: "Обзор",    hash: "#/dashboard", match: /^#\/(dashboard|settings)/ },
];

export function mountTabbar() {
  if (document.getElementById("tabbar")) return;
  const nav = el(`<nav id="tabbar" aria-label="Разделы"></nav>`);
  TABS.forEach(t => {
    const a = el(`<a class="tab"><span class="ti"></span><span class="tl"></span></a>`);
    a.href = t.hash;
    a.querySelector(".ti").textContent = t.icon;
    a.querySelector(".tl").textContent = t.label;
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
