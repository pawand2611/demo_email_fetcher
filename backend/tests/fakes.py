"""Test doubles: an in-memory IMAP "server", a client that speaks to it, and
a stand-in for the trained document model."""

from __future__ import annotations

from email.message import EmailMessage

from mailbox_viewer.classifier import AttachmentInput, Prediction


class FakeDocumentModel:
    """Stands in for the LayoutLMv3 attachment model. Predicts from the
    filename so tests are deterministic: names containing "invoice" are
    ``invoice`` at 0.95 (decisive), anything else ``not_invoice`` at 0.70
    (below the 0.8 bar, so the body model is consulted too).
    ``overrides`` and ``fail_on`` target single files."""

    name = "fake-attachment"

    def __init__(self) -> None:
        self.overrides: dict[str, Prediction] = {}
        self.fail_on: set[str] = set()
        self.calls: list[str] = []

    def predict(self, attachment: AttachmentInput) -> Prediction | None:
        self.calls.append(attachment.filename)
        if attachment.filename in self.fail_on:
            raise RuntimeError("model could not read the file")
        if attachment.filename in self.overrides:
            return self.overrides[attachment.filename]
        if "invoice" in attachment.filename.lower():
            return Prediction("invoice", 0.95)
        return Prediction("not_invoice", 0.70)


class FakeBodyModel:
    """Stands in for Laya. Picks the document type from words in the subject
    and body: statement 0.93, invoice 0.92, receipt 0.91, otherwise other 0.80.
    ``overrides`` maps a subject to a fixed prediction.

    Payment confirmation: 0.95 when the body says "payment received" or
    "have paid", otherwise 0.20; ``confirmations`` maps a subject to a score."""

    name = "fake-body"

    def __init__(self) -> None:
        self.overrides: dict[str, Prediction] = {}
        self.confirmations: dict[str, float] = {}
        self.calls: list[str] = []
        self.confirmation_calls: list[str] = []

    def confirms_payment(self, subject: str, body: str) -> float | None:
        self.confirmation_calls.append(subject)
        if subject in self.confirmations:
            return self.confirmations[subject]
        text = body.lower()
        return 0.95 if "payment received" in text or "have paid" in text else 0.20

    def predict(self, subject: str, body: str) -> Prediction | None:
        self.calls.append(subject)
        if subject in self.overrides:
            return self.overrides[subject]
        text = f"{subject} {body}".lower()
        for label, score in (("statement", 0.93), ("invoice", 0.92), ("receipt", 0.91)):
            if label in text:
                return Prediction(label, score)
        return Prediction("other", 0.80)


class FakeIntentTier1:
    """Stands in for Laya's intent question. ``new_invoice`` at 0.90 unless
    ``overrides`` maps a subject to (intent, confidence). Records each state."""

    name = "fake-intent-tier1"

    def __init__(self) -> None:
        self.overrides: dict[str, tuple[str, float]] = {}
        self.fail_on: set[str] = set()
        self.states: list[dict] = []

    def classify(self, state):
        from mailbox_viewer.intent import INTENT_KEYS, checked_tier1

        self.states.append(dict(state))
        if state["subject"] in self.fail_on:
            raise RuntimeError("laya failed")
        intent, confidence = self.overrides.get(state["subject"], ("new_invoice", 0.90))
        probs = {k: (0.65 if k == intent else 0.05) for k in INTENT_KEYS}
        return checked_tier1(intent, probs, confidence)


class FakeIntentTier2:
    """Stands in for DeepSeek: answers ``result`` (default: partial_payment, ok, 104 s)."""

    name = "fake-intent-tier2"

    def __init__(self, result=None) -> None:
        from mailbox_viewer.intent import TIER2_OK, Tier2Result

        self.result = result or Tier2Result("partial_payment", TIER2_OK, 104.0)
        self.up = True
        self.states: list[dict] = []

    def healthy(self) -> bool:
        return self.up

    def classify(self, state):
        self.states.append(dict(state))
        return self.result


def fake_intent_stage(tier1=None, tier2=None, *, tier2_enabled: bool = True, inline: bool = False,
                      min_confidence: float = 0.4):
    from mailbox_viewer.intent import IntentStage

    return IntentStage(tier1 or FakeIntentTier1(), tier2 if tier2 is not None else FakeIntentTier2(),
                       min_confidence=min_confidence, tier2_enabled=tier2_enabled, tier2_inline=inline,
                       clean=lambda text: text.strip(), max_chars=3000)


def fake_classifier(body=None, document=None, min_confidence: float = 0.5, body_min_confidence: float = 0.7,
                    intent=None):
    """An EmailClassifier (the real LangGraph flow) over the fake models."""
    from mailbox_viewer.classification_graph import EmailClassifier

    return EmailClassifier(body or FakeBodyModel(), document or FakeDocumentModel(), min_confidence,
                           body_min_confidence, intent=intent)


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

    def __init__(self, server: FakeMailServer, folder: str = "INBOX") -> None:
        self._server = server
        self.folder = folder
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
