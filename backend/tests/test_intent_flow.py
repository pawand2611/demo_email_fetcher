"""Intent classification end to end on a throw-away SQLite file and a fake
mailbox: the sync stores intents for payment emails (model-decided and
inherited), the tier-2 job resolves the pending ones, reclassify and redecide
keep intents consistent, and the API exposes them."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.main import Services, create_app
from mailbox_viewer import repository as repo
from mailbox_viewer.attachment_store import FileSystemStore
from mailbox_viewer.config import ConfigError, Settings, load_settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.intent import (
    DECIDED_FALLBACK,
    DECIDED_TIER1,
    DECIDED_TIER2,
    TIER2_OK,
    TIER2_PENDING,
    TIER2_UNAVAILABLE,
    Tier2Result,
)
from mailbox_viewer.intent_jobs import resolve_pending_intents
from mailbox_viewer.models import Email, EmailIntent
from mailbox_viewer.sync import run_sync

from .fakes import (
    FakeBodyModel,
    FakeDocumentModel,
    FakeIntentTier1,
    FakeIntentTier2,
    FakeMailClient,
    FakeMailServer,
    build_message,
    fake_classifier,
    fake_intent_stage,
)

PDF = b"%PDF-1.4 pretend invoice"


def settings_for(root: Path, **overrides) -> Settings:
    base = Settings(
        imap_host="fake", imap_port=993, imap_user="me@example.com", imap_password="secret", imap_folder="INBOX",
        imap_timeout=5, database_url=f"sqlite:///{(root / 'cache.db').as_posix()}", initial_fetch_limit=200,
        attachment_dir=str(root / "attachments"), store_only_payment=True,
    )
    return dataclasses.replace(base, **overrides)


class IntentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_for(Path(self.tmp.name))
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.factory = make_session_factory(self.engine)
        self.server = FakeMailServer()
        self.store = FileSystemStore(self.settings.attachment_dir)
        self.tier1 = FakeIntentTier1()
        self.tier2 = FakeIntentTier2()
        self.body = FakeBodyModel()
        self.classifier = fake_classifier(body=self.body, intent=fake_intent_stage(self.tier1, self.tier2))

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def sync(self):
        return run_sync(self.settings, self.factory, lambda _s, folder: FakeMailClient(self.server, folder),
                        store=self.store, classifier=self.classifier)

    def seed(self) -> None:
        self.server.add(1, build_message(subject="Invoice #42", sender="Acme <billing@acme.example>",
                                         message_id="<root@x>", text="Please pay by Friday.",
                                         date="Mon, 01 Sep 2026 09:00:00 +0000",
                                         attachments=[("invoice_aug.pdf", "application/pdf", PDF)]))
        # A reply in the payment thread: payment by inheritance; Laya's document question is not asked.
        self.server.add(2, build_message(subject="Re: Invoice #42", sender="Ravi <ravi@example.com>",
                                         message_id="<reply@x>", in_reply_to="<root@x>", references="<root@x>",
                                         text="We paid half today, the rest next week.",
                                         date="Mon, 01 Sep 2026 10:00:00 +0000"))
        self.server.add(3, build_message(subject="Lunch?", sender="Mia <mia@example.com>", message_id="<lunch@x>"))
        self.tier1.overrides["Re: Invoice #42"] = ("payment_reminder", 0.19)  # unsure: goes to tier 2

    def intents(self) -> dict[str, EmailIntent]:
        with self.factory() as session:
            rows = session.execute(select(Email.message_id, EmailIntent).join(EmailIntent)).all()
            for _, row in rows:
                session.expunge(row)
            return {mid: row for mid, row in rows}

    # -- sync -------------------------------------------------------------------------

    def test_sync_gives_payment_emails_an_intent_and_queues_the_unsure_ones(self) -> None:
        self.seed()
        result = self.sync()

        self.assertEqual((result.kept, result.dropped, result.intents, result.intents_pending), (2, 1, 2, 1))
        self.assertIn("intents: 2 classified, 1 waiting for tier 2", result.summary())
        intents = self.intents()
        self.assertEqual(set(intents), {"<root@x>", "<reply@x>"})  # the lunch email is not payment

        root = intents["<root@x>"]
        self.assertEqual((root.intent, root.decided_by, root.needs_review, root.tier2_status, root.tier2_input),
                         ("new_invoice", DECIDED_TIER1, False, None, None))
        self.assertAlmostEqual(sum(root.tier1_probabilities.values()), 1.0)

        reply = intents["<reply@x>"]
        self.assertEqual((reply.intent, reply.decided_by, reply.needs_review, reply.tier2_status),
                         ("payment_reminder", DECIDED_TIER1, True, TIER2_PENDING))
        self.assertEqual(reply.tier2_input["body"], "We paid half today, the rest next week.")
        self.assertEqual(self.tier2.states, [])  # not run during the sync
        # Laya's document question: skipped for the invoice (the attachment decided) and the inherited reply.
        self.assertEqual(self.body.calls, ["Lunch?"])

    def test_tier2_job_resolves_pending_intents_and_forgets_the_text(self) -> None:
        self.seed()
        self.sync()

        counts = resolve_pending_intents(self.factory, self.tier2, limit=10)

        self.assertEqual(counts, {"processed": 1, "tier2": 1, "fallback": 0, "skipped": 0, "remaining": 0})
        self.assertEqual(self.tier2.states[0]["subject"], "Re: Invoice #42")  # the same state tier 1 saw
        reply = self.intents()["<reply@x>"]
        self.assertEqual((reply.intent, reply.decided_by, reply.needs_review, reply.tier2_intent, reply.tier2_status),
                         ("partial_payment", DECIDED_TIER2, False, "partial_payment", TIER2_OK))
        self.assertEqual(reply.tier1_intent, "payment_reminder")
        self.assertIsNone(reply.tier2_input)
        self.assertEqual(resolve_pending_intents(self.factory, self.tier2, limit=10)["processed"], 0)

    def test_unavailable_tier2_falls_back_and_can_be_retried(self) -> None:
        self.seed()
        self.sync()
        self.tier2.result = Tier2Result(None, TIER2_UNAVAILABLE, None)

        counts = resolve_pending_intents(self.factory, self.tier2, limit=10)

        self.assertEqual((counts["processed"], counts["fallback"], counts["remaining"]), (1, 1, 0))
        reply = self.intents()["<reply@x>"]
        self.assertEqual((reply.intent, reply.decided_by, reply.needs_review, reply.tier2_status),
                         ("payment_reminder", DECIDED_FALLBACK, True, TIER2_UNAVAILABLE))
        self.assertIsNotNone(reply.tier2_input)  # kept for a retry

        self.tier2.result = Tier2Result("partial_payment", TIER2_OK, 98.0)
        self.assertEqual(resolve_pending_intents(self.factory, self.tier2, limit=10)["processed"], 0)  # not without retry
        self.assertEqual(resolve_pending_intents(self.factory, self.tier2, limit=10, retry_failed=True)["tier2"], 1)
        self.assertEqual(self.intents()["<reply@x>"].decided_by, DECIDED_TIER2)

    def test_job_limit_bounds_one_run(self) -> None:
        self.tier1.overrides["Invoice #42"] = ("other", 0.1)
        self.seed()
        self.sync()
        counts = resolve_pending_intents(self.factory, self.tier2, limit=1)
        self.assertEqual((counts["processed"], counts["remaining"]), (1, 1))

    def test_tier2_disabled_falls_back_at_once(self) -> None:
        self.classifier = fake_classifier(intent=fake_intent_stage(self.tier1, self.tier2, tier2_enabled=False))
        self.seed()
        result = self.sync()
        self.assertEqual(result.intents_pending, 0)
        reply = self.intents()["<reply@x>"]
        self.assertEqual((reply.decided_by, reply.needs_review, reply.tier2_status, reply.tier2_input),
                         (DECIDED_FALLBACK, True, None, None))

    def test_without_an_intent_model_nothing_changes(self) -> None:
        self.classifier = fake_classifier()
        self.seed()
        result = self.sync()
        self.assertEqual((result.kept, result.intents), (2, 0))
        self.assertEqual(self.intents(), {})

    # -- reclassify / redecide ------------------------------------------------------------

    def test_reclassify_recomputes_intents(self) -> None:
        self.seed()
        self.sync()
        resolve_pending_intents(self.factory, self.tier2, limit=10)
        self.tier1.overrides["Invoice #42"] = ("credit_note", 0.8)

        with self.factory() as session, session.begin():
            repo.reclassify_all(session, self.store, self.classifier)

        intents = self.intents()
        self.assertEqual(intents["<root@x>"].intent, "credit_note")
        self.assertEqual(intents["<reply@x>"].tier2_status, TIER2_PENDING)  # re-queued: tier 1 is still unsure
        self.assertEqual(self.tier1.states[-1]["from"], "Ravi <ravi@example.com>")  # headers rebuilt from the database

    def test_redecide_removes_the_intent_of_an_email_that_is_no_longer_payment(self) -> None:
        self.seed()
        self.sync()
        # The attachment model's label is changed so the root is no longer an invoice.
        with self.factory() as session, session.begin():
            for att in session.scalars(select(Email).where(Email.message_id == "<root@x>")).one().attachments:
                att.doc_type, att.confidence = "not_invoice", 0.99
        with self.factory() as session, session.begin():
            repo.redecide_all(session, self.classifier)
        self.assertNotIn("<root@x>", self.intents())

    # -- database ---------------------------------------------------------------------------

    def test_database_refuses_a_label_outside_the_eight(self) -> None:
        self.seed()
        self.sync()
        with self.assertRaises(IntegrityError):
            with self.factory() as session, session.begin():
                session.scalars(select(EmailIntent)).first().intent = "refund"

    def test_deleting_an_email_deletes_its_intent(self) -> None:
        self.seed()
        self.sync()
        with self.factory() as session, session.begin():
            session.delete(session.scalars(select(Email).where(Email.message_id == "<root@x>")).one())
        self.assertNotIn("<root@x>", self.intents())

    def test_summary(self) -> None:
        self.seed()
        self.sync()
        resolve_pending_intents(self.factory, self.tier2, limit=10)
        with self.factory() as session:
            summary = repo.intent_summary(session)
        self.assertEqual((summary["total"], summary["pending"], summary["needs_review"]), (2, 0, 0))
        self.assertEqual(summary["by_decided_by"], {"tier1": 1, "tier2": 1})
        self.assertEqual(summary["by_intent"], {"new_invoice": 1, "partial_payment": 1})
        self.assertEqual((summary["tier2_failure_rate"], summary["tier2_seconds_p50"]), (0.0, 104.0))


class IntentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = settings_for(Path(self.tmp.name))
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.server = FakeMailServer()
        self.tier1 = FakeIntentTier1()
        self.tier2 = FakeIntentTier2()
        self.app = self.make_app(fake_intent_stage(self.tier1, self.tier2))
        self.client = TestClient(self.app)

    def make_app(self, stage):
        return create_app(Services(
            settings=self.settings, engine=self.engine, session_factory=make_session_factory(self.engine),
            store=FileSystemStore(self.settings.attachment_dir),
            classifier=fake_classifier(FakeBodyModel(), FakeDocumentModel(), intent=stage),
            client_factory=lambda _s, folder: FakeMailClient(self.server, folder),
        ))

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def wait(self, response) -> dict:
        self.assertEqual(response.status_code, 202, response.text)
        job = self.app.state.jobs.get(response.json()["id"])
        self.assertTrue(job.done.wait(10))
        return self.client.get(f"/jobs/{job.id}").json()

    def test_messages_carry_their_intent_and_tier2_job_resolves_them(self) -> None:
        self.server.add(1, build_message(subject="Invoice #7", message_id="<i7@x>",
                                         attachments=[("invoice_7.pdf", "application/pdf", PDF)]))
        self.tier1.overrides["Invoice #7"] = ("payment_reminder", 0.2)
        sync = self.wait(self.client.post("/sync"))
        self.assertEqual((sync["result"]["intents"], sync["result"]["intents_pending"]), (1, 1))

        thread = self.client.get(f"/threads/{self.client.get('/threads').json()[0]['id']}").json()
        intent = thread["messages"][0]["intent"]
        self.assertEqual((intent["intent"], intent["tier2_status"], intent["needs_review"]),
                         ("payment_reminder", "pending", True))
        self.assertEqual(set(intent["tier1_probabilities"]), set(self.client.get("/profiles").json()[0]["intent_labels"]))

        job = self.wait(self.client.post("/intents/tier2"))
        self.assertEqual((job["kind"], job["result"]["tier2"], job["result"]["remaining"]), ("intents", 1, 0))
        thread = self.client.get(f"/threads/{thread['id']}").json()
        self.assertEqual(thread["messages"][0]["intent"]["decided_by"], "tier2")

        summary = self.client.get("/intents/summary").json()
        self.assertEqual((summary["total"], summary["pending"], summary["by_decided_by"]), (1, 0, {"tier2": 1}))

    def test_profile_describes_the_intent_setup(self) -> None:
        profile = self.client.get("/profiles").json()[0]
        self.assertEqual((profile["tier1_min_confidence"], profile["tier2"]), (0.4, "background"))
        self.assertIn("fake-intent-tier1", profile["intent_model"])

    def test_tier2_job_needs_tier2(self) -> None:
        client = TestClient(self.make_app(None))
        self.assertEqual(client.post("/intents/tier2").status_code, 503)
        self.assertIsNone(client.get("/profiles").json()[0]["intent_model"])
        self.assertEqual(client.get("/intents/summary").json()["total"], 0)
        client.close()

    def test_email_intents_table_is_browsable(self) -> None:
        self.assertIn("email_intents", [t["name"] for t in self.client.get("/tables").json()])
        self.assertEqual(self.client.get("/tables/email_intents/rows").status_code, 200)


class IntentConfigTests(unittest.TestCase):
    REQUIRED = {"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p"}
    KEYS = ("LAYA_DIR", "TIER1_MIN_CONFIDENCE", "TIER2_ENABLED", "TIER2_URL", "TIER2_TIMEOUT_S", "TIER2_TEMPERATURE",
            "TIER2_INLINE", "TIER2_MAX_PER_JOB")

    def load(self, **env) -> Settings:
        saved = {k: os.environ.get(k) for k in (*self.REQUIRED, *self.KEYS)}
        try:
            for k in self.KEYS:
                os.environ.pop(k, None)
            os.environ.update({**self.REQUIRED, **env})
            return load_settings(env_file=os.devnull)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_defaults_follow_the_specification(self) -> None:
        s = self.load()
        self.assertEqual((s.laya_dir, s.laya_checkpoint, s.tier1_min_confidence, s.tier2_enabled, s.tier2_url,
                          s.tier2_timeout_s, s.tier2_max_tokens, s.tier2_temperature, s.email_body_max_chars,
                          s.tier2_inline),
                         (None, "typed-decisions", 0.4, True, "http://127.0.0.1:8080", 900.0, 1536, 0.6, 3000, False))

    def test_tier2_must_stay_on_this_machine(self) -> None:
        self.assertEqual(self.load(TIER2_URL="http://localhost:9000/").tier2_url, "http://localhost:9000")
        with self.assertRaises(ConfigError):
            self.load(TIER2_URL="https://api.example.com")

    def test_invalid_values_are_rejected(self) -> None:
        for key, value in (("TIER1_MIN_CONFIDENCE", "1.5"), ("TIER2_TIMEOUT_S", "0"), ("TIER2_TEMPERATURE", "3"),
                           ("TIER2_ENABLED", "maybe")):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.load(**{key: value})


@unittest.skipUnless(os.getenv("LAYA_DIR"), "set LAYA_DIR to run the real Laya intent model")
class LayaIntentIntegrationTests(unittest.TestCase):
    """Slow: loads Laya typed-decisions from LAYA_DIR."""

    def test_laya_returns_a_full_distribution(self) -> None:
        from mailbox_viewer.document_model import LayaIntentModel

        model = LayaIntentModel(Path(os.environ["LAYA_DIR"]), os.getenv("LAYA_CHECKPOINT", "typed-decisions"))
        result = model.classify({"from": "billing@acme.example", "to": "ap@example.com", "subject": "Credit note CN-12",
                                 "body": "We have issued a credit note for the overcharge on invoice 4471."})
        self.assertEqual(len(result.probabilities), 8)
        self.assertAlmostEqual(sum(result.probabilities.values()), 1.0, places=2)
        self.assertTrue(0.0 <= result.confidence <= 1.0)


@unittest.skipUnless(os.getenv("TIER2_URL_TEST"), "set TIER2_URL_TEST to a running llama-server to run this")
class DeepSeekIntegrationTests(unittest.TestCase):
    """Slow (minutes on CPU): asks the real DeepSeek tier 2."""

    def test_short_payment_is_partial_payment(self) -> None:
        from mailbox_viewer.intent import LlamaServerTier2

        tier2 = LlamaServerTier2(os.environ["TIER2_URL_TEST"], timeout_s=900, max_tokens=1536, temperature=0.6)
        result = tier2.classify({"from": "ar@vendor.example", "to": "ap@example.com", "subject": "Payment received",
                                 "body": "Thanks for the payment of €3,516.74. Please note this was a short payment; "
                                         "€2,344.49 is still due"})
        self.assertEqual((result.status, result.intent), (TIER2_OK, "partial_payment"))


if __name__ == "__main__":
    unittest.main()
