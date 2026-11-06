"""Intent classification, unit level: state building, the strict tier-2 parser,
every router branch, the llama.cpp client against a local HTTP server, and the
intent nodes of the LangGraph flow. No model files are needed."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mailbox_viewer.classifier import AttachmentInput, EmailInput
from mailbox_viewer.intent import (
    DECIDED_FALLBACK,
    DECIDED_TIER1,
    DECIDED_TIER2,
    INTENT_KEYS,
    INTENTS,
    TIER2_OK,
    TIER2_PENDING,
    TIER2_TIMEOUT,
    TIER2_UNAVAILABLE,
    TIER2_UNPARSED,
    IntentError,
    LlamaServerTier2,
    Tier2Result,
    build_prompt,
    build_state,
    checked_tier1,
    classify_intent,
    decide_after_tier1,
    decide_after_tier2,
    headers_for,
    html_to_text,
    parse_tier2,
)

from .fakes import FakeBodyModel, FakeIntentTier1, FakeIntentTier2, fake_classifier, fake_intent_stage

PDF = b"%PDF-1.4 pretend"


def t1(intent: str = "new_invoice", confidence: float = 0.9):
    return checked_tier1(intent, {k: (0.65 if k == intent else 0.05) for k in INTENT_KEYS}, confidence)


class LabelTests(unittest.TestCase):
    def test_exactly_the_eight_specified_intents(self) -> None:
        self.assertEqual(INTENT_KEYS, ("new_invoice", "payment_reminder", "payment_confirmation", "credit_note",
                                       "dispute_query", "partial_payment", "cancellation", "other"))
        self.assertEqual(INTENTS["other"], "anything else, for human review")

    def test_tier1_answer_outside_the_labels_is_rejected(self) -> None:
        with self.assertRaises(IntentError):
            checked_tier1("refund", {}, 0.9)

    def test_tier1_probabilities_cover_all_labels_and_confidence_is_clamped(self) -> None:
        result = checked_tier1("credit_note", {"credit_note": 0.71, "junk": 0.2}, 1.7)
        self.assertEqual(set(result.probabilities), set(INTENT_KEYS))
        self.assertNotIn("junk", result.probabilities)
        self.assertEqual(result.confidence, 1.0)


class StateTests(unittest.TestCase):
    def test_header_mapping_and_cleaning(self) -> None:
        state = build_state(headers_for("Acme <billing@acme.example>", "me@example.com", "Invoice 42"),
                            "Please pay.\n\n> quoted reply", clean=lambda b: b.split("\n\n>")[0])
        self.assertEqual(state, {"from": "Acme <billing@acme.example>", "to": "me@example.com",
                                 "subject": "Invoice 42", "body": "Please pay."})

    def test_body_is_cut_to_max_chars_after_cleaning(self) -> None:
        state = build_state({}, "  " + "x" * 50, clean=str.strip, max_chars=10)
        self.assertEqual(state["body"], "x" * 10)
        self.assertEqual(state["from"], "")

    def test_html_only_email_is_converted_to_text(self) -> None:
        html = "<html><head><style>p{}</style></head><body><p>Payment of &euro;10</p><script>x()</script><br>received</body></html>"
        self.assertEqual(html_to_text(html), "Payment of €10\nreceived")
        stage = fake_intent_stage()
        state = stage.state_for("a@x", "b@x", "Receipt", "   ", html)
        self.assertEqual(state["body"], "Payment of €10\nreceived")

    def test_plain_text_wins_over_html(self) -> None:
        self.assertEqual(fake_intent_stage().state_for("a", "b", "s", "plain", "<p>html</p>")["body"], "plain")

    def test_prompt_follows_the_specification(self) -> None:
        prompt = build_prompt({"from": "a@x", "to": "", "subject": "Short payment", "body": "Only half paid"})
        self.assertTrue(prompt.startswith("Classify the intent of this billing email into exactly one label."))
        self.assertIn("- partial_payment: acknowledges or announces that only part of the amount was paid", prompt)
        self.assertIn("This wins over payment_reminder and payment_confirmation.", prompt)
        self.assertIn("From: a@x\nSubject: Short payment\nBody: Only half paid", prompt)
        self.assertNotIn("To:", prompt)  # empty fields are left out
        self.assertTrue(prompt.endswith('exactly like {"intent": "<label>"} using one of the labels above.'))


class ParserTests(unittest.TestCase):
    def test_valid_json_line(self) -> None:
        self.assertEqual(parse_tier2('Short payment.\n{"intent": "partial_payment"}', ""), "partial_payment")

    def test_json_only_in_reasoning(self) -> None:
        self.assertEqual(parse_tier2("", 'thinking... {"intent":"credit_note"}'), "credit_note")
        self.assertEqual(parse_tier2(None, '{ "intent" : "cancellation" }'), "cancellation")

    def test_several_json_lines_last_valid_wins(self) -> None:
        self.assertEqual(parse_tier2('{"intent": "new_invoice"}\n{"intent": "dispute_query"}', ""), "dispute_query")
        self.assertEqual(parse_tier2('{"intent": "credit_note"}\n{"intent": "refund"}', ""), "credit_note")

    def test_invalid_label_is_unparsed(self) -> None:
        self.assertIsNone(parse_tier2('{"intent": "refund"}', ""))

    def test_no_json_is_unparsed_and_free_text_is_never_guessed(self) -> None:
        self.assertIsNone(parse_tier2("The intent is partial_payment.", "partial_payment"))
        self.assertIsNone(parse_tier2("", ""))


class RouterTests(unittest.TestCase):
    def test_confident_tier1_is_final(self) -> None:
        d = decide_after_tier1(t1("credit_note", 0.47), 0.4, tier2_enabled=True)
        self.assertEqual((d.intent, d.decided_by, d.needs_review, d.tier2_status), ("credit_note", DECIDED_TIER1, False, None))

    def test_threshold_is_inclusive(self) -> None:
        self.assertEqual(decide_after_tier1(t1(confidence=0.4), 0.4, True).decided_by, DECIDED_TIER1)
        self.assertTrue(decide_after_tier1(t1(confidence=0.39), 0.4, True).pending)

    def test_confident_other_needs_review(self) -> None:
        d = decide_after_tier1(t1("other", 0.9), 0.4, True)
        self.assertEqual((d.decided_by, d.needs_review), (DECIDED_TIER1, True))

    def test_unsure_tier1_waits_for_tier2(self) -> None:
        d = decide_after_tier1(t1("payment_reminder", 0.19), 0.4, True)
        self.assertEqual((d.intent, d.decided_by, d.needs_review, d.tier2_status),
                         ("payment_reminder", DECIDED_TIER1, True, TIER2_PENDING))

    def test_unsure_tier1_with_tier2_disabled_falls_back(self) -> None:
        d = decide_after_tier1(t1("payment_reminder", 0.19), 0.4, tier2_enabled=False)
        self.assertEqual((d.decided_by, d.needs_review, d.tier2_status), (DECIDED_FALLBACK, True, None))

    def test_valid_tier2_answer_decides(self) -> None:
        pending = decide_after_tier1(t1("payment_reminder", 0.19), 0.4, True)
        d = decide_after_tier2(pending, Tier2Result("partial_payment", TIER2_OK, 104.0))
        self.assertEqual((d.intent, d.decided_by, d.needs_review, d.tier2_intent, d.tier2_status, d.tier2_seconds),
                         ("partial_payment", DECIDED_TIER2, False, "partial_payment", TIER2_OK, 104.0))
        self.assertEqual((d.tier1_intent, d.tier1_confidence), ("payment_reminder", 0.19))

    def test_tier2_other_needs_review(self) -> None:
        pending = decide_after_tier1(t1(confidence=0.1), 0.4, True)
        self.assertTrue(decide_after_tier2(pending, Tier2Result("other", TIER2_OK, 9.0)).needs_review)

    def test_tier2_failures_fall_back_to_tier1_for_review(self) -> None:
        pending = decide_after_tier1(t1("payment_reminder", 0.19), 0.4, True)
        for status in (TIER2_TIMEOUT, TIER2_UNPARSED, TIER2_UNAVAILABLE):
            with self.subTest(status=status):
                d = decide_after_tier2(pending, Tier2Result(None, status, 900.0))
                self.assertEqual((d.intent, d.decided_by, d.needs_review, d.tier2_intent, d.tier2_status),
                                 ("payment_reminder", DECIDED_FALLBACK, True, None, status))

    def test_synchronous_router(self) -> None:
        tier1 = FakeIntentTier1()
        tier1.overrides["unsure"] = ("payment_reminder", 0.2)
        tier2 = FakeIntentTier2()
        self.assertEqual(classify_intent({"subject": "sure"}, tier1, tier2, 0.4, True).decided_by, DECIDED_TIER1)
        self.assertEqual(tier2.states, [])
        d = classify_intent({"subject": "unsure"}, tier1, tier2, 0.4, True)
        self.assertEqual((d.intent, d.decided_by), ("partial_payment", DECIDED_TIER2))
        tier2.result = Tier2Result(None, TIER2_TIMEOUT, 900.0)
        self.assertEqual(classify_intent({"subject": "unsure"}, tier1, tier2, 0.4, True).decided_by, DECIDED_FALLBACK)


# -- the llama.cpp client against a real local HTTP server ------------------------------------------


class _FakeLlamaServer:
    """A minimal llama-server: /health and /v1/chat/completions."""

    def __init__(self) -> None:
        self.health_status = 200
        self.reply: dict | str = {"choices": [{"message": {"content": '{"intent": "partial_payment"}', "reasoning_content": "short payment"}}]}
        self.delay = 0.0
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self.send_response(owner.health_status if self.path == "/health" else 404)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(json.loads(body))
                time.sleep(owner.delay)
                payload = owner.reply if isinstance(owner.reply, str) else json.dumps(owner.reply)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload.encode())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class LlamaClientTests(unittest.TestCase):
    STATE = {"from": "a@x", "to": "b@x", "subject": "Payment", "body": "Thanks for the partial payment."}

    def setUp(self) -> None:
        self.server = _FakeLlamaServer()
        self.client = LlamaServerTier2(self.server.url, timeout_s=5, max_tokens=1536, temperature=0.6)

    def tearDown(self) -> None:
        self.server.close()

    def test_ok_and_request_follows_deepseek_guidance(self) -> None:
        result = self.client.classify(self.STATE)
        self.assertEqual((result.intent, result.status), ("partial_payment", TIER2_OK))
        self.assertIsNotNone(result.seconds)
        sent = self.server.requests[0]
        self.assertEqual([m["role"] for m in sent["messages"]], ["user"])  # no system prompt
        self.assertEqual((sent["temperature"], sent["top_p"], sent["max_tokens"], sent["seed"]), (0.6, 0.95, 1536, 42))
        self.assertEqual(sent["messages"][0]["content"], build_prompt(self.STATE))

    def test_answer_only_in_reasoning(self) -> None:
        self.server.reply = {"choices": [{"message": {"content": "", "reasoning_content": '... {"intent": "credit_note"}'}}]}
        self.assertEqual(self.client.classify(self.STATE).intent, "credit_note")

    def test_unparsed(self) -> None:
        self.server.reply = {"choices": [{"message": {"content": "It is a partial payment."}}]}
        result = self.client.classify(self.STATE)
        self.assertEqual((result.intent, result.status), (None, TIER2_UNPARSED))
        self.server.reply = {"unexpected": True}
        self.assertEqual(self.client.classify(self.STATE).status, TIER2_UNPARSED)

    def test_timeout(self) -> None:
        self.server.delay = 1.5
        client = LlamaServerTier2(self.server.url, timeout_s=0.3, max_tokens=10, temperature=0.6)
        result = client.classify(self.STATE)
        self.assertEqual((result.intent, result.status), (None, TIER2_TIMEOUT))

    def test_unhealthy_server_is_not_called(self) -> None:
        self.server.health_status = 503
        self.assertFalse(self.client.healthy())
        self.assertEqual(self.client.classify(self.STATE).status, TIER2_UNAVAILABLE)
        self.assertEqual(self.server.requests, [])

    def test_no_server_is_unavailable(self) -> None:
        url = self.server.url
        self.server.close()
        result = LlamaServerTier2(url, timeout_s=1, max_tokens=10, temperature=0.6).classify(self.STATE)
        self.assertEqual(result.status, TIER2_UNAVAILABLE)
        self.server = _FakeLlamaServer()  # for tearDown

    def test_bad_json_is_unavailable(self) -> None:
        self.server.reply = "not json"
        self.assertEqual(self.client.classify(self.STATE).status, TIER2_UNAVAILABLE)


# -- the intent nodes of the LangGraph flow ------------------------------------------------------------


def invoice_email(subject: str = "Invoice 42", body: str = "Please pay.") -> EmailInput:
    return EmailInput(subject, body, (AttachmentInput("invoice_42.pdf", "application/pdf", PDF),),
                      sender="Acme <billing@acme.example>", recipients="me@example.com")


class GraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tier1 = FakeIntentTier1()
        self.tier2 = FakeIntentTier2()
        self.body = FakeBodyModel()
        self.classifier = fake_classifier(body=self.body, intent=fake_intent_stage(self.tier1, self.tier2))

    def test_payment_email_gets_a_tier1_intent(self) -> None:
        out = self.classifier.run(invoice_email())
        self.assertTrue(out.decision.is_payment)
        self.assertEqual((out.intent.intent, out.intent.decided_by), ("new_invoice", DECIDED_TIER1))
        self.assertIsNone(out.intent_input)  # nothing kept when tier 2 is not needed
        self.assertEqual(self.tier1.states, [{"from": "Acme <billing@acme.example>", "to": "me@example.com",
                                              "subject": "Invoice 42", "body": "Please pay."}])

    def test_attachment_names_never_reach_the_intent_model(self) -> None:
        self.classifier.run(invoice_email())
        self.assertNotIn("invoice_42.pdf", json.dumps(self.tier1.states))

    def test_non_payment_email_gets_no_intent(self) -> None:
        out = self.classifier.run(EmailInput("Lunch?", "See you at noon."))
        self.assertFalse(out.decision.is_payment)
        self.assertIsNone(out.intent)
        self.assertEqual(self.tier1.states, [])

    def test_inherited_payment_gets_an_intent_without_the_body_model(self) -> None:
        out = self.classifier.run(EmailInput("Re: Invoice 42", "Paid half today.", sender="Ravi <r@x>"), inherited=True)
        self.assertTrue(out.decision.inherited)
        self.assertEqual(out.intent.intent, "new_invoice")
        self.assertEqual(self.body.calls, [])  # Laya's document question is not asked
        self.assertEqual(self.tier1.states[0]["subject"], "Re: Invoice 42")

    def test_unsure_email_waits_for_the_background_job(self) -> None:
        self.tier1.overrides["Invoice 42"] = ("payment_reminder", 0.19)
        out = self.classifier.run(invoice_email())
        self.assertTrue(out.intent.pending)
        self.assertEqual(out.intent_input["subject"], "Invoice 42")
        self.assertEqual(self.tier2.states, [])  # not run inline on CPU

    def test_inline_tier2_decides_in_the_flow(self) -> None:
        classifier = fake_classifier(intent=fake_intent_stage(self.tier1, self.tier2, inline=True))
        self.tier1.overrides["Invoice 42"] = ("payment_reminder", 0.19)
        out = classifier.run(invoice_email())
        self.assertEqual((out.intent.intent, out.intent.decided_by), ("partial_payment", DECIDED_TIER2))
        self.assertIsNone(out.intent_input)

    def test_inline_tier2_failure_keeps_the_input_for_a_retry(self) -> None:
        self.tier2.result = Tier2Result(None, TIER2_UNAVAILABLE, None)
        classifier = fake_classifier(intent=fake_intent_stage(self.tier1, self.tier2, inline=True))
        self.tier1.overrides["Invoice 42"] = ("payment_reminder", 0.19)
        out = classifier.run(invoice_email())
        self.assertEqual((out.intent.decided_by, out.intent.tier2_status), (DECIDED_FALLBACK, TIER2_UNAVAILABLE))
        self.assertIsNotNone(out.intent_input)

    def test_failing_tier1_never_stops_the_payment_decision(self) -> None:
        self.tier1.fail_on.add("Invoice 42")
        out = self.classifier.run(invoice_email())
        self.assertTrue(out.decision.is_payment)
        self.assertIsNone(out.intent)

    def test_classify_and_inherit_skip_the_intent_step(self) -> None:
        self.assertTrue(self.classifier.classify(invoice_email()).is_payment)
        self.assertTrue(self.classifier.inherit(invoice_email()).inherited)
        self.assertEqual(self.tier1.states, [])

    def test_without_an_intent_stage(self) -> None:
        out = fake_classifier().run(invoice_email())
        self.assertTrue(out.decision.is_payment)
        self.assertIsNone(out.intent)

    def test_graph_diagram_shows_the_intent_nodes(self) -> None:
        diagram = self.classifier.mermaid()
        for node in ("inherit", "intent_tier1", "intent_tier2"):
            self.assertIn(node, diagram)


if __name__ == "__main__":
    unittest.main()
