# Насыщенная синтетика для «жёсткого» прогона: рынок, склад с партиями/комплектами, дубли, просевшие,
# устаревшие, горячие покупатели, просроченные ответы, XSS/длинные имена/EN/PL тексты.
# python3 seed_hard.py <repo_root> <db_path>   (сначала вызывает seed.py)
import subprocess
import sys
import pathlib
from datetime import datetime, timedelta

repo, dbp = sys.argv[1], sys.argv[2]
subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "seed.py"), repo, dbp], check=True)
sys.path.insert(0, repo)
import database as db  # noqa: E402

db.DB_PATH = pathlib.Path(dbp)
from modules import db_inventory as inv, db_market  # noqa: E402

NOW = datetime.utcnow()


def ts(h):
    return (NOW - timedelta(hours=h)).isoformat(timespec="seconds")


def days(d):
    return ts(d * 24)


inv.ensure_schema()
db_market.ensure_schema()
XSS = '<img src=x onerror="window.__xss=1">'
LONG = "Sitzbank " + "Peugeot Traveller Rückbank Leder schwarz komplett " * 6

with db.get_conn() as c:
    # --- рынок (чужие объявления scout), ключ — скамья Traveller
    def scout(ad, title, price, plz, city, age_d=3, active=1, kind="part"):
        c.execute("insert or replace into scout_listings(ad_id,kind,title,url,price_eur,price_raw,plz,city,first_seen_at,"
                  "last_seen_at,active,part_type) values(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ad, kind, title, f"https://www.kleinanzeigen.de/s-anzeige/x/{ad}", price, f"{price} €", plz, city,
                   days(age_d + 5), days(age_d), active, "bench"))
    prices = [300, 340, 380, 399, 420, 450, 480, 520, 650, 900]
    for i, p in enumerate(prices):
        scout(f"3100{i:05d}", f"Sitzbank Peugeot Traveller 2+1 Leder #{i}", p, "50667", "Köln", age_d=i % 4)
    for i, p in enumerate([150, 170, 190, 210, 230]):
        scout(f"3200{i:05d}", f"Einzelsitz Fahrersitz VW T6 Nr{i}", p, "80331", "München", age_d=1)
    scout("3300000001", "Klapptisch Multivan Tisch", 55, "10115", "Berlin")
    scout("3300000002", "Klapptisch Multivan Tisch", 0, "10115", "Berlin")          # мусорная цена
    scout("3300000003", "Sitzbank " + "X" * 400, 99999999, "00000", "", 1)            # гигантская цена/заголовок
    scout("3300000004", XSS + "Sitzbank", 100, "50667", "Köln")                      # XSS в чужом заголовке

# --- склад
P = {}
P["bank"] = inv.create_product(title="Sitzbank Peugeot Traveller 2+1 Leder schwarz", category="sitzbank",
                               condition="gebraucht", list_price_eur=450, min_price_eur=380)
P["vip"] = inv.create_product(title="Einzelsitz Fahrersitz VW T6 VIP", category="einzelsitz", condition="gebraucht",
                              list_price_eur=250, min_price_eur=200)
P["tisch"] = inv.create_product(title="Klapptisch Multivan", category="tisch", list_price_eur=80, min_price_eur=60)
P["kit"] = inv.create_product(title="Komplettsatz Traveller: Bank + 2 VIP + Tisch", category="sitzbank",
                              list_price_eur=1100, min_price_eur=900)
P["xss"] = inv.create_product(title=XSS + "Hack<script>window.__xss2=1</script>", category="tisch", list_price_eur=10)
P["long"] = inv.create_product(title=LONG, category="sitzbank", list_price_eur=450)
P["zero"] = inv.create_product(title="Товар без цены и остатка", category="tisch")
P["dead"] = inv.create_product(title="Старый товар (устарел) Sitzbank Opel Vivaro", category="sitzbank", list_price_eur=400)
inv.set_kit_components(P["kit"], [(P["bank"], 1), (P["vip"], 2), (P["tisch"], 1)])
with db.get_conn() as c:
    bid = inv.create_batch(total_cost_eur=2900, occurred_at=days(20), note="партия QA", created_by="qa", conn=c)
    inv.add_movement(product_id=P["bank"], kind="receipt", qty=5, unit_cost_eur=500, occurred_at=days(20), batch_id=bid, conn=c)
    inv.add_movement(product_id=P["vip"], kind="receipt", qty=6, unit_cost_eur=170, occurred_at=days(20), batch_id=bid, conn=c)
    inv.add_movement(product_id=P["tisch"], kind="receipt", qty=10, unit_cost_eur=50, occurred_at=days(20), batch_id=bid, conn=c)
    inv.add_movement(product_id=P["bank"], kind="sale", qty=-1, unit_price_eur=420, occurred_at=days(6), source="manual", conn=c)
    inv.add_movement(product_id=P["vip"], kind="sale", qty=-2, unit_price_eur=230, occurred_at=days(3), source="manual", conn=c)
    inv.add_movement(product_id=P["tisch"], kind="writeoff", qty=-1, occurred_at=days(2), note="брак", conn=c)
    inv.add_movement(product_id=P["xss"], kind="receipt", qty=3, unit_cost_eur=5, occurred_at=days(9), conn=c)

L = [  # ad_id, product, acc, title, price, last_seen_days_ago, state
    ("2900000000", "bank", 1, "Rückbank Peugeot Traveller 2+1 Sitzbank Leder schwarz komplett", 450, 0.1, "active"),
    ("4000000001", "bank", 2, "Sitzbank Peugeot Traveller 2+1 Leder schwarz", 450, 0.2, "active"),   # дубль в др. аккаунте
    ("4000000002", "bank", 1, "Sitzbank Peugeot Traveller 2+1 Leder schwarz", 450, 0.2, "active"),   # дубль в том же
    ("4000000003", "bank", 3, "Sitzbank Peugeot Traveller", 380, 41, "active"),                      # устарела 41 день
    ("4000000004", "vip", 4, "Einzelsitz Fahrersitz VW T6 VIP", 250, 0.3, "active"),
    ("4000000005", "vip", 5, "Gelöscht • Einzelsitz Fahrersitz VW T6 VIP", 250, 8, "deleted"),
    ("4000000006", "tisch", 1, "Klapptisch Multivan", 80, 35, "active"),                            # просевшая/старая
    ("4000000007", "kit", 2, "Komplettsatz Traveller Bank + 2 VIP + Tisch", 1100, 0.5, "active"),
    ("4000000008", "xss", 3, XSS + "Hack", 10, 1, "active"),
    ("4000000009", "long", 4, LONG, 450, 1, "active"),
    ("4000000010", "dead", 5, "Sitzbank Opel Vivaro", 400, 90, "deleted"),
]
for ad, pk, acc, title, price, seen_d, st in L:
    inv.upsert_listing(ad_id=ad, product_id=P[pk], link_method="seed", seen_at=days(seen_d), account_id=acc,
                       title=title, price_eur=price, url=f"https://www.kleinanzeigen.de/s-anzeige/qa/{ad}", state=st)
with db.get_conn() as c:
    c.execute("update inv_listings set first_seen_at=? where ad_id in ('4000000004','4000000001')", (days(12),))
    # история цен: «просевшие» (цена падала) и «подорожала»
    for ad, seq in [("4000000006", [120, 100, 80]), ("4000000001", [520, 480, 450]), ("4000000004", [200, 250])]:
        for i, p in enumerate(seq):
            c.execute("insert into market_price_history(ad_id,price_eur,price_raw,seen_at) values(?,?,?,?)",
                      (ad, p, f"{p} €", days(30 - i * 10)))
    # выдача: наш вес в поиске
    c.execute("insert into market_runs(source,keywords,category,pages,cards,started_at,finished_at) values('market','sitzbank peugeot traveller','c223',3,60,?,?)", (days(1), days(1)))
    rid = c.execute("select max(id) from market_runs").fetchone()[0]
    for pos, ad in [(4, "2900000000"), (17, "4000000001"), (41, "4000000002")]:
        c.execute("insert into market_serp(run_id,ad_id,page,pos,organic_pos,is_top,title,price_eur,plz,city,seen_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, ad, (pos - 1) // 20 + 1, pos, pos, 0, "Sitzbank Peugeot Traveller", 450, "50667", "Köln", days(1)))
    # --- письма: просроченный ответ (80 ч), горячий, EN, XSS-имя, длинное имя, мусор
    def msg(tid, buyer, text, ru, lang, h, title, price, acc, status="pending", de_ans=None, ad="2900000001"):
        c.execute("insert into messages(account_id,direction,ad_url,ad_title,ad_price,ad_id,buyer_name,buyer_display_name,"
                  "de_client,ru_client,ru_answer,de_answer,status,gmail_message_id,gmail_thread_id,created_at,email_subject,"
                  "client_lang,answer_lang,reminder_state,is_reminder,is_auto_ack,is_autopilot_reply) "
                  "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acc, "in", f"https://www.kleinanzeigen.de/s-anzeige/qa/{ad}", title, price, ad,
                   f"b{abs(hash(tid))%9999}@example.invalid", buyer, text, ru, None, de_ans, status,
                   f"<qa-{tid}@example.invalid>", tid, ts(h), f"Anfrage {title}", lang, lang, "none", 0, 0, 0))
    msg("qa-h1", "Overdue Olaf", "Hallo? Bitte antworten, ich warte seit Tagen.", "Алло? Жду уже несколько дней.", "de", 80, "Sitzbank Peugeot Traveller", "450 €", 1)
    msg("qa-h2", "HotHans", "Ich nehme sie. Kann heute 450 bar abholen, bitte Adresse.", "Беру. Сегодня заберу за 450 наличными, адрес?", "de", 0.1, "Sitzbank Peugeot Traveller", "450 €", 2, ad="4000000001")
    msg("qa-h3", "John Smith", "Hi, is the seat still available? Can you ship to the UK?", "Привет, сиденье ещё есть? Доставка в UK?", "en", 3, "Einzelsitz Fahrersitz VW T6 VIP", "250 €", 4, ad="4000000004")
    msg("qa-h4", '<script>window.__xss3=1</script>Mallory', XSS + " kaufen?", "XSS-тест", "de", 4, XSS + "Hack", "10 €", 3, ad="4000000008")
    msg("qa-h5", "Wolfeschlegelsteinhausenbergerdorff " * 4, "Preis? " * 300, "Цена? " * 300, "de", 5, LONG, "450 €", 4, ad="4000000009")
    msg("qa-h6", "Zażółć Gęślą Jaźń", "Cześć, ile kosztuje stolik? Wysyłka?", "Привет, сколько столик? Доставка?", "pl", 6, "Klapptisch Multivan", "80 €", 1, ad="4000000006")
    msg("qa-h7", "Neg Ned", "Ich biete -50 Euro. 0 Euro. 99999999999 Euro", "Предлагаю -50", "de", 7, "Sitzbank Peugeot Traveller", "-5 €", 2, ad="4000000001")
with db.get_conn() as c:
    def run(kw, rows):
        c.execute("insert into market_runs(source,keywords,category,pages,cards,started_at,finished_at) values('market',?,'c223',3,60,?,?)", (kw, days(0.5), days(0.5)))
        rid = c.execute("select max(id) from market_runs").fetchone()[0]
        for pos, ad, t, pr in rows:
            c.execute("insert into market_serp(run_id,ad_id,page,pos,organic_pos,is_top,title,price_eur,plz,city,seen_at) values(?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, ad, (pos - 1) // 20 + 1, pos, pos, 0, t, pr, "50667", "Köln", days(0.5)))
    # столик: нашего нет на 3 страницах («пропал»), VIP: на 55-м месте («просел»), скамья: ранг 41
    run("klapptisch multivan", [(i, f"33000000{i:02d}", "Klapptisch Multivan Tisch", 55) for i in range(1, 6)])
    run("einzelsitz fahrersitz vw t6", [(55, "4000000004", "Einzelsitz Fahrersitz VW T6 VIP", 250)] + [(i, f"32000000{i:02d}", "Einzelsitz VW T6", 190) for i in range(1, 5)])
    run("sitzbank traveller leder", [(41, "4000000002", "Sitzbank Peugeot Traveller 2+1 Leder schwarz", 450)] + [(i, f"31000{i:05d}", "Sitzbank Peugeot Traveller", 400) for i in range(1, 6)])
print("hard-seeded", dbp)
