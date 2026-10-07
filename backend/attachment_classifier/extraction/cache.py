"""Optional on-disk cache of PageFeatures keyed by sha256(data).

Layout: <cache_dir>/<sha256>.json (words, boxes, flags) + <cache_dir>/<sha256>.png (image).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from PIL import Image

from .extract import PageFeatures

logger = logging.getLogger(__name__)


def content_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FeatureCache:
    def __init__(self, cache_dir: Path | str) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.dir / f"{key}.json", self.dir / f"{key}.png"

    def image_path(self, key: str) -> Path:
        """Path of the cached page PNG (lets datasets load images lazily instead of holding them in RAM)."""
        return self._paths(key)[1]

    def get(self, key: str) -> PageFeatures | None:
        meta_path, img_path = self._paths(key)
        if not (meta_path.is_file() and img_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            image = Image.open(img_path)
            image.load()
            return PageFeatures(words=meta["words"], boxes=meta["boxes"], image=image.convert("RGB"),
                                has_text_layer=bool(meta["has_text_layer"]),
                                page_count=int(meta["page_count"]), meta=meta.get("meta", {}))
        except Exception as exc:  # noqa: BLE001 - a corrupt cache entry must not kill the run
            logger.warning("Corrupt cache entry %s (%s); ignoring", key, exc)
            return None

    def put(self, key: str, feats: PageFeatures) -> None:
        meta_path, img_path = self._paths(key)
        tmp_meta = meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps({
            "words": feats.words, "boxes": feats.boxes, "has_text_layer": feats.has_text_layer,
            "page_count": feats.page_count, "meta": feats.meta}), encoding="utf-8")
        feats.image.save(img_path, format="PNG")
        tmp_meta.replace(meta_path)
