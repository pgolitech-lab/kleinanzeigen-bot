"""Адрес туннеля для MA: файл → параметр api= в web_app-ссылках бота."""
from unittest.mock import patch

from modules import telegram_bot as tb

GOOD = "https://abc-def-123.trycloudflare.com"


def _with_file(tmp_path, content):
    f = tmp_path / ("tunnel_url" if content is not None else "missing")
    if content is not None:
        f.write_text(content)
    return patch.object(tb, "TUNNEL_URL_FILE", f)


def test_tunnel_url_valid(tmp_path):
    with _with_file(tmp_path, GOOD + "\n"):
        assert tb.tunnel_url() == GOOD


def test_tunnel_url_broken(tmp_path):
    for bad in ("http://abc.trycloudflare.com", "https://evil.com", GOOD + "/x", ""):
        with _with_file(tmp_path, bad):
            assert tb.tunnel_url() is None


def test_tunnel_url_missing(tmp_path):
    with _with_file(tmp_path, None):
        assert tb.tunnel_url() is None


def test_deep_link_with_and_without_tunnel(tmp_path):
    with _with_file(tmp_path, GOOD):
        link = tb._ma_deep_link("dashboard")
    assert link.endswith("?tgWebAppStartParam=dashboard&api=https%3A%2F%2Fabc-def-123.trycloudflare.com")
    with _with_file(tmp_path, None):
        assert tb._ma_deep_link("dashboard").endswith("?tgWebAppStartParam=dashboard")


def test_set_menu_button_counts_successes(tmp_path):
    calls = []

    def post(method, payload):
        calls.append(payload["chat_id"])
        if payload["chat_id"] == 2:
            raise RuntimeError("chat not found")
        return {}

    with _with_file(tmp_path, GOOD), patch.object(tb, "_http_post_single", post), \
         patch.object(tb, "config") as mc:
        mc.telegram_operator_dm_ids.return_value = ["1", "2", "3"]
        assert tb.set_menu_button() == 2
    assert calls == [1, 2, 3]


def test_env_var_overrides_file(monkeypatch, tmp_path):
    import importlib
    f = tmp_path / "t"
    f.write_text(GOOD)
    monkeypatch.setenv("KAB_TUNNEL_URL_FILE", str(f))
    mod = importlib.reload(tb)
    try:
        assert mod.tunnel_url() == GOOD
    finally:
        monkeypatch.delenv("KAB_TUNNEL_URL_FILE")
        importlib.reload(tb)
