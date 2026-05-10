// Autopilot start form (floor + notify mode) + status block с действием.

import { api } from "../api.js?v=20260510-12";
import { el } from "../utils.js?v=20260510-12";


export function buildAutopilotForm({threadId, onSubmitComplete, onCancel}) {
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
      <div class="d-flex gap-2">
        <button class="btn btn-primary save-btn">🚀 Старт</button>
        <button class="btn btn-outline-secondary cancel-btn">✕ Отмена</button>
      </div>
      <div class="form-error text-danger small mt-2 d-none"></div>
    </div>
  `);

  const errEl = form.querySelector(".form-error");
  const floorInput = form.querySelector(".floor-input");

  form.querySelector(".cancel-btn").addEventListener("click", () => onCancel());

  form.querySelector(".save-btn").addEventListener("click", async () => {
    errEl.classList.add("d-none");
    const floor = parseFloat(floorInput.value);
    if (Number.isNaN(floor) || floor <= 0) {
      errEl.textContent = "Введите положительное число для floor";
      errEl.classList.remove("d-none");
      return;
    }
    const mode = form.querySelector("input[name=notify_mode]:checked")?.value || "silent";
    form.querySelector(".save-btn").disabled = true;
    form.querySelector(".cancel-btn").disabled = true;
    try {
      const res = await api(`/api/ma/threads/${encodeURIComponent(threadId)}/autopilot/start`, {
        method: "POST",
        body: {floor_eur: floor, notify_mode: mode},
      });
      onSubmitComplete(res);
    } catch (e) {
      errEl.textContent = e.message ?? String(e);
      errEl.classList.remove("d-none");
      form.querySelector(".save-btn").disabled = false;
      form.querySelector(".cancel-btn").disabled = false;
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
