# Тесты очистки тела письма от служебного текста Kleinanzeigen.
# Особый фокус — web-relay уведомления (ответ клиента через сайт/приложение KZ),
# где реальный текст завёрнут в шаблон и часто схлопнут в одну строку (HTML-only).

from modules import parser


# Реальные (по структуре) relay-уведомления, схлопнутые _strip_html в одну строку.
RELAY_REPLY = (
    'Antwort zur Anzeige "Opel Zafira Life, Vivaro C, Peugeot Traveller '
    'Rückleuchten" Du hast eine Antwort zur Anzeige: Opel Zafira Life, Vivaro C, '
    'Peugeot Traveller Rückleuchten (Anzeigennummer: 3459339969) erhalten. '
    'Antwort von Mayo Vielen Dank Mfg Beantworte diese Nachricht einfach mit der '
    '"Antworten"-Funktion deines E-Mail-Programms oder auf unserer Website bzw. '
    'in unseren Apps. Antworten Schütze dich vor Betrug: Tipps für deine '
    'Sicherheit. Dein Team von Kleinanzeigen Wenn du Fragen hast, schaue bitte '
    'in unseren Hilfebereich.'
)

RELAY_INQUIRY = (
    'Anfrage zu deiner Anzeige "Peugeot Traveller Expert" Ein Interessent hat '
    'eine Anfrage zu deiner Anzeige Peugeot Traveller Expert (Anzeigennummer: '
    '3470579923) gesendet. Nachricht von Anna Stautz Guten Abend, wäre eine '
    'Reservierung möglich? Viele Grüße Anna Stautz Beantworte diese Nachricht '
    'einfach mit der "Antworten"-Funktion deines E-Mail-Programms.'
)


def test_relay_reply_stripped_with_name():
    out = parser.clean_email_body(RELAY_REPLY, sender_name="Mayo")
    assert out == "Vielen Dank Mfg"


def test_relay_inquiry_stripped_with_name():
    out = parser.clean_email_body(RELAY_INQUIRY, sender_name="Anna Stautz")
    # Служебная шапка и футер срезаны; собственная подпись клиента сохранена.
    assert out.startswith("Guten Abend, wäre eine Reservierung möglich?")
    assert "Beantworte diese Nachricht" not in out
    assert "Anzeigennummer" not in out
    assert "Ein Interessent" not in out


def test_relay_name_with_dot_escaped():
    # Имя «B.ST» содержит спецсимвол regex — должно срезаться буквально.
    text = (
        'Antwort zur Anzeige "X" Du hast eine Antwort zur Anzeige: X '
        '(Anzeigennummer: 111) erhalten. Antwort von B.ST Ja, alles gut. Grüße '
        'Beantworte diese Nachricht einfach mit der "Antworten"-Funktion.'
    )
    out = parser.clean_email_body(text, sender_name="B.ST")
    assert out == "Ja, alles gut. Grüße"


def test_relay_without_name_keeps_name_prefix_but_strips_boilerplate():
    # Даже без sender_name служебная обёртка снимается (имя останется префиксом).
    out = parser.clean_email_body(RELAY_REPLY)
    assert out.startswith("Mayo Vielen Dank Mfg")
    assert "Beantworte diese Nachricht" not in out
    assert "Schütze dich vor Betrug" not in out


def test_relay_idempotent():
    once = parser.clean_email_body(RELAY_REPLY, sender_name="Mayo")
    twice = parser.clean_email_body(once, sender_name="Mayo")
    assert once == twice == "Vielen Dank Mfg"


def test_plain_client_message_untouched():
    plain = "Hallo,\nist das noch verfügbar?\nGrüße"
    assert parser.clean_email_body(plain, sender_name="Hans") == plain


def test_short_followup_untouched():
    # Прямой email-ответ клиента (не relay) — не должен ничего терять.
    text = "👍 Danke. Die Kinder freuen sich schon.\nGrüße"
    assert parser.clean_email_body(text, sender_name="B.ST") == text


def test_old_format_inquiry_still_stripped():
    old = (
        "Ein Interessent hat eine Anfrage zu Ihrer Anzeige gesendet: 3390278104:\n\n"
        "    Hallo, ist der Tisch noch da?\n\n"
        "Bitte antworten Sie nicht direkt auf diese E-Mail."
    )
    assert parser.clean_email_body(old) == "Hallo, ist der Tisch noch da?"


def test_empty_body():
    assert parser.clean_email_body("") == ""
    assert parser.clean_email_body("", sender_name="X") == ""


def test_message_mentioning_von_not_falsely_stripped():
    # Обычное письмо, где есть слово «von», но НЕТ футера relay — не режем шапку.
    text = "Das Paket kommt von DHL. Wann kann ich es abholen?"
    assert parser.clean_email_body(text, sender_name="Klaus") == text
