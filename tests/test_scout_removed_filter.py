"""Снятые объявления не должны попадать в списки и счётчики.

active обновляется только при полном прогоне, поэтому «снято» считаем по
факту отсутствия в выдаче (last_seen_at), а не по флагу (инцидент 2026-07-21:
66 из 126 «активных» не появлялись в выдаче больше недели).
"""
from __future__ import annotations
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from web.api_ma import _scout_listing_dict, _days_unseen


def _row(**f):
    r = MagicMock()
    r.__getitem__.side_effect = f.__getitem__
    r.keys.return_value = list(f.keys())
    return r


def _listing(last_seen: str, active: int = 1):
    return _row(
        ad_id="123", kind="part", title="Sitzbank", url="https://x/123",
        price_eur=500.0, negotiable=0, plz="50667", city="Köln",
        bundesland="Nordrhein-Westfalen", year=2020, mileage_km=None,
        fuel=None, gearbox=None, model_family=None, part_type="bench",
        condition="gebraucht", posted_raw="Heute", last_seen_at=last_seen, active=active,
    )


def _cutoff(days: int = 7) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def test_recent_listing_not_removed():
    fresh = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    d = _scout_listing_dict(_listing(fresh), _cutoff())
    assert d["removed"] is False
    assert d["days_unseen"] == 0


def test_long_unseen_listing_marked_removed():
    """Активный флаг, но месяц не в выдаче — снято."""
    old = (datetime.utcnow() - timedelta(days=30)).isoformat()
    d = _scout_listing_dict(_listing(old, active=1), _cutoff())
    assert d["removed"] is True
    assert d["days_unseen"] == 30


def test_inactive_flag_marks_removed_even_if_recently_seen():
    fresh = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    d = _scout_listing_dict(_listing(fresh, active=0), _cutoff())
    assert d["removed"] is True


def test_no_cutoff_keeps_active_listing_visible():
    old = (datetime.utcnow() - timedelta(days=30)).isoformat()
    d = _scout_listing_dict(_listing(old, active=1), None)
    assert d["removed"] is False


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("не дата", None),
])
def test_days_unseen_handles_garbage(raw, expected):
    assert _days_unseen(raw) is expected


def test_days_unseen_tolerates_timezone_suffix():
    ts = (datetime.utcnow() - timedelta(days=3)).isoformat() + "+02:00"
    assert _days_unseen(ts) == 3
