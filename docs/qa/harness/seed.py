# Синтетическая БД для QA-прогона Mini App. Никаких реальных данных: имена, письма,
# объявления выдуманы. Токен бота — фейковый (initData считается от него же).
# Запуск: python3 seed.py <repo_root> <db_path>
import json
import sys
import pathlib
from datetime import datetime, timedelta

sys.path.insert(0, sys.argv[1])
import database as db

DB = pathlib.Path(sys.argv[2])
if DB.exists():
    DB.unlink()
db.DB_PATH = DB
db.init_db()

FAKE_TOKEN = "123456:QA_FAKE_TOKEN_xyz"
QA_USER_ID = "777000777"
NOW = datetime.utcnow()


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


with db.get_conn() as c:
    for k, v in {
        "telegram_bot_token": FAKE_TOKEN,
        "telegram_chat_id": QA_USER_ID,
        "telegram_authorized": QA_USER_ID,
        "send_mode": "disabled",
        "polling_paused": "1",
    }.items():
        c.execute("insert or replace into settings(key,value,updated_at) values(?,?,?)", (k, v, ts(0)))

    accs = ["Ersatzteile Nord", "Autoteile Süd", "Werkstatt Ost", "Garage West", "Teile-Lager Mitte"]
    for i, name in enumerate(accs, 1):
        c.execute(
            "insert into accounts(name,gmail_email,gmail_app_password,is_active,created_at,auto_ack_enabled) "
            "values(?,?,?,?,?,?)", (name, f"qa{i}@example.invalid", "", 1, ts(900), i % 2))

    brief = json.dumps({"summary_ru": "Покупатель торгуется, предлагает 300€", "expected_next": "ждём ответ",
                        "negotiated_price_eur": 300, "client_assessment": "серьёзный"}, ensure_ascii=False)
    long_de = ("Hallo, ich interessiere mich sehr für Ihre Sitzbank. Ist sie noch verfügbar? Gibt es Risse oder "
               "Verfärbungen am Leder? Können Sie mir weitere Fotos schicken, besonders von der Unterseite und den "
               "Befestigungspunkten? Wäre Versand nach 80331 München möglich und was würde das kosten? "
               "Ich könnte auch am Wochenende abholen. Mit freundlichen Grüßen") * 2
    threads = [
        # (thread, acc, buyer, lang, de_client, ru_client, ad_title, price, status, hours_ago, de_answer, ru_answer, sent)
        ("qa-t1", 1, "Klaus M.", "de", "Ist die Rückbank noch da? Letzter Preis 300?", "Задняя скамья ещё есть? Последняя цена 300?",
         "Rückbank Peugeot Traveller 2+1 Sitzbank Leder schwarz komplett", "450 €", "pending", 0.3,
         "Hallo Klaus, ja, sie ist noch da. 300 € ist leider zu wenig, 400 € wäre möglich.\nMit freundlichen Grüßen",
         "Да, ещё есть. 300 мало, 400 можно.", None),
        ("qa-t2", 2, "Anna_K", "de", long_de, "Здравствуйте, очень интересует скамья... (длинный текст)",
         "Sitzbank Citroën Jumpy", "1000 €", "pending", 2, None, None, None),
        ("qa-t3", 3, "Piotr", "pl", "Dzień dobry, czy fotel jest dostępny? Ile kosztuje wysyłka do Polski?",
         "Добрый день, кресло доступно? Сколько стоит доставка в Польшу?",
         "Einzelsitz Fahrersitz VW T6", "250 €", "pending", 5,
         "Dzień dobry, tak, fotel jest dostępny. Wysyłka do Polski kosztuje 60 €.\nPozdrawiam",
         "Да, доступно. Доставка в Польшу 60€.", None),
        ("qa-t4", 4, "M.Yilmaz", "de", "Danke, ich melde mich morgen.", "Спасибо, напишу завтра.",
         "Sitzbank 2er Mercedes Vito", "600 €", "sent", 30,
         "Gerne, ich warte auf Ihre Nachricht.\nViele Grüße", "Хорошо, жду.", ts(29)),
        ("qa-t5", 5, "B.ST", "de", "Kann ich die Bank heute abholen? Barzahlung.", "Могу забрать сегодня? Наличными.",
         "Klappbank Renault Trafic", "350 €", "sent", 50,
         "Ja, heute ab 17 Uhr möglich.\nMit freundlichen Grüßen", "Да, сегодня после 17.", ts(49)),
        ("qa-t6", 1, "Jean", "fr", "Bonjour, prix final ?", "Здравствуйте, финальная цена?",
         "Sitzbank Ford Transit Custom", "500 €", "new", 1, None, None, None),
    ]
    mid = 0
    for (tid, acc, buyer, lang, dec, ruc, title, price, status, h, dea, rua, sent) in threads:
        ad_id = str(2900000000 + mid)
        c.execute(
            "insert into messages(account_id,direction,ad_url,ad_title,ad_price,ad_id,buyer_name,buyer_display_name,"
            "de_client,ru_client,ru_answer,de_answer,ru_translation,status,gmail_message_id,gmail_thread_id,"
            "created_at,sent_at,email_subject,client_lang,answer_lang,deal_brief_json,reminder_state,is_reminder,"
            "is_auto_ack,is_autopilot_reply) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (acc, "in", f"https://www.kleinanzeigen.de/s-anzeige/qa/{ad_id}", title, price, ad_id,
             f"{buyer.lower().replace(' ', '')}@example.invalid", buyer, dec, ruc, rua, dea, rua,
             status, f"<qa-{tid}-{mid}@example.invalid>", tid, ts(h), sent,
             f"Nutzer-Anfrage zu deiner Anzeige \"{title}\"", lang, lang, brief, "none", 0, 0, 0))
        mid += 1
    # Второе сообщение в треде qa-t1 — история переписки
    c.execute(
        "insert into messages(account_id,direction,ad_title,ad_price,ad_id,buyer_name,buyer_display_name,de_client,"
        "ru_client,de_answer,ru_answer,status,gmail_message_id,gmail_thread_id,created_at,sent_at,client_lang,"
        "reminder_state,is_reminder,is_auto_ack,is_autopilot_reply) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "in", threads[0][6], "450 €", "2900000000", "klausm.@example.invalid", "Klaus M.",
         "Hallo, ist die Bank noch verfügbar?", "Здравствуйте, скамья ещё доступна?",
         "Hallo, ja, noch verfügbar.\nMit freundlichen Grüßen", "Да, доступна.", "sent",
         "<qa-t1-early@example.invalid>", "qa-t1", ts(26), ts(25), "de", "none", 0, 0, 0))
    # Проданное объявление + одна продажа
    c.execute("update messages set sold_price_eur=?, sold_at=? where gmail_thread_id='qa-t5'", (330, ts(20)))
    c.execute("insert into ad_briefs(ad_id,ad_title,ad_price,brief_md,created_at,updated_at,sold_at) values(?,?,?,?,?,?,?)",
              ("2900000004", "Klappbank Renault Trafic", "350 €", "Бриф: складная скамья, б/у", ts(50), ts(20), ts(20)))
    c.execute("insert into detected_sales(gmail_thread_id,is_sale,confidence,sold_price_eur,detected_ad_title,"
              "detected_sold_at,evidence,model,created_at) values(?,?,?,?,?,?,?,?,?)",
              ("qa-t4", 1, 0.8, 580, "Sitzbank 2er Mercedes Vito", ts(10), "Покупатель: «komme morgen abholen»",
               "haiku", ts(10)))
    c.execute("insert into thread_flags(gmail_thread_id,is_pinned,operator_unread,updated_at) values(?,?,?,?)",
              ("qa-t2", 1, 1, ts(1)))
print("seeded", DB)
