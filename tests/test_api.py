"""Backend API end to end, in-process: temporary SQLite, fake IMAP server,
fake document model. Also exercises the frontend's ApiClient against it."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import Services, create_app
from frontend.api_client import ApiClient, ApiError
from mailbox_viewer.attachment_store import FileSystemStore
from mailbox_viewer.config import Settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory

from .fakes import FakeDocumentModel, FakeMailClient, FakeMailServer, build_message

PDF = b"%PDF-1.4 pretend statement"


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
        services = Services(
            settings=self.settings,
            engine=self.engine,
            session_factory=make_session_factory(self.engine),
            store=FileSystemStore(self.settings.attachment_dir),
            model=self.model,
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
                                         date="Mon, 01 Sep 2026 09:00:00 +0000", attachments=[("statement_aug.pdf", "application/pdf", PDF)]))
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
        self.assertEqual((health["status"], health["database"], health["model"]), ("ok", "ok", "fake"))

        [profile] = self.client.get("/profiles").json()
        self.assertEqual(profile["name"], "statement_recon")
        self.assertEqual(profile["folders"], ["INBOX"])
        self.assertEqual(profile["keep_policy"], "every_message")
        self.assertEqual(profile["payment_labels"], ["invoice", "receipt", "statement"])

    def test_sync_job_then_threads_messages_and_download(self) -> None:
        self.seed()
        job = self.run_sync()

        self.assertEqual(job["state"], "succeeded")
        self.assertEqual((job["result"]["status"], job["result"]["kept"], job["result"]["payment_hits"]), ("ok", 3, 1))
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
        self.assertEqual((root["doc_type"], root["confidence"], root["matched_directly"]), ("statement", 0.95, True))
        self.assertEqual(root["decision"]["decision"], "payment")
        self.assertEqual(root["participants"][0], {"role": "from", "name": "Acme", "address": "billing@acme.example"})
        [att] = root["attachments"]
        self.assertEqual(att["blob_key"], f"{hashlib.sha256(PDF).hexdigest()}/statement_aug.pdf")

        download = self.client.get(att["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, PDF)
        self.assertEqual(download.headers["content-type"], "application/pdf")
        self.assertIn("statement_aug.pdf", download.headers["content-disposition"])

        decision = self.client.get("/decisions/<root@x>").json()
        self.assertEqual((decision["tier"], decision["decision"]), (1, "payment"))

    def test_second_sync_is_idempotent_and_stats_add_up(self) -> None:
        self.seed()
        self.run_sync()
        again = self.run_sync()
        self.assertEqual((again["result"]["candidates"], again["result"]["kept"]), (0, 0))

        stats = self.client.get("/stats").json()
        self.assertEqual((stats["threads"], stats["emails"], stats["attachments"], stats["payment_threads"]), (2, 3, 1, 1))
        self.assertEqual((stats["judged"], stats["dropped"]), (3, 0))
        self.assertEqual(stats["by_decision"], {"payment": 1, "none": 2})
        self.assertEqual(stats["sync_states"][0]["last_seen_uid"], 3)

        latest = self.client.get("/sync/latest").json()
        self.assertEqual(latest["id"], again["id"])

    def test_reclassify_uses_the_model_and_stored_files(self) -> None:
        self.seed()
        self.run_sync()
        self.model.calls.clear()

        out = self.client.post("/reclassify").json()

        # Only the stored PDF is read again. The reply loses its sync-time note
        # "kept as part of a payment thread", which counts as one change.
        self.assertEqual(out, {"changed": 1, "model": "fake"})
        self.assertEqual(self.model.calls, ["statement_aug.pdf"])
        self.assertEqual(self.client.post("/reclassify").json()["changed"], 0)
        self.assertEqual(self.client.get("/stats").json()["payment_threads"], 1)

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
        jobs = self.app.state.jobs
        jobs._running = object()  # simulate a sync in progress
        try:
            self.assertEqual(self.client.post("/reclassify").status_code, 409)
        finally:
            jobs._running = None


class ApiClientTests(unittest.TestCase):
    """The frontend's client, pointed at the in-process app."""

    def setUp(self) -> None:
        self.api_tests = ApiTests("test_health_and_profile")
        self.api_tests.setUp()
        self.api = ApiClient(base_url="http://testserver", http=self.api_tests.client)

    def tearDown(self) -> None:
        self.api_tests.tearDown()

    def test_client_round_trip(self) -> None:
        self.api_tests.seed()
        job = self.api.start_sync()
        self.api_tests.app.state.jobs.get(job["id"]).done.wait(10)
        self.assertEqual(self.api.sync_job(job["id"])["state"], "succeeded")

        threads = self.api.threads(search="acme")
        self.assertEqual([t["subject"] for t in threads], ["Invoice #42"])
        detail = self.api.thread(threads[0]["id"])
        att = detail["messages"][-1]["attachments"][0]
        self.assertEqual(self.api.attachment_bytes(att["id"]), PDF)
        self.assertEqual(self.api.stats()["emails"], 3)
        self.assertIn("CREATE TABLE", self.api.ddl("sqlite"))
        self.assertEqual(self.api.table_rows("threads", limit=1)["total"], 2)

    def test_errors_become_api_errors(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.api.thread(999)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_unreachable_backend_explains_how_to_start_it(self) -> None:
        api = ApiClient(base_url="http://127.0.0.1:9")  # nothing listens on the discard port
        with self.assertRaises(ApiError) as ctx:
            api.health()
        self.assertIn("uvicorn backend.main:create_app", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
