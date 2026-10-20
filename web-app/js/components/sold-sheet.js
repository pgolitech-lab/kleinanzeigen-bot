// «💰 Продано» — один лист для треда и для свайпа во Входящих.
// Итог QA «ленивый продажник»: цена предзаполнена (наше последнее / цена
// объявления), быстрые суммы чипами, «закрыть остальных» включено по умолчанию.
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
      <div class="chips-row sold-chips"></div>
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
  [...new Set((opts.prices ?? []).filter(v => v != null).map(v => Math.round(v)))].forEach(v => {
    const c = el(`<button type="button" class="qchip"></button>`);
    c.textContent = `${v.toLocaleString("de-DE")} €`;
    c.addEventListener("click", () => { input.value = String(v); input.classList.remove("is-invalid"); });
    chips.appendChild(c);
  });
  if (!chips.children.length) chips.remove();

  const errEl = form.querySelector(".form-error");
  form.querySelector(".cancel-btn").addEventListener("click", () => closeSheet());
  const save = form.querySelector(".save-btn");
  save.addEventListener("click", async () => {
    const price = Number(input.value.trim().replace(",", "."));
    if (!input.value.trim() || Number.isNaN(price) || price < 0) {
      input.classList.add("is-invalid");
      return;
    }
    save.disabled = true;
    save.textContent = "⏳ Сохраняю…";
    try {
      await postSold(opts, { price_eur: price, close_other_threads_for_ad: form.querySelector(".close-other").checked });
      closeSheet();
      toast(`✅ Продажа ${price.toLocaleString("de-DE")} € записана`, { level: "ok" });
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
