// Bottom-sheet — единый паттерн для меню действий, форм и подтверждений
// (вместо трёх разных: inline-морф кнопки / tg.showConfirm / window.confirm).

import { el } from "../utils.js?v=20260716-150240";

let _ov = null, _sh = null;

function ensureMounted() {
  if (_sh) return;
  _ov = el(`<div id="sheet-ov"></div>`);
  _sh = el(`<div id="sheet" role="dialog" aria-modal="true"></div>`);
  document.body.appendChild(_ov);
  document.body.appendChild(_sh);
  _ov.addEventListener("click", () => closeSheet());
}

// content: DOM-узел или массив узлов. title — опциональный заголовок.
export function openSheet(content, title) {
  ensureMounted();
  _sh.replaceChildren(el(`<div class="grab"></div>`));
  if (title) {
    const h = el(`<h3></h3>`);
    h.textContent = title;
    _sh.appendChild(h);
  }
  (Array.isArray(content) ? content : [content]).forEach(n => { if (n) _sh.appendChild(n); });
  // form-элементам внутри шита не нужен нижний отступ таб-бара
  requestAnimationFrame(() => {
    _ov.classList.add("on");
    _sh.classList.add("on");
  });
}

export function closeSheet() {
  if (!_sh) return;
  _ov.classList.remove("on");
  _sh.classList.remove("on");
}

// Меню действий: items = [{label, danger?, note?, onClick}]. Клик закрывает шит.
export function menuSheet(items, title) {
  const menu = el(`<div class="menu"></div>`);
  items.forEach(item => {
    if (!item) return;
    if (item.note) {
      const n = el(`<div class="menu-note"></div>`);
      n.textContent = item.note;
      menu.appendChild(n);
      return;
    }
    const b = el(`<button type="button"></button>`);
    b.textContent = item.label;
    if (item.danger) b.classList.add("danger");
    b.addEventListener("click", () => {
      closeSheet();
      item.onClick?.();
    });
    menu.appendChild(b);
  });
  openSheet(menu, title);
}

// Подтверждение: Promise<boolean>. Показывает сообщение + [Отмена][Да].
export function confirmSheet(message, confirmLabel = "Да") {
  return new Promise(resolve => {
    let settled = false;
    const done = (ok) => { if (!settled) { settled = true; closeSheet(); resolve(ok); } };
    const box = el(`
      <div>
        <div class="msg" style="white-space:pre-wrap; font-size:.95em; margin-bottom:14px"></div>
        <div class="d-flex gap-2">
          <button class="btn no-btn" style="flex:1">Отмена</button>
          <button class="btn btn-primary yes-btn" style="flex:1"></button>
        </div>
      </div>
    `);
    box.querySelector(".msg").textContent = message;
    box.querySelector(".yes-btn").textContent = confirmLabel;
    box.querySelector(".no-btn").addEventListener("click", () => done(false));
    box.querySelector(".yes-btn").addEventListener("click", () => done(true));
    ensureMounted();
    // закрытие по оверлею = отмена
    const ovCancel = () => { done(false); _ov.removeEventListener("click", ovCancel); };
    _ov.addEventListener("click", ovCancel);
    openSheet(box);
  });
}
