# Тесты self-heal перечистки входящих от web-relay шаблона Kleinanzeigen.
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest


RELAY = (
    'Antwort zur Anzeige "X" Du hast eine Antwort zur Anzeige: X '
    '(Anzeigennummer: 111) erhalten. Antwort von Mayo Vielen Dank Mfg '
    'Beantworte diese Nachricht einfach mit der "Antworten"-Funktion. '
    'Dein Team von Kleinanzeigen'
)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    import database as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO accounts (id, name, gmail_email, gmail_app_password, "
            "is_active, created_at) VALUES (1, 'test', 't@x', 'p', 1, ?)",
            (datetime.utcnow().isoformat(),),
        )
    return db_mod


def _add_incoming(db, de_client: str, name: str = "Mayo") -> int:
    return db.add_message(
        account_id=1,
        direction="in",
        gmail_thread_id="t-reclean",
        de_client=de_client,
        buyer_display_name=name,
        ru_client="старый мусорный перевод",
        client_lang="de",
        status="new",
    )


def test_count_polluted_detects_relay(tmp_db):
    from modules import incoming
    _add_incoming(tmp_db, RELAY)
    _add_incoming(tmp_db, "Hallo, ist das noch da?")  # чистое
    assert incoming.count_polluted_incoming() == 1


def test_reclean_fixes_and_translates(tmp_db):
    from modules import incoming
    mid = _add_incoming(tmp_db, RELAY)
    with patch.object(
        incoming.claude, "detect_and_translate_to_ru",
        return_value={"translation_ru": "Спасибо", "lang": "de", "cost_usd": 0.0},
    ) as mock_tr:
        summary = incoming.reclean_incoming_bodies()
    row = tmp_db.get_message(mid)
    assert row["de_client"] == "Vielen Dank Mfg"
    assert row["ru_client"] == "Спасибо"
    assert "fixed=1" in summary
    mock_tr.assert_called_once()
    assert incoming.count_polluted_incoming() == 0


def test_reclean_idempotent(tmp_db):
    from modules import incoming
    _add_incoming(tmp_db, RELAY)
    with patch.object(
        incoming.claude, "detect_and_translate_to_ru",
        return_value={"translation_ru": "Спасибо", "lang": "de", "cost_usd": 0.0},
    ):
        incoming.reclean_incoming_bodies()
        second = incoming.reclean_incoming_bodies()
    assert "fixed=0" in second
    assert "scanned=0" in second


def test_reclean_no_translate_on_failure_still_cleans(tmp_db):
    from modules import incoming
    mid = _add_incoming(tmp_db, RELAY)
    with patch.object(
        incoming.claude, "detect_and_translate_to_ru",
        side_effect=RuntimeError("Haiku down"),
    ):
        incoming.reclean_incoming_bodies()
    row = tmp_db.get_message(mid)
    assert row["de_client"] == "Vielen Dank Mfg"        # текст всё равно очищен
    assert row["ru_client"] == "старый мусорный перевод"  # перевод не тронут


def test_reclean_clean_rows_untouched(tmp_db):
    from modules import incoming
    mid = _add_incoming(tmp_db, "Hallo, ist das noch verfügbar?")
    with patch.object(incoming.claude, "detect_and_translate_to_ru") as mock_tr:
        summary = incoming.reclean_incoming_bodies()
    row = tmp_db.get_message(mid)
    assert row["de_client"] == "Hallo, ist das noch verfügbar?"
    mock_tr.assert_not_called()
    assert "fixed=0" in summary
