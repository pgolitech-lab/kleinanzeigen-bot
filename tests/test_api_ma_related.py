"""Связанные треды: точно тот же клиент (email) отдельно от «возможно тот же покупатель»."""
from unittest.mock import MagicMock, patch


def _row(tid, maybe):
    d = {"gmail_thread_id": tid, "ad_title": "T", "ad_price": "1€", "sent_at": None,
         "created_at": "2026-10-01", "maybe_same": maybe}
    r = MagicMock()
    r.__getitem__.side_effect = d.__getitem__
    r.keys.return_value = d.keys()
    return r


def test_related_splits_exact_and_maybe():
    from web import api_ma
    seen = {}

    def new(buyer_email, exclude_thread_id=None, limit=10, *, display_name=None,
            account_id=None, include_maybe=False):
        seen.update(display_name=display_name, account_id=account_id, include_maybe=include_maybe)
        return [_row("a", 0), _row("b", 1)]

    with patch.object(api_ma.db, "find_related_inquiries", new):
        res = api_ma._related("x@relay", "Hans", exclude_thread_id="t0", account_id=3)
    assert [m["thread_id"] for m in res["matches"]] == ["a"]
    assert [m["thread_id"] for m in res["maybe_same"]] == ["b"]
    assert seen == {"display_name": "Hans", "account_id": 3, "include_maybe": True}


def test_related_old_db_signature_falls_back():
    from web import api_ma

    def old(buyer_email, exclude_thread_id=None, limit=10):
        return [_row("a", 0)]

    with patch.object(api_ma.db, "find_related_inquiries", old):
        res = api_ma._related("x@relay", "Hans", exclude_thread_id="t0")
    assert len(res["matches"]) == 1 and res["maybe_same"] == []


def test_related_without_email_is_empty():
    from web import api_ma
    assert api_ma._related(None, "Hans") == {"buyer_display_name": "Hans", "matches": [], "maybe_same": []}


def test_regenerate_refused_for_sent_message():
    """Отправленное сообщение не превращается обратно в черновик."""
    import pytest
    from fastapi import HTTPException
    from web import api_ma
    for st in ("sent", "sent_debug", "skipped_sold"):
        with pytest.raises(HTTPException) as ei:
            api_ma._ensure_draft_editable({"status": st})
        assert ei.value.status_code == 409
    api_ma._ensure_draft_editable({"status": "pending"})


def test_pipeline_single_flight():
    """Параллельные /pipeline и /dashboard при пустом кэше делают один SQL."""
    import threading
    from web import api_ma
    api_ma.invalidate_pipeline_cache()
    gate, calls = threading.Event(), []

    def slow():
        calls.append(1)
        gate.wait(5)
        return ["row"]

    out = []
    with patch.object(api_ma.db, "pipeline_threads", slow):
        ts = [threading.Thread(target=lambda: out.append(api_ma._pipeline_rows())) for _ in range(5)]
        for t in ts:
            t.start()
        threading.Event().wait(0.2)
        gate.set()
        for t in ts:
            t.join()
    assert calls == [1] and out == [["row"]] * 5
    api_ma.invalidate_pipeline_cache()
