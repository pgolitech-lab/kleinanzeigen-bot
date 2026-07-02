"""TestClient тесты для POST /api/ma/threads/{thread_id}/compose[-preview]."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_init_data, TEST_BOT_TOKEN


TEST_USER = {"id": 999, "first_name": "Pg", "username": "pgtest"}


def _row(**fields):
    row = MagicMock()
    row.__getitem__.side_effect = fields.__getitem__
    row.keys.return_value = list(fields.keys())
    return row


@pytest.fixture
def client():
    with patch("modules.tg_init_data.config") as mc, \
         patch("web.api_ma.db") as mdb, \
         patch("web.api_ma.telegram_bot") as mtb, \
         patch("web.api_ma.operator_lock") as mol, \
         patch("web.api_ma.scheduler") as msched, \
         patch("web.api_ma.claude") as mclaude:
        mc.telegram_bot_token.return_value = TEST_BOT_TOKEN
        mc.telegram_authorized_ids.return_value = {"999"}
        mdb.thread_history.return_value = [
            _row(id=42, direction="in", gmail_thread_id="abc", status="sent",
                 client_lang="de", ad_title="Sitzbank"),
        ]
        msched.send_manual_compose.return_value = {"kind": "ok", "message_id": 99}
        # Реалистичное поведение detect_lang_override по умолчанию: директивы нет.
        mclaude.detect_lang_override.side_effect = lambda text: (None, text)
        mclaude.is_empty_directive.return_value = False
        # db.get_conn() context manager returning a MagicMock conn whose
        # execute(...).fetchall() yields an empty list (no pending drafts to skip).
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value.execute.return_value.fetchall.return_value = []
        mdb.get_conn.return_value = conn_cm
        from web.app import app
        yield TestClient(app), mdb, msched, mclaude


def _compose_body(**overrides):
    body = {"text": "Здравствуйте, у меня вопрос...", "final_text": "Hallo, ich habe eine Frage...",
            "target_lang": "de"}
    body.update(overrides)
    return body


def test_compose_success(client):
    c, mdb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json=_compose_body(),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["closed_drafts"] == []
    # /compose больше НЕ переводит сам — отправляет ровно final_text, подтверждённый на preview-шаге.
    msched.send_manual_compose.assert_called_once_with(
        42, "Здравствуйте, у меня вопрос...", "Hallo, ich habe eine Frage...", "de",
    )


def test_compose_closes_pending_drafts(client):
    """После успешного compose — pending in-rows треда → status='skipped'."""
    c, mdb, msched, mclaude = client
    # Simulate two pending drafts to be closed
    conn_cm = MagicMock()
    pending_rows = [_row(id=1965), _row(id=1970)]
    conn_cm.__enter__.return_value.execute.return_value.fetchall.return_value = pending_rows
    mdb.get_conn.return_value = conn_cm

    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json=_compose_body(text="manual reply", final_text="manuelle Antwort"),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert set(body["closed_drafts"]) == {1965, 1970}


def test_compose_404_thread_missing(client):
    c, mdb, msched, mclaude = client
    mdb.thread_history.return_value = []
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/nonexistent/compose",
                 json=_compose_body(),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 404


def test_compose_empty_text_422(client):
    c, mdb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json=_compose_body(text=""),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_compose_missing_final_text_422(client):
    """Без подтверждённого final_text отправка невозможна — обязателен шаг /compose-preview."""
    c, mdb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json={"text": "test"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_compose_empty_final_text_422(client):
    c, mdb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json=_compose_body(final_text=""),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 422


def test_compose_smtp_failure_500(client):
    c, mdb, msched, mclaude = client
    msched.send_manual_compose.return_value = {"kind": "error", "message": "SMTP timeout"}
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose",
                 json=_compose_body(),
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 500
    assert "SMTP timeout" in res.json()["detail"]


def test_compose_requires_auth(client):
    c, mdb, msched, mclaude = client
    res = c.post("/api/ma/threads/abc/compose", json=_compose_body())
    assert res.status_code == 422


# ============================================================
# /compose-preview — покрывает баг 2026-07-02: оператор написал ответ сразу на
# немецком (без директивы «на немецком: »), backend слепо считал текст русским
# и получил на выходе русский текст, который ушёл клиенту.
# ============================================================

def test_compose_preview_translates_russian_input(client):
    c, mdb, msched, mclaude = client
    mclaude.translate_only.side_effect = [
        {"translation": "Hallo, das Auto ist verfügbar."},  # ru -> de
        {"translation": "Здравствуйте, машина доступна."},  # de -> ru back-translate
    ]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose-preview",
                 json={"text": "Здравствуйте, машина доступна."},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["target_lang"] == "de"
    assert body["translated"] == "Hallo, das Auto ist verfügbar."
    assert "note" not in body
    assert mclaude.translate_only.call_count == 2


def test_compose_preview_non_cyrillic_input_skips_translation(client):
    """Корневой фикс: текст без кириллицы не отдаём переводчику как «русский»."""
    c, mdb, msched, mclaude = client
    init = make_init_data(TEST_USER)
    german_text = "Guten Tag, das Auto ist noch verfügbar. Mit freundlichen Gruessen"
    mclaude.translate_only.return_value = {"translation": "Добрый день, машина ещё в наличии."}
    res = c.post("/api/ma/threads/abc/compose-preview",
                 json={"text": german_text},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["target_lang"] == "de"
    # Текст уходит как есть — НЕ "переведён" через forward-перевод (и уж точно не
    # превращён в русский), только back-translate для сверки смысла оператором.
    assert body["translated"] == german_text
    assert body["back_ru"] == "Добрый день, машина ещё в наличии."
    assert "note" in body
    mclaude.translate_only.assert_called_once_with(
        german_text, target_lang="ru", source_lang="de",
    )


def test_compose_preview_empty_directive_returns_400(client):
    """«на немецком:» без текста после двоеточия — понятная ошибка, а не запрос к LLM."""
    c, mdb, msched, mclaude = client
    mclaude.is_empty_directive.return_value = True
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose-preview",
                 json={"text": "на немецком:"},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 400
    mclaude.translate_only.assert_not_called()


def test_compose_preview_respects_language_override_directive(client):
    c, mdb, msched, mclaude = client
    mclaude.detect_lang_override.side_effect = None
    mclaude.detect_lang_override.return_value = ("en", "The car is still available.")
    mclaude.translate_only.side_effect = [
        {"translation": "The car is still available."},
        {"translation": "Машина ещё доступна."},
    ]
    init = make_init_data(TEST_USER)
    res = c.post("/api/ma/threads/abc/compose-preview",
                 json={"text": "на английском: The car is still available."},
                 headers={"X-Telegram-Init-Data": init})
    assert res.status_code == 200
    body = res.json()
    assert body["target_lang"] == "en"
    assert body["ru_text"] == "The car is still available."
