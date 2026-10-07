"""Response models of the HTTP API. All datetimes are UTC with an offset."""

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
    payment_labels: list[str]
    attachment_store: str


class SyncStateOut(BaseModel):
    folder_name: str
    uid_validity: int | None
    last_seen_uid: int
    last_sync_at: datetime | None


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
    kind: str  # sync | reclassify
    state: str  # running | succeeded | failed
    started_at: datetime
    finished_at: datetime | None
    result: dict[str, Any] | None
    error: str | None


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
