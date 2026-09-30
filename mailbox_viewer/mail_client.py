"""Thin IMAP client: connect, list message UIDs, fetch raw messages.

This module knows nothing about parsing or the database. It hands back raw
RFC 822 bytes and lets :mod:`mail_parser` turn them into something useful.

Why IMAP: it is an open standard supported by Gmail and Outlook.com with an
app password, it lives in the Python standard library (``imaplib``), and it
gives every message a stable, monotonically increasing UID per folder. That
UID is exactly what an incremental sync needs to ask "what is new since last
time?" with a single ``UID SEARCH`` command.
"""

from __future__ import annotations

import imaplib
import re
from datetime import datetime
from types import TracebackType

from .config import Settings

_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')
_INTERNALDATE_FORMAT = "%d-%b-%Y %H:%M:%S %z"


class MailClientError(RuntimeError):
    """Any failure talking to the IMAP server."""


class MailConnectionLost(MailClientError):
    """The connection dropped mid-session; the caller should stop the run."""


class MailClient:
    """Context-managed IMAP session bound to one folder, opened read-only.

    Read-only matters: we never want a cache sync to flip messages to "read"
    or otherwise mutate the mailbox.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: imaplib.IMAP4_SSL | None = None
        self.uidvalidity: int | None = None
        self.message_count: int = 0

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> "MailClient":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        settings = self._settings
        try:
            conn = imaplib.IMAP4_SSL(
                settings.imap_host, settings.imap_port, timeout=settings.imap_timeout
            )
            conn.login(settings.imap_user, settings.imap_password)
            status, data = conn.select(_quote_mailbox(settings.imap_folder), readonly=True)
        except imaplib.IMAP4.error as exc:
            raise MailClientError(f"IMAP error during connect/login: {exc}") from exc
        except OSError as exc:  # DNS, socket, TLS failures
            raise MailClientError(
                f"Could not reach {settings.imap_host}:{settings.imap_port}: {exc}"
            ) from exc

        if status != "OK":
            raise MailClientError(
                f"Could not select folder {settings.imap_folder!r}: {_first(data)}"
            )

        self._conn = conn
        self.message_count = int(_first(data) or 0)
        self.uidvalidity = self._read_uidvalidity()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            self._conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        self._conn = None

    # -- queries -------------------------------------------------------------

    def search_uids(self, after_uid: int | None = None) -> list[int]:
        """Return UIDs in the folder, ascending. With ``after_uid``, only newer ones.

        IMAP's ``n:*`` range always includes the highest UID even when ``n`` is
        already past it, so we filter client-side as well.
        """
        criteria = f"UID {after_uid + 1}:*" if after_uid else "ALL"
        status, data = self._command("SEARCH", None, criteria)
        if status != "OK":
            raise MailClientError(f"UID SEARCH failed: {_first(data)}")

        raw = data[0] if data and data[0] else b""
        uids = sorted(int(token) for token in raw.split())
        if after_uid:
            uids = [uid for uid in uids if uid > after_uid]
        return uids

    def fetch_message(self, uid: int) -> tuple[bytes, datetime | None]:
        """Fetch one message as raw RFC 822 bytes plus the server's INTERNALDATE.

        ``BODY.PEEK[]`` is used instead of ``RFC822`` so that even on a
        writable connection the ``\\Seen`` flag is never set.
        """
        status, data = self._command("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
        if status != "OK":
            raise MailClientError(f"UID FETCH {uid} failed: {_first(data)}")

        raw_body: bytes | None = None
        metadata = b""
        for item in data:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                metadata += item[0]
                if raw_body is None:
                    raw_body = item[1]
            elif isinstance(item, bytes):
                metadata += item

        if raw_body is None:
            raise MailClientError(f"UID FETCH {uid} returned no message body")
        return raw_body, _parse_internaldate(metadata)

    # -- internals -----------------------------------------------------------

    def _command(self, name: str, *args: str | None):
        if self._conn is None:
            raise MailClientError("Not connected; call connect() first")
        try:
            return self._conn.uid(name, *args)
        except imaplib.IMAP4.abort as exc:
            raise MailConnectionLost(f"IMAP connection lost during {name}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailClientError(f"IMAP {name} failed: {exc}") from exc
        except OSError as exc:
            raise MailConnectionLost(f"Socket error during {name}: {exc}") from exc

    def _read_uidvalidity(self) -> int | None:
        assert self._conn is not None
        _, values = self._conn.response("UIDVALIDITY")
        if values and values[0]:
            try:
                return int(values[0])
            except ValueError:
                return None
        return None


def _quote_mailbox(name: str) -> str:
    """Quote folder names with spaces, e.g. ``[Gmail]/All Mail``."""
    if " " in name and not (name.startswith('"') and name.endswith('"')):
        return f'"{name}"'
    return name


def _first(data) -> str:
    if not data:
        return ""
    head = data[0]
    return head.decode("utf-8", errors="replace") if isinstance(head, bytes) else str(head)


def _parse_internaldate(metadata: bytes) -> datetime | None:
    match = _INTERNALDATE_RE.search(metadata)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode("ascii"), _INTERNALDATE_FORMAT)
    except (ValueError, UnicodeDecodeError):
        return None
