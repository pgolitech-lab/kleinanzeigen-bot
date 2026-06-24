// Compose form: пишешь по-русски → «Перевести» (предпросмотр DE + обратный RU) → «Отправить».
import { api } from "../api.js?v=20260624-024500";
import { el } from "../utils.js?v=20260624-024500";
import { tg } from "../tg.js?v=20260624-024500";

export function buildComposeForm({ threadId, onSubmitComplete, onCancel }) {
  const form = el(`
    <div class="compose-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold">✉️ Написать клиенту (на русском — переведём)</div>
      <textarea class="form-control mb-2 ru-input" rows="5" placeholder="Введите текст по-русски…"></textarea>
      <div class="preview d-none mb-2">
        <div class="text-muted small">DE → клиенту</div>
        <div class="de-preview p-2 rounded bg-primary-subtle small"></div>
        <div class="text-muted small mt-1">RU обратный перевод (проверь смысл)</div>
        <div class="back-preview p-2 rounded fst-italic small"></div>
      </div>
      <div class="d-flex gap-2 flex-wrap">
        <button class="btn btn-outline-info translate-btn">🔁 Перевести</button>
        <button class="btn btn-primary save-btn">📨 Отправить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const textarea = form.querySelector(".ru-input");
  const errEl = form.querySelector(".form-error");
  const previewBox = form.querySelector(".preview");
  const dePrev = form.querySelector(".de-preview");
  const backPrev = form.querySelector(".back-preview");
  const translateBtn = form.querySelector(".translate-btn");
  const saveBtn = form.querySelector(".save-btn");
  const cancelBtn = form.querySelector(".cancel-btn");

  function showErr(m) { errEl.textContent = m; errEl.classList.remove("d-none"); }
  function clearErr() { errEl.classList.add("d-none"); }

  function validate() {
    const text = textarea.value.trim();
    if (!text) { showErr("Текст не может быть пустым"); return null; }
    if (text.length > 4000) { showErr("Слишком длинный текст (макс. 4000)"); return null; }
    return text;
  }

  // Сброс предпросмотра при правке текста
  textarea.addEventListener("input", () => previewBox.classList.add("d-none"));

  translateBtn.addEventListener("click", async () => {
    clearErr();
    const text = validate();
    if (!text) return;
    translateBtn.disabled = true;
    translateBtn.textContent = "⏳ перевожу…";
    try {
      const r = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose-preview`, {
        method: "POST", body: { text },
      });
      dePrev.textContent = r.translated ?? "";
      backPrev.textContent = r.back_ru ?? "";
      previewBox.classList.remove("d-none");
    } catch (e) {
      showErr(e.message ?? String(e));
    } finally {
      translateBtn.disabled = false;
      translateBtn.textContent = "🔁 Перевести";
    }
  });

  cancelBtn.addEventListener("click", () => onCancel());

  saveBtn.addEventListener("click", async () => {
    clearErr();
    const text = validate();
    if (!text) return;
    saveBtn.disabled = true; cancelBtn.disabled = true; translateBtn.disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose`, {
        method: "POST", body: { text },
      });
      try { tg?.HapticFeedback?.notificationOccurred("success"); } catch (e) {}
      try {
        if (tg?.showPopup) {
          tg.showPopup({ title: "Отправлено", message: "Письмо ушло клиенту.", buttons: [{ type: "ok" }] },
            () => onSubmitComplete(res));
        } else {
          onSubmitComplete(res);
        }
      } catch (e) { onSubmitComplete(res); }
    } catch (e) {
      showErr(e.message ?? String(e));
      saveBtn.disabled = false; cancelBtn.disabled = false; translateBtn.disabled = false;
    }
  });

  return form;
}
