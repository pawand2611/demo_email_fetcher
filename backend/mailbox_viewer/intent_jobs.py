"""Intent tier 2 as background work: resolve the emails tier 1 was unsure about.

The sync stores such an email at once with ``tier2_status = "pending"`` and
the cleaned email state it classified. This module drains that queue, which is
just the ``email_intents`` table: one DeepSeek call per email (about 100 s on
CPU), each outcome written in its own short transaction, so a crash loses at
most the email in flight and the UI keeps reading while it runs.

The API runs it as a job (``POST /intents/tier2``) through the same
one-at-a-time job manager as sync; ``sync_mail.py --intents-tier2`` runs it
from the command line. ``limit`` bounds one run so a long queue does not block
syncs for hours; run it again for the rest.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from . import repository as repo
from .intent import DECIDED_TIER2, IntentTier2Model, decide_after_tier2, log_decision

logger = logging.getLogger(__name__)


def resolve_pending_intents(
    session_factory: sessionmaker[Session],
    tier2: IntentTier2Model,
    limit: int,
    retry_failed: bool = False,
) -> dict[str, int]:
    """Run tier 2 on up to ``limit`` waiting emails. ``retry_failed`` also
    retries emails whose earlier tier-2 call timed out or found no server.
    Never raises for a single email: tier 2 reports failures as a status."""
    with session_factory() as session:
        waiting = repo.pending_intents(session, limit, retry_failed)

    counts = {"processed": 0, "tier2": 0, "fallback": 0, "skipped": 0}
    for item in waiting:
        result = tier2.classify(item.state)  # outside any transaction: it can take minutes
        final = decide_after_tier2(item.decision, result)
        with session_factory() as session, session.begin():
            stored = repo.complete_intent(session, item.email_id, final, item.state)
        if not stored:  # deleted, or finished by someone else meanwhile
            counts["skipped"] += 1
            continue
        log_decision(item.message_id, final)
        counts["processed"] += 1
        counts["tier2" if final.decided_by == DECIDED_TIER2 else "fallback"] += 1

    with session_factory() as session:
        counts["remaining"] = repo.count_pending_intents(session, retry_failed)
    logger.info("intent tier 2: %d processed (%d by tier 2, %d fell back to tier 1), %d still waiting",
                counts["processed"], counts["tier2"], counts["fallback"], counts["remaining"])
    return counts
