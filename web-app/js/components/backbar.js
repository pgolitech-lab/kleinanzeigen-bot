// Централизованная in-app кнопка «← Назад» (sticky сверху). Гарантирует back
// на КАЖДОМ deep-экране, не полагаясь на нативную Telegram BackButton.
// Router вызывает showBack(handler) на не-root экранах и hideBack() на табах.
import { el } from "../utils.js?v=20260702-000003";

let _handler = null;

export function mountBackbar() {
  if (document.getElementById("topback")) return;
  const bar = el(`<div id="topback" class="d-none"><button class="btn btn-sm btn-link p-0 back-btn">← Назад</button></div>`);
  const app = document.getElementById("app");
  document.body.insertBefore(bar, app);
  bar.querySelector(".back-btn").addEventListener("click", () => { if (_handler) _handler(); });
}

export function showBack(handler) {
  _handler = handler;
  const bar = document.getElementById("topback");
  if (bar) bar.classList.remove("d-none");
}

export function hideBack() {
  _handler = null;
  const bar = document.getElementById("topback");
  if (bar) bar.classList.add("d-none");
}
