import { api } from "../api.js?v=20260510-14";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260510-14";

function threadCard(thread) {
  const card = el(`
    <a class="list-group-item list-group-item-action py-2" role="button">
      <div class="d-flex justify-content-between align-items-start">
        <div class="flex-grow-1 me-2">
          <div class="title fw-semibold"></div>
          <div class="meta text-muted small mt-1"></div>
        </div>
        <div class="text-end small">
          <div class="when text-muted"></div>
          <div class="badges"></div>
        </div>
      </div>
    </a>
  `);

  card.href = `#/thread/${encodeURIComponent(thread.thread_id)}`;
  card.querySelector(".title").textContent =
    `${thread.ad_title ?? "(без названия)"} · ${thread.ad_price ?? "?"}`;

  const metaParts = [];
  if (thread.buyer_display_name) metaParts.push(`👤 ${thread.buyer_display_name}`);
  if (thread.deal_brief_json) {
    try {
      const brief = typeof thread.deal_brief_json === "string"
        ? JSON.parse(thread.deal_brief_json)
        : thread.deal_brief_json;
      if (brief?.summary_ru) metaParts.push(`💬 ${brief.summary_ru}`);
    } catch (e) { /* invalid JSON, skip */ }
  }
  card.querySelector(".meta").textContent = metaParts.join(" · ");

  card.querySelector(".when").textContent = berlinTime(thread.last_event_at);

  const badges = card.querySelector(".badges");
  if (thread.is_autopilot) {
    const b = el(`<span class="badge bg-warning text-dark">🤖 автопилот</span>`);
    badges.appendChild(b);
  }
  if (thread.pending_drafts_count > 0) {
    const b = el(`<span class="badge bg-info ms-1">📝 ${thread.pending_drafts_count}</span>`);
    badges.appendChild(b);
  }

  return card;
}

function sectionBlock(title, color, threads) {
  const sec = el(`
    <div class="mb-3">
      <h6 class="text-muted small text-uppercase mb-2"></h6>
      <div class="list-group list-group-flush"></div>
    </div>
  `);
  sec.querySelector("h6").textContent = `${color} ${title}: ${threads.length}`;
  const list = sec.querySelector(".list-group");
  if (threads.length === 0) {
    list.appendChild(el(`<div class="text-muted small fst-italic px-2 py-1">пусто</div>`));
  } else {
    threads.forEach(t => list.appendChild(threadCard(t)));
  }
  return sec;
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю pipeline…");
  try {
    const data = await api("/api/ma/pipeline");
    const container = el(`<div></div>`);
    const headerRow = el(`
      <div class="d-flex justify-content-between align-items-center mb-2">
        <button class="btn btn-sm btn-outline-secondary refresh-btn">↻ Обновить</button>
        <a class="btn btn-sm btn-outline-secondary" href="#/settings">⚙</a>
      </div>
    `);
    headerRow.querySelector(".refresh-btn").addEventListener("click", () => render(mount, params));
    container.appendChild(headerRow);
    container.appendChild(sectionBlock("ждут нас", "🔴", data.red ?? []));
    container.appendChild(sectionBlock("ждём клиента", "🟢", data.green ?? []));
    mount.replaceChildren(container);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
