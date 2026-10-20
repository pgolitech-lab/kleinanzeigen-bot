# Сравнение GET-ответов двух QA-бэкендов (база vs новая версия) на одинаковой синтетической БД.
# python3 api_diff.py <port_base> <port_new>
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import initdata  # noqa: E402

A, B = sys.argv[1], sys.argv[2]
PATHS = ["/api/ma/pipeline", "/api/ma/dashboard", "/api/ma/clients", "/api/ma/clients?q=klaus",
         "/api/ma/clients/klausm.%40example.invalid/history", "/api/ma/clients/anna_k%40example.invalid/history",
         "/api/ma/threads/qa-t1", "/api/ma/threads/qa-t2", "/api/ma/threads/qa-t3", "/api/ma/threads/qa-t4",
         "/api/ma/threads/qa-t5", "/api/ma/threads/qa-t6", "/api/ma/threads/qa-r1", "/api/ma/threads/qa-r2", "/api/ma/threads/qa-r3", "/api/ma/clients/klaus.other%40example.invalid/history", "/api/ma/threads/nope",
         "/api/ma/messages/1", "/api/ma/messages/6", "/api/ma/messages/999999",
         "/api/ma/sales", "/api/ma/detected-sales", "/api/ma/accounts", "/api/ma/settings",
         "/api/ma/scout/overview", "/api/ma/scout/status", "/health"]
# Поля, которые меняются от вызова к вызову (время, локи) — не сравниваем
VOLATILE = {"remaining_min", "now", "generated_at", "server_time", "lock", "acquired_at"}


def get(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Telegram-Init-Data": initdata.make()})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null"), (time.perf_counter() - t) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace"), (time.perf_counter() - t) * 1000


def strip(x):
    if isinstance(x, dict):
        return {k: strip(v) for k, v in x.items() if k not in VOLATILE}
    if isinstance(x, list):
        return [strip(v) for v in x]
    return x


def diff(a, b, path="", out=None, limit=25):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"`{path}.{k}`: новое поле = {json.dumps(b[k], ensure_ascii=False)[:80]}")
            elif k not in b:
                out.append(f"`{path}.{k}`: поле пропало (было {json.dumps(a[k], ensure_ascii=False)[:80]})")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"`{path}`: длина {len(a)} → {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(f"`{path}`: {json.dumps(a, ensure_ascii=False)[:80]} → {json.dumps(b, ensure_ascii=False)[:80]}")
    return out


print("| запрос | база | новая | мс база / новая | расхождения |")
print("|---|---|---|---|---|")
details = []
for p in PATHS:
    sa, ja, ta = get(A, p)
    sb, jb, tb = get(B, p)
    d = diff(strip(ja), strip(jb)) if sa == sb == 200 else ([] if sa == sb else [f"статус {sa} → {sb}"])
    print(f"| `{p}` | {sa} | {sb} | {ta:.0f} / {tb:.0f} | {len(d) if d else '—'} |")
    if d:
        details.append((p, d))
for p, d in details:
    print(f"\n**{p}**\n")
    for x in d:
        print(f"- {x}")
