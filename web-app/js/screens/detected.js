import { api } from "../api.js?v=20260715-183110";
import { el, esc, berlinTime, setLoading, setError } from "../utils.js?v=20260715-183110";

function eur(n) {
  if (n == null) return "—";
  return n.toLocaleString("de-DE", {minimumFractionDigits: 0, maximumFractionDigits: 2}) + "€";
}

function confidenceBadge(conf) {
  const map = {
    high:   "bg-success",
    medium: "bg-warning text-dark",
    low:    "bg-secondary",
  };
  return `<span class="badge ${map[conf] || "bg-secondary"}">${esc(conf)}</span>`;
}

function detectionCard(d, onApply, onReject) {
  const card = el(`
    <div class="card mb-2">
      <div class="card-body p-3">
        <div class="d-flex justify-content-between align-items-baseline gap-2 mb-1">
          <div class="fw-semibold ad-title flex-grow-1"></div>
          <div class="conf"></div>
        </div>
        <div class="small text-muted mb-2 meta"></div>
        <div class="alert alert-info small py-2 mb-2 evidence"></div>
        <div class="row g-2 align-items-center mb-2">
          <div class="col-auto">
            <label class="form-label small mb-0">Цена €:</label>
          </div>
          <div class="col">
            <input type="number" inputmode="decimal" min="0" step="0.01"
                   class="form-control form-control-sm price-input">
          </div>
          <div class="col-auto small text-muted disc"></div>
        </div>
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-success flex-grow-1 apply-btn">✅ Принять</button>
          <button class="btn btn-sm btn-outline-danger reject-btn">❌ Отклонить</button>
          <a class="btn btn-sm btn-outline-secondary" href="#/thread/" data-thread>📂 Тред</a>
        </div>
      </div>
    </div>
  `);
  card.querySelector(".ad-title").textContent = d.thread_ad_title || d.detected_ad_title || "?";
  card.querySelector(".conf").innerHTML = confidenceBadge(d.confidence);

  const metaParts = [];
  if (d.detected_sold_at) metaParts.push(`🕒 ${d.detected_sold_at}`);
  if (d.buyer_display_name) metaParts.push(`👤 ${d.buyer_display_name}`);
  if (d.account_name) metaParts.push(`@${d.account_name}`);
  if (d.thread_ad_price) metaParts.push(`объявлено: ${d.thread_ad_price}`);
  card.querySelector(".meta").textContent = metaParts.join(" · ");

  card.querySelector(".evidence").textContent = d.evidence
    ? `«${d.evidence}»`
    : "(нет evidence)";

  const priceInput = card.querySelector(".price-input");
  if (d.sold_price_eur != null) priceInput.value = d.sold_price_eur;

  const discEl = card.querySelector(".disc");
  function updateDiscount() {
    const p = Number(priceInput.value);
    if (!Number.isFinite(p) || !d.thread_ad_price_eur) {
      discEl.textContent = "";
      return;
    }
    const diff = d.thread_ad_price_eur - p;
    const sign = diff >= 0 ? "−" : "+";
    discEl.textContent = `(${sign}${eur(Math.abs(diff))})`;
  }
  priceInput.addEventListener("input", updateDiscount);
  updateDiscount();

  const link = card.querySelector("[data-thread]");
  link.href = `#/thread/${encodeURIComponent(d.thread_id)}`;

  card.querySelector(".apply-btn").addEventListener("click", async () => {
    const p = Number(priceInput.value);
    if (!Number.isFinite(p) || p < 0) {
      priceInput.classList.add("is-invalid");
      return;
    }
    card.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      await onApply(d.id, p);
    } catch (e) {
      alert("Ошибка: " + (e.message || e));
      card.querySelectorAll("button").forEach(b => b.disabled = false);
    }
  });

  card.querySelector(".reject-btn").addEventListener("click", async () => {
    card.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      await onReject(d.id);
    } catch (e) {
      alert("Ошибка: " + (e.message || e));
      card.querySelectorAll("button").forEach(b => b.disabled = false);
    }
  });

  return card;
}

export async function render(mount, params) {
  setLoading(mount, "Загружаю detections…");
  try {
    const data = await api("/api/ma/detected-sales");
    paint(mount, data);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}

function paint(mount, data) {
  const root = el(`<div></div>`);
  root.appendChild(el(`
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h5 class="mb-0">🔍 Возможные продажи (${data.count})</h5>
      <a class="btn btn-sm btn-outline-secondary" href="#/sales">↩ Продажи</a>
    </div>
  `));

  root.appendChild(el(`
    <div class="alert alert-secondary small mb-3">
      LLM-сканер прошёл по архиву переписки и нашёл возможные сделки.<br>
      High-confidence уже автоматически записаны в Продажи.<br>
      Здесь — кандидаты которые требуют твоей проверки. Поправь цену если LLM ошиблась.
    </div>
  `));

  if (!data.count) {
    root.appendChild(el(`
      <div class="text-center text-muted fst-italic py-4">
        Нет кандидатов на review. Все обработаны 👍
      </div>
    `));
  }

  async function onApply(id, price) {
    await api(`/api/ma/detected-sales/${id}/apply`, {
      method: "POST", body: {price_eur: price},
    });
    render(mount);  // re-fetch
  }
  async function onReject(id) {
    await api(`/api/ma/detected-sales/${id}/reject`, {method: "POST"});
    render(mount);
  }

  for (const d of data.items) {
    root.appendChild(detectionCard(d, onApply, onReject));
  }
  mount.replaceChildren(root);
}
