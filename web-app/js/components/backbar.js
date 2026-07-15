// Кнопка «← Назад» и заголовок в шапке #hd (единственный back-аффорданс UI).
// Router вызывает showBack(handler) на deep-экранах и hideBack() на корневых табах;
// setTitle(t) ставит заголовок экрана (router — статический, экраны могут уточнять,
// например тред ставит название объявления после загрузки).
// mountBackbar() оставлен для совместимости с app.js — шапка уже в index.html.

let _handler = null;

export function mountBackbar() {
  const btn = document.querySelector("#hd .hback");
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", () => { if (_handler) _handler(); });
  }
}

export function setTitle(text) {
  const h = document.getElementById("htitle");
  if (h) h.textContent = text ?? "";
}

export function showBack(handler) {
  _handler = handler;
  const btn = document.querySelector("#hd .hback");
  if (btn) btn.classList.remove("d-none");
}

export function hideBack() {
  _handler = null;
  const btn = document.querySelector("#hd .hback");
  if (btn) btn.classList.add("d-none");
}
