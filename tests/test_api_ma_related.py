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
    with patch.object(api_ma, "db") as mdb:
        mdb.find_related_inquiries.return_value = [_row("a", 0), _row("b", 1)]
        res = api_ma._related("x@relay", "Hans", exclude_thread_id="t0", account_id=3)
    assert [m["thread_id"] for m in res["matches"]] == ["a"]
    assert [m["thread_id"] for m in res["maybe_same"]] == ["b"]
    kw = mdb.find_related_inquiries.call_args.kwargs
    assert kw["include_maybe"] is True and kw["account_id"] == 3 and kw["display_name"] == "Hans"


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
