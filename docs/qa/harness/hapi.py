# Помощник: запросы к QA-бэкенду с initData. from hapi import g
import sys, json, time, urllib.request, urllib.error, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import initdata
PORT = 8821
def g(p, m="GET", b=None, raw=None, timeout=40):
    data = raw if raw is not None else (json.dumps(b).encode() if b is not None else None)
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{p}", method=m, data=data,
                               headers={"X-Telegram-Init-Data": initdata.make(), "Content-Type": "application/json"})
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.status, x.read().decode("utf-8", "replace"), (time.perf_counter() - t) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400], (time.perf_counter() - t) * 1000
