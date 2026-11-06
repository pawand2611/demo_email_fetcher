"""Response models of the HTTP API. All datetimes are in IST (+05:30), with the offset."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class HealthOut(BaseModel):
    status: str  # ok | degraded
    database: str  # ok | error
    database_error: str | None = None
    model: str


class ProfileOut(BaseModel):
    name: str
    active: bool
    mailbox: str
    folders: list[str]
    keep_policy: str  # every_message | payment_only
    model: str
    model_min_confidence: float
    body_min_confidence: float
    attachment_decides_confidence: float
    ticket_close_confidence: float
    payment_labels: list[str]
    attachment_store: str
    # Intent classification of payment emails; intent_model is None when LAYA_DIR is not set.
    intent_model: str | None = None
    intent_labels: list[str] = []
    tier1_min_confidence: float | None = None
    tier2: str = "off"  # off | background | inline


class SyncStateOut(BaseModel):
    folder_name: str
    uid_validity: int | None
    last_seen_uid: int
    last_sync_at: datetime | None


class TicketCountsOut(BaseModel):
    open: int
    in_progress: int
    closed: int
    oldest_active_days: float | None
    active_by_bucket: dict[str, int]


class StatsOut(BaseModel):
    threads: int
    emails: int
    attachments: int
    payment_threads: int
    needs_review: int
    judged: int
    dropped: int
    by_doc_type: dict[str, int]
    by_decision: dict[str, int]
    sync_states: list[SyncStateOut]
    tickets: TicketCountsOut


class ParticipantOut(BaseModel):
    role: str
    name: str
    address: str


class AttachmentOut(BaseModel):
    id: int
    filename: str
    content_type: str
    size_bytes: int
    blob_key: str
    doc_type: str | None
    confidence: float | None
    download_url: str


class DecisionOut(BaseModel):
    message_id: str
    tier: int
    decision: str
    confidence: float | None
    reason: str | None
    decided_at: datetime


class IntentOut(BaseModel):
    """What a payment email is for, and which tier decided it."""

    intent: str  # one of the 8 intent keys
    decided_by: str  # tier1 | tier2 | tier1_fallback
    needs_review: bool
    tier1_intent: str
    tier1_confidence: float
    tier1_probabilities: dict[str, float]
    tier2_intent: str | None
    tier2_status: str | None  # pending | ok | unparsed | timeout | unavailable | None (not called)
    tier2_seconds: float | None
    decided_at: datetime


class IntentSummaryOut(BaseModel):
    total: int
    needs_review: int
    pending: int
    by_intent: dict[str, int]
    by_decided_by: dict[str, int]
    by_tier2_status: dict[str, int]
    tier2_failure_rate: float | None
    tier2_seconds_p50: float | None
    tier2_seconds_p90: float | None
    tier2_seconds_max: float | None


class MessageOut(BaseModel):
    id: int
    message_id: str
    subject: str
    body_text: str
    received_at: datetime | None
    in_reply_to: str | None
    references_header: str | None
    has_attachments: bool
    matched_directly: bool
    doc_type: str | None
    confidence: float | None
    decision_reason: str | None
    participants: list[ParticipantOut]
    attachments: list[AttachmentOut]
    decision: DecisionOut | None
    intent: IntentOut | None = None  # payment emails only


class ThreadOut(BaseModel):
    id: int
    conversation_key: str
    subject: str
    message_count: int
    last_message_at: datetime | None
    has_payment: bool
    participants: str
    attachment_count: int


class ThreadDetailOut(ThreadOut):
    messages: list[MessageOut]


class FolderResultOut(BaseModel):
    folder: str
    status: str
    candidates: int
    last_seen_uid: int
    full_rewalk: bool
    error: str | None


class SyncResultOut(BaseModel):
    status: str
    candidates: int
    kept: int
    payment_hits: int
    dropped: int
    skipped: int
    backfilled: int
    failed: int
    tickets_opened: int = 0
    tickets_closed: int = 0
    intents: int = 0
    intents_pending: int = 0
    error: str | None
    duration_seconds: float
    summary: str
    folders: list[FolderResultOut]


class ReclassifyOut(BaseModel):
    changed: int
    model: str


class JobOut(BaseModel):
    """A background job. ``result`` has the SyncResultOut fields for kind
    ``sync`` and the ReclassifyOut fields for kind ``reclassify``."""

    id: str
    kind: str  # sync | reclassify | redecide | tickets | intents
    state: str  # running | succeeded | failed
    started_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: str | None


class TicketOut(BaseModel):
    id: int
    thread_id: int
    subject: str
    message_count: int
    last_message_at: datetime | None
    status: str  # open | in_progress | closed
    opened_at: datetime
    in_progress_at: datetime | None
    closed_at: datetime | None
    closed_by: str | None  # auto | manual
    close_reason: str | None
    close_confidence: float | None
    age_days: float
    bucket: str


class TicketEventOut(BaseModel):
    event: str  # opened | in_progress | closed | reopened
    actor: str  # auto | manual
    at: datetime
    message_id: str | None
    reason: str | None
    confidence: float | None


class TicketDetailOut(TicketOut):
    events: list[TicketEventOut]


class TicketActionIn(BaseModel):
    reason: str | None = None


class TableInfoOut(BaseModel):
    name: str
    rows: int
    columns: list[str]


class TableRowsOut(BaseModel):
    name: str
    total: int
    columns: list[str]
    rows: list[dict[str, Any]]


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    primary_key: bool
    unique: bool
    references: list[str]
    indexed: bool


class SchemaTableOut(BaseModel):
    name: str
    columns: list[ColumnOut]
    constraints: list[str]
    indexes: list[str]
