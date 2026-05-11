"""Unit tests for telegram_bot.refresh_pipeline_for_active_chats.

Mocks _http_post_single + _format_pipeline_messages, verifies:
  - no-op when no active chats
  - edits each tracked pipeline msg via editMessageText
  - grow: appends new sendMessage on count increase
  - shrink: deletes excess on count decrease
  - 'not modified' errors are swallowed
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

from modules import telegram_bot


def _make_kb_dict(label: str) -> MagicMock:
    """Mock InlineKeyboardMarkup with to_dict()."""
    kb = MagicMock()
    kb.to_dict.return_value = {"inline_keyboard": [[{"text": label, "web_app": {"url": "x"}}]]}
    return kb


def test_refresh_noop_when_no_active_chats():
    telegram_bot._CHAT_PIPELINE_MSGS.clear()
    with patch.object(telegram_bot, "_http_post_single") as m:
        telegram_bot.refresh_pipeline_for_active_chats()
    assert m.call_count == 0


def test_refresh_edits_same_count():
    telegram_bot._CHAT_PIPELINE_MSGS.clear()
    telegram_bot._CHAT_PIPELINE_MSGS[111] = [10, 11, 12]

    fake_messages = [
        ("header", None),
        ("card1", _make_kb_dict("btn1")),
        ("card2", _make_kb_dict("btn2")),
    ]
    with patch.object(telegram_bot, "_format_pipeline_messages", return_value=fake_messages), \
         patch.object(telegram_bot, "_http_post_single") as m:
        telegram_bot.refresh_pipeline_for_active_chats()

    edit_calls = [c for c in m.call_args_list if c.args[0] == "editMessageText"]
    assert len(edit_calls) == 3
    assert all(c.args[1]["chat_id"] == 111 for c in edit_calls)
    assert [c.args[1]["message_id"] for c in edit_calls] == [10, 11, 12]
    assert "reply_markup" in edit_calls[1].args[1]  # cards have kb
    assert "reply_markup" not in edit_calls[0].args[1]  # header skip

    assert telegram_bot._CHAT_PIPELINE_MSGS[111] == [10, 11, 12]


def test_refresh_grows_on_more_cards():
    telegram_bot._CHAT_PIPELINE_MSGS.clear()
    telegram_bot._CHAT_PIPELINE_MSGS[222] = [20, 21]  # 1 header + 1 card

    fake_messages = [
        ("header", None),
        ("card1", _make_kb_dict("b1")),
        ("card2_new", _make_kb_dict("b2")),
    ]

    def fake_post(method, payload):
        if method == "sendMessage":
            return {"message_id": 99}
        return {}

    with patch.object(telegram_bot, "_format_pipeline_messages", return_value=fake_messages), \
         patch.object(telegram_bot, "_http_post_single", side_effect=fake_post) as m:
        telegram_bot.refresh_pipeline_for_active_chats()

    methods = [c.args[0] for c in m.call_args_list]
    assert methods.count("editMessageText") == 2
    assert methods.count("sendMessage") == 1

    assert telegram_bot._CHAT_PIPELINE_MSGS[222] == [20, 21, 99]


def test_refresh_shrinks_on_fewer_cards():
    telegram_bot._CHAT_PIPELINE_MSGS.clear()
    telegram_bot._CHAT_PIPELINE_MSGS[333] = [30, 31, 32, 33]

    fake_messages = [("header", None), ("only_card", _make_kb_dict("x"))]

    with patch.object(telegram_bot, "_format_pipeline_messages", return_value=fake_messages), \
         patch.object(telegram_bot, "_http_post_single") as m:
        telegram_bot.refresh_pipeline_for_active_chats()

    methods = [c.args[0] for c in m.call_args_list]
    assert methods.count("editMessageText") == 2
    assert methods.count("deleteMessage") == 2

    del_targets = sorted(c.args[1]["message_id"] for c in m.call_args_list if c.args[0] == "deleteMessage")
    assert del_targets == [32, 33]

    assert telegram_bot._CHAT_PIPELINE_MSGS[333] == [30, 31]


def test_refresh_swallows_not_modified():
    telegram_bot._CHAT_PIPELINE_MSGS.clear()
    telegram_bot._CHAT_PIPELINE_MSGS[444] = [40]

    def fake_post(method, payload):
        raise RuntimeError("Telegram API ошибка: message is not modified")

    fake_messages = [("header", None)]
    with patch.object(telegram_bot, "_format_pipeline_messages", return_value=fake_messages), \
         patch.object(telegram_bot, "_http_post_single", side_effect=fake_post):
        # Should not raise
        telegram_bot.refresh_pipeline_for_active_chats()

    assert telegram_bot._CHAT_PIPELINE_MSGS[444] == [40]
