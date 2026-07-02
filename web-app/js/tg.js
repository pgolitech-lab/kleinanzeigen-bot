// Helpers вокруг Telegram WebApp SDK.
// NOTE: telegram-web-app.js должен загружаться SYNC (no async/defer)
// в <head> до этого модуля; иначе window.Telegram.WebApp будет undefined.

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

// Диалог подтверждения перед необратимым действием (напр. отправка письма клиенту).
// Возвращает Promise<boolean>. Фоллбэк на window.confirm вне Telegram WebApp.
export function confirm(message) {
  return new Promise((resolve) => {
    if (tg?.showConfirm) {
      tg.showConfirm(message, (ok) => resolve(!!ok));
    } else {
      resolve(window.confirm(message));
    }
  });
}

// Открыть внешнюю ссылку (объявление Kleinanzeigen) в браузере.
export function openLink(url) {
  if (tg?.openLink) tg.openLink(url);
  else window.open(url, "_blank");
}

// BackButton API
export function showBack(handler) {
  if (!tg?.BackButton) return;
  tg.BackButton.show();
  tg.BackButton.offClick();
  tg.BackButton.onClick(handler);
}

export function hideBack() {
  if (!tg?.BackButton) return;
  tg.BackButton.offClick();
  tg.BackButton.hide();
}
