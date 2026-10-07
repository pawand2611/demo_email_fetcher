"""Turn attachment bytes into first-page words + normalised boxes + page image.

PDF  -> pdfplumber text layer; falls back to OCR when the page has no words.
Image -> pytesseract OCR.
DOCX -> LibreOffice headless conversion to PDF, then the PDF path.
"""
from __future__ import annotations

import hashlib
import io
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pytesseract
from PIL import Image

from ..config import Config

logger = logging.getLogger(__name__)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png"}


@dataclass
class PageFeatures:
    words: list[str]
    boxes: list[list[int]]
    image: Image.Image
    has_text_layer: bool
    page_count: int
    meta: dict = field(default_factory=dict)


class ExtractionError(RuntimeError):
    """Raised for any failure turning bytes into PageFeatures."""


def _norm_box(x0: float, y0: float, x1: float, y1: float, w: float, h: float, scale: int) -> list[int]:
    def n(v: float, d: float) -> int:
        return int(max(0, min(scale, round(v / d * scale))))

    bx0, by0, bx1, by1 = n(x0, w), n(y0, h), n(x1, w), n(y1, h)
    return [min(bx0, bx1), min(by0, by1), max(bx0, bx1), max(by0, by1)]


def _ocr(image: Image.Image, cfg: Config) -> tuple[list[str], list[list[int]]]:
    if cfg.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = cfg.tesseract_cmd
    data = pytesseract.image_to_data(image, lang=cfg.ocr_lang, output_type=pytesseract.Output.DICT)
    w, h = image.size
    words: list[str] = []
    boxes: list[list[int]] = []
    for text, conf, left, top, width, height in zip(
            data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]):
        text = (text or "").strip()
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            conf_f = -1.0
        if not text or conf_f <= cfg.min_ocr_conf:
            continue
        words.append(text)
        boxes.append(_norm_box(left, top, left + width, top + height, w, h, cfg.bbox_scale))
    return words, boxes


def _extract_pdf(data: bytes, cfg: Config) -> PageFeatures:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise ExtractionError("PDF has no pages")
        page = pdf.pages[0]
        w, h = float(page.width), float(page.height)
        try:
            raw_words = page.extract_words() or []
        except Exception as exc:  # pdfplumber can choke on malformed content streams
            logger.warning("extract_words failed (%s); treating page as scanned", exc)
            raw_words = []
        image = page.to_image(resolution=cfg.pdf_render_dpi).original.convert("RGB")

    if raw_words:
        words = [wd["text"] for wd in raw_words]
        boxes = [_norm_box(wd["x0"], wd["top"], wd["x1"], wd["bottom"], w, h, cfg.bbox_scale) for wd in raw_words]
        return PageFeatures(words, boxes, image, has_text_layer=True, page_count=page_count)

    words, boxes = _ocr(image, cfg)
    return PageFeatures(words, boxes, image, has_text_layer=False, page_count=page_count)


def _extract_image(data: bytes, cfg: Config) -> PageFeatures:
    image = Image.open(io.BytesIO(data))
    image.load()
    image = image.convert("RGB")
    words, boxes = _ocr(image, cfg)
    return PageFeatures(words, boxes, image, has_text_layer=False, page_count=1)


def _run_soffice(cfg: Config, sources: list[Path], outdir: str) -> None:
    if not cfg.soffice_cmd:
        raise ExtractionError("LibreOffice (soffice) not found; cannot convert DOCX")
    profile = Path(cfg.soffice_profile_dir).resolve().as_uri()
    cmd = [cfg.soffice_cmd, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--nologo",
           "--nolockcheck", "--convert-to", "pdf", "--outdir", outdir, *map(str, sources)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=cfg.soffice_timeout_s)
    except subprocess.CalledProcessError as exc:
        raise ExtractionError(f"soffice failed: {exc.stderr.decode(errors='ignore')[:200]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("soffice timed out") from exc


def convert_docx_batch(blobs: list[bytes], cfg: Config) -> int:
    """Convert many DOCX files in ONE soffice call (amortises its slow start-up) into the
    sha256-keyed PDF cache that _docx_to_pdf reads. Returns the number newly converted."""
    if cfg.docx_pdf_cache_dir is None:
        raise ValueError("docx_pdf_cache_dir must be set to pre-convert DOCX files")
    cache = Path(cfg.docx_pdf_cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    todo = {hashlib.sha256(b).hexdigest(): b for b in blobs}
    todo = {d: b for d, b in todo.items() if not (cache / f"{d}.pdf").is_file()}
    if not todo:
        return 0
    with tempfile.TemporaryDirectory(prefix="docx2pdf_") as tmp:
        srcs = []
        for digest, data in todo.items():
            src = Path(tmp) / f"{digest}.docx"
            src.write_bytes(data)
            srcs.append(src)
        _run_soffice(cfg, srcs, tmp)
        for src in srcs:
            out = src.with_suffix(".pdf")
            if out.is_file():
                (cache / out.name).write_bytes(out.read_bytes())
    logger.info("Batch-converted %d DOCX files into %s", len(todo), cache)
    return len(todo)


def _docx_to_pdf(data: bytes, cfg: Config) -> bytes:
    digest = hashlib.sha256(data).hexdigest()
    cached: Path | None = None
    if cfg.docx_pdf_cache_dir is not None:
        cached = Path(cfg.docx_pdf_cache_dir) / f"{digest}.pdf"
        if cached.is_file():
            return cached.read_bytes()

    with tempfile.TemporaryDirectory(prefix="docx2pdf_") as tmp:
        src = Path(tmp) / f"{digest}.docx"
        src.write_bytes(data)
        _run_soffice(cfg, [src], tmp)
        out = src.with_suffix(".pdf")
        if not out.is_file():
            raise ExtractionError("soffice produced no PDF")
        pdf_bytes = out.read_bytes()

    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(pdf_bytes)
    return pdf_bytes


def extract_page(data: bytes, content_type: str, cfg: Config | None = None) -> PageFeatures:
    """Extract first-page features from raw bytes. Raises ExtractionError on failure."""
    cfg = cfg or Config()
    ct = (content_type or "").lower().split(";")[0].strip()
    try:
        if ct == "application/pdf":
            return _extract_pdf(data, cfg)
        if ct in IMAGE_TYPES:
            return _extract_image(data, cfg)
        if ct == DOCX_TYPE:
            feats = _extract_pdf(_docx_to_pdf(data, cfg), cfg)
            feats.meta["converted_from"] = "docx"
            return feats
        raise ExtractionError(f"Unsupported content type: {content_type!r}")
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - wrap everything so callers have one error type
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc
