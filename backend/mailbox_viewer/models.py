"""Data model: the six mail tables (threads, emails, email_participants,
attachments, sync_state, decision_log) plus two for aging tickets (tickets,
ticket_events).

Ownership:

* ``threads``            one row per conversation, keyed by the root Message-ID.
                         ``has_payment`` flips when any message in it matches.
* ``emails``             one row per message, pointing at its thread.
                         ``message_id`` is unique: the de-duplication guarantee.
* ``email_participants`` one row per person per role (from / to / cc / bcc).
* ``attachments``        metadata tied to the message it arrived on; bytes live
                         outside the database and ``blob_key`` points at them.
* ``sync_state``         the bookmark: where the next incremental sync resumes.
* ``decision_log``       the audit trail: what the classifier decided and why.
* ``tickets``            one per payment thread: open, in progress or closed,
                         with when it opened and what closed it.
* ``ticket_events``      every status change of a ticket, automatic or manual.
* ``email_intents``      what a payment email is for (new invoice, reminder,
                         confirmation, ...), which tier decided, and whether a
                         human should review it.

Datetimes are naive UTC (SQLite has no timezone-aware type; PostgreSQL gets
TIMESTAMP WITHOUT TIME ZONE). The same models compile to both dialects.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .intent import DECIDED_BY, INTENT_KEYS, TIER2_STATUSES

PARTICIPANT_ROLES = ("from", "to", "cc", "bcc")
DOC_TYPES = ("statement", "invoice", "receipt", "other")
DECISIONS = ("payment", "none", "review")
TICKET_STATUSES = ("open", "in_progress", "closed")
TICKET_EVENTS = ("opened", "in_progress", "closed", "reopened")
TICKET_ACTORS = ("auto", "manual")


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class Base(DeclarativeBase):
    pass


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_key: Mapped[str] = mapped_column(String(998), unique=True, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    has_payment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)

    emails: Mapped[list[Email]] = relationship(back_populates="thread", order_by="Email.received_at")


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_emails_confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(998), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references_header: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_directly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    doc_type: Mapped[str | None] = mapped_column(String(32), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)

    thread: Mapped[Thread] = relationship(back_populates="emails")
    participants: Mapped[list[EmailParticipant]] = relationship(
        back_populates="email", cascade="all, delete-orphan", order_by="EmailParticipant.id"
    )
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="email", cascade="all, delete-orphan", order_by="Attachment.id"
    )
    intent: Mapped[EmailIntent | None] = relationship(
        back_populates="email", cascade="all, delete-orphan", uselist=False
    )


class EmailParticipant(Base):
    __tablename__ = "email_participants"
    __table_args__ = (
        CheckConstraint("role IN ('from', 'to', 'cc', 'bcc')", name="ck_email_participants_role"),
        Index("ix_email_participants_email_role", "email_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(4), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    address: Mapped[str] = mapped_column(String(320), nullable=False, index=True)

    email: Mapped[Email] = relationship(back_populates="participants")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_attachments_confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_key: Mapped[str] = mapped_column(Text, nullable=False)  # reference only; bytes live in the blob store
    doc_type: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[float | None] = mapped_column(Float)

    email: Mapped[Email] = relationship(back_populates="attachments")


class SyncState(Base):
    __tablename__ = "sync_state"

    folder_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    uid_validity: Mapped[int | None] = mapped_column(Integer)
    last_seen_uid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)


class DecisionLog(Base):
    __tablename__ = "decision_log"
    __table_args__ = (
        CheckConstraint("decision IN ('payment', 'none', 'review')", name="ck_decision_log_decision"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_decision_log_confidence"),
    )

    message_id: Mapped[str] = mapped_column(String(998), primary_key=True)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)


class Ticket(Base):
    """An aging ticket: one per payment thread. Times are the mail's own times
    for automatic changes, so the age reflects when payment was asked for."""

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'in_progress', 'closed')", name="ck_tickets_status"),
        CheckConstraint("closed_by IS NULL OR closed_by IN ('auto', 'manual')", name="ck_tickets_closed_by"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    opened_by_message_id: Mapped[str | None] = mapped_column(String(998))
    in_progress_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[str | None] = mapped_column(String(8))
    closed_by_message_id: Mapped[str | None] = mapped_column(String(998))
    close_reason: Mapped[str | None] = mapped_column(Text)
    close_confidence: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)

    thread: Mapped[Thread] = relationship()
    events: Mapped[list[TicketEvent]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", order_by="TicketEvent.id"
    )


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    __table_args__ = (
        CheckConstraint("event IN ('opened', 'in_progress', 'closed', 'reopened')", name="ck_ticket_events_event"),
        CheckConstraint("actor IN ('auto', 'manual')", name="ck_ticket_events_actor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998))
    reason: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)

    ticket: Mapped[Ticket] = relationship(back_populates="events")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class EmailIntent(Base):
    """What a payment email is for (intent.py): one row per stored payment email.

    The CHECK constraints guarantee that no label outside the 8 intents is ever
    stored. ``tier2_input`` holds the cleaned email state only while tier 2 is
    still to run (or failed and may be retried); it is cleared once tier 2 has
    answered."""

    __tablename__ = "email_intents"
    __table_args__ = (
        CheckConstraint(f"intent IN ({_sql_in(INTENT_KEYS)})", name="ck_email_intents_intent"),
        CheckConstraint(f"tier1_intent IN ({_sql_in(INTENT_KEYS)})", name="ck_email_intents_tier1_intent"),
        CheckConstraint(f"tier2_intent IS NULL OR tier2_intent IN ({_sql_in(INTENT_KEYS)})",
                        name="ck_email_intents_tier2_intent"),
        CheckConstraint(f"decided_by IN ({_sql_in(DECIDED_BY)})", name="ck_email_intents_decided_by"),
        CheckConstraint(f"tier2_status IS NULL OR tier2_status IN ({_sql_in(TIER2_STATUSES)})",
                        name="ck_email_intents_tier2_status"),
        CheckConstraint("tier1_confidence >= 0 AND tier1_confidence <= 1", name="ck_email_intents_tier1_confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), unique=True, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    decided_by: Mapped[str] = mapped_column(String(16), nullable=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    tier1_intent: Mapped[str] = mapped_column(String(32), nullable=False)
    tier1_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    tier1_probabilities: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tier2_intent: Mapped[str | None] = mapped_column(String(32))
    tier2_status: Mapped[str | None] = mapped_column(String(16), index=True)
    tier2_seconds: Mapped[float | None] = mapped_column(Float)
    tier2_input: Mapped[dict | None] = mapped_column(JSON)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_naive)

    email: Mapped[Email] = relationship(back_populates="intent")
