# Сводка results.json → markdown. С двумя аргументами — сравнение «база → новый прогон».
# python3 report.py results.json                 → таблицы
# python3 report.py base.json new.json           → что починилось / что сломалось
import json
import sys


def load(p):
    return json.load(open(p))


def metrics(res):
    """(device, screen) → dict метрик; плюс плоский список флагов флоу."""
    out = {}
    for dev, r in res.items():
        for name, s in r["screens"].items():
            if not isinstance(s, dict):
                continue
            out[(dev, name)] = {"hscroll": s["hscroll"], "overflow": s["overflowN"], "small": s["smallN"],
                                "trunc": s["truncN"],
                                "back_ok": None if "back" not in s else (s["back"]["how"] is not None
                                                                         and s["back"]["to"] != s["back"]["from"])}
        ev = r["events"]
        for e in ev["errors"]:
            out.setdefault((dev, e["screen"]), {}).setdefault("js_errors", 0)
            out[(dev, e["screen"])]["js_errors"] += 1
        for h in ev["http"]:
            out.setdefault((dev, h["screen"]), {}).setdefault("http_errors", 0)
            out[(dev, h["screen"])]["http_errors"] += 1
        f = r["flows"]
        out[(dev, "_flows")] = {
            "unsaved_kept": (f.get("unsaved") or {}).get("kept"),
            "sheet_open_after_nav": f.get("sheet_open_after_nav"),
            "kb_textarea_visible": (f.get("keyboard") or {}).get("textarea_visible"),
            "kb_send_visible": (f.get("keyboard") or {}).get("send_visible"),
            "kb_compose_primary_visible": (f.get("keyboard_compose") or {}).get("primary_visible"),
            "compose_kept_after_back": bool(f.get("compose_kept_after_back")),
            "flow_errors": sum(1 for k in f if k.endswith("_error")),
        }
    return out


def table(res):
    devs = list(res)
    screens = []
    for r in res.values():
        for n, s in r["screens"].items():
            if isinstance(s, dict) and n not in screens:
                screens.append(n)
    print("### Горизонтальная прокрутка (px) / элементов за правым краем / тап-целей < 44 px / обрезанного текста\n")
    print("| экран | " + " | ".join(devs) + " |")
    print("|---|" + "---|" * len(devs))
    for n in screens:
        row = []
        for d in devs:
            s = res[d]["screens"].get(n)
            if not isinstance(s, dict):
                row.append("—"); continue
            cell = f"{s['hscroll']}/{s['overflowN']}/{s['smallN']}/{s['truncN']}"
            if s["hscroll"] > 0 or s["overflowN"] > 0:
                cell = f"**{cell}**"
            row.append(cell)
        print(f"| {n} | " + " | ".join(row) + " |")
    print("\n### Флоу\n")
    keys = ["unsaved_kept", "sheet_open_after_nav", "kb_textarea_visible", "kb_send_visible",
            "kb_compose_primary_visible", "compose_kept_after_back", "flow_errors"]
    m = metrics(res)
    print("| проверка | " + " | ".join(devs) + " |")
    print("|---|" + "---|" * len(devs))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(m[(d, '_flows')].get(k)) for d in devs) + " |")
    print("\n### Ошибки JS / HTTP / не-GET\n")
    for d in devs:
        ev = res[d]["events"]
        nong = sorted({f"{x['method']} {x['path']}" for x in ev["nonget"]})
        print(f"- **{d}**: JS {len(ev['errors'])}, HTTP {len(ev['http'])}, диалогов {len(ev['dialogs'])}, "
              f"не-GET (фейк 200) {len(ev['nonget'])}: {', '.join(nong[:8])}")


def compare(base, new):
    a, b = metrics(base), metrics(new)
    fixed, broke = [], []
    for k in sorted(set(a) | set(b)):
        ma, mb = a.get(k, {}), b.get(k, {})
        for key in sorted(set(ma) | set(mb)):
            va, vb = ma.get(key), mb.get(key)
            if va == vb:
                continue
            better_low = key in ("hscroll", "overflow", "small", "trunc", "js_errors", "http_errors",
                                 "flow_errors", "sheet_open_after_nav")
            va_n, vb_n = (va or 0), (vb or 0)
            if better_low:
                (fixed if vb_n < va_n else broke).append(f"{k[0]} · {k[1]} · {key}: {va} → {vb}")
            else:
                (fixed if bool(vb) and not bool(va) else broke).append(f"{k[0]} · {k[1]} · {key}: {va} → {vb}")
    print("### Починилось\n"); print("\n".join(f"- {x}" for x in fixed) or "- —")
    print("\n### Сломалось / хуже\n"); print("\n".join(f"- {x}" for x in broke) or "- —")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        table(load(sys.argv[1]))
    else:
        compare(load(sys.argv[1]), load(sys.argv[2]))
