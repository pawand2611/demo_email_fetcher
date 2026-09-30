"""Test doubles: an in-memory IMAP "server" and a client that speaks to it."""

from __future__ import annotations

from email.message import EmailMessage


def build_message(
    *,
    subject: str = "Hello",
    sender: str = "Someone <someone@example.com>",
    to: str = "you@example.com",
    cc: str | None = None,
    message_id: str | None = "<default@example.com>",
    in_reply_to: str | None = None,
    references: str | None = None,
    date: str | None = "Mon, 01 Sep 2026 09:30:00 +0000",
    text: str = "Body text.",
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    """Return raw RFC 822 bytes. ``attachments`` is a list of (filename, mime, data)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if date:
        msg["Date"] = date
    msg.set_content(text)
    for filename, mime, data in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


class FakeMailServer:
    """Holds messages by UID and records what the client asked for."""

    def __init__(self, uidvalidity: int = 1) -> None:
        self.uidvalidity = uidvalidity
        self.messages: dict[int, bytes] = {}
        self.fail_fetch: dict[int, Exception] = {}
        self.search_calls: list[int | None] = []
        self.fetch_calls: list[int] = []

    def add(self, uid: int, raw: bytes) -> None:
        self.messages[uid] = raw


class FakeMailClient:
    """Drop-in for :class:`mailbox_viewer.mail_client.MailClient`."""

    def __init__(self, server: FakeMailServer) -> None:
        self._server = server
        self.uidvalidity = server.uidvalidity
        self.message_count = len(server.messages)

    def __enter__(self) -> "FakeMailClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def search_uids(self, after_uid: int | None = None) -> list[int]:
        self._server.search_calls.append(after_uid)
        uids = sorted(self._server.messages)
        if after_uid:
            uids = [uid for uid in uids if uid > after_uid]
        return uids

    def fetch_message(self, uid: int):
        self._server.fetch_calls.append(uid)
        if uid in self._server.fail_fetch:
            raise self._server.fail_fetch[uid]
        return self._server.messages[uid], None

    def find_uid_by_message_id(self, message_id: str) -> int | None:
        import email
        from email import policy

        for uid, raw in sorted(self._server.messages.items()):
            try:
                if str(email.message_from_bytes(raw, policy=policy.default).get("Message-ID", "")).strip() == message_id:
                    return uid
            except Exception:
                continue
        return None
