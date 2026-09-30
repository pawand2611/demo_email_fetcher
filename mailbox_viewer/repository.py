"""All database reads and writes for the six-table model, one function per
responsibility.

Functions take an open :class:`Session` and return plain dataclasses, never
ORM instances, so callers (the sync loop, the Streamlit pages) do not have to
care about session lifetimes or lazy loading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .attachment_store import AttachmentStoreError, FileSystemStore
from .classifier import AttachmentFacts, Decision, MailFacts, classify
from .mail_parser import ParsedEmail
from .models import Attachment, DecisionLog, Email, EmailParticipant, SyncState, Thread, to_naive_utc, utcnow_naive
from .threads import ancestor_ids, conversation_key

# -- read models -----------------------------------------------------------------


@dataclass(frozen=True)
class ThreadSummary:
    id: int
    conversation_key: str
    subject: str
    message_count: int
    last_message_at: datetime | None
    has_payment: bool
    participants: str  # "Name, Name, …" of distinct senders, for the list
    attachment_count: int


@dataclass(frozen=True)
class ParticipantInfo:
    role: str
    name: str
    address: str

    @property
    def display(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address


@dataclass(frozen=True)
class AttachmentInfo:
    id: int
    filename: str
    content_type: str
    size_bytes: int
    blob_key: str
    doc_type: str | None
    confidence: float | None


@dataclass(frozen=True)
class EmailSummary:
    id: int
    thread_id: int
    message_id: str
    subject: str
    sender: ParticipantInfo | None
    received_at: datetime | None
    has_attachments: bool
    attachment_count: int
    matched_directly: bool
    doc_type: str | None
    confidence: float | None
    decision_reason: str | None


@dataclass(frozen=True)
class EmailDetail:
    id: int
    thread_id: int
    message_id: str
    in_reply_to: str | None
    references_header: str | None
    subject: str
    body_text: str
    received_at: datetime | None
    created_at: datetime
    has_attachments: bool
    matched_directly: bool
    doc_type: str | None
    confidence: float | None
    decision_reason: str | None
    participants: tuple[ParticipantInfo, ...]
    attachments: tuple[AttachmentInfo, ...]

    def by_role(self, role: str) -> list[ParticipantInfo]:
        return [p for p in self.participants if p.role == role]


@dataclass(frozen=True)
class AttachmentFile:
    info: AttachmentInfo
    content: bytes | None
    error: str | None = None


@dataclass(frozen=True)
class DecisionInfo:
    message_id: str
    tier: int
    decision: str
    confidence: float | None
    reason: str | None
    decided_at: datetime


@dataclass(frozen=True)
class SyncStateInfo:
    folder_name: str
    uid_validity: int | None
    last_seen_uid: int
    last_sync_at: datetime | None


# -- writes ------------------------------------------------------------------------


def find_email_id(session: Session, message_id: str) -> int | None:
    return session.scalar(select(Email.id).where(Email.message_id == message_id))


def is_judged(session: Session, message_id: str) -> bool:
    """True once a decision has been logged for this message, kept or dropped."""
    return session.get(DecisionLog, message_id) is not None


def find_thread_id(session: Session, mail: ParsedEmail) -> int | None:
    """The stored thread this mail would join, by conversation key or ancestor, or None."""
    key = conversation_key(mail)
    thread_id = session.scalar(select(Thread.id).where(Thread.conversation_key == key))
    if thread_id is not None:
        return thread_id
    for ancestor in reversed(ancestor_ids(mail)):
        thread_id = session.scalar(select(Email.thread_id).where(Email.message_id == ancestor))
        if thread_id is not None:
            return thread_id
    return None


def thread_has_payment(session: Session, thread_id: int) -> bool:
    return bool(session.scalar(select(Thread.has_payment).where(Thread.id == thread_id)))


def log_decision(session: Session, message_id: str, decision: Decision) -> None:
    """Write the audit line for a message that is not being stored."""
    _log_decision(session, message_id, decision)
    session.flush()


def insert_email(session: Session, mail: ParsedEmail, decision: Decision, blob_keys: Sequence[str]) -> int:
    """Insert one parsed mail: thread (found or created), participants,
    attachments (metadata + blob key), decision log, thread roll-ups.

    ``blob_keys`` is one key per attachment, already written to the store.
    The caller owns the transaction; a UNIQUE violation on ``message_id``
    surfaces as ``IntegrityError`` and means another writer got there first.
    """
    if len(blob_keys) != len(mail.attachments):
        raise ValueError("one blob key is required per attachment")

    thread = _thread_for(session, mail)
    received = to_naive_utc(mail.received_at)

    row = Email(
        thread=thread,
        message_id=mail.message_id,
        in_reply_to=mail.in_reply_to,
        references_header=mail.references_header,
        subject=mail.subject,
        body_text=mail.body_text,
        received_at=received,
        has_attachments=mail.has_attachments,
        matched_directly=decision.is_payment,
        doc_type=decision.doc_type,
        confidence=decision.confidence,
        decision_reason=decision.reason,
        created_at=utcnow_naive(),
    )
    for p in mail.participants:
        row.participants.append(EmailParticipant(role=p.role, name=p.name, address=p.address))
    for att, key, verdict in zip(mail.attachments, blob_keys, decision.attachments):
        row.attachments.append(
            Attachment(
                filename=att.filename,
                content_type=att.content_type,
                size_bytes=att.size_bytes,
                blob_key=key,
                doc_type=verdict.doc_type,
                confidence=verdict.confidence,
            )
        )
    session.add(row)

    thread.message_count += 1
    if received and (thread.last_message_at is None or received > thread.last_message_at):
        thread.last_message_at = received
    if not thread.subject and mail.subject:
        thread.subject = mail.subject
    if decision.is_payment:
        thread.has_payment = True

    _log_decision(session, mail.message_id, decision)
    session.flush()
    return row.id


def _thread_for(session: Session, mail: ParsedEmail) -> Thread:
    """Find the thread this mail belongs to, or start one."""
    key = conversation_key(mail)
    thread = session.scalar(select(Thread).where(Thread.conversation_key == key))
    if thread is not None:
        return thread
    # The root may not be cached (older than the sync window), but a nearer
    # ancestor might be: join its thread instead of starting a new one.
    for ancestor in reversed(ancestor_ids(mail)):
        thread_id = session.scalar(select(Email.thread_id).where(Email.message_id == ancestor))
        if thread_id is not None:
            return session.get_one(Thread, thread_id)
    thread = Thread(conversation_key=key, subject=mail.subject, message_count=0, has_payment=False)
    session.add(thread)
    session.flush()
    return thread


def _log_decision(session: Session, message_id: str, decision: Decision) -> None:
    log = session.get(DecisionLog, message_id)
    if log is None:
        log = DecisionLog(message_id=message_id)
        session.add(log)
    log.tier = decision.tier
    log.decision = decision.decision
    log.confidence = decision.confidence
    log.reason = decision.reason
    log.decided_at = utcnow_naive()


def reclassify_all(session: Session) -> int:
    """Re-run the rules over every cached mail using only stored columns.
    Updates emails, attachments, decision_log and thread roll-ups. Returns
    how many emails changed."""
    changed = 0
    rows = session.scalars(select(Email).options(selectinload(Email.attachments))).all()
    for row in rows:
        facts = MailFacts(
            subject=row.subject,
            body_text=row.body_text,
            attachments=tuple(AttachmentFacts(a.filename, a.content_type) for a in row.attachments),
        )
        decision = classify(facts)
        before = (row.doc_type, row.confidence, row.decision_reason, row.matched_directly)
        row.doc_type = decision.doc_type
        row.confidence = decision.confidence
        row.decision_reason = decision.reason
        row.matched_directly = decision.is_payment
        for att, verdict in zip(row.attachments, decision.attachments):
            att.doc_type = verdict.doc_type
            att.confidence = verdict.confidence
        _log_decision(session, row.message_id, decision)
        if before != (row.doc_type, row.confidence, row.decision_reason, row.matched_directly):
            changed += 1
    session.flush()
    _recompute_thread_flags(session)
    return changed


def _recompute_thread_flags(session: Session) -> None:
    matched = select(Email.thread_id).where(Email.matched_directly.is_(True)).distinct()
    payment_ids = set(session.scalars(matched).all())
    for thread in session.scalars(select(Thread)):
        thread.has_payment = thread.id in payment_ids
    session.flush()


# -- reads: threads ----------------------------------------------------------------


def list_threads(
    session: Session,
    *,
    payment_only: bool = False,
    search: str | None = None,
    limit: int = 1000,
) -> list[ThreadSummary]:
    stmt = select(Thread)
    if payment_only:
        stmt = stmt.where(Thread.has_payment.is_(True))
    if search:
        pattern = f"%{search.strip()}%"
        matching_emails = (
            select(Email.thread_id)
            .join(EmailParticipant, EmailParticipant.email_id == Email.id, isouter=True)
            .where(
                or_(
                    Email.subject.ilike(pattern),
                    EmailParticipant.name.ilike(pattern),
                    EmailParticipant.address.ilike(pattern),
                )
            )
        )
        stmt = stmt.where(Thread.id.in_(matching_emails))
    stmt = stmt.order_by(Thread.last_message_at.desc().nulls_last(), Thread.id.desc()).limit(limit)
    threads = session.scalars(stmt).all()
    if not threads:
        return []

    ids = [t.id for t in threads]
    senders = session.execute(
        select(Email.thread_id, EmailParticipant.name, EmailParticipant.address)
        .join(EmailParticipant, EmailParticipant.email_id == Email.id)
        .where(Email.thread_id.in_(ids), EmailParticipant.role == "from")
        .order_by(Email.received_at)
    ).all()
    sender_names: dict[int, list[str]] = {}
    for thread_id, name, address in senders:
        label = name or address
        bucket = sender_names.setdefault(thread_id, [])
        if label not in bucket:
            bucket.append(label)
    attachment_counts = dict(
        session.execute(
            select(Email.thread_id, func.count(Attachment.id))
            .join(Attachment, Attachment.email_id == Email.id)
            .where(Email.thread_id.in_(ids))
            .group_by(Email.thread_id)
        ).all()
    )
    return [
        ThreadSummary(
            id=t.id,
            conversation_key=t.conversation_key,
            subject=t.subject,
            message_count=t.message_count,
            last_message_at=t.last_message_at,
            has_payment=t.has_payment,
            participants=", ".join(sender_names.get(t.id, [])),
            attachment_count=attachment_counts.get(t.id, 0),
        )
        for t in threads
    ]


def list_thread_emails(session: Session, thread_id: int) -> list[EmailSummary]:
    rows = session.scalars(
        select(Email)
        .options(selectinload(Email.participants), selectinload(Email.attachments))
        .where(Email.thread_id == thread_id)
        .order_by(Email.received_at.desc().nulls_last(), Email.id.desc())
    ).all()
    return [_email_summary(r) for r in rows]


def _email_summary(row: Email) -> EmailSummary:
    sender = next((p for p in row.participants if p.role == "from"), None)
    return EmailSummary(
        id=row.id,
        thread_id=row.thread_id,
        message_id=row.message_id,
        subject=row.subject,
        sender=ParticipantInfo(sender.role, sender.name, sender.address) if sender else None,
        received_at=row.received_at,
        has_attachments=row.has_attachments,
        attachment_count=len(row.attachments),
        matched_directly=row.matched_directly,
        doc_type=row.doc_type,
        confidence=row.confidence,
        decision_reason=row.decision_reason,
    )


# -- reads: emails -----------------------------------------------------------------


def get_email_detail(session: Session, email_id: int) -> EmailDetail | None:
    row = session.get(Email, email_id, options=[selectinload(Email.participants), selectinload(Email.attachments)])
    if row is None:
        return None
    return EmailDetail(
        id=row.id,
        thread_id=row.thread_id,
        message_id=row.message_id,
        in_reply_to=row.in_reply_to,
        references_header=row.references_header,
        subject=row.subject,
        body_text=row.body_text,
        received_at=row.received_at,
        created_at=row.created_at,
        has_attachments=row.has_attachments,
        matched_directly=row.matched_directly,
        doc_type=row.doc_type,
        confidence=row.confidence,
        decision_reason=row.decision_reason,
        participants=tuple(ParticipantInfo(p.role, p.name, p.address) for p in row.participants),
        attachments=tuple(_attachment_info(a) for a in row.attachments),
    )


def get_attachment_files(session: Session, email_id: int, store: FileSystemStore) -> list[AttachmentFile]:
    """Attachments of one mail with their bytes from the store. A missing file
    is reported per attachment instead of failing the list."""
    rows = session.scalars(select(Attachment).where(Attachment.email_id == email_id).order_by(Attachment.id))
    files: list[AttachmentFile] = []
    for att in rows:
        info = _attachment_info(att)
        try:
            files.append(AttachmentFile(info=info, content=store.get(att.blob_key)))
        except AttachmentStoreError as exc:
            files.append(AttachmentFile(info=info, content=None, error=str(exc)))
    return files


def get_decision(session: Session, message_id: str) -> DecisionInfo | None:
    row = session.get(DecisionLog, message_id)
    if row is None:
        return None
    return DecisionInfo(row.message_id, row.tier, row.decision, row.confidence, row.reason, row.decided_at)


def _attachment_info(att: Attachment) -> AttachmentInfo:
    return AttachmentInfo(
        id=att.id,
        filename=att.filename,
        content_type=att.content_type,
        size_bytes=att.size_bytes,
        blob_key=att.blob_key,
        doc_type=att.doc_type,
        confidence=att.confidence,
    )


# -- counts ------------------------------------------------------------------------


def count_threads(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Thread)) or 0


def count_emails(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Email)) or 0


def count_attachments(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Attachment)) or 0


def count_payment_threads(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Thread).where(Thread.has_payment.is_(True))) or 0


def count_decisions(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(DecisionLog)) or 0


def count_by_doc_type(session: Session) -> dict[str, int]:
    rows = session.execute(select(Email.doc_type, func.count()).group_by(Email.doc_type))
    return {(doc_type or "none"): count for doc_type, count in rows}


def count_by_decision(session: Session) -> dict[str, int]:
    rows = session.execute(select(DecisionLog.decision, func.count()).group_by(DecisionLog.decision))
    return {decision: count for decision, count in rows}


# -- sync state --------------------------------------------------------------------


def get_sync_state(session: Session, folder_name: str) -> SyncStateInfo | None:
    row = session.get(SyncState, folder_name)
    return SyncStateInfo(row.folder_name, row.uid_validity, row.last_seen_uid, row.last_sync_at) if row else None


def save_sync_state(session: Session, folder_name: str, *, uid_validity: int | None, last_seen_uid: int) -> SyncStateInfo:
    row = session.get(SyncState, folder_name)
    if row is None:
        row = SyncState(folder_name=folder_name)
        session.add(row)
    row.uid_validity = uid_validity
    row.last_seen_uid = last_seen_uid
    row.last_sync_at = utcnow_naive()
    session.flush()
    return SyncStateInfo(row.folder_name, row.uid_validity, row.last_seen_uid, row.last_sync_at)


def delete_sync_state(session: Session, folder_name: str) -> bool:
    row = session.get(SyncState, folder_name)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
