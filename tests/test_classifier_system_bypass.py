"""A KZ system email landing in an already-active thread must still be gated by
the inquiry classifier — is_system_message_body detects it (Bug 7)."""
from __future__ import annotations

from modules import parser


def test_detects_automatic_email():
    assert parser.is_system_message_body(
        "Diese E-Mail wurde automatisch generiert. Bitte antworten Sie nicht darauf."
    ) is True


def test_detects_no_reply_note():
    assert parser.is_system_message_body(
        "Bitte antworten Sie nicht auf diese E-Mail."
    ) is True


def test_normal_buyer_message_is_not_system():
    assert parser.is_system_message_body(
        "Hallo, ist der Sitz noch verfügbar? Wäre 200 Euro möglich?"
    ) is False


def test_empty_is_not_system():
    assert parser.is_system_message_body("") is False
