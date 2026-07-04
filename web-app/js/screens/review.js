import { api } from "../api.js?v=20260704-203937";
import { setLoading, setError } from "../utils.js?v=20260704-203937";

export async function render(mount, params) {
  setLoading(mount, "Открываю карточку…");
  try {
    const review = await api(`/api/ma/messages/${encodeURIComponent(params.msg_id)}`);
    if (!review.thread_id) {
      setError(mount, "Тред не найден");
      return;
    }
    location.hash = `#/thread/${encodeURIComponent(review.thread_id)}/msg/${encodeURIComponent(params.msg_id)}`;
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
