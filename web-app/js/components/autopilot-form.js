// Autopilot start form (floor + notify mode + preview) + status block с действием.

import { api } from "../api.js?v=20260624-020000";
import { el } from "../utils.js?v=20260624-020000";


export function buildAutopilotForm({threadId, onSubmitComplete, onCancel}) {
  let lastPreview = null;  // closure-state для preview данных

  const form = el(`
    <div class="autopilot-form border rounded p-3 mt-3">
      <div class="fw-semibold small mb-2">🚀 Запустить автопилот</div>
      <div class="mb-2">
        <label class="form-label small mb-1">Floor цена €</label>
        <input type="number" step="10" min="0" class="form-control floor-input" placeholder="1200" />
      </div>
      <div class="mb-3">
        <div class="form-check">
          <input class="form-check-input" type="radio" name="notify_mode" id="ap_silent" value="silent" checked>
          <label class="form-check-label" for="ap_silent">🤫 Silent (тихо)</label>
        </div>
        <div class="form-check">
          <input class="form-check-input" type="radio" name="notify_mode" id="ap_notify" value="notify">
          <label class="form-check-label" for="ap_notify">🔔 Notify (пинг при каждом ответе)</label>
        </div>
      </div>

      <div class="preview-block mb-3"></div>

      <div class="d-flex gap-2 flex-wrap">
        <button class="btn btn-outline-info preview-btn">👁 Сгенерировать превью</button>
        <button class="btn btn-primary save-btn">🚀 Старт</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const errEl = form.querySelector(".form-error");
  const floorInput = form.querySelector(".floor-input");
  const previewBlock = form.querySelector(".preview-block");
  const previewBtn = form.querySelector(".preview-btn");
  const saveBtn = form.querySelector(".save-btn");
  const cancelBtn = form.querySelector(".cancel-btn");

  function readForm() {
    const floor = parseFloat(floorInput.value);
    const mode = form.querySelector("input[name=notify_mode]:checked")?.value || "silent";
    return {floor, mode};
  }

  function showError(msg) {
    errEl.textContent = msg;
    errEl.classList.remove("d-none");
  }

  function clearError() {
    errEl.classList.add("d-none");
  }

  function renderPreview(preview) {
    lastPreview = preview;
    previewBlock.replaceChildren();
    if (!preview) return;

    const block = el(`
      <div class="border-top pt-3">
        <div class="text-muted small mb-2 fw-semibold">👁 Превью первого ответа:</div>
        <div class="ru-block mb-2">
          <div class="text-muted small">RU (идея)</div>
          <div class="ru-text p-2 rounded bg-secondary-subtle"></div>
        </div>
        <div class="client-block mb-2">
          <div class="text-muted small">DE → клиенту</div>
          <div class="client-text p-2 rounded bg-primary-subtle"></div>
        </div>
        <div class="trans-block mb-2">
          <div class="text-muted small">RU обратный перевод</div>
          <div class="trans-text p-2 rounded fst-italic small"></div>
        </div>
        <div class="brief-text text-muted small mb-2"></div>
        <button class="btn btn-sm btn-outline-info regen-btn">🔁 Другой вариант</button>
      </div>
    `);
    block.querySelector(".ru-text").textContent = preview.ru_text || "";
    block.querySelector(".client-text").textContent = preview.client_text || "";
    block.querySelector(".trans-text").textContent = preview.ru_translation || "";

    const brief = preview.deal_brief || {};
    const briefParts = [];
    if (brief.summary_ru) briefParts.push(`💬 ${brief.summary_ru}`);
    if (brief.negotiated_price_eur) briefParts.push(`💰 ${brief.negotiated_price_eur}€`);
    if (brief.client_assessment) briefParts.push(`🏷 ${brief.client_assessment}`);
    block.querySelector(".brief-text").textContent = briefParts.join(" · ");

    block.querySelector(".regen-btn").addEventListener("click", () => generatePreview());
    previewBlock.appendChild(block);

    // После preview: «🚀 Старт» меняет надпись чтобы было ясно
    saveBtn.textContent = "🚀 Старт с этим ответом";
  }

  async function generatePreview() {
    clearError();
    const {floor, mode} = readForm();
    if (Number.isNaN(floor) || floor <= 0) {
      showError("Сначала задай floor цену");
      return;
    }
    previewBtn.disabled = true;
    previewBtn.textContent = "👁 Генерирую…";
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/autopilot/preview`, {
        method: "POST",
        body: {floor_eur: floor, notify_mode: mode},
      });
      renderPreview(res.preview);
    } catch (e) {
      showError(`Превью не удалось: ${e.message ?? String(e)}`);
    } finally {
      previewBtn.disabled = false;
      previewBtn.textContent = lastPreview ? "👁 Перегенерировать" : "👁 Сгенерировать превью";
    }
  }

  cancelBtn.addEventListener("click", () => onCancel());

  previewBtn.addEventListener("click", generatePreview);

  saveBtn.addEventListener("click", async () => {
    clearError();
    const {floor, mode} = readForm();
    if (Number.isNaN(floor) || floor <= 0) {
      showError("Введите положительное число для floor");
      return;
    }
    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    previewBtn.disabled = true;
    try {
      const body = {floor_eur: floor, notify_mode: mode};
      if (lastPreview) body.preview = lastPreview;
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/autopilot/start`, {
        method: "POST",
        body,
      });
      onSubmitComplete(res);
    } catch (e) {
      showError(e.message ?? String(e));
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
      previewBtn.disabled = false;
    }
  });

  return form;
}


export function buildAutopilotStatus({autopilotState, onStop, onStart}) {
  /**
   * autopilotState: {active, messages_sent, floor_eur, notify_mode} | null
   */
  const block = el(`
    <div class="autopilot-status border rounded p-2 mt-2">
      <div class="ap-info"></div>
      <div class="ap-actions mt-2"></div>
    </div>
  `);

  const info = block.querySelector(".ap-info");
  const actions = block.querySelector(".ap-actions");

  if (autopilotState?.active) {
    info.textContent = `🤖 Активен · ${autopilotState.messages_sent ?? 0}/20 · floor ${autopilotState.floor_eur ?? "?"}€ · ${autopilotState.notify_mode ?? "?"}`;
    const stopBtn = el(`<button class="btn btn-sm btn-danger">🛑 Остановить автопилот</button>`);
    stopBtn.addEventListener("click", onStop);
    actions.appendChild(stopBtn);
  } else {
    info.textContent = "🚀 Автопилот не активен";
    const startBtn = el(`<button class="btn btn-sm btn-outline-primary">🚀 Запустить</button>`);
    startBtn.addEventListener("click", onStart);
    actions.appendChild(startBtn);
  }

  return block;
}
