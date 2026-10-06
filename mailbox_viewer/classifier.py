"""Model-based document classification, with an audit trail.

Classification comes only from a trained document model (LayoutLMv3), which
reads each attachment the model can handle (PDFs and images). There are no
keyword rules.

For every message the classifier produces one :class:`Decision`:

* ``doc_type``   the label of the strongest prediction across the attachments
                 (for example statement, invoice, receipt, other), or None
* ``tier``       1 when the model made a prediction, 0 when nothing could be
                 classified (no model configured, or no attachment the model reads)
* ``confidence`` the model's score for that label, 0..1
* ``decision``   ``payment`` when the label is one of :data:`PAYMENT_LABELS`
                 and the score reaches ``MODEL_MIN_CONFIDENCE``, else ``none``
* ``reason``     human-readable explanation, stored in ``decision_log``

Each attachment also gets its own label and score. The model itself lives
behind the small :class:`DocumentModel` interface; see ``document_model.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Protocol, Sequence

from .mail_parser import ParsedEmail

logger = logging.getLogger(__name__)

DECISION_PAYMENT = "payment"
DECISION_NONE = "none"

TIER_NONE = 0
TIER_MODEL = 1

# Labels that count as payment documents. Anything else the model predicts is
# recorded but does not mark the message or its thread as payment.
PAYMENT_LABELS = frozenset({"statement", "invoice", "receipt"})

MODEL_CONTENT_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp"}
)
MODEL_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})

_MAX_LABEL_LEN = 32  # emails.doc_type / attachments.doc_type column width


# -- inputs, outputs and the model interface -------------------------------------------


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
class Prediction:
    label: str
    confidence: float


class DocumentModel(Protocol):
    """What the classifier needs from a trained model."""

    name: str

    def predict(self, attachment: AttachmentInput) -> Prediction | None:
        """Label one document, or None if this file cannot be classified."""
        ...


class NoModel:
    """Stand-in used until a trained model is configured: classifies nothing."""

    name = "none"

    def predict(self, attachment: AttachmentInput) -> Prediction | None:
        return None


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

    def with_note(self, note: str) -> "Decision":
        return replace(self, reason=f"{self.reason}; {note}")


# -- classification ------------------------------------------------------------------------


def is_model_input(attachment: AttachmentInput) -> bool:
    """True for files the document model reads: PDFs and images."""
    if attachment.content_type.lower() in MODEL_CONTENT_TYPES:
        return True
    lowered = attachment.filename.lower()
    return any(lowered.endswith(ext) for ext in MODEL_EXTENSIONS)


def classify(
    attachments: Sequence[AttachmentInput],
    model: DocumentModel,
    min_confidence: float = 0.5,
) -> Decision:
    """Run the model on every readable document attachment and decide."""
    per_attachment: list[AttachmentDecision] = []
    scored: list[tuple[AttachmentInput, Prediction]] = []
    candidates = 0
    errors = 0

    for att in attachments:
        if att.data is None or not is_model_input(att):
            per_attachment.append(AttachmentDecision(None, None))
            continue
        candidates += 1
        try:
            prediction = model.predict(att)
        except Exception as exc:  # one unreadable file must not stop the sync
            logger.warning("model %s failed on %r: %s: %s", model.name, att.filename, type(exc).__name__, exc)
            prediction = None
            errors += 1
        if prediction is None:
            per_attachment.append(AttachmentDecision(None, None))
            continue
        prediction = Prediction(prediction.label.strip().lower()[:_MAX_LABEL_LEN], _clamp(prediction.confidence))
        per_attachment.append(AttachmentDecision(prediction.label, prediction.confidence))
        scored.append((att, prediction))

    attachment_decisions = tuple(per_attachment)

    if candidates == 0:
        return Decision(None, TIER_NONE, None, DECISION_NONE, "no PDF or image attachment for the model", attachment_decisions)

    if not scored:
        if isinstance(model, NoModel):
            reason = f"no classification model configured; {candidates} document(s) left unclassified"
        else:
            reason = f"model {model.name} could not classify {candidates} document(s)" + (f" ({errors} error(s))" if errors else "")
        return Decision(None, TIER_NONE, None, DECISION_NONE, reason, attachment_decisions)

    payment = [(a, p) for a, p in scored if p.label in PAYMENT_LABELS and p.confidence >= min_confidence]
    if payment:
        att, best = max(payment, key=lambda item: item[1].confidence)
        reason = f'model {model.name}: "{att.filename}" is {best.label} ({best.confidence:.2f})'
        return Decision(best.label, TIER_MODEL, best.confidence, DECISION_PAYMENT, reason, attachment_decisions)

    att, best = max(scored, key=lambda item: item[1].confidence)
    if best.label in PAYMENT_LABELS:
        why = f"below the {min_confidence:.2f} threshold"
    else:
        why = "not a payment document"
    reason = f'model {model.name}: "{att.filename}" is {best.label} ({best.confidence:.2f}), {why}'
    return Decision(best.label, TIER_MODEL, best.confidence, DECISION_NONE, reason, attachment_decisions)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
