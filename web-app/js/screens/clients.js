// 👥 Клиенты — база обращений (CRM). Список покупателей с агрегатами + поиск.
// Клик → история клиента (#/client/<email> → все его переписки).
import { api } from "../api.js?v=20260715-2";
import { el, berlinTime, setLoading, setError } from "../utils.js?v=20260715-2";

function clientRow(c) {
  const row = el(`
    <a class="tcard">
      <div class="tc-m">
        <div class="tc-t"><b class="name"></b></div>
        <div class="tc-sub meta"></div>
      </div>
      <div class="tc-side"><time class="when"></time></div>
    </a>`);
  const name = c.display_name || c.email || "?";
  row.href = `#/client/${encodeURIComponent(c.email)}`;
  row.querySelector(".name").textContent = name;
  row.querySelector(".meta").textContent =
    `${c.email ?? ""} · ${c.thread_count ?? 0} обращ. · ${c.ad_count ?? 0} товаров · ${c.msg_count ?? 0} сообщ.`;
  row.querySelector(".when").textContent = berlinTime(c.last_at);
  return row;
}

let _all = [];

function applyFilter(listEl, q) {
  const term = (q || "").trim().toLowerCase();
  const filtered = !term ? _all : _all.filter(c =>
    (c.display_name || "").toLowerCase().includes(term) ||
    (c.email || "").toLowerCase().includes(term));
  listEl.replaceChildren();
  if (!filtered.length) {
    listEl.appendChild(el(`<div class="text-muted small fst-italic px-2 py-3 text-center">Ничего не найдено</div>`));
    return;
  }
  filtered.forEach(c => listEl.appendChild(clientRow(c)));
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю клиентов…");
  let data;
  try {
    data = await api("/api/ma/clients");
  } catch (e) { setError(mount, e.message ?? String(e)); return; }
  _all = data.clients ?? [];

  const root = el(`
    <div>
      <input type="search" class="form-control mb-2 search" placeholder="Поиск по имени / email…">
      <div class="sec total"></div>
      <div class="clist"></div>
    </div>`);
  root.querySelector(".total").textContent = `Всего: ${_all.length}`;
  const listEl = root.querySelector(".clist");
  const search = root.querySelector(".search");
  search.addEventListener("input", () => applyFilter(listEl, search.value));
  applyFilter(listEl, "");
  mount.replaceChildren(root);
}
