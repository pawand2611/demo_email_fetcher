"""Rule-based payment-document detection, with a tiered audit trail.

For every message the classifier produces one :class:`Decision`:

* ``doc_type``   statement | invoice | receipt | other | None
* ``tier``       where the evidence came from: 1 attachment filename,
                 2 subject, 3 body text, 0 nothing matched
* ``confidence`` 0..1, fixed per tier (filename evidence is strongest)
* ``decision``   ``payment`` when a payment document was recognised, else ``none``
* ``reason``     human-readable explanation, stored in ``decision_log``

A mail must carry a document attachment (PDF, CSV, XLS, XLSX) to count as a
payment document; a subject alone is a notice, not a document. A document
attachment without any payment keyword gets ``doc_type = other``.

Everything is keyword based: no network, no model. All rules live here so
tuning is a one-file change and ``sync_mail.py --reclassify`` re-applies them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .mail_parser import ParsedEmail

DOC_STATEMENT = "statement"
DOC_INVOICE = "invoice"
DOC_RECEIPT = "receipt"
DOC_OTHER = "other"

DECISION_PAYMENT = "payment"
DECISION_NONE = "none"

TIER_NONE = 0
TIER_FILENAME = 1
TIER_SUBJECT = 2
TIER_BODY = 3

CONFIDENCE = {TIER_FILENAME: 0.95, TIER_SUBJECT: 0.80, TIER_BODY: 0.60}
CONFIDENCE_OTHER_DOCUMENT = 0.30

_BODY_SCAN_CHARS = 5000

# -- inputs and outputs -----------------------------------------------------------


@dataclass(frozen=True)
class AttachmentFacts:
    filename: str
    content_type: str


@dataclass(frozen=True)
class MailFacts:
    """The subset of a mail the rules look at. Built from a parsed message or
    from a stored row, so rules can be re-applied to the cache."""

    subject: str
    body_text: str
    attachments: tuple[AttachmentFacts, ...]

    @classmethod
    def from_parsed(cls, mail: ParsedEmail) -> "MailFacts":
        return cls(
            subject=mail.subject,
            body_text=mail.body_text,
            attachments=tuple(AttachmentFacts(a.filename, a.content_type) for a in mail.attachments),
        )


@dataclass(frozen=True)
class AttachmentDecision:
    doc_type: str | None
    confidence: float | None


@dataclass(frozen=True)
class Decision:
    doc_type: str | None
    tier: int
    confidence: float | None
    decision: str
    reason: str
    attachments: tuple[AttachmentDecision, ...] = field(default_factory=tuple)

    @property
    def is_payment(self) -> bool:
        return self.decision == DECISION_PAYMENT


# -- keyword matching ---------------------------------------------------------------

_SEPARATORS_RE = re.compile(r"[\s_\-.]+")


def normalise(text: str) -> str:
    """Fold separators so ``Card_Statement`` and ``e-statement`` read as phrases."""
    return _SEPARATORS_RE.sub(" ", text).strip()


class KeywordMatcher:
    """Case-insensitive phrase matcher on normalised text with word boundaries
    and an optional plural, so ``bill`` matches ``bills`` but not ``billion``.
    Longer phrases first: the first alternative wins and is reported."""

    def __init__(self, keywords: Sequence[str]) -> None:
        self.keywords = tuple(keywords)
        self._regex = re.compile("|".join(self._pattern(k) for k in self.keywords), re.IGNORECASE)

    @staticmethod
    def _pattern(keyword: str) -> str:
        left = r"(?<![a-z0-9])" if keyword[0].isalnum() else ""
        right = r"(?:s|es)?(?![a-z0-9])" if keyword[-1].isalnum() else ""
        return left + re.escape(keyword) + right

    def find(self, text: str) -> str | None:
        if not text:
            return None
        match = self._regex.search(normalise(text))
        if not match:
            return None
        found = match.group(0).lower()
        for keyword in self.keywords:
            if found in (keyword, keyword + "s", keyword + "es"):
                return keyword
        return found


# -- rules --------------------------------------------------------------------------

# Evaluated in this order; the first family with a hit names the doc_type.
DOC_TYPE_KEYWORDS: tuple[tuple[str, KeywordMatcher], ...] = (
    (
        DOC_STATEMENT,
        KeywordMatcher(
            (
                "account statement",
                "bank statement",
                "card statement",
                "billing statement",
                "account summary",
                "e statement",
                "estatement",
                "statement",
            )
        ),
    ),
    (
        DOC_INVOICE,
        KeywordMatcher(("tax invoice", "proforma invoice", "invoice", "bill", "amount due", "payment due")),
    ),
    (
        DOC_RECEIPT,
        KeywordMatcher(("payment receipt", "payment confirmation", "payment received", "receipt", "transaction")),
    ),
)

DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".csv", ".xls", ".xlsx"})


def is_document(att: AttachmentFacts) -> bool:
    if att.content_type.lower() in DOCUMENT_CONTENT_TYPES:
        return True
    lowered = att.filename.lower()
    return any(lowered.endswith(ext) for ext in DOCUMENT_EXTENSIONS)


def find_doc_type(text: str) -> tuple[str, str] | None:
    """Return (doc_type, keyword) for the first payment phrase in ``text``."""
    for doc_type, matcher in DOC_TYPE_KEYWORDS:
        keyword = matcher.find(text)
        if keyword:
            return doc_type, keyword
    return None


def classify(facts: MailFacts) -> Decision:
    document_indexes = [i for i, att in enumerate(facts.attachments) if is_document(att)]
    no_attachment_decisions = tuple(AttachmentDecision(None, None) for _ in facts.attachments)

    if not document_indexes:
        hit = find_doc_type(facts.subject)
        reason = (
            f'subject mentions "{hit[1]}" but no document is attached'
            if hit
            else "no document attachment and no payment keyword"
        )
        return Decision(None, TIER_NONE, None, DECISION_NONE, reason, no_attachment_decisions)

    # Tier 1: a document's own filename names the document type.
    filename_hits = {i: find_doc_type(facts.attachments[i].filename) for i in document_indexes}
    filename_hits = {i: hit for i, hit in filename_hits.items() if hit}
    if filename_hits:
        first_index, (doc_type, keyword) = next(iter(filename_hits.items()))
        confidence = CONFIDENCE[TIER_FILENAME]
        per_attachment = tuple(
            AttachmentDecision(filename_hits[i][0], confidence)
            if i in filename_hits
            else AttachmentDecision(DOC_OTHER, CONFIDENCE_OTHER_DOCUMENT) if i in document_indexes else AttachmentDecision(None, None)
            for i in range(len(facts.attachments))
        )
        reason = f'tier 1: attachment "{facts.attachments[first_index].filename}" contains "{keyword}"'
        return Decision(doc_type, TIER_FILENAME, confidence, DECISION_PAYMENT, reason, per_attachment)

    # Tier 2 / 3: the mail text names the type; every attached document inherits it.
    for tier, text, label in (
        (TIER_SUBJECT, facts.subject, "subject"),
        (TIER_BODY, facts.body_text[:_BODY_SCAN_CHARS], "body"),
    ):
        hit = find_doc_type(text)
        if hit:
            doc_type, keyword = hit
            confidence = CONFIDENCE[tier]
            per_attachment = tuple(
                AttachmentDecision(doc_type, confidence) if i in document_indexes else AttachmentDecision(None, None)
                for i in range(len(facts.attachments))
            )
            reason = f'tier {tier}: {label} contains "{keyword}"; {len(document_indexes)} document attachment(s)'
            return Decision(doc_type, tier, confidence, DECISION_PAYMENT, reason, per_attachment)

    per_attachment = tuple(
        AttachmentDecision(DOC_OTHER, CONFIDENCE_OTHER_DOCUMENT) if i in document_indexes else AttachmentDecision(None, None)
        for i in range(len(facts.attachments))
    )
    return Decision(
        DOC_OTHER,
        TIER_NONE,
        CONFIDENCE_OTHER_DOCUMENT,
        DECISION_NONE,
        f"{len(document_indexes)} document attachment(s) but no payment keyword anywhere",
        per_attachment,
    )
