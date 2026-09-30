"""Which conversation does a message belong to?

The conversation key is the root Message-ID of the reply chain:

1. the first entry of the ``References`` header (RFC 5322 puts the oldest
   ancestor first), else
2. the ``In-Reply-To`` header, else
3. the message's own Message-ID: it starts a new thread.

If the key names a message we have already stored, the new mail joins that
message's thread even when the key itself is not the stored thread's root
(for example when a reply's References header was truncated by the client).
"""

from __future__ import annotations

from .mail_parser import ParsedEmail


def conversation_key(mail: ParsedEmail) -> str:
    if mail.references:
        return mail.references[0]
    if mail.in_reply_to:
        return mail.in_reply_to
    return mail.message_id


def ancestor_ids(mail: ParsedEmail) -> list[str]:
    """Every Message-ID this mail claims to descend from, most distant first."""
    seen: list[str] = []
    for mid in [*mail.references, mail.in_reply_to or ""]:
        if mid and mid not in seen and mid != mail.message_id:
            seen.append(mid)
    return seen
