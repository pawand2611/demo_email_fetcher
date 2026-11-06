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


def _ticket(tid, status, closed_at=None, closed_by=None, reason=None, confidence=None, age=9.5, bucket="8+ days"):
    return {"id": tid, "thread_id": 1, "subject": "Invoice" if tid == 1 else "Statement Q3", "message_count": 2,
            "last_message_at": NOW, "status": status, "opened_at": NOW, "in_progress_at": None,
            "closed_at": closed_at, "closed_by": closed_by, "close_reason": reason, "close_confidence": confidence,
            "age_days": age, "bucket": bucket}


TICKETS = {
    1: _ticket(1, "open"),
    2: _ticket(2, "closed", closed_at=NOW, closed_by="auto", reason="payment confirmed by email (0.95)",
               confidence=0.95, age=3.0, bucket="3–7 days"),
}
ACTIONS: list[tuple] = []  # manual ticket actions the pages sent


INTENT_PROBS = {"new_invoice": 0.1, "payment_reminder": 0.3, "payment_confirmation": 0.1, "credit_note": 0.1,
                "dispute_query": 0.1, "partial_payment": 0.2, "cancellation": 0.05, "other": 0.05}


class FakeApi:
    """Answers like the backend for every call the pages make."""

    base_url = "http://fake"

    def health(self):
        return {"status": "ok", "database": "ok", "database_error": None, "model": "layoutlmv3+laya"}

    def profiles(self):
        return [{"name": "statement_recon", "active": True, "mailbox": "me@example.com",
                 "folders": ["INBOX", "[Gmail]/Sent Mail"], "keep_policy": "payment_only",
                 "model": "layoutlmv3+laya", "model_min_confidence": 0.5, "body_min_confidence": 0.7,
                 "attachment_decides_confidence": 0.8, "ticket_close_confidence": 0.8,
                 "payment_labels": ["invoice", "receipt", "statement"],
                 "attachment_store": "local folder data/attachments",
                 "intent_model": "laya-intent -> deepseek-r1-distill (llama.cpp) (background job)",
                 "intent_labels": list(INTENT_PROBS), "tier1_min_confidence": 0.4, "tier2": "background"}]

    def intent_summary(self):
        return {"total": 2, "needs_review": 1, "pending": 1,
                "by_intent": {"new_invoice": 1, "payment_reminder": 1},
                "by_decided_by": {"tier1": 2}, "by_tier2_status": {"pending": 1}, "tier2_failure_rate": None,
                "tier2_seconds_p50": None, "tier2_seconds_p90": None, "tier2_seconds_max": None}

    def resolve_intents(self, *, retry_failed=False):
        ACTIONS.append(("resolve_intents", retry_failed))
        return {"id": "j1", "kind": "intents", "state": "succeeded", "started_at": NOW, "finished_at": NOW,
                "result": {"processed": 1, "tier2": 1, "fallback": 0, "skipped": 0, "remaining": 0}, "error": None}

    def stats(self):
        return {"threads": 1, "emails": 2, "attachments": 1, "payment_threads": 1, "needs_review": 0,
                "judged": 3, "dropped": 1, "by_doc_type": {"invoice": 1, "none": 1},
                "by_decision": {"payment": 2, "none": 1},
                "sync_states": [{"folder_name": "INBOX", "uid_validity": 1, "last_seen_uid": 10, "last_sync_at": NOW}],
                "tickets": {"open": 1, "in_progress": 0, "closed": 1, "oldest_active_days": 9.5,
                            "active_by_bucket": {"0–2 days": 0, "3–7 days": 0, "8+ days": 1}}}

    def tickets(self, status=None):
        rows = [TICKETS[1], TICKETS[2]]
        return [t for t in rows if status is None or t["status"] == status]

    def ticket(self, ticket_id):
        t = TICKETS[ticket_id]
        events = [{"event": "opened", "actor": "auto", "at": NOW, "message_id": "<a@x>",
                   "reason": "first payment email in the thread", "confidence": None}]
        if t["status"] == "closed":
            events.append({"event": "closed", "actor": "auto", "at": NOW, "message_id": "<z@x>",
                           "reason": "payment confirmed by email (0.95)", "confidence": 0.95})
        return {**t, "events": events}

    def close_ticket(self, ticket_id, reason=None):
        ACTIONS.append(("close", ticket_id, reason))
        return self.ticket(ticket_id)

    def reopen_ticket(self, ticket_id, reason=None):
        ACTIONS.append(("reopen", ticket_id, reason))
        return self.ticket(ticket_id)

    def threads(self, *, payment_only=False, search=None, limit=1000):
        return [{"id": 1, "conversation_key": "<a@x>", "subject": "Invoice", "message_count": 2,
                 "last_message_at": NOW, "has_payment": True, "participants": "Acme <a@x>", "attachment_count": 1}]

    def thread(self, thread_id):
        def message(mid, subject, decision, tier, reason, attachments, intent=None):
            return {"id": 1, "message_id": mid, "subject": subject, "body_text": "PFA", "received_at": NOW,
                    "intent": intent,
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
        pending = {"intent": "payment_reminder", "decided_by": "tier1", "needs_review": True,
                   "tier1_intent": "payment_reminder", "tier1_confidence": 0.19, "tier1_probabilities": INTENT_PROBS,
                   "tier2_intent": None, "tier2_status": "pending", "tier2_seconds": None, "decided_at": NOW}
        return {**self.threads()[0], "messages": [
            message("<b@x>", "Re: Invoice", "payment", 0, INHERITED, [], intent=pending),
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
        ui._client.clear()
        ui.clear_cache()
        patcher = patch("ui.ApiClient", FakeApi)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ui._client.clear)
        self.addCleanup(ui.clear_cache)

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

    def test_overview_shows_what_payment_emails_are_for(self) -> None:
        at = self.run_page()
        self.assertIn("What payment emails are for", [s.value for s in at.subheader])
        metrics = {m.label: m.value for m in at.metric}
        self.assertEqual((metrics["Waiting for DeepSeek"], metrics["Needs review"]), ("1", "1"))

    def test_message_shows_its_intent(self) -> None:
        at = self.run_page()
        at.button(key="open-1").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any("waiting for DeepSeek" in c.value and "needs review" in c.value for c in at.caption))
        intent = next(c.value for c in at.code if c.value.startswith("intent"))
        self.assertIn("tier 1 (Laya): payment_reminder · confidence 0.19", intent)
        self.assertIn("status pending", intent)

    def test_resolving_unsure_intents_from_the_sidebar(self) -> None:
        ACTIONS.clear()
        at = self.run_page()
        next(b for b in at.button if b.label == "🤖 Resolve unsure intents").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(ACTIONS, [("resolve_intents", False)])
        self.assertTrue(any("1 decided by DeepSeek" in s.value for s in at.success))

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

    def test_tickets_page_lists_and_counts(self) -> None:
        at = self.run_page("views/tickets.py")
        values = {m.label: m.value for m in at.metric}
        self.assertEqual((values["🔴 Open"], values["🟢 Closed"], values["Oldest active"]), ("1", "1", "10 days"))
        frame = at.dataframe[0].value
        self.assertEqual(list(frame["Status"]), ["🔴 Open"])  # default view: active tickets only

    def test_closing_a_ticket_by_hand(self) -> None:
        ACTIONS.clear()
        at = AppTest.from_file("../app.py", default_timeout=30)
        at.session_state["open_ticket"] = 1
        at.run()
        at.switch_page("views/tickets.py").run()
        at.text_input(key="close-reason-1").input("paid by bank transfer").run()
        at.button(key="close-1").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(ACTIONS, [("close", 1, "paid by bank transfer")])

    def test_closed_ticket_offers_reopen(self) -> None:
        ACTIONS.clear()
        at = AppTest.from_file("../app.py", default_timeout=30)
        at.session_state["open_ticket"] = 2
        at.run()
        at.switch_page("views/tickets.py").run()
        at.radio[0].set_value("Closed").run()
        self.assertEqual(at.main.subheader[0].value, "Statement Q3")
        at.button(key="reopen-2").click().run()
        self.assertEqual(ACTIONS, [("reopen", 2, "")])

    def test_inbox_shows_the_ticket_badge(self) -> None:
        at = self.run_page()
        at.button(key="open-1").click().run()
        self.assertIn("ticket age 9.5 days (8+ days)", " ".join(c.value for c in at.caption))

    def test_data_model_and_tables(self) -> None:
        self.run_page("views/data_model.py")
        self.run_page("views/tables.py")


class CacheTests(unittest.TestCase):
    """Reads are served from the cache; anything that changes data clears it."""

    def setUp(self) -> None:
        self.calls: list[str] = []
        outer = self

        class CountingApi(FakeApi):
            def stats(self):
                outer.calls.append("stats")
                return super().stats()

        ui._client.clear()
        ui.clear_cache()
        patcher = patch("ui.ApiClient", CountingApi)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ui._client.clear)
        self.addCleanup(ui.clear_cache)

    def test_repeat_reads_hit_the_cache_and_writes_clear_it(self) -> None:
        api = ui.get_api()
        api.stats()
        api.stats()
        self.assertEqual(self.calls, ["stats"])  # second read came from the cache
        api.close_ticket(1, "paid")
        api.stats()
        self.assertEqual(self.calls, ["stats", "stats"])  # the close cleared it


class WhichCheck(unittest.TestCase):
    def test_maps_tier_and_reason_to_check(self) -> None:
        self.assertEqual(ui.which_check(0, INHERITED), 1)
        self.assertEqual(ui.which_check(1, "attachment decided"), 2)
        self.assertEqual(ui.which_check(2, "body: receipt"), 3)
        self.assertEqual(ui.which_check(3, "both"), 3)
        self.assertIsNone(ui.which_check(0, "body: no body model configured"))


if __name__ == "__main__":
    unittest.main()
