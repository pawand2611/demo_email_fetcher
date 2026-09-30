"""Parser tests that need no mailbox: synthetic messages, including broken ones.

Run with:  python -m unittest -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from email.message import EmailMessage

from mailbox_viewer.mail_parser import parse_message


def _build(
    *,
    subject: str = "Your August statement",
    sender: str = "Acme Bank <statements@acme.example>",
    message_id: str | None = "<abc123@acme.example>",
    date: str | None = "Mon, 01 Sep 2026 09:30:00 +0530",
    text: str = "Please find your statement attached.",
    html: str | None = "<p>Please find your <b>statement</b> attached.</p>",
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "you@example.com"
    if message_id:
        msg["Message-ID"] = message_id
    if date:
        msg["Date"] = date
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


class ParseMessageTests(unittest.TestCase):
    def test_metadata_and_bodies(self) -> None:
        parsed = parse_message(42, _build().as_bytes())

        self.assertEqual(parsed.imap_uid, 42)
        self.assertEqual(parsed.message_id, "<abc123@acme.example>")
        self.assertFalse(parsed.message_id_generated)
        self.assertEqual(parsed.subject, "Your August statement")
        self.assertEqual(parsed.sender_name, "Acme Bank")
        self.assertEqual(parsed.sender_email, "statements@acme.example")
        self.assertEqual(parsed.received_at, datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc))
        self.assertIn("statement attached", parsed.body_text)
        self.assertIn("<b>statement</b>", parsed.body_html)
        self.assertFalse(parsed.has_attachments)

    def test_attachments_are_extracted_with_hash_and_size(self) -> None:
        msg = _build()
        pdf = b"%PDF-1.4 fake statement bytes"
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="statement_aug.pdf")
        msg.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="rows.csv")

        parsed = parse_message(1, msg.as_bytes())

        self.assertEqual([a.filename for a in parsed.attachments], ["statement_aug.pdf", "rows.csv"])
        first = parsed.attachments[0]
        self.assertEqual(first.content_type, "application/pdf")
        self.assertEqual(first.size_bytes, len(pdf))
        self.assertEqual(first.data, pdf)
        self.assertEqual(len(first.sha256), 64)
        self.assertTrue(parsed.has_attachments)

    def test_missing_message_id_gets_deterministic_substitute(self) -> None:
        raw = _build(message_id=None).as_bytes()

        first = parse_message(1, raw)
        second = parse_message(1, raw)

        self.assertTrue(first.message_id_generated)
        self.assertTrue(first.message_id.startswith("generated-"))
        self.assertEqual(first.message_id, second.message_id)

    def test_bad_date_falls_back_to_internaldate(self) -> None:
        raw = _build(date="not a date at all").as_bytes()
        server_time = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)

        parsed = parse_message(1, raw, internal_date=server_time)

        self.assertEqual(parsed.received_at, server_time)

    def test_missing_date_and_no_internaldate_is_none(self) -> None:
        parsed = parse_message(1, _build(date=None).as_bytes())
        self.assertIsNone(parsed.received_at)

    def test_inline_signature_image_is_not_an_attachment(self) -> None:
        msg = _build(html=None)
        msg.add_related(b"\x89PNG fake", maintype="image", subtype="png", filename="logo.png", cid="<logo@x>")
        msg.get_payload()[-1].replace_header("Content-Disposition", 'inline; filename="logo.png"')

        parsed = parse_message(1, msg.as_bytes())

        self.assertEqual(parsed.attachments, [])

    def test_hostile_filename_is_sanitised(self) -> None:
        msg = _build()
        msg.add_attachment(b"x", maintype="application", subtype="octet-stream", filename="..\\..\\evil<>.exe")

        parsed = parse_message(1, msg.as_bytes())

        self.assertEqual(parsed.attachments[0].filename, "evil__.exe")

    def test_participants_are_split_by_role(self) -> None:
        msg = _build()
        msg.replace_header("To", "Me <me@example.com>, other@example.com")
        msg["Cc"] = "Partner <partner@example.com>"

        parsed = parse_message(1, msg.as_bytes())

        self.assertEqual(
            [(p.role, p.name, p.address) for p in parsed.participants],
            [
                ("from", "Acme Bank", "statements@acme.example"),
                ("to", "Me", "me@example.com"),
                ("to", "", "other@example.com"),
                ("cc", "Partner", "partner@example.com"),
            ],
        )

    def test_threading_headers_are_parsed(self) -> None:
        msg = _build(message_id="<reply2@x>")
        msg["In-Reply-To"] = "<reply1@x>"
        msg["References"] = "<root@x> <reply1@x>"

        parsed = parse_message(1, msg.as_bytes())

        self.assertEqual(parsed.in_reply_to, "<reply1@x>")
        self.assertEqual(parsed.references, ["<root@x>", "<reply1@x>"])
        self.assertEqual(parsed.references_header, "<root@x> <reply1@x>")

        plain = parse_message(1, _build().as_bytes())
        self.assertIsNone(plain.in_reply_to)
        self.assertEqual(plain.references, [])
        self.assertIsNone(plain.references_header)

    def test_list_headers_mark_bulk_mail(self) -> None:
        plain = _build()
        self.assertFalse(parse_message(1, plain.as_bytes()).is_bulk)

        newsletter = _build()
        newsletter["List-Unsubscribe"] = "<mailto:unsub@acme.example>"
        self.assertTrue(parse_message(1, newsletter.as_bytes()).is_bulk)

        bulk = _build()
        bulk["Precedence"] = "bulk"
        self.assertTrue(parse_message(1, bulk.as_bytes()).is_bulk)

    def test_garbage_bytes_do_not_raise(self) -> None:
        parsed = parse_message(7, b"\xff\xfe this is not an email at all \x00\x01")

        self.assertEqual(parsed.imap_uid, 7)
        self.assertTrue(parsed.message_id_generated)
        self.assertEqual(parsed.attachments, [])


if __name__ == "__main__":
    unittest.main()
