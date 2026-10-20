// «💰 Продано» — один лист для треда и для свайпа во Входящих.
// Итог QA «ленивый продажник» + склад: цена обязательна и предзаполнена
// (/threads/{id}/sale-price — переговоры, бриф, наш ответ, объявление),
// кандидаты — чипами, количество, «закрыть остальных» включено по умолчанию;
// после записи — «на складе осталось N» или «товар не найден на складе».
// Эндпоинт: /threads/{id}/sold (работает и без черновика), пока api его не
// выкатил — фолбэк на /messages/{msgId}/sold.
import { api } from "../api.js";
import { el } from "../utils.js";
import { openSheet, closeSheet } from "./sheet.js";
import { toast } from "./kit.js";

async function postSold({ threadId, msgId }, body) {
  if (threadId) {
    try {
      return await api(`/api/ma/threads/${encodeURIComponent(threadId)}/sold`, { method: "POST", body });
    } catch (e) {
      // эндпоинта ещё нет — старый путь через сообщение
      if (!(e.status === 404 || e.status === 405) || !msgId) throw e;
    }
  }
  return api(`/api/ma/messages/${msgId}/sold`, { method: "POST", body });
}

// opts: {threadId, msgId, title, price, prices: [числа для чипов], onDone}
export function openSoldSheet(opts) {
  const form = el(`
    <div class="sold-form">
      <div class="sold-title"></div>
      <label class="form-label small">Цена продажи, €</label>
      <input type="number" inputmode="decimal" min="0" step="1" class="form-control sold-price">
      <div class="sold-hint"></div>
      <div class="chips-row sold-chips"></div>
      <label class="form-label small mt-1">Сколько штук</label>
      <input type="number" inputmode="numeric" min="1" step="1" value="1" class="form-control sold-qty">
      <label class="form-check mt-2">
        <input type="checkbox" class="form-check-input close-other" checked>
        <span class="form-check-label">Закрыть остальные переговоры по этому объявлению</span>
      </label>
      <div class="form-error text-danger small my-2 d-none"></div>
      <div class="sheet-actions">
        <button class="btn cancel-btn">Отмена</button>
        <button class="btn btn-primary save-btn">✅ Продано</button>
      </div>
    </div>`);
  const input = form.querySelector(".sold-price");
  form.querySelector(".sold-title").textContent = opts.title ?? "";
  if (opts.price != null) input.value = String(Math.round(opts.price));
  const chips = form.querySelector(".sold-chips");
  const hint = form.querySelector(".sold-hint");
  const paintChips = prices => {
    chips.replaceChildren();
    [...new Set(prices.filter(v => v != null && Number.isFinite(Number(v))).map(v => Math.round(v)))].forEach(v => {
      const c = el(`<button type="button" class="qchip"></button>`);
      c.textContent = `${v.toLocaleString("de-DE")} €`;
      c.addEventListener("click", () => { input.value = String(v); input.classList.remove("is-invalid"); });
      chips.appendChild(c);
    });
    chips.hidden = !chips.children.length;
  };
  paintChips(opts.prices ?? []);
  // подсказка склада: сколько обычно / о чём договорились
  if (opts.threadId) {
    api(`/api/ma/threads/${encodeURIComponent(opts.threadId)}/sale-price`).then(r => {
      if (!form.isConnected || !r) return;
      if (r.price_eur != null && !input.dataset.touched) input.value = String(Math.round(r.price_eur));
      const SRC = { negotiation: "из переговоров", deal_brief: "из брифа", our_reply: "из нашего ответа",
        buyer: "из письма покупателя", listing: "цена объявления" };
      if (r.price_eur != null) hint.textContent = `Подсказка: ${Math.round(r.price_eur).toLocaleString("de-DE")} € — ${SRC[r.source] ?? r.source ?? ""}`;
      paintChips([...(r.candidates ?? []).map(c => (typeof c === "number" ? c : c?.price_eur)), ...(opts.prices ?? [])]);
    }).catch(e => { if (e.status !== 404) console.warn("[sold] sale-price:", e); });
  }
  input.addEventListener("input", () => { input.dataset.touched = "1"; });

  const errEl = form.querySelector(".form-error");
  form.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  const save = form.querySelector(".save-btn");
  save.addEventListener("click", async () => {
    const price = Number(input.value.trim().replace(",", "."));
    if (!input.value.trim() || Number.isNaN(price) || price < 0) {
      input.classList.add("is-invalid");
      return;
    }
    const qty = Math.max(1, Math.round(Number(form.querySelector(".sold-qty").value) || 1));
    save.disabled = true;
    save.textContent = "⏳ Сохраняю…";
    try {
      const res = await postSold(opts, { price_eur: price, qty, close_other_threads_for_ad: form.querySelector(".close-other").checked });
      closeSheet();
      const st = res?.stock;
      let tail = "";
      if (st && res.product_id === null) tail = " · товар не найден на складе — привяжите в «Склад»";
      else if (st?.stock_tracked && st.qty_after != null) tail = ` · на складе осталось ${st.qty_after}`;
      toast(`✅ Продажа ${price.toLocaleString("de-DE")} € записана${tail}`, { ms: tail ? 5000 : 3200 });
      opts.onDone?.(price);
    } catch (err) {
      errEl.textContent = err.message ?? String(err);
      errEl.classList.remove("d-none");
      save.disabled = false;
      save.textContent = "✅ Продано";
    }
  });
  openSheet(form, "💰 Продано");
}
