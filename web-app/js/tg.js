// Helpers вокруг Telegram WebApp SDK.

export const tg = window.Telegram?.WebApp;

export function ready() {
  if (!tg) return;
  tg.ready();
  tg.expand();
}

export function initData() {
  return tg?.initData ?? "";
}

export function user() {
  return tg?.initDataUnsafe?.user ?? null;
}

export function startParam() {
  return tg?.initDataUnsafe?.start_param ?? null;
}

export function close() {
  tg?.close();
}
