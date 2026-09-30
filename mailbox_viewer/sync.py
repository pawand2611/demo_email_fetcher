"""The sync run: fetch what is new from IMAP, de-duplicate, persist, record state.

Idempotent by construction: running it twice in a row changes nothing the
second time. Incremental by UID: after the first run only messages with a
UID above ``sync_state.last_seen_uid`` are ever requested from the server.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import repository as repo
from .attachment_store import AttachmentStoreError, FileSystemStore, build_store
from .classifier import MailFacts, classify
from .config import Settings
from .mail_client import MailClient, MailClientError, MailConnectionLost
from .mail_parser import parse_message

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


class MailSource(Protocol):
    """What the sync needs from a mail client. ``MailClient`` satisfies it;
    tests plug in a fake."""

    uidvalidity: int | None

    def __enter__(self) -> "MailSource": ...
    def __exit__(self, *exc: object) -> None: ...
    def search_uids(self, after_uid: int | None = None) -> list[int]: ...
    def fetch_message(self, uid: int): ...


ClientFactory = Callable[[Settings], MailSource]


@dataclass
class SyncResult:
    status: str
    candidates: int = 0
    new: int = 0
    skipped: int = 0
    failed: int = 0
    payment_hits: int = 0
    error: str | None = None
    last_seen_uid: int = 0
    full_rewalk: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def summary(self) -> str:
        text = f"{self.new} new ({self.payment_hits} payment), {self.skipped} already cached, {self.failed} failed"
        if self.error:
            text += f" ({self.status}: {self.error})"
        return text


def run_sync(
    settings: Settings,
    session_factory: sessionmaker[Session],
    client_factory: ClientFactory = MailClient,
    store: FileSystemStore | None = None,
) -> SyncResult:
    started = time.monotonic()
    folder = settings.imap_folder
    result = SyncResult(status=STATUS_OK)
    store = store or build_store(settings)

    with session_factory() as session:
        state = repo.get_sync_state(session, folder)

    uid_validity = state.uid_validity if state else None
    result.last_seen_uid = state.last_seen_uid if state else 0

    try:
        with client_factory(settings) as client:
            uid_validity = client.uidvalidity
            after_uid, result.full_rewalk = _resume_point(state, client.uidvalidity)
            if result.full_rewalk:
                result.last_seen_uid = 0

            uids = client.search_uids(after_uid)
            if after_uid is None:
                uids = uids[-settings.initial_fetch_limit :]
            result.candidates = len(uids)
            logger.info(
                "sync %s: %d candidate(s), resuming after UID %s%s",
                folder, len(uids), after_uid, " (full re-walk)" if result.full_rewalk else "",
            )

            for uid in uids:  # ascending, so last_seen_uid only ever moves forward
                outcome = _process_uid(client, uid, session_factory, store)
                if outcome == "new":
                    result.new += 1
                elif outcome == "payment":
                    result.new += 1
                    result.payment_hits += 1
                elif outcome == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                result.last_seen_uid = max(result.last_seen_uid, uid)

    except MailConnectionLost as exc:
        result.status, result.error = STATUS_PARTIAL, str(exc)
        logger.warning("sync interrupted: %s", exc)
    except (MailClientError, AttachmentStoreError) as exc:
        result.status, result.error = STATUS_FAILED, str(exc)
        logger.error("sync failed: %s", exc)

    with session_factory() as session, session.begin():
        repo.save_sync_state(session, folder, uid_validity=uid_validity, last_seen_uid=result.last_seen_uid)

    result.duration_seconds = round(time.monotonic() - started, 2)
    logger.info("sync finished: %s in %.1fs", result.summary(), result.duration_seconds)
    return result


def _resume_point(state: repo.SyncStateInfo | None, server_uidvalidity: int | None) -> tuple[int | None, bool]:
    """Return (after_uid, full_rewalk). ``after_uid`` is ``None`` for a first
    sync or when the server renumbered the folder (UIDVALIDITY changed), in
    which case every UID is a candidate again and de-duplication by
    Message-ID does the filtering."""
    if state is None or not state.last_seen_uid:
        return None, False
    if state.uid_validity is not None and state.uid_validity != server_uidvalidity:
        logger.warning("UIDVALIDITY changed %s -> %s; re-walking folder", state.uid_validity, server_uidvalidity)
        return None, True
    return state.last_seen_uid, False


def _process_uid(client: MailSource, uid: int, session_factory: sessionmaker[Session], store: FileSystemStore) -> str:
    """Fetch, parse, classify, store attachments and persist one message.

    Returns ``"new"``, ``"payment"`` (new and recognised as a payment
    document), ``"skipped"`` or ``"failed"``. Only a lost connection
    propagates, because nothing after it can succeed either.
    """
    try:
        raw, internal_date = client.fetch_message(uid)
        mail = parse_message(uid, raw, internal_date)
        decision = classify(MailFacts.from_parsed(mail))
    except MailConnectionLost:
        raise
    except Exception as exc:  # malformed message or a single failed fetch
        logger.warning("UID %s skipped: %s: %s", uid, type(exc).__name__, exc)
        return "failed"

    try:
        with session_factory() as session:
            if repo.find_email_id(session, mail.message_id) is not None:
                return "skipped"

        # Files are written between transactions so a slow disk never holds
        # the database write lock while the UI is reading.
        blob_keys = [store.put(sha256=a.sha256, filename=a.filename, data=a.data) for a in mail.attachments]

        with session_factory() as session, session.begin():
            repo.insert_email(session, mail, decision, blob_keys)
        return "payment" if decision.is_payment else "new"
    except IntegrityError:
        logger.info("UID %s already cached (concurrent insert)", uid)
        return "skipped"
    except AttachmentStoreError as exc:
        logger.error("UID %s: attachment store rejected a file: %s", uid, exc)
        return "failed"
    except Exception as exc:
        logger.error("UID %s could not be persisted: %s: %s", uid, type(exc).__name__, exc)
        return "failed"
