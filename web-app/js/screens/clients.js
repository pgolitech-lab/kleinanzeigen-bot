// 👥 Клиенты (v3): поиск + строки «аватар · имя · статус · обращений/товаров».
// Тап → карточка клиента (#/client/<email>); на Fold список слева, карточка справа.
import { api } from "../api.js";
import { el, setLoading, setError } from "../utils.js";
import { avatar } from "../components/kit.js";
import { shortTime } from "../components/thread-row.js";

// Статус последнего события → короткая метка для продажника
const STATUS = {
  pending: ["hot", "ждёт ответа"], new: ["hot", "ждёт ответа"], edited: ["hot", "ждёт ответа"], approved: ["hot", "ждёт ответа"],
  sent: ["", "ждём клиента"], sent_debug: ["", "ждём клиента"], sent_external: ["", "ждём клиента"],
  skipped_sold: ["good", "купил"], skipped: ["mut", "закрыто"], archived: ["mut", "архив"],
};

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
}

function clientRow(c) {
  const a = el(`
    <a class="crow">
      <span class="cr-av"></span>
      <span class="cr-main"><span class="cr-1"><b></b><time></time></span><span class="cr-2"><span class="cr-st"></span><span class="cr-meta"></span></span></span>
    </a>`);
  a.href = `#/client/${encodeURIComponent(c.email)}`;
  a.dataset.key = c.email;
  a.querySelector(".cr-av").appendChild(avatar(c.display_name || c.email, c.account_id ?? null, c.client_lang, "md"));
  a.querySelector("b").textContent = c.display_name || c.email || "?";
  a.querySelector("time").textContent = shortTime(c.last_at);
  const [cls, label] = STATUS[c.last_status] ?? ["mut", ""];
  const st = a.querySelector(".cr-st");
  if (label) { st.textContent = label; if (cls) st.classList.add(cls); } else st.remove();
  const n = c.thread_count ?? 0, ads = c.ad_count ?? 0;
  a.querySelector(".cr-meta").textContent =
    `${n} ${plural(n, "обращение", "обращения", "обращений")} · ${ads} ${plural(ads, "товар", "товара", "товаров")}`;
  return a;
}

let _all = [];

function applyFilter(listEl, q) {
  const term = (q || "").trim().toLowerCase();
  const filtered = !term ? _all : _all.filter(c =>
    (c.display_name || "").toLowerCase().includes(term) ||
    (c.email || "").toLowerCase().includes(term));
  listEl.replaceChildren();
  if (!filtered.length) {
    listEl.appendChild(el(`<div class="empty"><p>Никого не нашли</p></div>`));
    return;
  }
  filtered.forEach(c => listEl.appendChild(clientRow(c)));
}

export async function render(mount) {
  setLoading(mount, "Загружаю клиентов…");
  let data;
  try {
    data = await api("/api/ma/clients");
  } catch (e) {
    if (mount.isConnected) setError(mount, e.message ?? String(e));
    return;
  }
  if (!mount.isConnected) return;
  _all = data.clients ?? [];
  const root = el(`
    <div class="clients">
      <input type="search" class="form-control search" placeholder="Имя или почта…" aria-label="Поиск клиента">
      <div class="lsec"><span>Клиенты</span><i></i></div>
      <div class="clist"></div>
    </div>`);
  root.querySelector(".lsec i").textContent = String(_all.length);
  const listEl = root.querySelector(".clist");
  const search = root.querySelector(".search");
  search.addEventListener("input", () => applyFilter(listEl, search.value));
  applyFilter(listEl, "");
  mount.replaceChildren(root);
}
