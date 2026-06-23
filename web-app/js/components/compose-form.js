// Compose form для operator-initiated message в тред.

import { api } from "../api.js?v=20260623-160000";
import { el } from "../utils.js?v=20260623-160000";
import { tg } from "../tg.js?v=20260623-160000";


export function buildComposeForm({threadId, onSubmitComplete, onCancel}) {
  const form = el(`
    <div class="compose-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold">✉️ Написать клиенту (на русском — переведём)</div>
      <textarea class="form-control mb-2" rows="6" placeholder="Введите текст…"></textarea>
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">📨 Отправить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const textarea = form.querySelector("textarea");
  const errEl = form.querySelector(".form-error");

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    errEl.classList.add("d-none");
    const text = textarea.value.trim();
    if (!text) {
      errEl.textContent = "Текст не может быть пустым";
      errEl.classList.remove("d-none");
      return;
    }
    if (text.length > 4000) {
      errEl.textContent = "Слишком длинный текст (макс. 4000)";
      errEl.classList.remove("d-none");
      return;
    }
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose`, {
        method: "POST",
        body: {text},
      });
      try { tg?.HapticFeedback?.notificationOccurred("success"); } catch (e) {}
      try {
        tg?.showPopup?.({
          title: "Сообщение отправлено",
          message: "Письмо ушло клиенту.",
          buttons: [{type: "ok"}],
        }, () => onSubmitComplete(res));
        // Fallback если showPopup недоступен — сразу re-render
        if (!tg?.showPopup) onSubmitComplete(res);
      } catch (e) {
        onSubmitComplete(res);
      }
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelector(".save-btn").disabled = false;
      form.querySelector(".cancel-btn").disabled = false;
    }
  });

  return form;
}
