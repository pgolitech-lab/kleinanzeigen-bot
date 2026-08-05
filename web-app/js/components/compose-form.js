// Compose form: пишешь по-русски → «Перевести» (предпросмотр, редактируемый) →
// «Подтвердить и отправить». Отправка ВСЕГДА идёт через explicit-review шаг: то, что
// оператор видит (и может поправить) в окне предпросмотра — это буквально то, что
// уйдёт клиенту. compose больше не переводит повторно при отправке (см. modules/outgoing.py,
// инцидент 2026-07-02: ответ на немецком без предпросмотра ушёл клиенту переведённым на русский).
import { api, LLM_TIMEOUT_MS } from "../api.js?v=20260805-052102";
import { el } from "../utils.js?v=20260805-052102";
import { tg, confirm as tgConfirm } from "../tg.js?v=20260805-052102";

export function buildComposeForm({ threadId, onSubmitComplete, onCancel }) {
  const form = el(`
    <div class="compose-form border-top pt-3 mt-3">
      <div class="text-muted small mb-2 fw-semibold">✉️ Написать клиенту (на русском — переведём)</div>
      <textarea class="form-control mb-2 ru-input" rows="5" placeholder="Введите текст по-русски… (или «на немецком: …» / «in english: …»)"></textarea>
      <div class="d-flex gap-2 flex-wrap mb-2">
        <button class="btn btn-outline-info translate-btn">🔁 Перевести</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="preview d-none mb-2">
        <div class="text-muted small fw-semibold mt-1">Текст клиенту — проверьте и при необходимости исправьте перед отправкой</div>
        <textarea class="form-control de-preview mb-2" rows="5"></textarea>
        <div class="translate-note text-warning small mb-2 d-none"></div>
        <div class="text-muted small">RU обратный перевод (проверь смысл)</div>
        <div class="back-preview p-2 rounded fst-italic small mb-2"></div>
        <button class="btn btn-primary confirm-send-btn">✅ Подтвердить и отправить</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const textarea = form.querySelector(".ru-input");
  const errEl = form.querySelector(".form-error");
  const previewBox = form.querySelector(".preview");
  const dePrev = form.querySelector(".de-preview");
  const noteEl = form.querySelector(".translate-note");
  const backPrev = form.querySelector(".back-preview");
  const translateBtn = form.querySelector(".translate-btn");
  const confirmSendBtn = form.querySelector(".confirm-send-btn");
  const cancelBtn = form.querySelector(".cancel-btn");

  // Заполняется ответом /compose-preview; обнуляется при правке ru-input.
  // Отправка возможна только когда это заполнено — гарантирует, что перевод
  // (и его подтверждение оператором) произошёл ДО отправки.
  let lastPreview = null; // { ru_text, target_lang }

  function showErr(m) { errEl.textContent = m; errEl.classList.remove("d-none"); }
  function clearErr() { errEl.classList.add("d-none"); }

  function validate() {
    const text = textarea.value.trim();
    if (!text) { showErr("Текст не может быть пустым"); return null; }
    if (text.length > 4000) { showErr("Слишком длинный текст (макс. 4000)"); return null; }
    return text;
  }

  // Правка исходного RU-текста делает предпросмотр неактуальным — обязателен повторный перевод.
  textarea.addEventListener("input", () => {
    previewBox.classList.add("d-none");
    lastPreview = null;
  });

  translateBtn.addEventListener("click", async () => {
    clearErr();
    const text = validate();
    if (!text) return;
    translateBtn.disabled = true;
    translateBtn.textContent = "⏳ перевожу…";
    try {
      const r = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose-preview`, {
        timeoutMs: LLM_TIMEOUT_MS,
        method: "POST", body: { text },
      });
      dePrev.value = r.translated ?? "";
      backPrev.textContent = r.back_ru ?? "";
      if (r.note) {
        noteEl.textContent = "⚠️ " + r.note;
        noteEl.classList.remove("d-none");
      } else {
        noteEl.classList.add("d-none");
      }
      lastPreview = { ru_text: r.ru_text ?? text, target_lang: r.target_lang ?? "de" };
      previewBox.classList.remove("d-none");
    } catch (e) {
      showErr(e.message ?? String(e));
    } finally {
      translateBtn.disabled = false;
      translateBtn.textContent = "🔁 Перевести";
    }
  });

  cancelBtn.addEventListener("click", () => onCancel());

  confirmSendBtn.addEventListener("click", async () => {
    clearErr();
    if (!lastPreview) { showErr("Сначала переведите текст"); return; }
    const finalText = dePrev.value.trim();
    if (!finalText) { showErr("Текст клиенту не может быть пустым"); return; }

    const ok = await tgConfirm(`Отправить это сообщение клиенту (${lastPreview.target_lang.toUpperCase()})?\n\n${finalText}`);
    if (!ok) return;

    confirmSendBtn.disabled = true; cancelBtn.disabled = true; translateBtn.disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/compose`, {
        timeoutMs: LLM_TIMEOUT_MS,
        method: "POST",
        body: { text: lastPreview.ru_text, final_text: finalText, target_lang: lastPreview.target_lang },
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
      confirmSendBtn.disabled = false; cancelBtn.disabled = false; translateBtn.disabled = false;
    }
  });

  return form;
}
