"""Aging tickets: one per payment thread, opened and closed by the mail itself.

Rules (the user's, 2026-10-14):

* **Open**: the first email of a thread judged payment on its own content
  opens the ticket. Its age counts from that email's received time.
* **In progress**: we reply in the thread (an email sent from our own mailbox).
* **Closed**: a later email that the body model is at least
  ``TICKET_CLOSE_CONFIDENCE`` sure confirms a payment, from them or from us.
  An email that carries a new invoice never closes a ticket: Laya scored a
  plain invoice 0.75 on the confirmation question, too close to the bar.
* **Reopened**: a new invoice arrives in a closed thread.
* **Manual**: a person can close or reopen any ticket; logged as ``manual``.

Every change is written to ``ticket_events`` with its time, actor and reason.
Automatic changes use the email's own time, manual ones the time of the click.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .classifier import Decision
from .models import Email, Thread, Ticket, TicketEvent, utcnow_naive

OPEN, IN_PROGRESS, CLOSED = "open", "in_progress", "closed"
ACTIVE = (OPEN, IN_PROGRESS)
AUTO, MANUAL = "auto", "manual"

# An attachment counts as a new invoice from this attachment-model score.
INVOICE_EVIDENCE = 0.5

# Age buckets in whole days: (up to and including, label).
BUCKETS = ((2, "0–2 days"), (7, "3–7 days"), (None, "8+ days"))


class TicketError(ValueError):
    """A manual action that does not apply, e.g. closing a closed ticket."""


@dataclass(frozen=True)
class MailFacts:
    """What the ticket rules need to know about one stored email."""

    message_id: str
    thread_id: int
    received_at: datetime | None
    payment: bool  # judged payment on its own content: may open a ticket
    new_invoice: bool  # carries an invoice: never closes, reopens a closed ticket
    from_us: bool  # sent from our own mailbox: moves an open ticket to in progress
    confirmation: float | None  # body model: how sure the email confirms a payment


def carries_invoice(decision: Decision) -> bool:
    return decision.doc_type == "invoice" or any(
        a.doc_type == "invoice" and (a.confidence or 0.0) >= INVOICE_EVIDENCE for a in decision.attachments
    )


def is_from(addresses: list[str], mailbox: str) -> bool:
    mailbox = mailbox.strip().lower()
    return bool(mailbox) and any(a.strip().lower() == mailbox for a in addresses)


def active_ticket(session: Session, thread_id: int) -> Ticket | None:
    return session.scalar(select(Ticket).where(Ticket.thread_id == thread_id, Ticket.status.in_(ACTIVE)))


# -- the automatic rules ------------------------------------------------------------------


def apply(session: Session, facts: MailFacts, close_at: float) -> str | None:
    """Apply one stored email to its thread's ticket. Returns the event, or None."""
    ticket = session.scalar(select(Ticket).where(Ticket.thread_id == facts.thread_id))
    at = facts.received_at or utcnow_naive()

    if ticket is None:
        if not facts.payment:
            return None
        ticket = Ticket(thread_id=facts.thread_id, status=OPEN, opened_at=at,
                        opened_by_message_id=facts.message_id, updated_at=utcnow_naive())
        session.add(ticket)
        _event(ticket, "opened", AUTO, at, facts.message_id, "first payment email in the thread")
        session.flush()
        return "opened"

    if ticket.status == CLOSED:
        if facts.new_invoice and (ticket.closed_at is None or at >= ticket.closed_at):
            _reopen(ticket, AUTO, at, facts.message_id, "a new invoice arrived in the thread")
            return "reopened"
        return None

    if facts.confirmation is not None and facts.confirmation >= close_at and not facts.new_invoice:
        _close(ticket, AUTO, at, facts.message_id, f"payment confirmed by email ({facts.confirmation:.2f})",
               facts.confirmation)
        return "closed"

    if facts.from_us and ticket.status == OPEN:
        ticket.status, ticket.in_progress_at, ticket.updated_at = IN_PROGRESS, at, utcnow_naive()
        _event(ticket, "in_progress", AUTO, at, facts.message_id, "we replied in the thread")
        return "in_progress"
    return None


# -- manual actions -----------------------------------------------------------------------


def close_manually(session: Session, ticket_id: int, reason: str | None = None) -> Ticket:
    ticket = _get(session, ticket_id)
    if ticket.status == CLOSED:
        raise TicketError(f"ticket {ticket_id} is already closed")
    _close(ticket, MANUAL, utcnow_naive(), None, (reason or "").strip() or "closed by hand", None)
    session.flush()
    return ticket


def reopen_manually(session: Session, ticket_id: int, reason: str | None = None) -> Ticket:
    ticket = _get(session, ticket_id)
    if ticket.status != CLOSED:
        raise TicketError(f"ticket {ticket_id} is not closed")
    _reopen(ticket, MANUAL, utcnow_naive(), None, (reason or "").strip() or "reopened by hand")
    session.flush()
    return ticket


# -- rebuilding from stored mail -------------------------------------------------------------


def rebuild(session: Session, confirmation, mailbox: str, close_at: float) -> dict[str, int]:
    """Recreate every ticket from the stored payment threads, oldest email first.

    ``confirmation(subject, body)`` is the body model's confirmation score; it is
    called only for emails that arrive while a ticket is active. Manual changes
    are not kept: the tickets come out as the mail alone would have left them.
    """
    session.execute(delete(TicketEvent))
    session.execute(delete(Ticket))
    session.flush()

    totals = {"opened": 0, "in_progress": 0, "closed": 0, "reopened": 0}
    thread_ids = session.scalars(select(Thread.id).where(Thread.has_payment.is_(True)).order_by(Thread.id)).all()
    for thread_id in thread_ids:
        emails = session.scalars(
            select(Email).where(Email.thread_id == thread_id).order_by(Email.received_at, Email.id)
        ).all()
        for e in emails:
            needs_check = active_ticket(session, thread_id) is not None
            senders = [p.address for p in e.participants if p.role == "from"]
            facts = MailFacts(
                message_id=e.message_id,
                thread_id=thread_id,
                received_at=e.received_at,
                payment=e.matched_directly,
                new_invoice=e.doc_type == "invoice"
                or any(a.doc_type == "invoice" and (a.confidence or 0.0) >= INVOICE_EVIDENCE for a in e.attachments),
                from_us=is_from(senders, mailbox),
                confirmation=confirmation(e.subject, e.body_text) if needs_check else None,
            )
            event = apply(session, facts, close_at)
            if event:
                totals[event] += 1
    return totals


# -- reading ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TicketView:
    id: int
    thread_id: int
    subject: str
    message_count: int
    last_message_at: datetime | None
    status: str
    opened_at: datetime
    in_progress_at: datetime | None
    closed_at: datetime | None
    closed_by: str | None
    close_reason: str | None
    close_confidence: float | None
    age_days: float
    bucket: str


@dataclass(frozen=True)
class EventView:
    event: str
    actor: str
    at: datetime
    message_id: str | None
    reason: str | None
    confidence: float | None


def age_days(ticket: Ticket, now: datetime) -> float:
    """Open tickets age until now; closed ones stop at the close."""
    end = ticket.closed_at if ticket.status == CLOSED and ticket.closed_at else now
    return max((end - ticket.opened_at).total_seconds() / 86400, 0.0)


def bucket(days: float) -> str:
    whole = int(days)
    for limit, label in BUCKETS:
        if limit is None or whole <= limit:
            return label
    return BUCKETS[-1][1]


def list_tickets(session: Session, status: str | None = None, now: datetime | None = None) -> list[TicketView]:
    """Active tickets oldest first, then closed ones most recently closed first."""
    now = now or utcnow_naive()
    stmt = select(Ticket, Thread).join(Thread, Thread.id == Ticket.thread_id)
    if status:
        stmt = stmt.where(Ticket.status == status)
    views = [_view(t, th, now) for t, th in session.execute(stmt)]
    active = sorted((v for v in views if v.status != CLOSED), key=lambda v: v.opened_at)
    closed = sorted((v for v in views if v.status == CLOSED), key=lambda v: v.closed_at or v.opened_at, reverse=True)
    return active + closed


def get_ticket(session: Session, ticket_id: int, now: datetime | None = None) -> tuple[TicketView, list[EventView]] | None:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        return None
    events = [EventView(e.event, e.actor, e.at, e.message_id, e.reason, e.confidence) for e in ticket.events]
    return _view(ticket, ticket.thread, now or utcnow_naive()), events


def counts(session: Session, now: datetime | None = None) -> dict:
    now = now or utcnow_naive()
    by_status = dict(session.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all())
    active = session.scalars(select(Ticket).where(Ticket.status.in_(ACTIVE))).all()
    ages = [age_days(t, now) for t in active]
    by_bucket = {label: 0 for _, label in BUCKETS}
    for days in ages:
        by_bucket[bucket(days)] += 1
    return {
        "open": by_status.get(OPEN, 0),
        "in_progress": by_status.get(IN_PROGRESS, 0),
        "closed": by_status.get(CLOSED, 0),
        "oldest_active_days": round(max(ages), 1) if ages else None,
        "active_by_bucket": by_bucket,
    }


# -- helpers --------------------------------------------------------------------------------


def _get(session: Session, ticket_id: int) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise LookupError(f"no ticket {ticket_id}")
    return ticket


def _event(ticket: Ticket, event: str, actor: str, at: datetime, message_id: str | None,
           reason: str, confidence: float | None = None) -> None:
    ticket.events.append(TicketEvent(event=event, actor=actor, at=at, message_id=message_id,
                                     reason=reason, confidence=confidence))


def _close(ticket: Ticket, actor: str, at: datetime, message_id: str | None, reason: str,
           confidence: float | None) -> None:
    ticket.status = CLOSED
    ticket.closed_at, ticket.closed_by, ticket.closed_by_message_id = at, actor, message_id
    ticket.close_reason, ticket.close_confidence, ticket.updated_at = reason, confidence, utcnow_naive()
    _event(ticket, "closed", actor, at, message_id, reason, confidence)


def _reopen(ticket: Ticket, actor: str, at: datetime, message_id: str | None, reason: str) -> None:
    ticket.status = OPEN
    ticket.closed_at = ticket.closed_by = ticket.closed_by_message_id = None
    ticket.close_reason = ticket.close_confidence = None
    ticket.updated_at = utcnow_naive()
    _event(ticket, "reopened", actor, at, message_id, reason)


def _view(t: Ticket, th: Thread, now: datetime) -> TicketView:
    days = age_days(t, now)
    return TicketView(
        id=t.id, thread_id=t.thread_id, subject=th.subject or "(no subject)", message_count=th.message_count,
        last_message_at=th.last_message_at, status=t.status, opened_at=t.opened_at,
        in_progress_at=t.in_progress_at, closed_at=t.closed_at, closed_by=t.closed_by,
        close_reason=t.close_reason, close_confidence=t.close_confidence,
        age_days=round(days, 1), bucket=bucket(days),
    )
