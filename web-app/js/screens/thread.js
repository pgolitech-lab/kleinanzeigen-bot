import { setLoading } from "../utils.js";

export function render(mount, params) {
  setLoading(mount, `Thread: stub for ${params.thread_id} (Task 7 fills this in)`);
}
