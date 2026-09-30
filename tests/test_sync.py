"""Sync behaviour on the six-table model, against a fake mail server and a
throw-away SQLite file: de-duplication, incremental UIDs, threading,
participants, attachments on disk, decision log, resilience."""

from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select, update

from mailbox_viewer import repository as repo
from mailbox_viewer.attachment_store import FileSystemStore
from mailbox_viewer.config import Settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.mail_client import MailClientError, MailConnectionLost
from mailbox_viewer.models import Attachment, DecisionLog, Email, EmailParticipant, Thread
from mailbox_viewer.sync import STATUS_FAILED, STATUS_OK, STATUS_PARTIAL, run_sync

from .fakes import FakeMailClient, FakeMailServer, build_message

PDF = b"%PDF-1.4 pretend statement"


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "cache.db"
        self.att_dir = Path(self.tmp.name) / "attachments"
        self.settings = Settings(
            imap_host="fake",
            imap_port=993,
            imap_user="me@example.com",
            imap_password="secret",
            imap_folder="INBOX",
            imap_timeout=5,
            database_url=f"sqlite:///{db_path.as_posix()}",
            initial_fetch_limit=200,
            attachment_dir=str(self.att_dir),
            store_only_payment=False,  # the flow tests below switch this on
        )
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.factory = make_session_factory(self.engine)
        self.server = FakeMailServer()
        self.servers: dict[str, FakeMailServer] = {}  # extra folders for multi-folder tests
        self.store = FileSystemStore(self.att_dir)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------

    def seed_three_messages(self) -> None:
        self.server.add(1, build_message(subject="Welcome", message_id="<m1@x>"))
        self.server.add(
            2,
            build_message(
                subject="Your account statement",
                sender="Acme Bank <statements@acme.example>",
                to="me@example.com",
                cc="Partner <partner@example.com>",
                message_id="<m2@x>",
                attachments=[("statement_aug.pdf", "application/pdf", PDF)],
            ),
        )
        self.server.add(3, build_message(subject="Lunch?", sender="Ravi <ravi@example.com>", message_id="<m3@x>"))

    def sync(self, **overrides):
        settings = dataclasses.replace(self.settings, **overrides)
        return run_sync(settings, self.factory, lambda _s, folder: FakeMailClient(self.servers.get(folder, self.server), folder), store=self.store)

    def counts(self) -> tuple[int, int, int, int]:
        with self.factory() as session:
            return (
                repo.count_threads(session),
                repo.count_emails(session),
                repo.count_attachments(session),
                repo.count_payment_threads(session),
            )

    def state(self):
        with self.factory() as session:
            return repo.get_sync_state(session, self.settings.imap_folder)

    # -- de-duplication ------------------------------------------------------------

    def test_first_sync_persists_and_second_sync_changes_nothing(self) -> None:
        self.seed_three_messages()

        first = self.sync()
        self.assertEqual((first.status, first.kept, first.payment_hits, first.skipped, first.failed), (STATUS_OK, 3, 1, 0, 0))
        self.assertEqual(self.counts(), (3, 3, 1, 1))
        self.assertEqual(self.state().last_seen_uid, 3)

        second = self.sync()
        self.assertEqual((second.candidates, second.kept, second.skipped), (0, 0, 0))
        self.assertEqual(self.counts(), (3, 3, 1, 1))
        self.assertEqual(self.server.search_calls[-1], 3)

    def test_rewalk_after_state_reset_adds_no_duplicate_rows(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session, session.begin():
            repo.delete_sync_state(session, self.settings.imap_folder)

        result = self.sync()

        self.assertEqual((result.candidates, result.kept, result.skipped), (3, 0, 3))
        self.assertEqual(self.counts(), (3, 3, 1, 1))
        with self.factory() as session:
            self.assertEqual(session.scalar(select(Thread.message_count).where(Thread.conversation_key == "<m2@x>")), 1)

    def test_uidvalidity_change_rewalks_without_duplicates(self) -> None:
        self.seed_three_messages()
        self.sync()
        self.server.messages = {uid + 10: raw for uid, raw in self.server.messages.items()}
        self.server.uidvalidity = 2

        result = self.sync()

        self.assertTrue(result.folders[0].full_rewalk)
        self.assertEqual((result.kept, result.skipped), (0, 3))
        self.assertEqual(self.counts(), (3, 3, 1, 1))
        self.assertEqual((self.state().uid_validity, self.state().last_seen_uid), (2, 13))

    # -- incremental -----------------------------------------------------------------

    def test_incremental_sync_fetches_only_new_messages(self) -> None:
        self.seed_three_messages()
        self.sync()
        self.server.fetch_calls.clear()
        self.server.add(4, build_message(subject="New arrival", message_id="<m4@x>"))

        result = self.sync()

        self.assertEqual((result.candidates, result.kept), (1, 1))
        self.assertEqual(self.server.fetch_calls, [4])
        self.assertEqual(self.state().last_seen_uid, 4)

    def test_initial_fetch_limit_caps_the_first_sync_only(self) -> None:
        for uid in range(1, 6):
            self.server.add(uid, build_message(subject=f"msg {uid}", message_id=f"<m{uid}@x>"))

        first = self.sync(initial_fetch_limit=2)
        self.assertEqual((first.candidates, first.kept), (2, 2))
        self.assertEqual(self.state().last_seen_uid, 5)
        self.assertEqual(self.sync(initial_fetch_limit=2).candidates, 0)

    # -- threads ---------------------------------------------------------------------

    def test_replies_join_the_root_thread_and_roll_up(self) -> None:
        self.server.add(1, build_message(subject="Invoice #42", message_id="<root@x>", date="Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(
            2,
            build_message(
                subject="Re: Invoice #42",
                sender="Ravi <ravi@example.com>",
                message_id="<reply1@x>",
                in_reply_to="<root@x>",
                references="<root@x>",
                date="Mon, 01 Sep 2026 10:00:00 +0000",
                attachments=[("invoice_42.pdf", "application/pdf", PDF)],
            ),
        )
        self.server.add(
            3,
            build_message(
                subject="Re: Re: Invoice #42",
                message_id="<reply2@x>",
                in_reply_to="<reply1@x>",
                references="<root@x> <reply1@x>",
                date="Mon, 01 Sep 2026 11:00:00 +0000",
            ),
        )

        self.sync()

        with self.factory() as session:
            threads = repo.list_threads(session)
            self.assertEqual(len(threads), 1)
            thread = threads[0]
            self.assertEqual((thread.conversation_key, thread.message_count, thread.has_payment), ("<root@x>", 3, True))
            self.assertEqual(thread.subject, "Invoice #42")
            self.assertEqual(thread.participants, "Someone, Ravi")
            self.assertEqual(thread.attachment_count, 1)
            self.assertEqual(thread.last_message_at.hour, 11)
            emails = repo.list_thread_emails(session, thread.id)
        self.assertEqual([e.subject for e in emails], ["Re: Re: Invoice #42", "Re: Invoice #42", "Invoice #42"])
        self.assertEqual([e.matched_directly for e in emails], [False, True, False])

    def test_reply_to_uncached_root_joins_a_cached_ancestor(self) -> None:
        # The root itself is older than the sync window; only a later reply is cached.
        self.server.add(5, build_message(subject="Re: Plan", message_id="<r1@x>", in_reply_to="<root@x>", references="<root@x>"))
        self.server.add(6, build_message(subject="Re: Re: Plan", message_id="<r2@x>", in_reply_to="<r1@x>", references="<root@x> <r1@x>"))
        self.sync()
        with self.factory() as session:
            self.assertEqual(repo.count_threads(session), 1)

    # -- participants, attachments, decisions -------------------------------------------

    def test_participants_attachments_and_decision_log_are_written(self) -> None:
        self.seed_three_messages()
        self.sync()

        with self.factory() as session:
            email = session.scalar(select(Email).where(Email.message_id == "<m2@x>"))
            people = session.execute(select(EmailParticipant.role, EmailParticipant.address).where(EmailParticipant.email_id == email.id).order_by(EmailParticipant.id)).all()
            att = session.scalar(select(Attachment).where(Attachment.email_id == email.id))
            log = session.get(DecisionLog, "<m2@x>")
            files = repo.get_attachment_files(session, email.id, self.store)
            detail = repo.get_email_detail(session, email.id)

        self.assertEqual(people, [("from", "statements@acme.example"), ("to", "me@example.com"), ("cc", "partner@example.com")])
        self.assertEqual((email.doc_type, email.confidence, email.matched_directly), ("statement", 0.95, True))
        self.assertEqual(att.blob_key, f"{hashlib.sha256(PDF).hexdigest()}/statement_aug.pdf")
        self.assertEqual((att.doc_type, att.confidence), ("statement", 0.95))
        self.assertTrue((self.att_dir / hashlib.sha256(PDF).hexdigest() / "statement_aug.pdf").exists())
        self.assertEqual((log.tier, log.decision, log.confidence), (1, "payment", 0.95))
        self.assertIn("statement_aug.pdf", log.reason)
        self.assertEqual(files[0].content, PDF)
        self.assertEqual([p.address for p in detail.by_role("cc")], ["partner@example.com"])

        with self.factory() as session:
            plain = session.get(DecisionLog, "<m3@x>")
        self.assertEqual((plain.tier, plain.decision, plain.confidence), (0, "none", None))

    def test_missing_file_is_reported_per_attachment(self) -> None:
        self.seed_three_messages()
        self.sync()
        for path in self.att_dir.rglob("*.pdf"):
            path.unlink()
        with self.factory() as session:
            email_id = session.scalar(select(Attachment.email_id))
            files = repo.get_attachment_files(session, email_id, self.store)
        self.assertIsNone(files[0].content)
        self.assertIn("missing", files[0].error)

    def test_reclassify_repairs_labels_and_thread_flags(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session, session.begin():
            session.execute(update(Email).values(doc_type=None, matched_directly=False))
            session.execute(update(Thread).values(has_payment=False))
            session.execute(update(DecisionLog).values(decision="none", tier=0))

        with self.factory() as session, session.begin():
            changed = repo.reclassify_all(session)

        self.assertEqual(changed, 1)
        self.assertEqual(self.counts()[3], 1)
        with self.factory() as session:
            self.assertEqual(session.get(DecisionLog, "<m2@x>").decision, "payment")
            self.assertEqual(repo.count_by_decision(session), {"payment": 1, "none": 2})
        with self.factory() as session, session.begin():
            self.assertEqual(repo.reclassify_all(session), 0)

    # -- resilience ---------------------------------------------------------------------

    def test_single_failed_fetch_is_counted_and_run_continues(self) -> None:
        self.seed_three_messages()
        self.server.fail_fetch[2] = MailClientError("server hiccup on this one message")

        result = self.sync()

        self.assertEqual((result.status, result.kept, result.failed), (STATUS_OK, 2, 1))
        self.assertEqual(self.state().last_seen_uid, 3)

    def test_garbage_message_is_stored_not_fatal(self) -> None:
        self.seed_three_messages()
        self.server.add(4, b"\xff\xfe this is not an email \x00")

        result = self.sync()

        self.assertEqual((result.kept, result.failed), (4, 0))

    def test_connection_lost_marks_partial_and_next_run_resumes(self) -> None:
        self.seed_three_messages()
        self.server.fail_fetch[2] = MailConnectionLost("socket closed")

        first = self.sync()
        self.assertEqual((first.status, first.kept), (STATUS_PARTIAL, 1))
        self.assertEqual(self.state().last_seen_uid, 1)

        self.server.fail_fetch.clear()
        second = self.sync()
        self.assertEqual((second.status, second.candidates, second.kept), (STATUS_OK, 2, 2))
        self.assertEqual(self.counts()[1], 3)

    def test_inbox_and_sent_folders_complete_a_thread(self) -> None:
        self.server.add(1, build_message(subject="Invoice #7", sender="Ravi <ravi@example.com>", message_id="<root@x>", date="Mon, 01 Sep 2026 09:00:00 +0000"))
        sent = FakeMailServer(uidvalidity=7)
        sent.add(1, build_message(subject="Re: Invoice #7", sender="Me <me@example.com>", to="ravi@example.com", message_id="<mine@x>", in_reply_to="<root@x>", references="<root@x>", date="Mon, 01 Sep 2026 10:00:00 +0000"))
        self.server.add(2, build_message(subject="Re: Re: Invoice #7", sender="Ravi <ravi@example.com>", message_id="<r2@x>", in_reply_to="<mine@x>", references="<root@x> <mine@x>", date="Mon, 01 Sep 2026 11:00:00 +0000"))
        self.servers["[Gmail]/Sent Mail"] = sent

        result = self.sync(imap_folders=("INBOX", "[Gmail]/Sent Mail"))

        self.assertEqual((result.status, result.kept), (STATUS_OK, 3))
        self.assertEqual([fr.folder for fr in result.folders], ["INBOX", "[Gmail]/Sent Mail"])
        self.assertEqual([fr.last_seen_uid for fr in result.folders], [2, 1])
        with self.factory() as session:
            threads = repo.list_threads(session)
            self.assertEqual(len(threads), 1)
            self.assertEqual((threads[0].message_count, threads[0].participants), (3, "Ravi, Me"))
            self.assertEqual(repo.get_sync_state(session, "[Gmail]/Sent Mail").uid_validity, 7)
            self.assertEqual(repo.get_sync_state(session, "INBOX").last_seen_uid, 2)

        again = self.sync(imap_folders=("INBOX", "[Gmail]/Sent Mail"))
        self.assertEqual((again.candidates, again.kept), (0, 0))

    def test_failed_folder_stops_the_run_with_failed_status(self) -> None:
        self.seed_three_messages()

        def factory(_s, folder):
            if folder == "BROKEN":
                raise MailClientError("no such folder")
            return FakeMailClient(self.server, folder)

        result = run_sync(dataclasses.replace(self.settings, imap_folders=("INBOX", "BROKEN")), self.factory, factory, store=self.store)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("BROKEN", result.error)
        self.assertEqual(result.kept, 3)

    def test_list_threads_filters(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session:
            self.assertEqual([t.subject for t in repo.list_threads(session, payment_only=True)], ["Your account statement"])
            self.assertEqual([t.subject for t in repo.list_threads(session, search="ravi")], ["Lunch?"])
            self.assertEqual([t.subject for t in repo.list_threads(session, search="acme")], ["Your account statement"])


class KeepOnlyPaymentFlowTests(SyncTests):
    """How data moves on one refresh when STORE_ONLY_PAYMENT is on."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = dataclasses.replace(self.settings, store_only_payment=True)

    # Inherited tests are re-run under the payment-only policy only where they
    # still hold; the ones that assume everything is stored are overridden.
    def test_first_sync_persists_and_second_sync_changes_nothing(self) -> None:
        self.seed_three_messages()

        first = self.sync()
        self.assertEqual((first.kept, first.payment_hits, first.dropped, first.skipped, first.failed), (1, 1, 2, 0, 0))
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        with self.factory() as session:
            self.assertEqual(repo.count_decisions(session), 3)  # every message judged
            self.assertEqual(repo.count_by_decision(session), {"payment": 1, "none": 2})

        second = self.sync()
        self.assertEqual((second.candidates, second.kept, second.dropped), (0, 0, 0))

    def test_rewalk_after_state_reset_adds_no_duplicate_rows(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session, session.begin():
            repo.delete_sync_state(session, self.settings.imap_folder)

        result = self.sync()

        # Dropped mail is "already judged" and never re-judged; kept mail is already stored.
        self.assertEqual((result.candidates, result.kept, result.dropped, result.skipped), (3, 0, 0, 3))
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_uidvalidity_change_rewalks_without_duplicates(self) -> None:
        self.seed_three_messages()
        self.sync()
        self.server.messages = {uid + 10: raw for uid, raw in self.server.messages.items()}
        self.server.uidvalidity = 2

        result = self.sync()

        self.assertTrue(result.folders[0].full_rewalk)
        self.assertEqual((result.kept, result.skipped), (0, 3))
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_incremental_sync_fetches_only_new_messages(self) -> None:
        self.seed_three_messages()
        self.sync()
        self.server.fetch_calls.clear()
        self.server.add(4, build_message(subject="New arrival", message_id="<m4@x>"))

        result = self.sync()

        self.assertEqual((result.candidates, result.dropped), (1, 1))
        self.assertEqual(self.server.fetch_calls, [4])

    def test_initial_fetch_limit_caps_the_first_sync_only(self) -> None:
        for uid in range(1, 6):
            self.server.add(uid, build_message(subject=f"msg {uid}", message_id=f"<m{uid}@x>"))

        first = self.sync(initial_fetch_limit=2)
        self.assertEqual((first.candidates, first.dropped), (2, 2))
        self.assertEqual(self.state().last_seen_uid, 5)

    def test_replies_join_the_root_thread_and_roll_up(self) -> None:
        self.skipTest("covered by the backfill and payment-thread tests under this policy")

    def test_reply_to_uncached_root_joins_a_cached_ancestor(self) -> None:
        self.skipTest("root and reply are both plain mail; nothing is stored under this policy")

    def test_participants_attachments_and_decision_log_are_written(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session:
            self.assertEqual(repo.count_emails(session), 1)
            plain = session.get(DecisionLog, "<m3@x>")
        self.assertEqual((plain.tier, plain.decision), (0, "none"))

    def test_missing_file_is_reported_per_attachment(self) -> None:
        super().test_missing_file_is_reported_per_attachment()

    def test_reclassify_repairs_labels_and_thread_flags(self) -> None:
        self.skipTest("reclassify covers stored rows only; exercised under keep-everything")

    def test_single_failed_fetch_is_counted_and_run_continues(self) -> None:
        self.seed_three_messages()
        self.server.fail_fetch[2] = MailClientError("server hiccup on this one message")

        result = self.sync()

        self.assertEqual((result.status, result.kept, result.dropped, result.failed), (STATUS_OK, 0, 2, 1))

    def test_garbage_message_is_stored_not_fatal(self) -> None:
        self.seed_three_messages()
        self.server.add(4, b"\xff\xfe this is not an email \x00")
        result = self.sync()
        self.assertEqual((result.kept, result.dropped, result.failed), (1, 3, 0))

    def test_connection_lost_marks_partial_and_next_run_resumes(self) -> None:
        self.seed_three_messages()
        self.server.fail_fetch[2] = MailConnectionLost("socket closed")

        first = self.sync()
        self.assertEqual((first.status, first.dropped), (STATUS_PARTIAL, 1))
        self.assertEqual(self.state().last_seen_uid, 1)

        self.server.fail_fetch.clear()
        second = self.sync()
        self.assertEqual((second.status, second.candidates, second.kept, second.dropped), (STATUS_OK, 2, 1, 1))

    def test_list_threads_filters(self) -> None:
        self.seed_three_messages()
        self.sync()
        with self.factory() as session:
            self.assertEqual([t.subject for t in repo.list_threads(session)], ["Your account statement"])

    def test_inbox_and_sent_folders_complete_a_thread(self) -> None:
        self.skipTest("multi-folder threading is exercised under keep-everything")

    def test_failed_folder_stops_the_run_with_failed_status(self) -> None:
        self.skipTest("exercised under keep-everything")

    # -- the flow itself -----------------------------------------------------------------

    def test_reply_into_payment_thread_is_kept_without_its_own_match(self) -> None:
        self.server.add(1, build_message(subject="Invoice #42", message_id="<root@x>", attachments=[("invoice_42.pdf", "application/pdf", PDF)]))
        self.sync()
        self.server.add(2, build_message(subject="Re: Invoice #42", sender="Ravi <ravi@example.com>", message_id="<reply@x>", in_reply_to="<root@x>", references="<root@x>", text="Thanks, paid."))

        result = self.sync()

        self.assertEqual((result.kept, result.payment_hits, result.dropped), (1, 0, 0))
        with self.factory() as session:
            thread = repo.list_threads(session)[0]
            self.assertEqual((thread.message_count, thread.has_payment), (2, True))
            log = session.get(DecisionLog, "<reply@x>")
        self.assertEqual(log.decision, "none")
        self.assertIn("kept as part of a payment thread", log.reason)

    def test_dropped_ancestors_are_backfilled_when_thread_turns_payment(self) -> None:
        self.server.add(1, build_message(subject="Plan", message_id="<root@x>", date="Mon, 01 Sep 2026 09:00:00 +0000"))
        self.server.add(2, build_message(subject="Re: Plan", message_id="<r1@x>", in_reply_to="<root@x>", references="<root@x>", date="Mon, 01 Sep 2026 10:00:00 +0000"))
        first = self.sync()
        self.assertEqual((first.kept, first.dropped), (0, 2))
        self.assertEqual(self.counts()[1], 0)

        self.server.add(
            3,
            build_message(
                subject="Re: Plan - statement attached",
                message_id="<r2@x>",
                in_reply_to="<r1@x>",
                references="<root@x> <r1@x>",
                date="Mon, 01 Sep 2026 11:00:00 +0000",
                attachments=[("statement_sep.pdf", "application/pdf", PDF)],
            ),
        )
        second = self.sync()

        # kept counts the message itself; the two earlier mails are reported as backfilled
        self.assertEqual((second.kept, second.payment_hits, second.backfilled), (1, 1, 2))
        with self.factory() as session:
            threads = repo.list_threads(session)
            self.assertEqual(len(threads), 1)
            self.assertEqual((threads[0].conversation_key, threads[0].message_count, threads[0].has_payment), ("<root@x>", 3, True))
            emails = repo.list_thread_emails(session, threads[0].id)
            self.assertEqual([e.subject for e in emails], ["Re: Plan - statement attached", "Re: Plan", "Plan"])
            self.assertIn("backfilled", session.get(DecisionLog, "<root@x>").reason)

        # A third run judges nothing again and stores nothing twice.
        third = self.sync()
        self.assertEqual((third.candidates, third.kept, third.dropped, third.skipped), (0, 0, 0, 0))
        self.assertEqual(self.counts()[1], 3)


if __name__ == "__main__":
    unittest.main()
