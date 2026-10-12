"""Every page renders against a fake backend (no server, database or models needed).

Run from the frontend/ folder:  python -m unittest -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import ui

NOW = "2026-10-08T10:00:00+05:30"
INHERITED = "part of a thread already classified as payment; body model not run"

COLUMNS = {
    "threads": ["id", "conversation_key", "subject"],
    "emails": ["id", "message_id", "thread_id", "subject"],
    "decision_log": ["message_id", "tier", "decision", "confidence", "reason", "decided_at"],
}
ROWS = {
    "threads": [{"id": 1, "conversation_key": "<a@x>", "subject": "Invoice"}],
    "emails": [
        {"id": 1, "message_id": "<a@x>", "thread_id": 1, "subject": "Invoice"},
        {"id": 2, "message_id": "<b@x>", "thread_id": 1, "subject": "Re: Invoice"},
    ],
    "decision_log": [
        {"message_id": "<b@x>", "tier": 0, "decision": "payment", "confidence": None, "reason": INHERITED, "decided_at": NOW},
        {"message_id": "<c@x>", "tier": 2, "decision": "none", "confidence": 0.92,
         "reason": "body: other (0.92) -> not a payment document", "decided_at": NOW},
        {"message_id": "<a@x>", "tier": 1, "decision": "payment", "confidence": 1.0,
         "reason": '"inv.jpg": invoice (1.00) -> payment', "decided_at": NOW},
    ],
}


class FakeApi:
    """Answers like the backend for every call the pages make."""

    base_url = "http://fake"

    def health(self):
        return {"status": "ok", "database": "ok", "database_error": None, "model": "layoutlmv3+laya"}

    def profiles(self):
        return [{"name": "statement_recon", "active": True, "mailbox": "me@example.com",
                 "folders": ["INBOX", "[Gmail]/Sent Mail"], "keep_policy": "payment_only",
                 "model": "layoutlmv3+laya", "model_min_confidence": 0.5, "body_min_confidence": 0.7,
                 "attachment_decides_confidence": 0.8, "payment_labels": ["invoice", "receipt", "statement"],
                 "attachment_store": "local folder data/attachments"}]

    def stats(self):
        return {"threads": 1, "emails": 2, "attachments": 1, "payment_threads": 1, "needs_review": 0,
                "judged": 3, "dropped": 1, "by_doc_type": {"invoice": 1, "none": 1},
                "by_decision": {"payment": 2, "none": 1},
                "sync_states": [{"folder_name": "INBOX", "uid_validity": 1, "last_seen_uid": 10, "last_sync_at": NOW}]}

    def threads(self, *, payment_only=False, search=None, limit=1000):
        return [{"id": 1, "conversation_key": "<a@x>", "subject": "Invoice", "message_count": 2,
                 "last_message_at": NOW, "has_payment": True, "participants": "Acme <a@x>", "attachment_count": 1}]

    def thread(self, thread_id):
        def message(mid, subject, decision, tier, reason, attachments):
            return {"id": 1, "message_id": mid, "subject": subject, "body_text": "PFA", "received_at": NOW,
                    "in_reply_to": None, "references_header": None, "has_attachments": bool(attachments),
                    "matched_directly": tier != 0, "doc_type": "invoice", "confidence": 1.0 if tier else None,
                    "decision_reason": reason,
                    "participants": [{"role": "from", "name": "Acme", "address": "a@x"},
                                     {"role": "to", "name": "", "address": "me@x"}],
                    "attachments": attachments,
                    "decision": {"message_id": mid, "tier": tier, "decision": decision, "confidence": None,
                                 "reason": reason, "decided_at": NOW}}
        invoice = {"id": 7, "filename": "inv.jpg", "content_type": "image/jpeg", "size_bytes": 2048,
                   "blob_key": "ab" * 32, "doc_type": "invoice", "confidence": 1.0, "download_url": "/attachments/7"}
        return {**self.threads()[0], "messages": [
            message("<b@x>", "Re: Invoice", "payment", 0, INHERITED, []),
            message("<a@x>", "Invoice", "payment", 1, '"inv.jpg": invoice (1.00)', [invoice]),
        ]}

    def attachment_bytes(self, attachment_id):
        return b"jpeg bytes"

    def tables(self):
        return [{"name": n, "rows": len(ROWS[n]), "columns": COLUMNS[n]} for n in COLUMNS]

    def table_rows(self, name, *, limit=100, order_by=None, descending=True):
        return {"name": name, "total": len(ROWS[name]), "columns": COLUMNS[name], "rows": ROWS[name]}

    def schema(self, dialect=None):
        def col(name, refs=()):
            return {"name": name, "type": "INTEGER", "nullable": False, "primary_key": name == "id",
                    "unique": False, "references": list(refs), "indexed": False}
        return [{"name": n, "columns": [col(c, ["threads.id"] if c == "thread_id" else ()) for c in COLUMNS[n]],
                 "constraints": [], "indexes": []} for n in COLUMNS]

    def ddl(self, dialect):
        return "CREATE TABLE threads (id INTEGER);"


class PagesRender(unittest.TestCase):
    def setUp(self) -> None:
        ui.get_api.clear()
        patcher = patch("ui.ApiClient", FakeApi)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ui.get_api.clear)

    def run_page(self, page: str | None = None) -> AppTest:
        at = AppTest.from_file("../app.py", default_timeout=30)
        at.run()
        if page:
            at.switch_page(page)
            at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertFalse(at.error, [e.value for e in at.error])
        return at

    def test_overview(self) -> None:
        at = self.run_page()
        self.assertIn("Statement Recon", at.title[0].value)
        self.assertEqual({m.label: m.value for m in at.metric}["Emails judged"], "3")

    def test_inbox(self) -> None:
        at = self.run_page("views/inbox.py")
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(len(at.main.expander), 0)  # nothing open until a conversation is chosen

    def test_open_from_overview_shows_the_conversation(self) -> None:
        at = self.run_page()
        at.button(key="open-1").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(at.subheader[0].value, "Invoice")
        self.assertEqual(len(at.main.expander), 2)
        decisions = [c.value for c in at.code]
        self.assertTrue(any("Check 1 · thread" in d for d in decisions))
        self.assertTrue(any("Check 2 · attachment" in d for d in decisions))

    def test_decision_log_shows_kept_and_not_kept(self) -> None:
        at = self.run_page("views/decisions.py")
        frame = at.dataframe[0].value
        self.assertEqual(len(frame), 3)
        self.assertIn("— not kept —", list(frame["Subject"]))
        self.assertEqual(set(frame["Decided by"]), {"Check 1 · thread", "Check 2 · attachment", "Check 3 · email text"})

    def test_how_it_decides_uses_live_thresholds(self) -> None:
        at = self.run_page("views/how_it_decides.py")
        values = {m.label: m.value for m in at.metric}
        self.assertEqual(values["Attachment decides alone"], "> 0.80")
        self.assertEqual(values["Review band"], "0.50 – 0.70")

    def test_data_model_and_tables(self) -> None:
        self.run_page("views/data_model.py")
        self.run_page("views/tables.py")


class WhichCheck(unittest.TestCase):
    def test_maps_tier_and_reason_to_check(self) -> None:
        self.assertEqual(ui.which_check(0, INHERITED), 1)
        self.assertEqual(ui.which_check(1, "attachment decided"), 2)
        self.assertEqual(ui.which_check(2, "body: receipt"), 3)
        self.assertEqual(ui.which_check(3, "both"), 3)
        self.assertIsNone(ui.which_check(0, "body: no body model configured"))


if __name__ == "__main__":
    unittest.main()
