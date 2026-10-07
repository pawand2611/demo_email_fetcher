"""Central configuration: paths, model name, thresholds, hyper-parameters, seed.

Every other module reads values from here; no magic numbers elsewhere.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LABEL_NOT_INVOICE = 0
LABEL_INVOICE = 1
ID2LABEL = {LABEL_NOT_INVOICE: "not_invoice", LABEL_INVOICE: "invoice"}
LABEL2ID = {v: k for k, v in ID2LABEL.items()}

# Folder names recognised by the filesystem source (case-insensitive) -> label.
FOLDER_LABELS = {"invoice": LABEL_INVOICE, "invoices": LABEL_INVOICE,
                 "random": LABEL_NOT_INVOICE, "not_invoice": LABEL_NOT_INVOICE}

SUPPORTED_EXTENSIONS = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png",
                        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

_LOCAL_PROGRAMS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")
_WIN_TESSERACT = [r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                  os.path.join(_LOCAL_PROGRAMS, "Tesseract-OCR", "tesseract.exe")]
_WIN_SOFFICE = [r"C:\Program Files\LibreOffice\program\soffice.exe",
                r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                os.path.join(_LOCAL_PROGRAMS, "LibreOffice", "program", "soffice.exe"),
                # admin-free install: `msiexec /a LibreOffice.msi /qn TARGETDIR=<this folder>`
                os.path.join(_LOCAL_PROGRAMS, "LibreOfficePortable", "program", "soffice.exe")]


HUB_MODEL_ID = "microsoft/layoutlmv3-base"
LOCAL_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "layoutlmv3-base"


def _default_model_name() -> str:
    if (LOCAL_MODEL_DIR / "config.json").is_file():
        return str(LOCAL_MODEL_DIR)
    return HUB_MODEL_ID


def _find_binary(name: str, candidates: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _find_soffice() -> str | None:
    """On Windows prefer soffice.com: soffice.exe returns before the conversion finishes."""
    exe = _find_binary("soffice", _WIN_SOFFICE)
    if exe and exe.lower().endswith("soffice.exe"):
        com = exe[:-4] + ".com"
        if os.path.exists(com):
            return com
    return exe


@dataclass
class Config:
    # ---- paths ------------------------------------------------------------
    source_root: Path = Path("./Attachments")
    artifacts_root: Path = Path("./artifacts")
    cache_dir: Path | None = Path("./.feature_cache")       # None disables the feature cache
    docx_pdf_cache_dir: Path | None = Path("./.docx_pdf_cache")  # None disables caching converted PDFs

    # ---- external binaries -------------------------------------------------
    tesseract_cmd: str | None = field(default_factory=lambda: _find_binary("tesseract", _WIN_TESSERACT))
    soffice_cmd: str | None = field(default_factory=_find_soffice)
    soffice_profile_dir: Path = Path("./.soffice_profile")  # persistent profile avoids first-run setup each call
    soffice_timeout_s: int = 600

    # ---- extraction --------------------------------------------------------
    ocr_lang: str = "eng+fra"
    pdf_render_dpi: int = 150
    min_ocr_conf: float = 0.0        # words with conf <= this are dropped (also drops -1)
    bbox_scale: int = 1000           # LayoutLMv3 expects 0-1000 boxes

    # ---- model / tokenisation ---------------------------------------------
    # A local copy (downloaded with curl, see README "Offline / proxy environments") is
    # preferred when present; otherwise the Hub id is used.
    model_name: str = field(default_factory=lambda: _default_model_name())
    max_length: int = 512
    threshold: float = 0.5           # prob_invoice >= threshold -> is_invoice

    # ---- dataset -----------------------------------------------------------
    test_size: float = 0.2
    seed: int = 42

    # ---- training ----------------------------------------------------------
    freeze_backbone: bool = False
    epochs: int = 5
    lr: float | None = None          # None -> 2e-5 (full fine-tune) or 1e-3 (frozen backbone)
    batch_size: int = 4
    weight_decay: float = 0.01
    warmup_ratio: float = 0.1
    class_weight_imbalance_ratio: float = 1.1  # apply class weights if majority/minority > this
    dataloader_workers: int = 0
    max_items: int | None = None     # cap on items per class for quick experiments; None = all

    @property
    def effective_lr(self) -> float:
        if self.lr is not None:
            return self.lr
        return 1e-3 if self.freeze_backbone else 2e-5

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Path):
                d[k] = str(v)
        d["effective_lr"] = self.effective_lr
        return d


def get_device() -> str:
    """Return 'cuda' > 'mps' > 'cpu' depending on availability, and log it."""
    import torch

    if torch.cuda.is_available():
        dev = "cuda"
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        dev = "mps"
    else:
        dev = "cpu"
    logger.info("Using device: %s", dev)
    return dev


def set_seed(seed: int) -> None:
    """Seed python, numpy and torch for determinism, and log the seed."""
    import random

    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    logger.info("Seed set to %d", seed)
