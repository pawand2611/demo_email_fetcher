"""Backend API end to end, in-process: temporary SQLite, fake IMAP server,
fake document model."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import Services, create_app
from mailbox_viewer.attachment_store import FileSystemStore
from mailbox_viewer.config import Settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory

from mailbox_viewer.classifier import Prediction

from .fakes import FakeBodyModel, FakeDocumentModel, FakeMailClient, FakeMailServer, build_message, fake_classifier

PDF = b"%PDF-1.4 pretend invoice"


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            imap_host="fake",
            imap_port=993,
            imap_user="me@example.com",
            imap_password="secret",
            imap_folder="INBOX",
            imap_timeout=5,
            database_url=f"sqlite:///{(root / 'api.db').as_posix()}",
            initial_fetch_limit=200,
            attachment_dir=str(root / "attachments"),
        )
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.server = FakeMailServer()
        self.model = FakeDocumentModel()
        self.body_model = FakeBodyModel()
        services = Services(
            settings=self.settings,
            engine=self.engine,
            session_factory=make_session_factory(self.engine),
            store=FileSystemStore(self.settings.attachment_dir),
            classifier=fake_classifier(self.body_model, self.model),
            client_factory=lambda _s, folder: FakeMailClient(self.server, folder),
        )
        self.app = create_app(services)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------------------

    def seed(self) -> None:
        self.server.add(1, build_message(subject="Invoice #42", sender="Acme <billing@acme.example>", message_id="<root@x>",
                                         date="Mon, 01 Sep 2026 09:00:00 +0000", attachments=[("invoice_aug.pdf", "application/pdf", PDF)]))
        self.server.add(2, build_message(subject="Re: Invoice #42", sender="Ravi <ravi@example.com>", message_id="<reply@x>",
                                         in_reply_to="<root@x>", references="<root@x>", date="Mon, 01 Sep 2026 10:00:00 +0000"))
        self.server.add(3, build_message(subject="Lunch?", sender="Mia <mia@example.com>", message_id="<lunch@x>"))

    def run_sync(self) -> dict:
        started = self.client.post("/sync")
        self.assertEqual(started.status_code, 202)
        job = self.app.state.jobs.get(started.json()["id"])
        self.assertTrue(job.done.wait(10), "sync job did not finish")
        return self.client.get(f"/sync/{job.id}").json()

    # -- tests ----------------------------------------------------------------------

    def test_health_and_profile(self) -> None:
        health = self.client.get("/health").json()
        self.assertEqual((health["status"], health["database"], health["model"]), ("ok", "ok", "body: fake-body, attachments: fake-attachment"))

        [profile] = self.client.get("/profiles").json()
        self.assertEqual(profile["name"], "statement_recon")
        self.assertEqual(profile["folders"], ["INBOX"])
        self.assertEqual(profile["keep_policy"], "every_message")
        self.assertEqual(profile["payment_labels"], ["invoice", "receipt", "statement"])

    def test_sync_job_then_threads_messages_and_download(self) -> None:
        self.seed()
        job = self.run_sync()

        self.assertEqual(job["state"], "succeeded")
        self.assertEqual((job["result"]["status"], job["result"]["kept"], job["result"]["payment_hits"]), ("ok", 3, 1))  # the reply inherits the thread's payment status
        self.assertEqual(job["result"]["folders"][0]["last_seen_uid"], 3)
        self.assertTrue(job["finished_at"].endswith("Z") or "+00:00" in job["finished_at"])

        threads = self.client.get("/threads").json()
        # newest conversation first: the invoice reply (10:00) is newer than "Lunch?" (09:30)
        self.assertEqual([t["subject"] for t in threads], ["Invoice #42", "Lunch?"])
        self.assertEqual([t["subject"] for t in self.client.get("/threads", params={"payment_only": True}).json()], ["Invoice #42"])
        self.assertEqual([t["subject"] for t in self.client.get("/threads", params={"search": "ravi"}).json()], ["Invoice #42"])

        invoice = next(t for t in threads if t["subject"] == "Invoice #42")
        self.assertEqual((invoice["message_count"], invoice["has_payment"], invoice["attachment_count"]), (2, True, 1))
        self.assertEqual(invoice["last_message_at"], "2026-09-01T10:00:00Z")  # UTC with an explicit offset

        detail = self.client.get(f"/threads/{invoice['id']}").json()
        self.assertEqual([m["subject"] for m in detail["messages"]], ["Re: Invoice #42", "Invoice #42"])
        root = detail["messages"][1]
        self.assertEqual((root["doc_type"], root["confidence"], root["matched_directly"]), ("invoice", 0.95, True))
        self.assertEqual(root["decision"]["decision"], "payment")
        self.assertEqual(root["participants"][0], {"role": "from", "name": "Acme", "address": "billing@acme.example"})
        [att] = root["attachments"]
        self.assertEqual(att["blob_key"], f"{hashlib.sha256(PDF).hexdigest()}/invoice_aug.pdf")

        download = self.client.get(att["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, PDF)
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertIn("invoice_aug.pdf", download.headers["content-disposition"])

        decision = self.client.get("/decisions/<root@x>").json()
        self.assertEqual((decision["tier"], decision["decision"]), (1, "payment"))  # decided by the invoice attachment alone

    def test_second_sync_is_idempotent_and_stats_add_up(self) -> None:
        self.seed()
        self.run_sync()
        again = self.run_sync()
        self.assertEqual((again["result"]["candidates"], again["result"]["kept"]), (0, 0))

        stats = self.client.get("/stats").json()
        self.assertEqual((stats["threads"], stats["emails"], stats["attachments"], stats["payment_threads"]), (2, 3, 1, 1))
        self.assertEqual((stats["judged"], stats["dropped"]), (3, 0))
        self.assertEqual(stats["by_decision"], {"payment": 2, "none": 1})  # root, and the reply by inheritance
        self.assertEqual(stats["needs_review"], 0)
        self.assertEqual(stats["sync_states"][0]["last_seen_uid"], 3)

        latest = self.client.get("/sync/latest").json()
        self.assertEqual(latest["id"], again["id"])

    def test_reclassify_runs_both_models_as_a_background_job(self) -> None:
        self.seed()
        self.run_sync()
        self.model.calls.clear()
        self.body_model.calls.clear()

        started = self.client.post("/reclassify")
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["kind"], "reclassify")
        job = self.app.state.jobs.get(started.json()["id"])
        self.assertTrue(job.done.wait(10))
        out = self.client.get(f"/jobs/{job.id}").json()

        self.assertEqual(out["state"], "succeeded")
        self.assertEqual(out["result"], {"changed": 0, "model": "body: fake-body, attachments: fake-attachment"})
        self.assertEqual(self.model.calls, ["invoice_aug.pdf"])  # attachment bytes re-read from the store
        self.assertEqual(self.body_model.calls, ["Lunch?"])  # root decided by its attachment, reply inherited
        self.assertEqual(self.client.get("/stats").json()["payment_threads"], 1)

    def test_unsure_invoice_attachment_without_body_support_is_review(self) -> None:
        self.model.overrides["scan.pdf"] = Prediction("invoice", 0.7)
        self.server.add(1, build_message(subject="Documents", text="See attached.", message_id="<rv@x>",
                                         attachments=[("scan.pdf", "application/pdf", PDF)]))
        self.run_sync()
        stats = self.client.get("/stats").json()
        self.assertEqual((stats["needs_review"], stats["payment_threads"]), (1, 0))
        self.assertEqual(self.client.get("/decisions/<rv@x>").json()["decision"], "review")

    def test_not_found_and_validation(self) -> None:
        self.assertEqual(self.client.get("/threads/999").status_code, 404)
        self.assertEqual(self.client.get("/attachments/999").status_code, 404)
        self.assertEqual(self.client.get("/sync/nope").status_code, 404)
        self.assertEqual(self.client.get("/decisions/<missing@x>").status_code, 404)
        self.assertEqual(self.client.get("/tables/users/rows").status_code, 404)
        self.assertEqual(self.client.get("/tables/emails/rows", params={"order_by": "nope"}).status_code, 422)
        self.assertEqual(self.client.get("/schema", params={"dialect": "oracle"}).status_code, 422)

    def test_schema_ddl_and_tables(self) -> None:
        self.seed()
        self.run_sync()

        schema = self.client.get("/schema", params={"dialect": "postgresql"}).json()
        self.assertEqual([t["name"] for t in schema], ["threads", "emails", "email_participants", "attachments", "sync_state", "decision_log"])
        emails = next(t for t in schema if t["name"] == "emails")
        message_id = next(c for c in emails["columns"] if c["name"] == "message_id")
        self.assertTrue(message_id["unique"])
        thread_id = next(c for c in emails["columns"] if c["name"] == "thread_id")
        self.assertEqual(thread_id["references"], ["threads.id"])

        ddl = self.client.get("/schema/ddl", params={"dialect": "postgresql"}).text
        self.assertIn("CREATE TABLE threads", ddl)
        self.assertIn("SERIAL", ddl)

        tables = {t["name"]: t["rows"] for t in self.client.get("/tables").json()}
        self.assertEqual((tables["threads"], tables["emails"], tables["email_participants"]), (2, 3, 6))
        rows = self.client.get("/tables/emails/rows", params={"limit": 2, "order_by": "received_at"}).json()
        self.assertEqual((rows["total"], len(rows["rows"])), (3, 2))
        self.assertIn("message_id", rows["columns"])

    def test_reclassify_refused_while_sync_runs(self) -> None:
        from datetime import datetime, timezone

        from api.jobs import Job

        jobs = self.app.state.jobs
        jobs._running = Job(id="busy", kind="sync", state="running", started_at=datetime.now(timezone.utc))
        try:
            self.assertEqual(self.client.post("/reclassify").status_code, 409)
            self.assertEqual(self.client.post("/sync").json()["id"], "busy")  # same kind: the running job is returned
        finally:
            jobs._running = None


if __name__ == "__main__":
    unittest.main()
