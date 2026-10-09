"""Two-model email classification: the parts and the decision rule.

* The **attachment model** (LayoutLMv3) labels each PDF, image or DOCX
  attachment ``invoice`` or ``not_invoice`` with a confidence.
* The **body model** (Laya, zero-shot) reads the email body and answers
  "what kind of document does this email send?" with one of
  :data:`BODY_LABELS` and a confidence.

Decision rule (the user's, 2026-10-07; scores 0..1):

1. **The email has an attachment LayoutLMv3 can read.**
   * LayoutLMv3 above ``ATTACHMENT_DECIDES_CONFIDENCE`` (0.8): decide on the
     attachment alone. An invoice is payment; confidently not an invoice is
     not payment. Laya is not run.
   * Otherwise Laya is consulted as well: a Laya payment label (statement,
     invoice, receipt) at ``BODY_MIN_CONFIDENCE`` (0.7) or more is payment; a
     Laya payment label from :data:`BODY_REVIEW_FLOOR` up to that, or a weak
     invoice call from LayoutLMv3 that Laya does not support, is **review**;
     anything else is none.
2. **No readable attachment** (none, or only CSV and similar): Laya alone,
   same bars.
3. **Thread rule** (applied by the sync, not here): once an email in a thread
   is classified payment, later emails in that thread are payment too
   (:func:`inherited_decision`). Laya is not run for them; LayoutLMv3 still
   labels their attachments so an invoice file is recognised as one.

``tier`` records which models decided: 0 none, 1 attachment model only,
2 body model only, 3 both.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from typing import Protocol, Sequence

from .mail_parser import ParsedEmail

logger = logging.getLogger(__name__)

DECISION_PAYMENT = "payment"
DECISION_NONE = "none"
DECISION_REVIEW = "review"

TIER_NONE = 0
TIER_ATTACHMENT = 1
TIER_BODY = 2
TIER_BOTH = 3

ATTACHMENT_INVOICE = "invoice"
ATTACHMENT_NOT_INVOICE = "not_invoice"

# Answer options the body model chooses from (Laya "doc_type" question).
BODY_LABELS: dict[str, str] = {
    "invoice": "a bill requesting payment for goods or services",
    "receipt": "proof of a payment that was already made",
    "purchase_order": "an order placed with a supplier",
    "quotation": "a price quote or estimate",
    "statement": "a bank statement, payslip, remittance or payment advice",
    "other": "any other document",
}

# Labels that count as payment documents.
PAYMENT_LABELS = frozenset({"statement", "invoice", "receipt"})

# Body model: a payment label below the payment bar but at least this high is sent to review.
BODY_REVIEW_FLOOR = 0.5
# Attachment model: above this score the attachment decides on its own (default; see settings).
ATTACHMENT_DECIDES = 0.8

MODEL_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/jpg",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
MODEL_EXTENSIONS = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

_MAX_LABEL_LEN = 32  # emails.doc_type / attachments.doc_type column width


# -- inputs and model interfaces ------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentInput:
    """One attachment as the model sees it. ``data`` is None when the bytes are
    unavailable (for example a file missing from the store)."""

    filename: str
    content_type: str
    data: bytes | None = field(default=None, repr=False)

    @classmethod
    def all_from_parsed(cls, mail: ParsedEmail) -> tuple["AttachmentInput", ...]:
        return tuple(cls(a.filename, a.content_type, a.data) for a in mail.attachments)


@dataclass(frozen=True)
class EmailInput:
    subject: str
    body: str
    attachments: tuple[AttachmentInput, ...] = ()

    @classmethod
    def from_parsed(cls, mail: ParsedEmail) -> "EmailInput":
        return cls(mail.subject, mail.body_text, AttachmentInput.all_from_parsed(mail))


@dataclass(frozen=True)
class Prediction:
    """A label and the model's score for that label, 0..1."""

    label: str
    confidence: float


class DocumentModel(Protocol):
    """The attachment model: labels one document file."""

    name: str

    def predict(self, attachment: AttachmentInput) -> Prediction | None: ...


class BodyModel(Protocol):
    """The body model: labels what kind of document the email is about."""

    name: str

    def predict(self, subject: str, body: str) -> Prediction | None: ...


class NoModel:
    """Stand-in when the attachment model is not installed: classifies nothing."""

    name = "none"

    def predict(self, attachment: AttachmentInput) -> Prediction | None:
        return None


class NoBodyModel:
    """Stand-in when the body model is not installed: classifies nothing."""

    name = "none"

    def predict(self, subject: str, body: str) -> Prediction | None:
        return None


# -- outputs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AttachmentDecision:
    doc_type: str | None
    confidence: float | None
    filename: str = ""
    note: str | None = None  # why it was not classified, if it was not


@dataclass(frozen=True)
class BodyDecision:
    label: str | None
    confidence: float | None
    note: str | None = None


@dataclass(frozen=True)
class Decision:
    doc_type: str | None
    tier: int
    confidence: float | None
    decision: str
    reason: str
    attachments: tuple[AttachmentDecision, ...] = field(default_factory=tuple)
    inherited: bool = False  # payment because the thread already was; models not run

    @property
    def is_payment(self) -> bool:
        return self.decision == DECISION_PAYMENT

    @property
    def matched_directly(self) -> bool:
        """Payment on this email's own content, not inherited from its thread."""
        return self.is_payment and not self.inherited

    @property
    def needs_review(self) -> bool:
        return self.decision == DECISION_REVIEW

    def with_note(self, note: str) -> "Decision":
        return replace(self, reason=f"{self.reason}; {note}")


# -- the two classification steps ---------------------------------------------------------------


def is_model_input(attachment: AttachmentInput) -> bool:
    """True for files the attachment model reads: PDF, PNG, JPEG, DOCX."""
    if attachment.content_type.lower().split(";")[0].strip() in MODEL_CONTENT_TYPES:
        return True
    lowered = attachment.filename.lower()
    return any(lowered.endswith(ext) for ext in MODEL_EXTENSIONS)


def model_content_type(attachment: AttachmentInput) -> str:
    """The content type to hand the model; falls back to the extension when the
    mail client sent a generic type such as application/octet-stream."""
    ct = attachment.content_type.lower().split(";")[0].strip()
    if ct in MODEL_CONTENT_TYPES:
        return "image/jpeg" if ct == "image/jpg" else ct
    for ext, mapped in MODEL_EXTENSIONS.items():
        if attachment.filename.lower().endswith(ext):
            return mapped
    return ct


def classify_attachments(attachments: Sequence[AttachmentInput], model: DocumentModel) -> tuple[AttachmentDecision, ...]:
    """Run the attachment model on every readable document; one result per attachment."""
    out: list[AttachmentDecision] = []
    for att in attachments:
        if att.data is None:
            out.append(AttachmentDecision(None, None, att.filename, "file bytes unavailable"))
            continue
        if not is_model_input(att):
            out.append(AttachmentDecision(None, None, att.filename, "not a PDF, image or DOCX"))
            continue
        try:
            prediction = model.predict(att)
        except Exception as exc:  # one unreadable file must not stop the sync
            logger.warning("attachment model %s failed on %r: %s: %s", model.name, att.filename, type(exc).__name__, exc)
            out.append(AttachmentDecision(None, None, att.filename, _short_error(exc)))
            continue
        if prediction is None:
            out.append(AttachmentDecision(None, None, att.filename, "no attachment model configured"))
            continue
        out.append(AttachmentDecision(_label(prediction.label), _clamp(prediction.confidence), att.filename))
    return tuple(out)


def classify_body(subject: str, body: str, model: BodyModel) -> BodyDecision:
    """Run the body model on the email text."""
    if not (body or "").strip() and not (subject or "").strip():
        return BodyDecision(None, None, "empty email body")
    try:
        prediction = model.predict(subject or "", body or "")
    except Exception as exc:
        logger.warning("body model %s failed: %s: %s", model.name, type(exc).__name__, exc)
        return BodyDecision(None, None, _short_error(exc))
    if prediction is None:
        return BodyDecision(None, None, "no body model configured")
    return BodyDecision(_label(prediction.label), _clamp(prediction.confidence))


# -- the combine rule -----------------------------------------------------------------------------


def attachment_decides(attachments: Sequence[AttachmentDecision], decides_above: float = ATTACHMENT_DECIDES) -> bool:
    """True when LayoutLMv3 alone settles the email (rule 1, first case):
    an invoice above the bar, or every readable attachment confidently not an invoice."""
    scored = [a for a in attachments if a.doc_type is not None and a.confidence is not None]
    if not scored:
        return False
    if any(a.doc_type == ATTACHMENT_INVOICE and a.confidence > decides_above for a in scored):
        return True
    return all(a.confidence > decides_above for a in scored)


def combine(
    body: BodyDecision,
    attachments: Sequence[AttachmentDecision],
    min_confidence: float = 0.5,
    body_min_confidence: float = 0.7,
    decides_above: float = ATTACHMENT_DECIDES,
) -> Decision:
    scored = [a for a in attachments if a.doc_type is not None and a.confidence is not None]
    has_body = body.label is not None and body.confidence is not None
    attachment_decisions = tuple(attachments)

    parts: list[str] = []
    if has_body:
        parts.append(f"body: {body.label} ({body.confidence:.2f})")
    elif body.note:
        parts.append(f"body: {body.note}")
    for a in attachments:
        parts.append(f'"{a.filename}": ' + (f"{a.doc_type} ({a.confidence:.2f})" if a.doc_type else (a.note or "not classified")))
    summary = "; ".join(parts) or "nothing to classify"

    def decide(decision: str, doc_type: str | None, confidence: float | None, tier: int, why: str) -> Decision:
        arrow = {DECISION_PAYMENT: "payment", DECISION_REVIEW: "review", DECISION_NONE: "not a payment document"}[decision]
        return Decision(doc_type, tier, confidence, decision, f"{summary} -> {arrow}: {why}", attachment_decisions)

    # Rule 1a: a confident attachment decides on its own.
    if attachment_decides(attachments, decides_above):
        strong_invoices = [a for a in scored if a.doc_type == ATTACHMENT_INVOICE and a.confidence > decides_above]
        if strong_invoices:
            best = max(strong_invoices, key=lambda a: a.confidence)
            return decide(DECISION_PAYMENT, ATTACHMENT_INVOICE, best.confidence, TIER_ATTACHMENT,
                          f"attachment model above {decides_above:.2f}")
        best = max(scored, key=lambda a: a.confidence)
        return decide(DECISION_NONE, "other", best.confidence, TIER_ATTACHMENT,
                      f"attachment model above {decides_above:.2f} says not an invoice")

    if not has_body and not scored:
        return Decision(None, TIER_NONE, None, DECISION_NONE, summary, attachment_decisions)

    tier = TIER_BOTH if scored and has_body else (TIER_BODY if has_body else TIER_ATTACHMENT)
    body_is_payment_label = has_body and body.label in PAYMENT_LABELS
    weak_invoices = [a for a in scored if a.doc_type == ATTACHMENT_INVOICE and a.confidence >= min_confidence]

    # Rules 1b and 2: Laya decides, with the attachment as supporting evidence.
    if body_is_payment_label and body.confidence >= body_min_confidence:
        doc_type = ATTACHMENT_INVOICE if weak_invoices and body.label == ATTACHMENT_INVOICE else body.label
        return decide(DECISION_PAYMENT, doc_type, body.confidence, tier, f"body model at or above {body_min_confidence:.2f}")
    if body_is_payment_label and body.confidence >= BODY_REVIEW_FLOOR:
        return decide(DECISION_REVIEW, body.label, body.confidence, tier,
                      f"{body.label} below the {body_min_confidence:.2f} payment bar")
    if weak_invoices:
        best = max(weak_invoices, key=lambda a: a.confidence)
        return decide(DECISION_REVIEW, ATTACHMENT_INVOICE, best.confidence, tier,
                      "attachment model unsure about invoice and body model does not confirm")

    if has_body:
        return decide(DECISION_NONE, body.label, body.confidence, tier, "no payment evidence")
    best = max(scored, key=lambda a: a.confidence)
    return decide(DECISION_NONE, "other", best.confidence, tier, "no payment evidence")


def inherited_decision(attachments: Sequence[AttachmentDecision] | int = ()) -> Decision:
    """Rule 3: the email belongs to a thread already classified as payment, so
    it is payment too. ``attachments`` are LayoutLMv3's labels for its files
    (labelling only; they do not change the decision), or a count when the
    files were not labelled."""
    if isinstance(attachments, int):
        attachments = tuple(AttachmentDecision(None, None) for _ in range(attachments))
    attachments = tuple(attachments)
    labelled = [a for a in attachments if a.doc_type is not None and a.confidence is not None]
    reason = "part of a thread already classified as payment; body model not run"
    if labelled:
        reason += "; attachments labelled: " + ", ".join(f'"{a.filename}": {a.doc_type} ({a.confidence:.2f})' for a in labelled)
    invoices = [a for a in labelled if a.doc_type == ATTACHMENT_INVOICE]
    best = max(invoices, key=lambda a: a.confidence) if invoices else None
    return Decision(
        ATTACHMENT_INVOICE if best else None,
        TIER_ATTACHMENT if labelled else TIER_NONE,
        best.confidence if best else None,
        DECISION_PAYMENT,
        reason,
        attachments,
        inherited=True,
    )


_BODY_IN_REASON = re.compile(r"^body: ([a-z_]+) \((\d\.\d\d)\)")
_BODY_NOTE_IN_REASON = re.compile(r"^body: ([^;]+?)(?:;| ->|$)")


def body_decision_from_reason(reason: str | None) -> BodyDecision:
    """Read the body model's stored prediction back from a decision reason.

    :func:`combine` always starts the reason with ``body: <label> (<score>)``
    or ``body: <note>``, so decisions can be recomputed under new rules
    without running the models again (scores are kept to two decimals).
    """
    if not reason:
        return BodyDecision(None, None, "no stored body prediction")
    match = _BODY_IN_REASON.match(reason)
    if match:
        return BodyDecision(match.group(1), float(match.group(2)))
    note = _BODY_NOTE_IN_REASON.match(reason)
    return BodyDecision(None, None, note.group(1).strip() if note else "no stored body prediction")


# -- helpers -----------------------------------------------------------------------------------


def _label(value: str) -> str:
    return value.strip().lower()[:_MAX_LABEL_LEN]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    if "tesseract" in text.lower():
        return "OCR program Tesseract is not installed"
    if "soffice" in text.lower() or "libreoffice" in text.lower():
        return "LibreOffice is not installed (needed for DOCX)"
    return f"{type(exc).__name__}: {text[:120]}"
