"""Inference entry point. This is the ONLY interface the future Postgres/Blob system calls."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import torch
from transformers import LayoutLMv3ForSequenceClassification, LayoutLMv3Processor

from ..config import LABEL_INVOICE, Config, get_device
from ..extraction import PageFeatures, extract_page

logger = logging.getLogger(__name__)


@dataclass
class PredictionResult:
    prob_invoice: float
    is_invoice: bool
    has_text_layer: bool
    n_words: int


class AttachmentClassifier:
    def __init__(self, model_dir: Path | str, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self.model_dir = Path(model_dir)
        self.device = get_device()
        self.processor = LayoutLMv3Processor.from_pretrained(self.model_dir, apply_ocr=False)
        self.model = LayoutLMv3ForSequenceClassification.from_pretrained(self.model_dir).to(self.device).eval()
        logger.info("Loaded model from %s (threshold=%.2f)", self.model_dir, self.cfg.threshold)

    def _encode(self, feats_list: list[PageFeatures]) -> dict[str, torch.Tensor]:
        images = [f.image.convert("RGB") for f in feats_list]
        words = [f.words if f.words else [""] for f in feats_list]
        boxes = [f.boxes if f.boxes else [[0, 0, 0, 0]] for f in feats_list]
        enc = self.processor(images, words, boxes=boxes, truncation=True, max_length=self.cfg.max_length,
                             padding="max_length", return_tensors="pt")
        return {k: v.to(self.device) for k, v in enc.items()}

    @torch.no_grad()
    def _predict_features(self, feats_list: list[PageFeatures]) -> list[PredictionResult]:
        results: list[PredictionResult] = []
        for start in range(0, len(feats_list), self.cfg.batch_size):
            chunk = feats_list[start:start + self.cfg.batch_size]
            logits = self.model(**self._encode(chunk)).logits
            probs = torch.softmax(logits.float(), dim=-1)[:, LABEL_INVOICE].cpu().tolist()
            for f, p in zip(chunk, probs):
                results.append(PredictionResult(prob_invoice=float(p), is_invoice=bool(p >= self.cfg.threshold),
                                                has_text_layer=f.has_text_layer, n_words=len(f.words)))
        return results

    def predict_features(self, feats: PageFeatures) -> PredictionResult:
        """Classify an already-extracted page (lets callers inspect the features too)."""
        return self._predict_features([feats])[0]

    def predict(self, data: bytes, content_type: str) -> PredictionResult:
        """Classify one attachment. Raises ExtractionError if the file cannot be read."""
        feats = extract_page(data, content_type, self.cfg)
        return self._predict_features([feats])[0]

    def predict_batch(self, items: list[tuple[bytes, str]]) -> list[PredictionResult]:
        """Classify many attachments; extraction errors propagate (callers decide how to skip)."""
        feats = [extract_page(data, ct, self.cfg) for data, ct in items]
        return self._predict_features(feats)
