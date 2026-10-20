// Отправка с окном «Отменить» (5 с). Сервер ставит письмо в очередь (delay_sec),
// отмена — POST /send/{id}/cancel; поздно — 409 «уже отправляется».
import { api } from "../api.js";
import { el, sendOutcome } from "../utils.js";
import { toast } from "./kit.js";

export const UNDO_SEC = 5;

export function postSend(msgId, { mode = "as_is", force = false, text = null } = {}) {
  const body = { mode, force, delay_sec: UNDO_SEC };
  if (text != null) body.text = text;
  return api(`/api/ma/messages/${msgId}/send`, { method: "POST", body });
}

// Показать итог. Письмо в очереди → тост с «Отменить»; onCancelled — когда отмена прошла.
export function announceSend(res, msgId, onCancelled) {
  if (!res?.queued) {
    const o = sendOutcome(res ?? {});
    toast(o.text, { level: o.level === "bad" ? "bad" : "", ms: 4500 });
    return o;
  }
  // отдельная полоса сверху: обычные тосты («Черновик изменился», ошибки) её не гасят
  document.querySelectorAll(".undo-bar").forEach(x => x.remove());
  const bar = el(`<div class="undo-bar" role="status"><span class="ub-t"></span><button type="button" class="ta">Отменить</button></div>`);
  const label = bar.querySelector(".ub-t");
  let left = UNDO_SEC;
  const paint = () => { label.textContent = `⏳ Уйдёт клиенту через ${left} с`; };
  paint();
  const timer = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(timer); bar.remove(); } else paint();
  }, 1000);
  bar.querySelector(".ta").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    clearInterval(timer);
    try {
      await api(`/api/ma/send/${res.job_id ?? msgId}/cancel`, { method: "POST" });
      bar.remove();
      toast("↩ Отменено — черновик вернулся", { ms: 3500 });
      onCancelled?.();
    } catch (e) {
      bar.remove();
      toast(e.message ?? "Не удалось отменить — письмо уже ушло", { level: "bad", ms: 5000 });
    }
  });
  (document.getElementById("shell") ?? document.body).appendChild(bar);
  return sendOutcome(res);
}
