// Отправка с окном «Отменить» (5 с). Сервер ставит письмо в очередь (delay_sec),
// отмена — POST /send/{id}/cancel; поздно — 409 «уже отправляется».
import { api } from "../api.js";
import { sendOutcome } from "../utils.js";
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
  toast(`⏳ Уйдёт клиенту через ${UNDO_SEC} с`, {
    ms: UNDO_SEC * 1000,
    action: {
      label: "Отменить",
      onClick: async () => {
        try {
          await api(`/api/ma/send/${res.job_id ?? msgId}/cancel`, { method: "POST" });
          toast("↩ Отменено — черновик вернулся", { ms: 3500 });
          onCancelled?.();
        } catch (e) {
          toast(e.message ?? "Не удалось отменить — письмо уже ушло", { level: "bad", ms: 5000 });
        }
      },
    },
  });
  return sendOutcome(res);
}
