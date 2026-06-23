// Unified suggest+edit+send form. Used когда оператор нажимает 🤖 Предложить.

import { api } from "../api.js?v=20260623-1";
import { el } from "../utils.js?v=20260623-1";


export function buildSuggestForm({msgId, threadId, review, onSubmitComplete, onCancel}) {
  /**
   * review: optional review payload (если есть pending — pre-fill textareas)
   * msgId: required for edit-ru/edit-de/send endpoints
   */
  const initialRu = review?.draft?.ru_answer ?? "";
  const initialDe = review?.draft?.de_answer ?? "";

  const form = el(`
    <div class="suggest-form border rounded p-3 mt-3">
      <div class="fw-semibold small mb-2">🤖 Предложить ответ</div>
      <div class="mb-2">
        <label class="form-label small mb-1 text-muted">RU (наша инструкция):</label>
        <textarea class="form-control ru-input" rows="5"></textarea>
      </div>
      <div class="mb-3">
        <label class="form-label small mb-1 text-muted">DE (текст клиенту):</label>
        <textarea class="form-control de-input" rows="5"></textarea>
      </div>
      <div class="d-grid mb-2">
        <button class="btn btn-sm btn-outline-info regen-btn">🔁 Перегенерировать (новый вариант от ИИ)</button>
      </div>
      <div class="d-flex gap-2">
        <button class="btn btn-primary send-btn">📨 Отправить</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const ruInput = form.querySelector(".ru-input");
  const deInput = form.querySelector(".de-input");
  ruInput.value = initialRu;
  deInput.value = initialDe;

  let lastRu = initialRu;
  let lastDe = initialDe;
  const errEl = form.querySelector(".form-error");

  function showError(msg) {
    errEl.textContent = msg;
    errEl.classList.remove("d-none");
  }
  function clearError() {
    errEl.classList.add("d-none");
  }

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".regen-btn").addEventListener("click", async () => {
    clearError();
    const btn = form.querySelector(".regen-btn");
    btn.disabled = true;
    btn.textContent = "🔁 Генерирую…";
    try {
      await api(`/api/ma/threads/${encodeURIComponent(threadId)}/suggest-reply`, {
        method: "POST",
      });
      // Fetch fresh review payload to get new ru_answer/de_answer
      const fresh = await api(`/api/ma/messages/${msgId}`);
      ruInput.value = fresh.draft?.ru_answer ?? "";
      deInput.value = fresh.draft?.de_answer ?? "";
      lastRu = ruInput.value;
      lastDe = deInput.value;
    } catch (e) {
      showError(`Ошибка: ${e.message ?? String(e)}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "🔁 Перегенерировать (новый вариант от ИИ)";
    }
  });

  form.querySelector(".send-btn").addEventListener("click", async () => {
    clearError();
    const ruNow = ruInput.value.trim();
    const deNow = deInput.value.trim();
    if (!deNow) {
      showError("DE текст не может быть пустым (это что уйдёт клиенту)");
      return;
    }
    const sendBtn = form.querySelector(".send-btn");
    const cancelBtn = form.querySelector(".cancel-btn");
    const regenBtn = form.querySelector(".regen-btn");
    sendBtn.disabled = true;
    cancelBtn.disabled = true;
    regenBtn.disabled = true;
    sendBtn.textContent = "⏳ Отправляю…";
    try {
      // 1. Apply RU edit if changed
      if (ruNow !== lastRu && ruNow) {
        await api(`/api/ma/messages/${msgId}/edit-ru`, {
          method: "POST",
          body: {text: ruNow},
        });
      }
      // 2. Apply DE edit if changed
      if (deNow !== lastDe) {
        await api(`/api/ma/messages/${msgId}/edit-de`, {
          method: "POST",
          body: {text: deNow},
        });
      }
      // 3. Send
      const res = await api(`/api/ma/messages/${msgId}/send`, {method: "POST"});
      onSubmitComplete(res);
    } catch (e) {
      showError(`Ошибка: ${e.message ?? String(e)}`);
      sendBtn.disabled = false;
      cancelBtn.disabled = false;
      regenBtn.disabled = false;
      sendBtn.textContent = "📨 Отправить";
    }
  });

  return form;
}
