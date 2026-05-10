// Action grid — sticky-bottom компонент с inline confirm state machine.

import { api } from "../api.js?v=20260510-22";
import { el } from "../utils.js?v=20260510-22";

const CONFIRM_TIMEOUT_MS = 5000;

// Action key → {label, path, body?, confirm, kind: "final"|"intermediate"|"edit"|"thread"}
const ACTIONS = {
  send:        {label: "✅ ОТПРАВИТЬ",     path: "/send",        confirm: "Отправить?",   kind: "final"},
  skip:        {label: "❌ Пропустить",   path: "/skip",        confirm: "Пропустить?",  kind: "final"},
  sold:        {label: "💰 Продано",      path: "/sold",        confirm: "Помечать продано?", kind: "final"},
  wait:        {label: "✋ Ждать",          kind: "thread"},  // wired via onWait callback (calls thread endpoint)
  edit_ru:     {label: "✏️ Правка RU",   kind: "edit", field: "ru"},
  edit_de:     {label: "✏️ Правка DE",   kind: "edit", field: "de"},
  instruction: {label: "📝 Своя инстр.", kind: "edit", field: "instruction"},
};


export function buildActionGrid({msgId, onActionComplete, onError, onEditRequest, onSuggest, onCompose, onHistory, onWait, autopilotBlock}) {
  const grid = el(`
    <div class="action-grid mt-3">
      <div class="row g-2 mb-2">
        <div class="col-4"><button class="btn btn-sm btn-outline-success w-100 suggest-btn">🤖 Предложить</button></div>
        <div class="col-4"><button class="btn btn-sm btn-outline-primary w-100 compose-btn">✉️ Написать</button></div>
        <div class="col-4"><button class="btn btn-sm btn-outline-secondary w-100 history-btn">📋 История</button></div>
      </div>
      <div class="d-grid mb-2">
        <button data-action="send" class="btn btn-primary">✅ ОТПРАВИТЬ</button>
      </div>
      <div class="row g-2 mb-2">
        <div class="col-6"><button data-action="edit_ru" class="btn btn-outline-secondary w-100">✏️ Правка RU</button></div>
        <div class="col-6"><button data-action="edit_de" class="btn btn-outline-secondary w-100">✏️ Правка DE</button></div>
      </div>
      <div class="row g-2 mb-2">
        <div class="col-6"><button data-action="instruction" class="btn btn-outline-secondary w-100">📝 Своя инстр.</button></div>
        <div class="col-6"><button data-action="wait" class="btn btn-outline-warning w-100">✋ Ждать</button></div>
      </div>
      <div class="row g-2 mb-2">
        <div class="col-6"><button data-action="skip" class="btn btn-outline-danger w-100">❌ Пропустить</button></div>
        <div class="col-6"><button data-action="sold" class="btn btn-danger w-100">💰 Продано</button></div>
      </div>
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
    if (a.kind === "edit" || a.kind === "thread") {
      fireAction(actionKey);  // no confirm for edit/thread (edit opens form; wait is benign)
    } else {
      startConfirm(actionKey);
    }
  });

  return grid;
}
