// «Ещё» — всё, что не нужно продажнику каждую минуту: обзор, рынок,
// найденные продажи, аккаунты, настройки. Крупные строки (≥ 56 px).
import { el } from "../utils.js";

const ITEMS = [
  { hash: "#/dashboard", icon: "📊", title: "Обзор",              sub: "сводка за сегодня и неделю" },
  { hash: "#/detected",  icon: "🔍", title: "Найденные продажи",  sub: "проверить, что продано по переписке" },
  { hash: "#/scout",     icon: "🗺", title: "Рынок",              sub: "найти товар и увидеть цены" },
  { hash: "#/accounts",  icon: "📮", title: "Аккаунты",           sub: "5 почт Kleinanzeigen, проверка входа, бэкап" },
  { hash: "#/settings",  icon: "⚙️", title: "Настройки",          sub: "режим отправки, напоминания, служебное" },
];

export function render(mount) {
  const list = el(`<div class="more-list"></div>`);
  ITEMS.forEach(it => {
    const a = el(`<a class="more-row"><span class="mi"></span><span class="mt"><b></b><small></small></span><span class="mc">›</span></a>`);
    a.href = it.hash;
    a.querySelector(".mi").textContent = it.icon;
    a.querySelector("b").textContent = it.title;
    a.querySelector("small").textContent = it.sub;
    list.appendChild(a);
  });
  mount.replaceChildren(list);
}
