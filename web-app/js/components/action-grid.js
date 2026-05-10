// Action grid — sticky-bottom компонент с inline confirm state machine.

import { api } from "../api.js?v=20260510-19";
import { el } from "../utils.js?v=20260510-19";

const CONFIRM_TIMEOUT_MS = 5000;

// Action key → {label, path, body?, confirm, kind: "final"|"intermediate"|"edit", field?}
const ACTIONS = {
  send:        {label: "✅ ОТПРАВИТЬ",     path: "/send",        confirm: "Отправить?",   kind: "final"},
  skip:        {label: "❌ Пропустить",   path: "/skip",        confirm: "Пропустить?",  kind: "final"},
  sold:        {label: "💰 Продано",      path: "/sold",        confirm: "Помечать продано?", kind: "final"},
  fest:        {label: "💎 Без торга",   path: "/regenerate",  body: {strategy: "fest"},  confirm: "Регенерировать?", kind: "intermediate"},
  harsh:       {label: "👊 Жёстче",      path: "/regenerate",  body: {strategy: "harsh"}, confirm: "Регенерировать?", kind: "intermediate"},
  friend:      {label: "☺️ Мягче",       path: "/regenerate",  body: {strategy: "friend"},confirm: "Регенерировать?", kind: "intermediate"},
  short:       {label: "✂️ Короче",      path: "/regenerate",  body: {strategy: "short"}, confirm: "Регенерировать?", kind: "intermediate"},
  regen:       {label: "🔁 Переформ.",   path: "/regenerate",  body: {strategy: "regen"}, confirm: "Регенерировать?", kind: "intermediate"},
  edit_ru:     {label: "✏️ Правка RU",  kind: "edit", field: "ru"},
  edit_de:     {label: "✏️ Правка DE",  kind: "edit", field: "de"},
  price:       {label: "💸 Своя цена",  kind: "edit", field: "price"},
  instruction: {label: "📝 Своя инстр.",kind: "edit", field: "instruction"},
};


export function buildActionGrid({msgId, onActionComplete, onError, onEditRequest}) {
  /**
   * onActionComplete(action_key, response_body) — после успешного API call
   * onError(action_key, message) — при HTTP error
   * onEditRequest(field) — оператор тапнул edit-* / price / instruction
   */
  const grid = el(`
    <div class="action-grid mt-3">
      <div class="d-grid mb-2">
        <button data-action="send" class="btn btn-primary">✅ ОТПРАВИТЬ</button>
      </div>
      <div class="row g-2">
        <div class="col-6"><button data-action="edit_ru" class="btn btn-outline-secondary w-100">✏️ Правка RU</button></div>
        <div class="col-6"><button data-action="edit_de" class="btn btn-outline-secondary w-100">✏️ Правка DE</button></div>
        <div class="col-6"><button data-action="fest" class="btn btn-outline-secondary w-100">💎 Без торга</button></div>
        <div class="col-6"><button data-action="price" class="btn btn-outline-secondary w-100">💸 Своя цена</button></div>
        <div class="col-6"><button data-action="harsh" class="btn btn-outline-secondary w-100">👊 Жёстче</button></div>
        <div class="col-6"><button data-action="friend" class="btn btn-outline-secondary w-100">☺️ Мягче</button></div>
        <div class="col-6"><button data-action="short" class="btn btn-outline-secondary w-100">✂️ Короче</button></div>
        <div class="col-6"><button data-action="regen" class="btn btn-outline-secondary w-100">🔁 Переформ.</button></div>
        <div class="col-6"><button data-action="instruction" class="btn btn-outline-secondary w-100">📝 Своя инстр.</button></div>
        <div class="col-6"><button data-action="skip" class="btn btn-outline-danger w-100">❌ Пропустить</button></div>
      </div>
      <div class="d-grid mt-2">
        <button data-action="sold" class="btn btn-danger">💰 Продано</button>
      </div>
    </div>
  `);

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
      // Тап другой кнопки во время confirm — отменяем confirm
      resetConfirm();
      return;
    }
    if (confirmingAction === actionKey) return; // confirm-mode, ждём [Да]/[Нет]

    const a = ACTIONS[actionKey];
    if (a.kind === "edit") {
      fireAction(actionKey);  // edit не требует confirm
    } else {
      startConfirm(actionKey);
    }
  });

  return grid;
}
