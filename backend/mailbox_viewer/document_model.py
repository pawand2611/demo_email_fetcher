"""Loading the trained document model.

The classifier talks to any object with ``name`` and ``predict(attachment)``
(see :class:`mailbox_viewer.classifier.DocumentModel`). This module decides
which one the backend uses:

* ``MODEL_PATH`` unset -> :class:`~mailbox_viewer.classifier.NoModel`;
  attachments stay unclassified and every message is still stored.
* ``MODEL_PATH`` set   -> the LayoutLMv3 adapter, to be added when the trained
  model is brought into this repository.

The adapter must load strictly from the local folder in ``MODEL_PATH`` with
Hugging Face offline mode and telemetry disabled (``HF_HUB_OFFLINE=1``,
``HF_HUB_DISABLE_TELEMETRY=1``, ``local_files_only=True``) so nothing contacts
the internet at start-up. Model weights stay outside git.
"""

from __future__ import annotations

from .classifier import DocumentModel, NoModel
from .config import ConfigError, Settings


def load_model(settings: Settings) -> DocumentModel:
    if not settings.model_path:
        return NoModel()
    raise ConfigError(
        f"MODEL_PATH is set to {settings.model_path!r}, but the LayoutLMv3 adapter is not wired in yet. "
        "Leave MODEL_PATH empty until the trained model is added to the project."
    )
