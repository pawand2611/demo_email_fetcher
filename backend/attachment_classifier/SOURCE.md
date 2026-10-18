# Vendored inference code

Copied unchanged from https://github.com/RoctorpalSandilya/invoice-classifier-layoutlmv3
(commit 7aaf27e, "Add v2 model: full fine-tune on a broader, format-balanced dataset"),
folder `attachment_classifier/`: `config.py`, `extraction/`, `inference/`.
Training, dataset and CLI code stay in that repository. Update by copying the same files
again from a newer commit; do not edit them here.

The trained weights are not in git. They live in `backend/models/layoutlmv3_invoice/`
(`ATTACHMENT_MODEL_DIR`), downloaded from that repository's Git LFS.

| Model | Source in that repository | Weights sha256 |
|---|---|---|
| v2 (in use since 2026-10-14) | `artifacts/run_full_v2/model`, full fine-tune, 3,800 documents | `5edafbb70e9f10648ea837ffb809a4501fc83812c0f745fcb35d9ea3b2d79297` |
| v1 (previous) | `artifacts/run_frozen_3ep/model`, frozen backbone, 1,400 documents | `275ba716…` |

History: v1 code and weights came from commit 6c9fecf.
