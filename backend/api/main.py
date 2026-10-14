"""
Run with:
    cd backend
    python -m uvicorn api.main:create_app --factory --host 127.0.0.1 --port 8000

There is no authentication in Task 1, so the service must stay bound to
127.0.0.1. Interactive API docs are at http://127.0.0.1:8000/docs.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import Table, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CheckConstraint, CreateIndex, CreateTable, ForeignKeyConstraint, UniqueConstraint

from mailbox_viewer import repository as repo
from mailbox_viewer import tickets as tk
from mailbox_viewer.attachment_store import AttachmentStoreError, FileSystemStore, build_store
from mailbox_viewer.classification_graph import EmailClassifier
from mailbox_viewer.classifier import PAYMENT_LABELS
from mailbox_viewer.config import Settings, load_settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.document_model import load_classifier
from mailbox_viewer.mail_client import MailClient
from mailbox_viewer.models import Base
from mailbox_viewer.sync import ClientFactory, SyncResult, run_sync
from mailbox_viewer.timeutil import to_ist

from . import schemas as s
from .jobs import KIND_RECLASSIFY, KIND_REDECIDE, KIND_SYNC, KIND_TICKETS, Job, Jobs

logger = logging.getLogger(__name__)

DIALECTS = {"sqlite": sqlite.dialect(), "postgresql": postgresql.dialect()}
TABLE_ORDER = ["threads", "emails", "email_participants", "attachments", "sync_state", "decision_log",
               "tickets", "ticket_events"]


@dataclass
class Services:
    """Everything the routes need. Built from settings in production; tests
    pass their own (temporary database, fake mail server, fake model)."""

    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    store: FileSystemStore
    classifier: EmailClassifier
    client_factory: ClientFactory = MailClient


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or load_settings()
    engine = make_engine(settings.database_url, settings.db_schema)
    init_db(engine, settings.db_schema)
    return Services(
        settings=settings,
        engine=engine,
        session_factory=make_session_factory(engine),
        store=build_store(settings),
        classifier=load_classifier(settings),
    )


def create_app(services: Services | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    svc = services or build_services()
    jobs = Jobs()

    def run_sync_job() -> SyncResult:
        return run_sync(svc.settings, svc.session_factory, svc.client_factory, store=svc.store, classifier=svc.classifier)

    def run_reclassify_job(only_with_attachments: bool = False) -> dict:
        with svc.session_factory() as session, session.begin():
            changed = repo.reclassify_all(session, svc.store, svc.classifier, only_with_attachments)
        return s.ReclassifyOut(changed=changed, model=svc.classifier.name).model_dump()

    def run_redecide_job() -> dict:
        with svc.session_factory() as session, session.begin():
            changed = repo.redecide_all(session, svc.classifier)
        return {"changed": changed, "body_min_confidence": svc.settings.body_min_confidence,
                "model_min_confidence": svc.settings.model_min_confidence}

    def run_tickets_job() -> dict:
        with svc.session_factory() as session, session.begin():
            totals = tk.rebuild(session, svc.classifier.payment_confirmation, svc.settings.imap_user,
                                svc.settings.ticket_close_confidence)
        return {"changed": sum(totals.values()), **totals}

    def start_job(kind: str, run) -> s.JobOut:
        job, started = jobs.start(kind, run)
        if not started and job.kind != kind:
            raise HTTPException(409, f"a {job.kind} job is running; try again when it has finished")
        return _job_out(job)

    app = FastAPI(
        title="Mailbox Viewer API",
        version="1.0",
        description="Syncs a mailbox into the cache, classifies each email with two local models (Laya on the body, LayoutLMv3 on attachments) through a LangGraph flow, and serves the results.",
    )
    app.state.services = svc
    app.state.jobs = jobs

    # -- service status ---------------------------------------------------------------

    @app.get("/health", response_model=s.HealthOut, tags=["service"])
    def health() -> s.HealthOut:
        try:
            with svc.session_factory() as session:
                session.execute(text("select 1"))
            return s.HealthOut(status="ok", database="ok", model=svc.classifier.name)
        except Exception as exc:
            return s.HealthOut(status="degraded", database="error", database_error=_short(exc), model=svc.classifier.name)

    @app.get("/profiles", response_model=list[s.ProfileOut], tags=["service"])
    def profiles() -> list[s.ProfileOut]:
        st_ = svc.settings
        return [
            s.ProfileOut(
                name=st_.profile_name,
                active=True,
                mailbox=st_.imap_user,
                folders=list(st_.imap_folders),
                keep_policy="payment_only" if st_.store_only_payment else "every_message",
                model=svc.classifier.name,
                model_min_confidence=st_.model_min_confidence,
                body_min_confidence=st_.body_min_confidence,
                attachment_decides_confidence=st_.attachment_decides_confidence,
                ticket_close_confidence=st_.ticket_close_confidence,
                payment_labels=sorted(PAYMENT_LABELS),
                attachment_store=svc.store.describe(),
            )
        ]

    @app.get("/stats", response_model=s.StatsOut, tags=["service"])
    def stats() -> s.StatsOut:
        # Few queries on purpose: on a remote database each one is a network round trip.
        with svc.session_factory() as session:
            n = repo.mailbox_counts(session)
            by_decision = repo.count_by_decision(session)
            states = repo.get_sync_states(session, svc.settings.imap_folders)
            return s.StatsOut(
                threads=n["threads"],
                emails=n["emails"],
                attachments=n["attachments"],
                payment_threads=n["payment_threads"],
                needs_review=by_decision.get("review", 0),
                judged=n["judged"],
                dropped=max(n["judged"] - n["emails"], 0),
                by_doc_type=repo.count_by_doc_type(session),
                by_decision=by_decision,
                sync_states=[
                    s.SyncStateOut(
                        folder_name=f,
                        uid_validity=states[f].uid_validity if f in states else None,
                        last_seen_uid=states[f].last_seen_uid if f in states else 0,
                        last_sync_at=_ist(states[f].last_sync_at) if f in states else None,
                    )
                    for f in svc.settings.imap_folders
                ],
                tickets=s.TicketCountsOut(**tk.counts(session)),
            )

    # -- sync and classification ----------------------------------------------------------

    @app.post("/sync", response_model=s.JobOut, status_code=202, tags=["jobs"])
    def start_sync() -> s.JobOut:
        """Start an incremental sync in the background, or return the one already running."""
        return start_job(KIND_SYNC, run_sync_job)

    @app.post("/reclassify", response_model=s.JobOut, status_code=202, tags=["jobs"])
    def reclassify(only_with_attachments: bool = False) -> s.JobOut:
        """Re-run both models over every cached email in the background. No mail-server contact."""
        return start_job(KIND_RECLASSIFY, lambda: run_reclassify_job(only_with_attachments))

    @app.post("/redecide", response_model=s.JobOut, status_code=202, tags=["jobs"])
    def redecide() -> s.JobOut:
        """Re-apply the decision rule to stored predictions. No models, no mail server; takes seconds."""
        return start_job(KIND_REDECIDE, run_redecide_job)

    @app.post("/tickets/rebuild", response_model=s.JobOut, status_code=202, tags=["jobs"])
    def rebuild_tickets() -> s.JobOut:
        """Recreate every ticket from the stored payment threads. Runs the body model on
        replies in open tickets; manual changes are not kept."""
        return start_job(KIND_TICKETS, run_tickets_job)

    @app.get("/sync/latest", response_model=s.JobOut | None, tags=["jobs"])
    def latest_sync() -> s.JobOut | None:
        job = jobs.latest(KIND_SYNC)
        return _job_out(job) if job else None

    @app.get("/jobs/{job_id}", response_model=s.JobOut, tags=["jobs"])
    @app.get("/sync/{job_id}", response_model=s.JobOut, tags=["jobs"], include_in_schema=False)
    def job_status(job_id: str) -> s.JobOut:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id!r}")
        return _job_out(job)

    # -- mail ---------------------------------------------------------------------------

    @app.get("/threads", response_model=list[s.ThreadOut], tags=["mail"])
    def list_threads(
        payment_only: bool = False,
        search: str | None = None,
        limit: int = Query(1000, ge=1, le=5000),
    ) -> list[s.ThreadOut]:
        with svc.session_factory() as session:
            rows = repo.list_threads(session, payment_only=payment_only, search=search or None, limit=limit)
        return [_thread_out(t) for t in rows]

    @app.get("/threads/{thread_id}", response_model=s.ThreadDetailOut, tags=["mail"])
    def get_thread(thread_id: int) -> s.ThreadDetailOut:
        with svc.session_factory() as session:
            thread = repo.get_thread(session, thread_id)
            if thread is None:
                raise HTTPException(404, f"no thread {thread_id}")
            messages = [_message_out(detail, decision) for detail, decision in repo.get_thread_messages(session, thread_id)]
        return s.ThreadDetailOut(**_thread_out(thread).model_dump(), messages=messages)

    @app.get("/attachments/{attachment_id}", tags=["mail"], response_class=Response)
    def download_attachment(attachment_id: int) -> Response:
        with svc.session_factory() as session:
            info = repo.get_attachment(session, attachment_id)
        if info is None:
            raise HTTPException(404, f"no attachment {attachment_id}")
        try:
            data = svc.store.get(info.blob_key)
        except AttachmentStoreError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(
            content=data,
            media_type=info.content_type or "application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(info.filename)}"},
        )

    @app.get("/decisions/{message_id:path}", response_model=s.DecisionOut, tags=["mail"])
    def get_decision(message_id: str) -> s.DecisionOut:
        with svc.session_factory() as session:
            decision = repo.get_decision(session, message_id)
        if decision is None:
            raise HTTPException(404, f"no decision for {message_id!r}")
        return _decision_out(decision)

    # -- aging tickets ----------------------------------------------------------------------

    @app.get("/tickets", response_model=list[s.TicketOut], tags=["tickets"])
    def list_tickets(status: str | None = Query(None, pattern="^(open|in_progress|closed)$")) -> list[s.TicketOut]:
        """Active tickets oldest first, then closed ones most recently closed first."""
        with svc.session_factory() as session:
            return [_ticket_out(v) for v in tk.list_tickets(session, status)]

    @app.get("/tickets/{ticket_id}", response_model=s.TicketDetailOut, tags=["tickets"])
    def get_ticket(ticket_id: int) -> s.TicketDetailOut:
        with svc.session_factory() as session:
            found = tk.get_ticket(session, ticket_id)
        if found is None:
            raise HTTPException(404, f"no ticket {ticket_id}")
        view, events = found
        return s.TicketDetailOut(
            **_ticket_out(view).model_dump(),
            events=[s.TicketEventOut(event=e.event, actor=e.actor, at=_ist(e.at), message_id=e.message_id,
                                     reason=e.reason, confidence=e.confidence) for e in events],
        )

    @app.post("/tickets/{ticket_id}/close", response_model=s.TicketDetailOut, tags=["tickets"])
    def close_ticket(ticket_id: int, body: s.TicketActionIn | None = None) -> s.TicketDetailOut:
        """Close by hand, e.g. when the payment was confirmed outside email."""
        _ticket_action(tk.close_manually, ticket_id, body)
        return get_ticket(ticket_id)

    @app.post("/tickets/{ticket_id}/reopen", response_model=s.TicketDetailOut, tags=["tickets"])
    def reopen_ticket(ticket_id: int, body: s.TicketActionIn | None = None) -> s.TicketDetailOut:
        _ticket_action(tk.reopen_manually, ticket_id, body)
        return get_ticket(ticket_id)

    def _ticket_action(action, ticket_id: int, body: s.TicketActionIn | None) -> None:
        try:
            with svc.session_factory() as session, session.begin():
                action(session, ticket_id, body.reason if body else None)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except tk.TicketError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- data model and raw tables ------------------------------------------------------------

    @app.get("/schema", response_model=list[s.SchemaTableOut], tags=["data"])
    def schema(dialect: str | None = Query(None, pattern="^(sqlite|postgresql)$")) -> list[s.SchemaTableOut]:
        d = DIALECTS[dialect] if dialect else svc.engine.dialect
        tables = Base.metadata.tables
        return [_schema_table(tables[name], d) for name in TABLE_ORDER]

    @app.get("/schema/ddl", response_class=PlainTextResponse, tags=["data"])
    def schema_ddl(dialect: str = Query("postgresql", pattern="^(sqlite|postgresql)$")) -> str:
        d = DIALECTS[dialect]
        parts: list[str] = []
        for table in Base.metadata.sorted_tables:
            parts.append(str(CreateTable(table).compile(dialect=d)).strip() + ";")
            parts.extend(str(CreateIndex(i).compile(dialect=d)).strip() + ";" for i in sorted(table.indexes, key=lambda i: i.name or ""))
            parts.append("")
        return "\n".join(parts)

    @app.get("/tables", response_model=list[s.TableInfoOut], tags=["data"])
    def tables() -> list[s.TableInfoOut]:
        out = []
        with svc.session_factory() as session:
            for name in TABLE_ORDER:
                table = Base.metadata.tables[name]
                rows = session.execute(select(func.count()).select_from(table)).scalar() or 0
                out.append(s.TableInfoOut(name=name, rows=rows, columns=[c.name for c in table.columns]))
        return out

    @app.get("/tables/{name}/rows", response_model=s.TableRowsOut, tags=["data"])
    def table_rows(
        name: str,
        limit: int = Query(100, ge=1, le=2000),
        order_by: str | None = None,
        descending: bool = True,
    ) -> s.TableRowsOut:
        table = Base.metadata.tables.get(name)
        if table is None or name not in TABLE_ORDER:
            raise HTTPException(404, f"no table {name!r}")
        stmt = select(table).limit(limit)
        if order_by:
            if order_by not in table.c:
                raise HTTPException(422, f"{name} has no column {order_by!r}")
            col = table.c[order_by]
            stmt = stmt.order_by(col.desc().nulls_last() if descending else col.asc())
        else:
            pk = list(table.primary_key.columns)
            if pk:
                stmt = stmt.order_by(*(c.desc() if descending else c.asc() for c in pk))
        with svc.session_factory() as session:
            total = session.execute(select(func.count()).select_from(table)).scalar() or 0
            rows = [
                {k: (_ist(v) if isinstance(v, datetime) else v) for k, v in r._mapping.items()}
                for r in session.execute(stmt)
            ]
        return s.TableRowsOut(name=name, total=total, columns=[c.name for c in table.columns], rows=jsonable_encoder(rows))

    return app


# -- mapping helpers ----------------------------------------------------------------------


def _ist(value: datetime | None) -> datetime | None:
    """Stored datetimes are naive UTC; every response shows them in IST (+05:30)."""
    return to_ist(value)


def _short(exc: BaseException) -> str:
    text_value = str(getattr(exc, "orig", None) or exc).strip()
    return text_value.splitlines()[0][:200] if text_value else type(exc).__name__


def _thread_out(t: repo.ThreadSummary) -> s.ThreadOut:
    return s.ThreadOut(
        id=t.id,
        conversation_key=t.conversation_key,
        subject=t.subject,
        message_count=t.message_count,
        last_message_at=_ist(t.last_message_at),
        has_payment=t.has_payment,
        participants=t.participants,
        attachment_count=t.attachment_count,
    )


def _ticket_out(v: tk.TicketView) -> s.TicketOut:
    return s.TicketOut(
        id=v.id, thread_id=v.thread_id, subject=v.subject, message_count=v.message_count,
        last_message_at=_ist(v.last_message_at), status=v.status, opened_at=_ist(v.opened_at),
        in_progress_at=_ist(v.in_progress_at), closed_at=_ist(v.closed_at), closed_by=v.closed_by,
        close_reason=v.close_reason, close_confidence=v.close_confidence, age_days=v.age_days, bucket=v.bucket,
    )


def _decision_out(d: repo.DecisionInfo) -> s.DecisionOut:
    return s.DecisionOut(
        message_id=d.message_id,
        tier=d.tier,
        decision=d.decision,
        confidence=d.confidence,
        reason=d.reason,
        decided_at=_ist(d.decided_at),
    )


def _message_out(d: repo.EmailDetail, decision: repo.DecisionInfo | None) -> s.MessageOut:
    return s.MessageOut(
        id=d.id,
        message_id=d.message_id,
        subject=d.subject,
        body_text=d.body_text,
        received_at=_ist(d.received_at),
        in_reply_to=d.in_reply_to,
        references_header=d.references_header,
        has_attachments=d.has_attachments,
        matched_directly=d.matched_directly,
        doc_type=d.doc_type,
        confidence=d.confidence,
        decision_reason=d.decision_reason,
        participants=[s.ParticipantOut(role=p.role, name=p.name, address=p.address) for p in d.participants],
        attachments=[
            s.AttachmentOut(
                id=a.id,
                filename=a.filename,
                content_type=a.content_type,
                size_bytes=a.size_bytes,
                blob_key=a.blob_key,
                doc_type=a.doc_type,
                confidence=a.confidence,
                download_url=f"/attachments/{a.id}",
            )
            for a in d.attachments
        ],
        decision=_decision_out(decision) if decision else None,
    )


def _job_out(job: Job) -> s.JobOut:
    if isinstance(job.result, SyncResult):
        result = _result_out(job.result).model_dump()
    else:
        result = job.result
    return s.JobOut(
        id=job.id,
        kind=job.kind,
        state=job.state,
        started_at=_ist(job.started_at),
        finished_at=_ist(job.finished_at),
        result=result,
        error=job.error,
    )


def _result_out(r: SyncResult) -> s.SyncResultOut:
    data = dataclasses.asdict(r)
    folders = data.pop("folders")
    return s.SyncResultOut(**data, summary=r.summary(), folders=[s.FolderResultOut(**f) for f in folders])


def _schema_table(table: Table, dialect) -> s.SchemaTableOut:
    unique_cols = {
        c.name
        for con in table.constraints
        if isinstance(con, UniqueConstraint) and len(con.columns) == 1
        for c in con.columns
    }
    indexed = {c.name for idx in table.indexes for c in idx.columns}
    constraints: list[str] = []
    for con in table.constraints:
        if isinstance(con, CheckConstraint):
            constraints.append(f"CHECK {con.sqltext}")
        elif isinstance(con, UniqueConstraint) and con.columns:
            constraints.append("UNIQUE (" + ", ".join(con.columns.keys()) + ")")
        elif isinstance(con, ForeignKeyConstraint):
            constraints.append(f"FOREIGN KEY ({', '.join(con.column_keys)}) ON DELETE {con.ondelete or 'NO ACTION'}")
    return s.SchemaTableOut(
        name=table.name,
        columns=[
            s.ColumnOut(
                name=c.name,
                type=str(c.type.compile(dialect=dialect)),
                nullable=bool(c.nullable),
                primary_key=bool(c.primary_key),
                unique=bool(c.unique) or c.name in unique_cols,
                references=[f"{fk.column.table.name}.{fk.column.name}" for fk in c.foreign_keys],
                indexed=c.name in indexed,
            )
            for c in table.columns
        ],
        constraints=sorted(constraints),
        indexes=[f"{i.name} ({', '.join(i.columns.keys())})" for i in sorted(table.indexes, key=lambda i: i.name or "")],
    )
