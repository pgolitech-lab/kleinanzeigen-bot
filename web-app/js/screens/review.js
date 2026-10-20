import { api } from "../api.js?v=20261011-085636";
import { setLoading, setError } from "../utils.js?v=20261011-085636";

export async function render(mount, params) {
  setLoading(mount, "Открываю карточку…");
  try {
    const review = await api(`/api/ma/messages/${encodeURIComponent(params.msg_id)}`);
    // Оператор успел уйти на другой экран — не утаскиваем его обратно в тред.
    if (!mount.isConnected) return;
    if (!review.thread_id) {
      setError(mount, "Тред не найден");
      return;
    }
    // replace: чтобы «Назад» из треда не возвращал на промежуточный review,
    // который тут же снова перекинул бы в тред.
    location.replace(`#/thread/${encodeURIComponent(review.thread_id)}/msg/${encodeURIComponent(params.msg_id)}`);
  } catch (e) {
    setError(mount, e.message ?? String(e));
  }
}
