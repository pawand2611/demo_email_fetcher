"""Loading the two trained models and wrapping them for the classifier.

* Attachment model: LayoutLMv3 invoice classifier, inference code vendored in
  ``backend/attachment_classifier/``, weights in ``ATTACHMENT_MODEL_DIR``.
* Body model: Laya (convaiinnovations/laya), loader files and weights in
  ``BODY_MODEL_DIR``.
* Intent model, tier 1: Laya typed-decisions in ``LAYA_DIR/LAYA_CHECKPOINT``
  (optional; see ``intent.py``). Tier 2 is a llama.cpp server, not loaded here.
  Both Laya folders import ``rl_agent_api`` by name, so the first one loaded
  provides that loader code; keep the two folders on the same Laya version.

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
from .intent import QUESTION as INTENT_QUESTION
from .intent import IntentError, IntentStage, LlamaServerTier2, Tier1Result, checked_tier1

logger = logging.getLogger(__name__)

# The Laya question that scored best zero-shot (body only, document type).
LAYA_QUESTION = {
    "type": "choice",
    "instructions": "What kind of document does the email in `body` send or discuss?",
    "criteria": BODY_LABELS,
}

# Tickets: a yes/no question. Checked 2026-10-14 on four samples: payment
# confirmations scored 0.90 and 0.97, an overdue reminder 0.33, but a plain
# invoice 0.75, close to the 0.80 bar. That is why an email carrying a new
# invoice never closes a ticket (see tickets.py).
LAYA_CONFIRMATION_QUESTION = {
    "type": "noul",
    "instructions": "Does the email in `body` confirm that a payment has been made or received?",
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

    def confirms_payment(self, subject: str, body: str) -> float | None:
        text = self._clean(body)
        if not text.strip():
            return None
        answer = self._agent.system_one({"body": text}, {"confirmed": LAYA_CONFIRMATION_QUESTION})["answers"]["confirmed"]
        return float(answer["noul"])


class LayaIntentModel:
    """Intent tier 1: Laya typed-decisions answering one ``choice`` question
    over the 8 intents. Loaded once per process; calls are serialised on the
    classifier's model thread because RLAgent is not documented as thread-safe."""

    name = "laya-intent"

    def __init__(self, laya_dir: Path, checkpoint: str) -> None:
        laya_dir = laya_dir.resolve()
        missing = [f for f in laya_intent_files(checkpoint) if not (laya_dir / f).is_file()]
        if missing:
            raise IntentError(
                f"LAYA_DIR {laya_dir} is missing {', '.join(missing)}; download them as described in the "
                "README section 'Intent of payment emails', or unset LAYA_DIR to turn intent classification off"
            )
        if str(laya_dir) not in sys.path:
            sys.path.insert(0, str(laya_dir))  # rl_agent_api imports rl_common by bare name
        from email_utils import clean_email_body  # type: ignore[import-not-found]
        from rl_agent_api import RLAgent  # type: ignore[import-not-found]

        self.clean = clean_email_body
        self._agent = RLAgent(str(laya_dir / checkpoint), device=None)  # cpu or cuda, chosen by Laya

    def classify(self, state) -> Tier1Result:
        answer = self._agent.system_one(dict(state), INTENT_QUESTION)["answers"]["intent"]
        # rl_agent.act_probability is deliberately ignored: it is 1.0 for every input.
        return checked_tier1(answer["choice"], answer["probabilities"], answer["confidence"])


def laya_intent_files(checkpoint: str) -> list[str]:
    """Every file the intent step needs under LAYA_DIR (specification section 3a)."""
    return ["rl_agent_api.py", "rl_common.py", "email_utils.py"] + [
        f"{checkpoint}/{f}" for f in ("model.safetensors", "rl_agent_config.json", "encoder/config.json",
                                      "tokenizer/tokenizer.json", "tokenizer/tokenizer_config.json")
    ]


def load_intent_stage(settings: Settings) -> IntentStage | None:
    """The intent step, or None when LAYA_DIR is not set. A LAYA_DIR with
    missing files is an error: it fails start-up instead of silently skipping."""
    if not settings.laya_dir:
        logger.warning("LAYA_DIR not set; payment emails will not get an intent")
        return None
    logger.info("loading intent model (Laya %s) from %s", settings.laya_checkpoint, settings.laya_dir)
    tier1 = LayaIntentModel(Path(settings.laya_dir), settings.laya_checkpoint)
    tier2 = None
    if settings.tier2_enabled:
        tier2 = LlamaServerTier2(settings.tier2_url, settings.tier2_timeout_s, settings.tier2_max_tokens,
                                 settings.tier2_temperature)
        if not tier2.healthy():
            logger.warning("intent tier 2 server not reachable at %s; unsure emails will fall back to tier 1 "
                           "for review until it is running", settings.tier2_url)
    return IntentStage(
        tier1=tier1,
        tier2=tier2,
        min_confidence=settings.tier1_min_confidence,
        tier2_enabled=settings.tier2_enabled,
        tier2_inline=settings.tier2_inline,
        clean=tier1.clean,
        max_chars=settings.email_body_max_chars,
    )


def load_classifier(settings: Settings) -> EmailClassifier:
    return EmailClassifier(
        body_model=_load_body_model(settings),
        document_model=_load_document_model(settings),
        min_confidence=settings.model_min_confidence,
        body_min_confidence=settings.body_min_confidence,
        decides_above=settings.attachment_decides_confidence,
        intent=load_intent_stage(settings),
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
