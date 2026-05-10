import { api } from "../api.js?v=20260510-14";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260510-14";

function threadRow(t) {
  const row = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="text-muted small meta"></div>
        </div>
        <div class="text-end small text-muted">
          <div class="when"></div>
          <div class="status badge bg-secondary"></div>
        </div>
      </div>
    </a>
  `);
  row.href = `#/thread/${encodeURIComponent(t.thread_id)}`;
  row.querySelector(".title").textContent = `${t.ad_title ?? "?"} · ${t.ad_price ?? "?"}`;
  row.querySelector(".meta").textContent = `${t.msg_count} сообщ.${t.ad_id ? ` · #${t.ad_id}` : ""}`;
  row.querySelector(".when").textContent = berlinTime(t.last_at);
  row.querySelector(".status").textContent = t.last_status ?? "?";
  return row;
}

export async function render(mount, params) {
  setLoading(mount, `Загружаю историю клиента ${params.email}…`);
  try {
    const data = await api(`/api/ma/clients/${encodeURIComponent(params.email)}/history`);
    const container = el(`
      <div>
        <h5 class="email-header"></h5>
        <div class="text-muted small total-count mb-2"></div>
        <div class="list-group list-group-flush mb-3"></div>
        <a class="btn btn-sm btn-outline-secondary" href="#/pipeline">↩ К pipeline</a>
      </div>
    `);
    container.querySelector(".email-header").textContent = data.buyer_email;
    container.querySelector(".total-count").textContent = `Всего тредов: ${data.threads.length}`;
    const list = container.querySelector(".list-group");
    if (data.threads.length === 0) {
      list.appendChild(el(`<div class="text-muted fst-italic px-2 py-1">Нет тредов.</div>`));
    } else {
      data.threads.forEach(t => list.appendChild(threadRow(t)));
    }
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
