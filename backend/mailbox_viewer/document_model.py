"""Loading the two trained models and wrapping them for the classifier.

* Attachment model: LayoutLMv3 invoice classifier, inference code vendored in
  ``backend/attachment_classifier/``, weights in ``ATTACHMENT_MODEL_DIR``.
* Body model: Laya (convaiinnovations/laya), loader files and weights in
  ``BODY_MODEL_DIR``.

Both load only from local folders. Hugging Face offline mode and telemetry
are forced off in ``mailbox_viewer/__init__.py`` before any of this is
imported. A missing folder means that model is skipped (NoModel / NoBodyModel)
with a warning, so the app still syncs and stores mail.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .classification_graph import EmailClassifier
from .classifier import (
    BODY_LABELS,
    AttachmentInput,
    BodyModel,
    DocumentModel,
    NoBodyModel,
    NoModel,
    Prediction,
    model_content_type,
)
from .config import Settings

logger = logging.getLogger(__name__)

# The Laya question that scored best zero-shot (body only, document type).
LAYA_QUESTION = {
    "type": "choice",
    "instructions": "What kind of document does the email in `body` send or discuss?",
    "criteria": BODY_LABELS,
}


class LayoutLMv3InvoiceModel:
    """Attachment model: invoice vs not_invoice for the first page of a file."""

    name = "layoutlmv3-invoice"

    def __init__(self, model_dir: Path) -> None:
        from attachment_classifier.config import Config
        from attachment_classifier.inference import AttachmentClassifier

        # No feature or DOCX caches on disk; DOCX conversion still needs LibreOffice.
        self._cfg = Config(cache_dir=None, docx_pdf_cache_dir=None)
        self._clf = AttachmentClassifier(model_dir, self._cfg)

    def predict(self, attachment: AttachmentInput) -> Prediction | None:
        result = self._clf.predict(attachment.data, model_content_type(attachment))
        if result.is_invoice:
            return Prediction("invoice", result.prob_invoice)
        return Prediction("not_invoice", 1.0 - result.prob_invoice)


class LayaBodyModel:
    """Body model: zero-shot document type of the email, from the body text."""

    name = "laya"

    def __init__(self, model_dir: Path) -> None:
        model_dir = model_dir.resolve()
        if not (model_dir / "model.safetensors").is_file():
            raise FileNotFoundError(f"Laya weights not found in {model_dir}")
        if str(model_dir) not in sys.path:
            sys.path.insert(0, str(model_dir))  # rl_agent_api imports rl_common by name
        from email_utils import clean_email_body  # type: ignore[import-not-found]
        from rl_agent_api import RLAgent  # type: ignore[import-not-found]

        self._clean = clean_email_body
        self._agent = RLAgent(str(model_dir), device="cpu")

    def predict(self, subject: str, body: str) -> Prediction | None:
        text = self._clean(body)
        if not text.strip():
            return None
        answer = self._agent.system_one({"body": text}, {"doc_type": LAYA_QUESTION})["answers"]["doc_type"]
        label = answer["choice"]
        return Prediction(label, float(answer["probabilities"][label]))


def load_classifier(settings: Settings) -> EmailClassifier:
    return EmailClassifier(
        body_model=_load_body_model(settings),
        document_model=_load_document_model(settings),
        min_confidence=settings.model_min_confidence,
        body_min_confidence=settings.body_min_confidence,
        decides_above=settings.attachment_decides_confidence,
    )


def _load_document_model(settings: Settings) -> DocumentModel:
    path = Path(settings.attachment_model_dir) if settings.attachment_model_dir else None
    if path is None or not (path / "config.json").is_file():
        logger.warning("attachment model not found at %s; attachments will not be classified", path)
        return NoModel()
    logger.info("loading attachment model from %s", path)
    return LayoutLMv3InvoiceModel(path)


def _load_body_model(settings: Settings) -> BodyModel:
    path = Path(settings.body_model_dir) if settings.body_model_dir else None
    if path is None or not (path / "model.safetensors").is_file():
        logger.warning("body model not found at %s; email bodies will not be classified", path)
        return NoBodyModel()
    logger.info("loading body model from %s", path)
    return LayaBodyModel(path)
