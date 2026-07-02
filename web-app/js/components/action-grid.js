// Action grid — sticky-bottom компонент с inline confirm state machine.

import { api } from "../api.js?v=20260702-000003";
import { el } from "../utils.js?v=20260702-000003";

const CONFIRM_TIMEOUT_MS = 5000;

// Action key → {label, path, body?, confirm, kind: "final"|"intermediate"|"edit"|"thread"|"sold"}
const ACTIONS = {
  skip:        {label: "❌ Пропустить",   path: "/skip",        confirm: "Пропустить?",  kind: "final"},
  sold:        {label: "💰 Продано",      path: "/sold",        kind: "sold"},
  wait:        {label: "✋ Ждать",          kind: "thread"},  // wired via onWait callback (calls thread endpoint)
  instruction: {label: "📝 Своя инстр.", kind: "edit", field: "instruction"},
};


export function buildActionGrid({msgId, onActionComplete, onError, onEditRequest, onSuggest, onCompose, onHistory, onWait, autopilotBlock}) {
  const grid = el(`
    <div class="action-grid mt-3">
      <div class="row g-2 mb-2 row-top">
        <div class="col-4"><button class="btn btn-sm btn-outline-success w-100 suggest-btn">🤖 Предложить</button></div>
        <div class="col-4"><button class="btn btn-sm btn-outline-primary w-100 compose-btn">✉️ Написать</button></div>
        <div class="col-4"><button class="btn btn-sm btn-outline-secondary w-100 history-btn">📋 История</button></div>
      </div>
      <div class="row g-2 mb-2 row-instr">
        <div class="col-6"><button data-action="instruction" class="btn btn-outline-secondary w-100">📝 Своя инстр.</button></div>
        <div class="col-6"><button data-action="wait" class="btn btn-outline-warning w-100">✋ Ждать</button></div>
      </div>
      <div class="row g-2 mb-2 row-final">
        <div class="col-6"><button data-action="skip" class="btn btn-outline-danger w-100">❌ Пропустить</button></div>
        <div class="col-6"><button data-action="sold" class="btn btn-danger w-100">💰 Продано</button></div>
      </div>
      <div class="sold-form-slot"></div>
      <div class="ap-slot"></div>
    </div>
  `);

  // Wire dedicated buttons (no confirm gate)
  grid.querySelector(".suggest-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (onSuggest) onSuggest();
  });
  grid.querySelector(".compose-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (onCompose) onCompose();
  });
  grid.querySelector(".history-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (onHistory) onHistory();
  });

  // Autopilot block slotted inside grid (passed from parent)
  if (autopilotBlock) {
    grid.querySelector(".ap-slot").appendChild(autopilotBlock);
  }

  let confirmTimer = null;
  let confirmingAction = null;
  const rowsToHide = [".row-top", ".row-instr", ".row-final"];

  function resetConfirm() {
    if (confirmTimer) {
      clearTimeout(confirmTimer);
      confirmTimer = null;
    }
    if (confirmingAction) {
      const btn = grid.querySelector(`[data-action="${confirmingAction}"]`);
      if (btn) {
        btn.textContent = ACTIONS[confirmingAction].label;
        if (btn.dataset.originalClass) {
          btn.className = btn.dataset.originalClass;
          delete btn.dataset.originalClass;
        }
      }
      confirmingAction = null;
    }
    grid.querySelectorAll("button[data-action]").forEach(b => b.disabled = false);
  }

  function showSoldForm() {
    resetConfirm();
    rowsToHide.forEach(sel => {
      const r = grid.querySelector(sel);
      if (r) r.style.display = "none";
    });
    const slot = grid.querySelector(".sold-form-slot");
    slot.innerHTML = "";

    const form = el(`
      <div class="card border-warning mb-2">
        <div class="card-body p-2">
          <div class="mb-2 small fw-semibold">💰 Зафиксировать продажу</div>
          <div class="mb-2">
            <label class="form-label small mb-1">Цена продажи (€)</label>
            <input type="number" inputmode="decimal" min="0" step="0.01"
                   class="form-control form-control-sm sold-price"
                   placeholder="напр. 1300"
                   autofocus>
          </div>
          <div class="form-check mb-2">
            <input type="checkbox" class="form-check-input close-other" id="sold-close-other">
            <label class="form-check-label small" for="sold-close-other">
              Закрыть остальные переговоры по этому объявлению
            </label>
          </div>
          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-success flex-grow-1 save-btn">✅ Сохранить</button>
            <button class="btn btn-sm btn-outline-secondary cancel-btn">Отмена</button>
          </div>
        </div>
      </div>
    `);
    slot.appendChild(form);
    form.querySelector(".sold-price").focus();

    form.querySelector(".cancel-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      hideSoldForm();
    });

    form.querySelector(".save-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      const priceStr = form.querySelector(".sold-price").value.trim();
      const price = Number(priceStr.replace(",", "."));
      if (!priceStr || Number.isNaN(price) || price < 0) {
        form.querySelector(".sold-price").classList.add("is-invalid");
        return;
      }
      const closeOther = form.querySelector(".close-other").checked;
      const saveBtn = form.querySelector(".save-btn");
      saveBtn.disabled = true;
      saveBtn.textContent = "⏳ Сохраняю…";
      try {
        const res = await api(`/api/ma/messages/${msgId}/sold`, {
          method: "POST",
          body: {price_eur: price, close_other_threads_for_ad: closeOther},
        });
        onActionComplete("sold", res);
      } catch (err) {
        onError("sold", err.message ?? String(err));
        hideSoldForm();
      }
    });
  }

  function hideSoldForm() {
    rowsToHide.forEach(sel => {
      const r = grid.querySelector(sel);
      if (r) r.style.display = "";
    });
    grid.querySelector(".sold-form-slot").innerHTML = "";
  }

  async function fireAction(actionKey) {
    const a = ACTIONS[actionKey];
    if (!a) return;
    if (a.kind === "edit") {
      resetConfirm();
      onEditRequest(a.field);
      return;
    }
    if (a.kind === "thread") {
      resetConfirm();
      if (onWait) onWait();
      return;
    }
    if (a.kind === "sold") {
      showSoldForm();
      return;
    }
    grid.querySelectorAll("button[data-action]").forEach(b => b.disabled = true);
    try {
      const opts = {method: "POST"};
      if (a.body) opts.body = a.body;
      const res = await api(`/api/ma/messages/${msgId}${a.path}`, opts);
      onActionComplete(actionKey, res);
    } catch (e) {
      onError(actionKey, e.message ?? String(e));
      resetConfirm();
    }
  }

  function startConfirm(actionKey) {
    const a = ACTIONS[actionKey];
    confirmingAction = actionKey;
    const btn = grid.querySelector(`[data-action="${actionKey}"]`);
    btn.dataset.originalClass = btn.className;
    btn.className = "btn btn-warning text-dark";
    btn.innerHTML = `⚠️ ${a.confirm} <span class="ms-1 text-success" data-confirm="yes">[Да]</span> <span class="ms-1 text-danger" data-confirm="no">[Нет]</span>`;
    grid.querySelectorAll("button[data-action]").forEach(b => {
      if (b !== btn) b.disabled = true;
    });
    confirmTimer = setTimeout(resetConfirm, CONFIRM_TIMEOUT_MS);
  }

  grid.addEventListener("click", (event) => {
    const target = event.target;
    const confirmHit = target.closest("[data-confirm]");
    if (confirmHit) {
      event.stopPropagation();
      const yes = confirmHit.dataset.confirm === "yes";
      const action = confirmingAction;
      resetConfirm();
      if (yes && action) fireAction(action);
      return;
    }
    const btn = target.closest("button[data-action]");
    if (!btn || btn.disabled) return;
    const actionKey = btn.dataset.action;
    if (confirmingAction && confirmingAction !== actionKey) {
      resetConfirm();
      return;
    }
    if (confirmingAction === actionKey) return;

    const a = ACTIONS[actionKey];
    if (a.kind === "edit" || a.kind === "thread" || a.kind === "sold") {
      fireAction(actionKey);  // no confirm — opens inline form / triggers callback
    } else {
      startConfirm(actionKey);
    }
  });

  return grid;
}
