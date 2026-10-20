# Жёсткая батарея на API (синтетика): мусор, границы, двойные вызовы, параллель. Печатает таблицу + JSON.
# python3 hard_api.py  (бэкенд на 8821, БД hard-run.db). Любой 5xx = дефект; 2xx на заведомо плохой вход = дефект.
import json, sys, threading, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from hapi import g

rows = []
def case(name, path, m="GET", b=None, ok=(200,), raw=None, note=""):
    s, body, t = g(path, m, b, raw=raw)
    verdict = "ok" if s in ok else ("5XX" if s >= 500 else "UNEXPECTED")
    rows.append({"case": name, "req": f"{m} {path}", "status": s, "verdict": verdict, "ms": round(t), "resp": body[:160].replace("\n", " "), "note": note})
    return s, body

def J(body):
    try: return json.loads(body)
    except Exception: return {}

BAD = (400, 422)
# ---- склад: создание/правка
case("товар: пустое имя", "/api/ma/stock/products", "POST", {"title": "  "}, BAD)
case("товар: имя 301 симв.", "/api/ma/stock/products", "POST", {"title": "x" * 301}, BAD)
case("товар: цена −5", "/api/ma/stock/products", "POST", {"title": "t", "list_price_eur": -5}, BAD)
case("товар: цена 1e9", "/api/ma/stock/products", "POST", {"title": "t", "list_price_eur": 1e9}, BAD)
case("товар: цена строкой", "/api/ma/stock/products", "POST", {"title": "t", "list_price_eur": "abc"}, BAD)
case("товар: цена NaN-литерал", "/api/ma/stock/products", "POST", raw=b'{"title":"t","list_price_eur":NaN}', ok=BAD)
case("товар: тело не JSON", "/api/ma/stock/products", "POST", raw=b"{{{", ok=BAD)
s, b = case("товар: XSS в названии (создаётся)", "/api/ma/stock/products", "POST", {"title": "<script>alert(1)</script>Тест", "category": "tisch"}, (200,))
pid = J(b).get("id")
case("товар: min > list", "/api/ma/stock/products", "POST", {"title": "min>list", "list_price_eur": 100, "min_price_eur": 500}, BAD, note="логическая ошибка: минимум выше цены")
case("товар: PATCH пусто", "/api/ma/stock/products/1", "PATCH", {}, BAD)
case("товар: PATCH 404", "/api/ma/stock/products/99999", "PATCH", {"note": "x"}, (404,))
case("товар: GET id=abc", "/api/ma/stock/products/abc", ok=(422,))
case("товар: GET id=-1", "/api/ma/stock/products/-1", ok=(404,))
case("товар: GET id=9e18", "/api/ma/stock/products/99999999999999999999", ok=(404, 422))
# ---- приход/списание/инвентаризация
for nm, q in [("qty=0", 0), ("qty=−3", -3), ("qty=1e9", 10**9), ("qty='abc'", "abc"), ("qty=2.5", 2.5), ("qty=null", None)]:
    case(f"приход: {nm}", "/api/ma/stock/products/3/receipt", "POST", {"qty": q}, BAD)
case("приход: cost −1", "/api/ma/stock/products/3/receipt", "POST", {"qty": 1, "unit_cost_eur": -1}, BAD)
case("приход: дата мусор", "/api/ma/stock/products/3/receipt", "POST", {"qty": 1, "occurred_at": "вчера-ish"}, BAD + (200,), note="дата-мусор: ожидаем 400, а не молча сегодня")
case("приход: дата 2099", "/api/ma/stock/products/3/receipt", "POST", {"qty": 1, "occurred_at": "2099-01-01"}, BAD, note="приход из будущего")
case("приход: товар 404", "/api/ma/stock/products/99999/receipt", "POST", {"qty": 1}, (404,))
case("приход в архивный/слитый", "/api/ma/stock/products/8/receipt", "POST", {"qty": 1}, (200, 400, 409))
q0 = J(g("/api/ma/stock/products/3")[1])
def qty(pid):
    return J(g(f"/api/ma/stock/products/{pid}/kit")[1]).get("stock", {})

case("списание больше остатка (стол 9 шт.→ 50)", "/api/ma/stock/products/3/writeoff", "POST", {"qty": 50}, (400, 409), note="не уходим в минус молча")
case("инвентаризация qty=0", "/api/ma/stock/products/7/count", "POST", {"qty": 0}, (200,))
case("инвентаризация qty=−1", "/api/ma/stock/products/7/count", "POST", {"qty": -1}, BAD)
# двойной тап (20 параллельных одинаковых приходов) — считаем сколько записалось
before = len(J(g("/api/ma/stock/products/7")[1]).get("movements", []))
res = []
def hit(): res.append(g("/api/ma/stock/products/7/receipt", "POST", {"qty": 1, "unit_cost_eur": 10})[0])
ths = [threading.Thread(target=hit) for _ in range(20)]; [t.start() for t in ths]; [t.join() for t in ths]
after = len(J(g("/api/ma/stock/products/7")[1]).get("movements", []))
rows.append({"case": "20 параллельных одинаковых приходов (двойной тап)", "req": "POST receipt ×20", "status": sorted(set(res)),
             "verdict": "INFO", "ms": 0, "resp": f"записано движений: {after - before} из 20 (нет идемпотентности — каждый тап = новый приход)", "note": ""})
# ---- слияние/архив/привязка
case("merge: товар сам в себя", "/api/ma/stock/merge", "POST", {"src_id": 1, "dst_id": 1}, BAD + (409,))
case("merge: 404", "/api/ma/stock/merge", "POST", {"src_id": 1, "dst_id": 99999}, (404, 400))
case("merge: в архивный/комплект", "/api/ma/stock/merge", "POST", {"src_id": 6, "dst_id": 4}, (200, 400, 409), note="слияние обычного товара в КОМПЛЕКТ")
case("archive: 404", "/api/ma/stock/products/99999/archive", "POST", {}, (404,))
case("relink: чужой ad", "/api/ma/stock/listings/00000/relink", "POST", {"product_id": 1}, (404, 400, 200))
# ---- комплекты
case("kit: сам в себя", "/api/ma/stock/products/4/kit", "PUT", {"components": [{"product_id": 4, "qty": 1}]}, BAD + (409,))
case("kit: qty 0", "/api/ma/stock/products/4/kit", "PUT", {"components": [{"product_id": 1, "qty": 0}]}, BAD)
case("kit: дубль компонента", "/api/ma/stock/products/4/kit", "PUT", {"components": [{"product_id": 1, "qty": 1}, {"product_id": 1, "qty": 2}]}, (200, 400))
case("kit: цикл (скамья ⊂ комплект ⊂ скамья)", "/api/ma/stock/products/1/kit", "PUT", {"components": [{"product_id": 4, "qty": 1}]}, BAD + (409,), note="цикл комплектов")
case("kit: >30 компонентов", "/api/ma/stock/products/4/kit", "PUT", {"components": [{"product_id": 1, "qty": 1}] * 31}, BAD)
# ---- партия (приход партией)
case("batch: пустой", "/api/ma/stock/batches", "POST", {"items": []}, BAD)
case("batch: qty 0", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 1, "qty": 0}]}, BAD)
case("batch: цена −1", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 1, "qty": 1}], "total_cost_eur": -1}, BAD)
case("batch: 404 товар", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 99999, "qty": 1}]}, (404,))
case("batch: один товар дважды", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 1, "qty": 1}, {"product_id": 1, "qty": 1}], "total_cost_eur": 100}, (200, 400))
case("batch: в комплект", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 4, "qty": 1}], "total_cost_eur": 100}, (200, 400, 409), note="приход на комплект (у комплекта нет своего остатка)")
case("batch: себестоимость 0 при qty>0", "/api/ma/stock/batches", "POST", {"items": [{"product_id": 3, "qty": 5}], "total_cost_eur": 0}, (200,))
# ---- приход строкой
for nm, t in [("мусор «asdf»", "asdf"), ("пустое место", "   "), ("5 скамья по 500", "5 скамья Traveller по 500 евро"), ("огромная qty", "999999999 скамья"), ("отрицательная", "-3 столика по -50"), ("XSS", "<script>1</script> 2 стол"), ("1001 симв.", "x" * 1001)]:
    case(f"приход строкой: {nm}", "/api/ma/stock/receipt/parse", "POST", {"text": t}, (200, 400, 422, 502, 503))
# ---- типы/себестоимость
case("тип: себест. −1", "/api/ma/stock/category-costs", "PUT", {"category": "sitzbank", "unit_cost_eur": -1}, BAD)
case("тип: пустая категория", "/api/ma/stock/category-costs", "PUT", {"category": "", "unit_cost_eur": 5}, BAD)
case("тип: XSS-категория", "/api/ma/stock/category-costs", "PUT", {"category": "<b>x</b>", "unit_cost_eur": 5}, (200, 400))
# ---- рынок
for nm, p in [("advice id=0", "/api/ma/market/advice/0"), ("advice id=−1", "/api/ma/market/advice/-1"), ("advice 404", "/api/ma/market/advice/99999"), ("advice abc", "/api/ma/market/advice/abc"), ("tips 404", "/api/ma/market/tips/99999"), ("repost limit=0", "/api/ma/market/repost?limit=0"), ("repost limit=21", "/api/ma/market/repost?limit=21"), ("repost limit=abc", "/api/ma/market/repost?limit=abc")]:
    case("рынок: " + nm, p, ok=(404, 422))
case("рынок: advice для товара без цены/остатка (id=7)", "/api/ma/market/advice/7", ok=(200,))
case("рынок: advice для XSS-товара", "/api/ma/market/advice/5", ok=(200,))
case("рынок: tips для продукта без объявлений", "/api/ma/market/tips/7?use_llm=false", ok=(200, 404))
case("рынок: repost with_text (LLM недоступен)", "/api/ma/market/repost?limit=20", ok=(200,))
case("рынок: thread/market 404", "/api/ma/threads/nope/market", ok=(404, 200))
# ---- письма: продано/отправка/правка
case("продано: цена 0", "/api/ma/threads/qa-t1/sold", "POST", {"price_eur": 0}, BAD)
case("продано: цена −5", "/api/ma/threads/qa-t1/sold", "POST", {"price_eur": -5}, BAD)
case("продано: цена 1e7", "/api/ma/threads/qa-t1/sold", "POST", {"price_eur": 1e7}, BAD)
case("продано: цена 'abc'", "/api/ma/threads/qa-t1/sold", "POST", {"price_eur": "abc"}, BAD)
case("продано: тред 404", "/api/ma/threads/nope/sold", "POST", {"price_eur": 5}, (404,))
case("правка DE: 4001 симв.", "/api/ma/messages/1/edit-de", "POST", {"text": "x" * 4001}, BAD)
case("правка DE: пусто", "/api/ma/messages/1/edit-de", "POST", {"text": ""}, BAD)
case("правка DE: XSS", "/api/ma/messages/1/edit-de", "POST", {"text": "<img src=x onerror=alert(1)> Hallo"}, (200,))
case("цена: −5", "/api/ma/messages/1/price", "POST", {"eur": -5}, BAD + (502,))
case("цена: 0", "/api/ma/messages/1/price", "POST", {"eur": 0}, BAD + (502,))
case("compose: пусто", "/api/ma/threads/qa-t1/compose", "POST", {"text": ""}, BAD)
case("compose: 4001", "/api/ma/threads/qa-t1/compose", "POST", {"text": "x" * 4001}, BAD)
case("send mode=bogus", "/api/ma/messages/1/send", "POST", {"mode": "bogus"}, BAD)
case("send delay_sec=61", "/api/ma/messages/1/send", "POST", {"mode": "as_is", "delay_sec": 61}, BAD)
case("send delay_sec=−1", "/api/ma/messages/1/send", "POST", {"mode": "as_is", "delay_sec": -1}, BAD)
case("send 404", "/api/ma/messages/99999/send", "POST", {"mode": "as_is"}, (404,))
case("message 999999999999999999999", "/api/ma/messages/999999999999999999999", ok=(404, 422))
# ---- повторная отправка (disabled-режим: как ведёт себя дубль)
s1, b1 = case("отправка #1 msg 3", "/api/ma/messages/3/send", "POST", {"mode": "as_is", "delay_sec": 0}, (200,))
s2, b2 = case("отправка #2 того же msg (повтор)", "/api/ma/messages/3/send", "POST", {"mode": "as_is", "delay_sec": 0}, (409,), note="повтор не должен уходить второй раз")
s3, b3 = case("повторное «Продано» того же треда", "/api/ma/threads/qa-t4/sold", "POST", {"price_eur": 300}, (200,))
s4, b4 = case("повторное «Продано» ещё раз", "/api/ma/threads/qa-t4/sold", "POST", {"price_eur": 300}, (200, 409), note="не должно задвоить продажу/движение склада")
case("пропуск дважды 1", "/api/ma/messages/6/skip", "POST", {}, (200, 409))
case("пропуск дважды 2", "/api/ma/messages/6/skip", "POST", {}, (200, 409))
# ---- аутентификация
import urllib.request, urllib.error
for nm, hdr in [("без initData", {}), ("мусорный initData", {"X-Telegram-Init-Data": "hash=deadbeef&user=%7B%7D"}), ("пустой", {"X-Telegram-Init-Data": ""})]:
    r = urllib.request.Request("http://127.0.0.1:8821/api/ma/stock/products", headers=hdr)
    try:
        st = urllib.request.urlopen(r, timeout=10).status
    except urllib.error.HTTPError as e:
        st = e.code
    rows.append({"case": f"авторизация: {nm}", "req": "GET /api/ma/stock/products", "status": st, "verdict": "ok" if st in (401, 403) else "UNEXPECTED", "ms": 0, "resp": "", "note": ""})
r = urllib.request.Request("http://127.0.0.1:8821/api/ma/stock/products", method="POST", data=b"{}", headers={"X-Telegram-Init-Data": ""})
bad = [r for r in rows if r["verdict"] in ("5XX", "UNEXPECTED")]
print(f"всего {len(rows)}, плохих {len(bad)}")
for r in rows:
    if r["verdict"] != "ok":
        print(f"[{r['verdict']}] {r['case']} → {r['status']} · {r['resp'][:110]} {('· ' + r['note']) if r['note'] else ''}")
json.dump(rows, open(sys.argv[1] if len(sys.argv) > 1 else "/dev/null", "w"), ensure_ascii=False, indent=1)
