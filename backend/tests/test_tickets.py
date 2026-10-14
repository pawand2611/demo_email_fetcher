"""Aging tickets: opened, moved and closed by the mail itself during a sync,
closed and reopened by hand, aged, rebuilt, and served by the API."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.main import Services, create_app
from mailbox_viewer import tickets as tk
from mailbox_viewer.attachment_store import FileSystemStore
from mailbox_viewer.config import Settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.models import Ticket
from mailbox_viewer.sync import run_sync

from .fakes import FakeBodyModel, FakeDocumentModel, FakeMailClient, FakeMailServer, build_message, fake_classifier

PDF = b"%PDF-1.4 pretend invoice"
ME = "me@example.com"
VENDOR = "Acme <billing@acme.example>"


def invoice(uid_date: str, mid: str = "<inv@x>", subject: str = "Invoice #42") -> bytes:
    return build_message(subject=subject, sender=VENDOR, to=ME, message_id=mid, date=uid_date,
                         text="Please find our invoice attached.", attachments=[("invoice_42.pdf", "application/pdf", PDF)])


def reply(mid: str, sender: str, text: str, date: str, attachments=None, parent: str = "<inv@x>") -> bytes:
    return build_message(subject="Re: Invoice #42", sender=sender, to="x@example.com", message_id=mid,
                         in_reply_to=parent, references=parent, date=date, text=text, attachments=attachments)


class TicketSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            imap_host="fake", imap_port=993, imap_user=ME, imap_password="secret", imap_folder="INBOX",
            imap_timeout=5, database_url=f"sqlite:///{(root / 't.db').as_posix()}", initial_fetch_limit=200,
            attachment_dir=str(root / "attachments"), store_only_payment=True,
        )
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.factory = make_session_factory(self.engine)
        self.server = FakeMailServer()
        self.body = FakeBodyModel()
        self.classifier = fake_classifier(self.body, FakeDocumentModel())
        self.store = FileSystemStore(self.settings.attachment_dir)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def sync(self, **overrides):
        settings = dataclasses.replace(self.settings, **overrides)
        return run_sync(settings, self.factory, lambda _s, folder: FakeMailClient(self.server, folder),
                        store=self.store, classifier=self.classifier)

    def ticket(self) -> Ticket:
        with self.factory() as session:
            ticket = session.scalars(select(Ticket)).one()
            session.expunge(ticket)
            return ticket

    def events(self) -> list[tuple[str, str]]:
        with self.factory() as session:
            return [(e.event, e.actor) for e in session.scalars(select(Ticket)).one().events]

    # -- automatic lifecycle ------------------------------------------------------------

    def test_invoice_opens_our_reply_moves_confirmation_closes(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        result = self.sync()
        ticket = self.ticket()
        self.assertEqual((ticket.status, result.tickets_opened), ("open", 1))
        self.assertEqual(ticket.opened_at, datetime(2026, 9, 1, 9, 0))  # the email's own time
        self.assertEqual(self.body.confirmation_calls, [])  # nothing to confirm on the opening email

        self.server.add(2, reply("<r1@x>", f"Me <{ME}>", "We will process this today.", "Tue, 02 Sep 2026 10:00:00 +0000"))
        self.sync()
        ticket = self.ticket()
        self.assertEqual((ticket.status, ticket.in_progress_at), ("in_progress", datetime(2026, 9, 2, 10, 0)))

        self.server.add(3, reply("<r2@x>", VENDOR, "Payment received, thank you.", "Fri, 05 Sep 2026 08:00:00 +0000",
                                 parent="<r1@x>"))
        result = self.sync()
        ticket = self.ticket()
        self.assertEqual((ticket.status, ticket.closed_by, ticket.closed_by_message_id), ("closed", "auto", "<r2@x>"))
        self.assertEqual((ticket.closed_at, ticket.close_confidence), (datetime(2026, 9, 5, 8, 0), 0.95))
        self.assertEqual(result.tickets_closed, 1)
        self.assertIn("tickets: 0 opened, 1 closed", result.summary())
        self.assertEqual(self.events(), [("opened", "auto"), ("in_progress", "auto"), ("closed", "auto")])

    def test_low_confidence_leaves_the_ticket_open(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", VENDOR, "Any update on this?", "Tue, 02 Sep 2026 10:00:00 +0000"))
        self.sync()
        self.assertEqual(self.ticket().status, "open")  # fake model answers 0.20
        self.assertEqual(self.body.confirmation_calls, ["Re: Invoice #42"])

    def test_bar_comes_from_settings(self) -> None:
        self.body.confirmations["Re: Invoice #42"] = 0.85
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", VENDOR, "Settled.", "Tue, 02 Sep 2026 10:00:00 +0000"))
        self.sync(ticket_close_confidence=0.9)
        self.assertEqual(self.ticket().status, "open")  # 0.85 is below a 0.90 bar

    def test_an_email_carrying_a_new_invoice_never_closes(self) -> None:
        self.body.confirmations["Re: Invoice #42"] = 0.99
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", VENDOR, "Payment received for the old one; new invoice attached.",
                                 "Tue, 02 Sep 2026 10:00:00 +0000",
                                 attachments=[("invoice_43.pdf", "application/pdf", PDF)]))
        self.sync()
        self.assertEqual(self.ticket().status, "open")

    def test_new_invoice_reopens_a_closed_ticket(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", VENDOR, "Payment received.", "Tue, 02 Sep 2026 10:00:00 +0000"))
        self.sync()
        self.assertEqual(self.ticket().status, "closed")

        self.server.add(3, reply("<r2@x>", VENDOR, "Next month's bill.", "Wed, 01 Oct 2026 09:00:00 +0000",
                                 attachments=[("invoice_43.pdf", "application/pdf", PDF)], parent="<r1@x>"))
        self.sync()
        ticket = self.ticket()
        self.assertEqual((ticket.status, ticket.closed_at), ("open", None))
        self.assertEqual(self.events()[-1], ("reopened", "auto"))

    def test_mail_outside_payment_threads_opens_nothing(self) -> None:
        self.server.add(1, build_message(subject="Lunch?", sender=VENDOR, message_id="<l@x>"))
        self.sync()
        with self.factory() as session:
            self.assertEqual(session.scalars(select(Ticket)).all(), [])

    # -- manual actions ----------------------------------------------------------------------

    def test_manual_close_and_reopen(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.sync()
        ticket_id = self.ticket().id
        with self.factory() as session, session.begin():
            tk.close_manually(session, ticket_id, "paid by cheque")
        ticket = self.ticket()
        self.assertEqual((ticket.status, ticket.closed_by, ticket.close_reason), ("closed", "manual", "paid by cheque"))

        with self.factory() as session, session.begin():
            with self.assertRaises(tk.TicketError):
                tk.close_manually(session, ticket_id)
            tk.reopen_manually(session, ticket_id)
            with self.assertRaises(tk.TicketError):
                tk.reopen_manually(session, ticket_id)
            with self.assertRaises(LookupError):
                tk.close_manually(session, 999)
        self.assertEqual(self.ticket().status, "open")
        self.assertEqual(self.events(), [("opened", "auto"), ("closed", "manual"), ("reopened", "manual")])

    # -- aging ----------------------------------------------------------------------------------

    def test_age_and_buckets(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.sync()
        with self.factory() as session:
            [view] = tk.list_tickets(session, now=datetime(2026, 9, 11, 9, 0))
            self.assertEqual((view.age_days, view.bucket), (10.0, "8+ days"))
            counts = tk.counts(session, now=datetime(2026, 9, 3, 9, 0))
            self.assertEqual((counts["open"], counts["oldest_active_days"]), (1, 2.0))
            self.assertEqual(counts["active_by_bucket"], {"0–2 days": 1, "3–7 days": 0, "8+ days": 0})
        self.assertEqual([tk.bucket(d) for d in (0, 2.9, 3, 7.5, 8)], ["0–2 days", "0–2 days", "3–7 days", "3–7 days", "8+ days"])

    def test_closed_ticket_stops_aging(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", VENDOR, "Payment received.", "Thu, 04 Sep 2026 09:00:00 +0000"))
        self.sync()
        with self.factory() as session:
            [view] = tk.list_tickets(session, now=datetime(2026, 12, 1))
        self.assertEqual((view.status, view.age_days), ("closed", 3.0))

    # -- rebuild ----------------------------------------------------------------------------------

    def test_rebuild_reproduces_the_mail_and_drops_manual_changes(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, reply("<r1@x>", f"Me <{ME}>", "On it.", "Tue, 02 Sep 2026 10:00:00 +0000"))
        self.sync()
        with self.factory() as session, session.begin():
            tk.close_manually(session, self.ticket().id)

        with self.factory() as session, session.begin():
            totals = tk.rebuild(session, self.classifier.payment_confirmation, ME, 0.8)
        self.assertEqual(totals, {"opened": 1, "in_progress": 1, "closed": 0, "reopened": 0})
        self.assertEqual(self.ticket().status, "in_progress")


class TicketApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            imap_host="fake", imap_port=993, imap_user=ME, imap_password="secret", imap_folder="INBOX",
            imap_timeout=5, database_url=f"sqlite:///{(root / 'a.db').as_posix()}", initial_fetch_limit=200,
            attachment_dir=str(root / "attachments"), store_only_payment=True,
        )
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.server = FakeMailServer()
        self.app = create_app(Services(
            settings=self.settings, engine=self.engine, session_factory=make_session_factory(self.engine),
            store=FileSystemStore(self.settings.attachment_dir), classifier=fake_classifier(),
            client_factory=lambda _s, folder: FakeMailClient(self.server, folder),
        ))
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def run_job(self, path: str) -> dict:
        started = self.client.post(path)
        self.assertEqual(started.status_code, 202)
        job = self.app.state.jobs.get(started.json()["id"])
        self.assertTrue(job.done.wait(10))
        return self.client.get(f"/jobs/{job.id}").json()

    def test_list_detail_close_reopen_and_stats(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.run_job("/sync")

        [t] = self.client.get("/tickets").json()
        self.assertEqual((t["status"], t["subject"], t["opened_at"]), ("open", "Invoice #42", "2026-09-01T14:30:00+05:30"))
        self.assertEqual(self.client.get("/tickets", params={"status": "closed"}).json(), [])
        self.assertEqual(self.client.get("/tickets", params={"status": "nope"}).status_code, 422)

        closed = self.client.post(f"/tickets/{t['id']}/close", json={"reason": "paid in cash"}).json()
        self.assertEqual((closed["status"], closed["closed_by"], closed["close_reason"]), ("closed", "manual", "paid in cash"))
        self.assertEqual([e["event"] for e in closed["events"]], ["opened", "closed"])
        self.assertEqual(self.client.post(f"/tickets/{t['id']}/close").status_code, 409)
        self.assertEqual(self.client.post(f"/tickets/{t['id']}/reopen").json()["status"], "open")
        self.assertEqual(self.client.post("/tickets/999/close").status_code, 404)
        self.assertEqual(self.client.get("/tickets/999").status_code, 404)

        stats = self.client.get("/stats").json()["tickets"]
        self.assertEqual((stats["open"], stats["in_progress"], stats["closed"]), (1, 0, 0))
        self.assertEqual(self.client.get("/profiles").json()[0]["ticket_close_confidence"], 0.8)

    def test_rebuild_job(self) -> None:
        self.server.add(1, invoice("Mon, 01 Sep 2026 09:00:00 +0000"))
        self.run_job("/sync")
        job = self.run_job("/tickets/rebuild")
        self.assertEqual((job["kind"], job["state"], job["result"]["opened"]), ("tickets", "succeeded", 1))


if __name__ == "__main__":
    unittest.main()
