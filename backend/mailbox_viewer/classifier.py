"""Two-model email classification: the parts and the combine rule.

* The **attachment model** (LayoutLMv3) labels each PDF, image or DOCX
  attachment ``invoice`` or ``not_invoice`` with a confidence.
* The **body model** (Laya, zero-shot) reads the email body and answers
  "what kind of document does this email send?" with one of
  :data:`BODY_LABELS` and a confidence.

:func:`combine` turns both into one :class:`Decision`. The LangGraph flow in
``classification_graph.py`` runs the two models and then ``combine``.

Combine rule (all scores 0..1):

* **Body (Laya)**, bar ``b`` = ``BODY_MIN_CONFIDENCE`` (default 0.9):
  a payment label (statement, invoice, receipt) scoring >= ``b`` is payment;
  a payment label scoring from :data:`BODY_REVIEW_FLOOR` up to ``b`` is
  **review** (worth a human look, not payment); any other label is never
  payment and never review on its own.
* **Attachment (LayoutLMv3)**, bar ``t`` = ``MODEL_MIN_CONFIDENCE`` (default
  0.5): an ``invoice`` scoring >= :data:`ATTACHMENT_DECISIVE` is payment; an
  ``invoice`` between ``t`` and that is **review** (the binary model is
  close to a coin flip there).
* **Review** also when the models contradict each other on "invoice": Laya
  says invoice (>= ``b``) while an attachment is decisively not an invoice,
  or an attachment is decisively an invoice while Laya confidently (>= ``b``)
  says it is not a payment document.
* Otherwise **payment** if either side says payment, else **none**.

``doc_type`` is ``invoice`` when an attachment confirms it, otherwise the body
label. ``tier`` records which models contributed: 0 none, 1 attachment model
only, 2 body model only, 3 both.
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
# Attachment model: binary invoice / not_invoice calls below this score are close to a coin flip.
ATTACHMENT_DECISIVE = 0.6

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

    @property
    def is_payment(self) -> bool:
        return self.decision == DECISION_PAYMENT

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


def combine(
    body: BodyDecision,
    attachments: Sequence[AttachmentDecision],
    min_confidence: float = 0.5,
    body_min_confidence: float = 0.9,
) -> Decision:
    scored = [a for a in attachments if a.doc_type is not None and a.confidence is not None]
    has_body = body.label is not None and body.confidence is not None
    attachment_decisions = tuple(attachments)

    if not has_body and not scored:
        notes = [f"body: {body.note}"] if body.note else []
        notes += [f'"{a.filename}": {a.note}' for a in attachments if a.note]
        reason = "; ".join(notes) or "nothing to classify"
        return Decision(None, TIER_NONE, None, DECISION_NONE, reason, attachment_decisions)

    tier = TIER_BOTH if has_body and scored else (TIER_BODY if has_body else TIER_ATTACHMENT)

    # Attachment model (binary): decisive calls only count as evidence.
    invoice_bar = max(min_confidence, ATTACHMENT_DECISIVE)
    invoices = [a for a in scored if a.doc_type == ATTACHMENT_INVOICE and a.confidence >= invoice_bar]
    borderline_invoices = [a for a in scored if a.doc_type == ATTACHMENT_INVOICE and min_confidence <= a.confidence < invoice_bar]
    decisive_not_invoice = [a for a in scored if a.doc_type != ATTACHMENT_INVOICE and a.confidence >= ATTACHMENT_DECISIVE]

    # Body model (six options): payment only at or above the body bar.
    body_is_payment_label = has_body and body.label in PAYMENT_LABELS
    body_payment = body_is_payment_label and body.confidence >= body_min_confidence
    body_borderline = body_is_payment_label and BODY_REVIEW_FLOOR <= body.confidence < body_min_confidence
    body_confident_other = has_body and not body_is_payment_label and body.confidence >= body_min_confidence

    parts: list[str] = []
    if has_body:
        parts.append(f"body: {body.label} ({body.confidence:.2f})")
    elif body.note:
        parts.append(f"body: {body.note}")
    for a in attachments:
        parts.append(f'"{a.filename}": ' + (f"{a.doc_type} ({a.confidence:.2f})" if a.doc_type else (a.note or "not classified")))
    summary = "; ".join(parts)

    def review(why: str, doc_type: str | None, confidence: float) -> Decision:
        return Decision(doc_type, tier, confidence, DECISION_REVIEW, f"{summary} -> review: {why}", attachment_decisions)

    # 1. The models contradict each other on "invoice".
    if body_payment and body.label == ATTACHMENT_INVOICE and not invoices and decisive_not_invoice:
        return review("models disagree on invoice", ATTACHMENT_INVOICE, body.confidence)
    if invoices and body_confident_other:
        return review("models disagree on invoice", ATTACHMENT_INVOICE, max(a.confidence for a in invoices))

    # 2. Payment: a confident body payment label, or a decisive invoice attachment.
    if invoices or body_payment:
        best_invoice = max(invoices, key=lambda a: a.confidence) if invoices else None
        doc_type = ATTACHMENT_INVOICE if best_invoice else body.label
        supporting = []
        if best_invoice:
            supporting.append(best_invoice.confidence)
        if body_payment:
            supporting.append(body.confidence)
        return Decision(doc_type, tier, max(supporting), DECISION_PAYMENT, f"{summary} -> payment", attachment_decisions)

    # 3. Borderline payment evidence: a human should look.
    if body_borderline:
        return review(f"{body.label} below the {body_min_confidence:.2f} payment bar", body.label, body.confidence)
    if borderline_invoices:
        best = max(borderline_invoices, key=lambda a: a.confidence)
        return review("attachment model is unsure about invoice", ATTACHMENT_INVOICE, best.confidence)

    if has_body:
        doc_type, confidence = body.label, body.confidence
    else:
        top = max(scored, key=lambda a: a.confidence)
        doc_type, confidence = ("other" if top.doc_type == ATTACHMENT_NOT_INVOICE else top.doc_type), top.confidence
    return Decision(doc_type, tier, confidence, DECISION_NONE, f"{'; '.join(parts)} -> not a payment document", attachment_decisions)


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
