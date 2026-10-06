"""Turn raw RFC 822 bytes into a :class:`ParsedEmail`.

Every step that touches untrusted message content is wrapped so that one
malformed header, charset or MIME part degrades to an empty value instead of
aborting the whole sync run.
"""

from __future__ import annotations

import email
import email.errors
import hashlib
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

# Errors the stdlib email package raises on malformed input. Anything else is
# a genuine bug and should surface.
_SAFE_ERRORS = (
    ValueError,  # includes UnicodeError
    LookupError,  # unknown charset
    TypeError,
    AttributeError,
    email.errors.MessageError,
)

_MAX_FILENAME_LEN = 200
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


@dataclass
class ParsedAttachment:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Participant:
    role: str  # from | to | cc | bcc
    name: str
    address: str


@dataclass
class ParsedEmail:
    imap_uid: int
    message_id: str
    message_id_generated: bool
    subject: str
    sender_name: str
    sender_email: str
    participants: list[Participant]
    in_reply_to: str | None
    references: list[str]
    received_at: datetime | None
    body_text: str
    body_html: str
    attachments: list[ParsedAttachment]
    raw_size: int
    is_bulk: bool = False  # sent to a list: List-Unsubscribe / List-Id / Precedence: bulk

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @property
    def references_header(self) -> str | None:
        return " ".join(self.references) if self.references else None


def parse_message(
    imap_uid: int, raw: bytes, internal_date: datetime | None = None
) -> ParsedEmail:
    """Parse one message. ``internal_date`` is the IMAP server timestamp,
    used when the ``Date`` header is missing or unparseable."""
    msg = email.message_from_bytes(raw, policy=policy.default)

    subject = _header(msg, "Subject")
    sender_name, sender_email = _parse_address(_header(msg, "From"))
    body_text, body_html = _extract_bodies(msg)

    message_id = _header(msg, "Message-ID")
    generated = not message_id
    if generated:
        message_id = _generated_message_id(raw)

    return ParsedEmail(
        imap_uid=imap_uid,
        message_id=message_id,
        message_id_generated=generated,
        subject=subject,
        sender_name=sender_name,
        sender_email=sender_email,
        participants=_participants(msg),
        in_reply_to=_first_message_id(_header(msg, "In-Reply-To")),
        references=_message_ids(_header(msg, "References")),
        received_at=_parse_date(_header(msg, "Date")) or _to_utc(internal_date),
        body_text=body_text,
        body_html=body_html,
        attachments=_extract_attachments(msg),
        raw_size=len(raw),
        is_bulk=_is_bulk(msg),
    )


# -- headers -----------------------------------------------------------------


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except _SAFE_ERRORS:
        return ""
    return "" if value is None else str(value).strip()


def _is_bulk(msg: Message) -> bool:
    """Mailing-list and marketing senders mark their mail; humans do not."""
    if _header(msg, "List-Unsubscribe") or _header(msg, "List-Id"):
        return True
    return _header(msg, "Precedence").lower() in {"bulk", "list", "junk"}


def _parse_address(value: str) -> tuple[str, str]:
    name, address = parseaddr(value)
    return name.strip(), address.strip().lower()


def _participants(msg: Message) -> list[Participant]:
    """Every person on the mail, one entry per role. Bad entries are dropped."""
    out: list[Participant] = []
    for role, header in (("from", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        raw = _header(msg, header)
        if not raw:
            continue
        try:
            pairs = getaddresses([raw])
        except _SAFE_ERRORS:
            continue
        for name, address in pairs:
            address = address.strip().lower()
            if address:
                out.append(Participant(role=role, name=name.strip(), address=address))
    return out


_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


def _message_ids(value: str) -> list[str]:
    """All ``<...>`` tokens in a References-style header, in order."""
    return _MESSAGE_ID_RE.findall(value) if value else []


def _first_message_id(value: str) -> str | None:
    ids = _message_ids(value)
    return ids[0] if ids else (value.strip() or None)


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except _SAFE_ERRORS:
        return None
    return _to_utc(parsed)


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _generated_message_id(raw: bytes) -> str:
    """Stable substitute for a missing Message-ID.

    The raw bytes of a given message are identical every time IMAP serves it,
    so hashing them gives a deterministic de-duplication key.
    """
    return "generated-" + hashlib.sha256(raw).hexdigest()


# -- bodies ------------------------------------------------------------------


def _extract_bodies(msg: EmailMessage) -> tuple[str, str]:
    return _body_part(msg, "plain"), _body_part(msg, "html")


def _body_part(msg: EmailMessage, subtype: str) -> str:
    try:
        part = msg.get_body(preferencelist=(subtype,))
    except _SAFE_ERRORS:
        return ""
    if part is None:
        return ""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except _SAFE_ERRORS:
        pass
    # Unknown or lying charset: decode leniently rather than lose the body.
    try:
        payload = part.get_payload(decode=True)
    except _SAFE_ERRORS:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return ""


# -- attachments -------------------------------------------------------------


def _extract_attachments(msg: EmailMessage) -> list[ParsedAttachment]:
    attachments: list[ParsedAttachment] = []
    for part in msg.walk():
        if part.is_multipart() or not _looks_like_attachment(part):
            continue
        try:
            data = part.get_payload(decode=True)
        except _SAFE_ERRORS:
            continue
        if not isinstance(data, bytes) or not data:
            continue
        content_type = part.get_content_type()
        filename = _safe_filename(_get_filename(part), content_type, len(attachments) + 1)
        attachments.append(
            ParsedAttachment(
                filename=filename,
                content_type=content_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        )
    return attachments


def _looks_like_attachment(part: Message) -> bool:
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return True
    filename = _get_filename(part)
    if not filename:
        return False
    # Inline images referenced from the HTML body (signature logos and the
    # like) are rendering assets, not attachments the user cares about.
    if disposition == "inline" and part.get_content_maintype() == "image" and part.get("Content-ID"):
        return False
    return True


def _get_filename(part: Message) -> str:
    try:
        return (part.get_filename() or "").strip()
    except _SAFE_ERRORS:
        return ""


def _safe_filename(filename: str, content_type: str, index: int) -> str:
    # Drop any directory component a hostile or buggy client might have sent.
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    filename = _UNSAFE_FILENAME_CHARS.sub("_", filename).strip(" .")
    if not filename:
        extension = mimetypes.guess_extension(content_type) or ""
        filename = f"attachment-{index}{extension}"
    return filename[:_MAX_FILENAME_LEN]
