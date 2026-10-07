"""Conversation keys from threading headers."""

from __future__ import annotations

import unittest

from mailbox_viewer.mail_parser import parse_message
from mailbox_viewer.threads import ancestor_ids, conversation_key

from .fakes import build_message


class ThreadKeyTests(unittest.TestCase):
    def test_new_mail_is_its_own_root(self) -> None:
        mail = parse_message(1, build_message(message_id="<a@x>"))
        self.assertEqual(conversation_key(mail), "<a@x>")
        self.assertEqual(ancestor_ids(mail), [])

    def test_references_first_entry_wins(self) -> None:
        mail = parse_message(1, build_message(message_id="<c@x>", in_reply_to="<b@x>", references="<a@x> <b@x>"))
        self.assertEqual(conversation_key(mail), "<a@x>")
        self.assertEqual(ancestor_ids(mail), ["<a@x>", "<b@x>"])

    def test_in_reply_to_used_when_references_missing(self) -> None:
        mail = parse_message(1, build_message(message_id="<b@x>", in_reply_to="<a@x>"))
        self.assertEqual(conversation_key(mail), "<a@x>")
        self.assertEqual(ancestor_ids(mail), ["<a@x>"])


if __name__ == "__main__":
    unittest.main()
