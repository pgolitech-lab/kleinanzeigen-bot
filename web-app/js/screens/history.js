import { setLoading } from "../utils.js";

export function render(mount, params) {
  setLoading(mount, `History: stub for ${params.email} (Task 8 fills this in)`);
}
