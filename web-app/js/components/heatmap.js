// 🗺 Тепловая карта земель ФРГ (tile-grid): цвет плитки ∝ числу объявлений.
// Общая для «Рынок → Сиденья» и «Рынок → Карта» (разведка scout).
import { el } from "../utils.js";

// [строка, столбец, аббревиатура] — схематичная география
export const TILE_GRID = {
  "Schleswig-Holstein": [0, 1, "SH"],
  "Bremen": [1, 0, "HB"], "Hamburg": [1, 1, "HH"], "Mecklenburg-Vorpommern": [1, 2, "MV"],
  "Niedersachsen": [2, 1, "NI"], "Brandenburg": [2, 2, "BB"], "Berlin": [2, 3, "BE"],
  "Nordrhein-Westfalen": [3, 0, "NW"], "Sachsen-Anhalt": [3, 1, "ST"], "Sachsen": [3, 2, "SN"],
  "Rheinland-Pfalz": [4, 0, "RP"], "Hessen": [4, 1, "HE"], "Thüringen": [4, 2, "TH"],
  "Saarland": [5, 0, "SL"], "Baden-Württemberg": [5, 1, "BW"], "Bayern": [5, 2, "BY"],
};

// counts: Map(земля → число). onPick(land) — нажатие на плитку; selected — подсвеченная земля.
export function heatGrid(counts, onPick, selected = null) {
  const max = Math.max(1, ...counts.values());
  const wrap = el(`<div class="heatmap" role="group" aria-label="Тепловая карта по землям"></div>`);
  for (const [land, [row, col, ab]] of Object.entries(TILE_GRID)) {
    const n = counts.get(land) || 0;
    const t = el(`<button type="button" class="heat-tile"><span class="ab"></span><span class="n"></span></button>`);
    t.style.gridRow = row + 1;
    t.style.gridColumn = col + 1;
    const k = n / max;
    // интенсивность — доля «горячего» цвета поверх карточки (токены темы, без жёстких rgba)
    if (n) t.style.setProperty("--heat", `${Math.round(18 + 72 * k)}%`);
    t.classList.toggle("hot", k > 0.5);
    t.classList.toggle("zero", !n);
    t.classList.toggle("sel", land === selected);
    t.title = `${land}: ${n}`;
    t.setAttribute("aria-label", `${land}: ${n}`);
    t.querySelector(".ab").textContent = ab;
    t.querySelector(".n").textContent = n;
    if (n && onPick) t.addEventListener("click", () => onPick(land));
    else t.disabled = true;
    wrap.appendChild(t);
  }
  return wrap;
}

// Обёртка для массива объявлений с полем bundesland.
export function heatMap(all, onPick, selected = null) {
  const counts = new Map();
  all.forEach(r => { if (r.bundesland) counts.set(r.bundesland, (counts.get(r.bundesland) || 0) + 1); });
  return heatGrid(counts, onPick, selected);
}
