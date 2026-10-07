"""M0 - connect to the mailbox, fetch the newest messages and print them.

No UI and no database yet. This proves the IMAP connection and the parser
against real mail before anything is persisted.

Usage:
    python fetch_mail.py               # newest 10 messages
    python fetch_mail.py --limit 25    # newest 25
    python fetch_mail.py --show-body   # include a preview of the text body
"""

from __future__ import annotations

import argparse
import sys

from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.mail_client import MailClient, MailClientError, MailConnectionLost
from mailbox_viewer.mail_parser import ParsedEmail, parse_message
from mailbox_viewer.timeutil import fmt_ist


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _force_utf8_console()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    printed = failed = 0
    try:
        with MailClient(settings) as client:
            print(
                f"Connected to {settings.imap_host} as {settings.imap_user}, "
                f"folder {settings.imap_folder!r}, UIDVALIDITY={client.uidvalidity}, "
                f"{client.message_count} messages"
            )
            uids = client.search_uids()
            newest = list(reversed(uids[-args.limit :]))
            print(f"Fetching the newest {len(newest)} of {len(uids)} messages\n")

            for uid in newest:
                try:
                    raw, internal_date = client.fetch_message(uid)
                    parsed = parse_message(uid, raw, internal_date)
                except MailConnectionLost:
                    raise
                except Exception as exc:  # one bad message must not end the run
                    failed += 1
                    print(f"[UID {uid}] FAILED: {type(exc).__name__}: {exc}\n")
                    continue
                _print_email(parsed, show_body=args.show_body)
                printed += 1
    except MailClientError as exc:
        print(f"Mailbox error: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {printed} printed, {failed} failed")
    return 0


def _force_utf8_console() -> None:
    """Subjects contain emoji; the default Windows console codec (cp1252) cannot print them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=10, help="how many of the newest messages to print")
    parser.add_argument("--show-body", action="store_true", help="print a preview of the text body")
    return parser.parse_args(argv)


def _print_email(mail: ParsedEmail, *, show_body: bool) -> None:
    received = fmt_ist(mail.received_at) if mail.received_at else "unknown date"
    sender = f"{mail.sender_name} <{mail.sender_email}>" if mail.sender_name else mail.sender_email
    print(f"[UID {mail.imap_uid}] {received} | From: {sender or '(none)'}")
    print(f"    Subject: {mail.subject or '(no subject)'}")
    print(f"    Message-ID: {mail.message_id}{' (generated)' if mail.message_id_generated else ''}")
    if mail.attachments:
        print(f"    Attachments ({len(mail.attachments)}):")
        for att in mail.attachments:
            print(f"      - {att.filename} ({att.content_type}, {_human_size(att.size_bytes)})")
    else:
        print("    Attachments: none")
    if show_body:
        preview = " ".join(mail.body_text.split())[:300] or "(no text body)"
        print(f"    Body: {preview}")
    print()


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


if __name__ == "__main__":
    sys.exit(main())
