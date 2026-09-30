"""Alias kept for scripts and pages that import the model by this name.

The six-table model is now the live model in :mod:`mailbox_viewer.models`.
"""

from .models import (  # noqa: F401
    DECISIONS,
    DOC_TYPES,
    PARTICIPANT_ROLES,
    Attachment,
    Base,
    DecisionLog,
    Email,
    EmailParticipant,
    SyncState,
    Thread,
    utcnow_naive,
)
