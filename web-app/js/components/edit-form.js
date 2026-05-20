// Edit form component — textarea/input для edit-ru / edit-de / price / instruction.

import { api } from "../api.js?v=20260521-1";
import { el } from "../utils.js?v=20260521-1";


const FIELD_CONFIG = {
  ru: {
    label: "Правка RU (наша инструкция)",
    path: "/edit-ru",
    inputType: "textarea",
    placeholder: "Введите текст…",
    valueFrom: r => r.draft?.ru_answer ?? "",
    bodyKey: "text",
  },
  de: {
    label: "Правка DE (текст для клиента)",
    path: "/edit-de",
    inputType: "textarea",
    placeholder: "Введите текст…",
    valueFrom: r => r.draft?.de_answer ?? "",
    bodyKey: "text",
  },
  price: {
    label: "Своя цена €",
    path: "/price",
    inputType: "number",
    placeholder: "1400",
    valueFrom: () => "",
    bodyKey: "eur",
    transform: v => parseFloat(v),
  },
  instruction: {
    label: "Своя инструкция",
    path: "/instruction",
    inputType: "textarea",
    placeholder: "Скажи что доставим в субботу...",
    valueFrom: () => "",
    bodyKey: "text",
  },
};


export function buildEditForm({msgId, field, review, onSubmitComplete, onCancel, onError}) {
  const cfg = FIELD_CONFIG[field];
  if (!cfg) return null;

  const form = el(`
    <div class="edit-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold form-label"></div>
      <div class="input-wrap mb-2"></div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">💾 Сохранить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);
  form.querySelector(".form-label").textContent = cfg.label;

  const wrap = form.querySelector(".input-wrap");
  let input;
  if (cfg.inputType === "textarea") {
    input = el(`<textarea class="form-control" rows="6"></textarea>`);
    input.value = cfg.valueFrom(review) || "";
  } else {
    input = el(`<input class="form-control" type="number" step="10" min="0" />`);
    input.placeholder = cfg.placeholder;
  }
  wrap.appendChild(input);

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    const errEl = form.querySelector(".form-error");
    errEl.classList.add("d-none");
    const raw = input.value;
    if (!raw || (typeof raw === "string" && !raw.trim())) {
      errEl.textContent = "Поле не может быть пустым";
      errEl.classList.remove("d-none");
      return;
    }
    const value = cfg.transform ? cfg.transform(raw) : raw;
    if (cfg.transform && (Number.isNaN(value) || value <= 0)) {
      errEl.textContent = "Введите положительное число";
      errEl.classList.remove("d-none");
      return;
    }
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/messages/${msgId}${cfg.path}`, {
        method: "POST",
        body: {[cfg.bodyKey]: value},
      });
      onSubmitComplete(field, res);
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelector(".save-btn").disabled = false;
      form.querySelector(".cancel-btn").disabled = false;
    }
  });

  return form;
}
