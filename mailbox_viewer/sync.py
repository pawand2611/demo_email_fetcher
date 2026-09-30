"""The sync run: how data moves on one refresh.

1. Read the bookmark (``sync_state``) and fetch the UIDs above it.
2. **Already judged?** ``decision_log`` is checked before anything else, so a
   rescan never re-judges a message.
3. **Find the thread.** A reply into a thread already flagged as payment is
   kept before classification even runs.
4. **Classify** tiers 1 to 3 and write the decision line.
5. **Keep?** A payment document, or mail in a payment thread, is saved in one
   transaction: thread, email, participants, attachments; files go to the
   store first. When a thread turns into a payment thread, its earlier mail
   that was dropped is backfilled from the mailbox. Anything else is dropped:
   only the log line remains. ``STORE_ONLY_PAYMENT=false`` keeps everything.
6. Move the bookmark.

Idempotent by construction; incremental by UID.
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
from .classifier import Decision, MailFacts, classify
from .config import Settings
from .mail_client import MailClient, MailClientError, MailConnectionLost
from .mail_parser import ParsedEmail, parse_message
from .threads import ancestor_ids

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

MAX_BACKFILL_PER_MESSAGE = 20


class MailSource(Protocol):
    """What the sync needs from a mail client. ``MailClient`` satisfies it;
    tests plug in a fake."""

    uidvalidity: int | None

    def __enter__(self) -> "MailSource": ...
    def __exit__(self, *exc: object) -> None: ...
    def search_uids(self, after_uid: int | None = None) -> list[int]: ...
    def fetch_message(self, uid: int): ...
    def find_uid_by_message_id(self, message_id: str) -> int | None: ...


ClientFactory = Callable[[Settings], MailSource]


@dataclass
class SyncResult:
    status: str
    candidates: int = 0
    kept: int = 0
    payment_hits: int = 0
    dropped: int = 0
    skipped: int = 0
    backfilled: int = 0
    failed: int = 0
    error: str | None = None
    last_seen_uid: int = 0
    full_rewalk: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def summary(self) -> str:
        text = (
            f"{self.kept} kept ({self.payment_hits} payment documents, {self.backfilled} backfilled), "
            f"{self.dropped} dropped, {self.skipped} already judged, {self.failed} failed"
        )
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

            processor = _Processor(client, session_factory, store, settings.store_only_payment, result)
            for uid in uids:  # ascending, so last_seen_uid only ever moves forward
                processor.process(uid)
                result.last_seen_uid = max(result.last_seen_uid, uid)

    except MailConnectionLost as exc:
        result.status, result.error = STATUS_PARTIAL, str(exc)
        logger.warning("sync interrupted: %s", exc)
    except (MailClientError, AttachmentStoreError) as exc:
        result.status, result.error = STATUS_FAILED, str(exc)
        logger.error("sync failed: %s", exc)

    # The bookmark is read first and moved last.
    with session_factory() as session, session.begin():
        repo.save_sync_state(session, folder, uid_validity=uid_validity, last_seen_uid=result.last_seen_uid)

    result.duration_seconds = round(time.monotonic() - started, 2)
    logger.info("sync finished: %s in %.1fs", result.summary(), result.duration_seconds)
    return result


def _resume_point(state: repo.SyncStateInfo | None, server_uidvalidity: int | None) -> tuple[int | None, bool]:
    if state is None or not state.last_seen_uid:
        return None, False
    if state.uid_validity is not None and state.uid_validity != server_uidvalidity:
        logger.warning("UIDVALIDITY changed %s -> %s; re-walking folder", state.uid_validity, server_uidvalidity)
        return None, True
    return state.last_seen_uid, False


class _Processor:
    """Steps 2 to 5 of the refresh for one message at a time."""

    def __init__(self, client: MailSource, session_factory, store: FileSystemStore, store_only_payment: bool, result: SyncResult) -> None:
        self.client = client
        self.session_factory = session_factory
        self.store = store
        self.store_only_payment = store_only_payment
        self.result = result

    def process(self, uid: int) -> None:
        try:
            raw, internal_date = self.client.fetch_message(uid)
            mail = parse_message(uid, raw, internal_date)
        except MailConnectionLost:
            raise
        except Exception as exc:  # malformed message or a single failed fetch
            logger.warning("UID %s skipped: %s: %s", uid, type(exc).__name__, exc)
            self.result.failed += 1
            return

        try:
            outcome = self._judge_and_store(mail)
        except MailConnectionLost:
            raise
        except IntegrityError:
            logger.info("UID %s already stored (concurrent insert)", uid)
            outcome = "skipped"
        except AttachmentStoreError as exc:
            logger.error("UID %s: attachment store rejected a file: %s", uid, exc)
            outcome = "failed"
        except Exception as exc:
            logger.error("UID %s could not be persisted: %s: %s", uid, type(exc).__name__, exc)
            outcome = "failed"

        if outcome == "skipped":
            self.result.skipped += 1
        elif outcome == "dropped":
            self.result.dropped += 1
        elif outcome == "failed":
            self.result.failed += 1
        else:
            self.result.kept += 1
            if outcome == "payment":
                self.result.payment_hits += 1

    def _judge_and_store(self, mail: ParsedEmail) -> str:
        # Step 2: already judged?
        with self.session_factory() as session:
            if repo.is_judged(session, mail.message_id) or repo.find_email_id(session, mail.message_id) is not None:
                return "skipped"
            # Step 3: find the thread; a payment thread keeps its replies.
            thread_id = repo.find_thread_id(session, mail)
            in_payment_thread = thread_id is not None and repo.thread_has_payment(session, thread_id)

        # Step 4: classify (rules are cheap, so every message gets a proper decision line).
        decision = classify(MailFacts.from_parsed(mail))
        if in_payment_thread and not decision.is_payment:
            decision = Decision(
                decision.doc_type, decision.tier, decision.confidence, decision.decision,
                decision.reason + "; kept as part of a payment thread", decision.attachments,
            )

        # Step 5: keep?
        keep = decision.is_payment or in_payment_thread or not self.store_only_payment
        if not keep:
            with self.session_factory() as session, session.begin():
                repo.log_decision(session, mail.message_id, decision)
            return "dropped"

        self._store(mail, decision)
        if decision.is_payment and self.store_only_payment:
            self._backfill_ancestors(mail)
        return "payment" if decision.is_payment else "kept"

    def _store(self, mail: ParsedEmail, decision: Decision) -> None:
        # Files go to the store first, between transactions, so a slow disk
        # never holds the database write lock while the UI is reading.
        blob_keys = [self.store.put(sha256=a.sha256, filename=a.filename, data=a.data) for a in mail.attachments]
        with self.session_factory() as session, session.begin():
            repo.insert_email(session, mail, decision, blob_keys)

    def _backfill_ancestors(self, mail: ParsedEmail) -> None:
        """Earlier mail of a thread that has just become a payment thread was
        dropped (only judged). Fetch it again from the mailbox and store it."""
        for ancestor in ancestor_ids(mail)[:MAX_BACKFILL_PER_MESSAGE]:
            with self.session_factory() as session:
                if repo.find_email_id(session, ancestor) is not None:
                    continue
            try:
                uid = self.client.find_uid_by_message_id(ancestor)
                if uid is None:
                    continue
                raw, internal_date = self.client.fetch_message(uid)
                earlier = parse_message(uid, raw, internal_date)
                earlier_decision = classify(MailFacts.from_parsed(earlier))
                if not earlier_decision.is_payment:
                    earlier_decision = Decision(
                        earlier_decision.doc_type, earlier_decision.tier, earlier_decision.confidence,
                        earlier_decision.decision, earlier_decision.reason + "; backfilled into a payment thread",
                        earlier_decision.attachments,
                    )
                self._store(earlier, earlier_decision)
                self.result.backfilled += 1
            except MailConnectionLost:
                raise
            except IntegrityError:
                continue
            except Exception as exc:
                logger.warning("backfill of %s failed: %s: %s", ancestor, type(exc).__name__, exc)
