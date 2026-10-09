"""Two-model classification: each step, the combine rule, and the LangGraph flow."""

from __future__ import annotations

import unittest

from mailbox_viewer.classification_graph import EmailClassifier
from mailbox_viewer.classifier import (
    DECISION_NONE,
    DECISION_PAYMENT,
    DECISION_REVIEW,
    TIER_ATTACHMENT,
    TIER_BODY,
    TIER_BOTH,
    TIER_NONE,
    AttachmentDecision,
    AttachmentInput,
    BodyDecision,
    EmailInput,
    NoBodyModel,
    NoModel,
    Prediction,
    classify_attachments,
    classify_body,
    attachment_decides,
    body_decision_from_reason,
    combine,
    inherited_decision,
    is_model_input,
    model_content_type,
)

from .fakes import FakeBodyModel, FakeDocumentModel, fake_classifier


def pdf(name: str, data: bytes = b"%PDF-1.4") -> AttachmentInput:
    return AttachmentInput(name, "application/pdf", data)


def body(label: str | None, conf: float | None = None, note: str | None = None) -> BodyDecision:
    return BodyDecision(label, conf, note)


def att(label: str | None, conf: float | None = None, name: str = "a.pdf", note: str | None = None) -> AttachmentDecision:
    return AttachmentDecision(label, conf, name, note)


class AttachmentStepTests(unittest.TestCase):
    def test_labels_each_document_and_explains_skipped_files(self) -> None:
        model = FakeDocumentModel()
        model.fail_on.add("broken.pdf")
        out = classify_attachments(
            [pdf("invoice_9.pdf"), pdf("plan.pdf"), AttachmentInput("rows.csv", "text/csv", b"a,b"),
             AttachmentInput("x.pdf", "application/pdf", None), pdf("broken.pdf")],
            model,
        )
        self.assertEqual([(a.doc_type, a.confidence) for a in out],
                         [("invoice", 0.95), ("not_invoice", 0.70), (None, None), (None, None), (None, None)])
        self.assertEqual([a.note for a in out[2:]], ["not a PDF, image or DOCX", "file bytes unavailable",
                                                       "RuntimeError: model could not read the file"])

    def test_no_model(self) -> None:
        [only] = classify_attachments([pdf("invoice.pdf")], NoModel())
        self.assertEqual((only.doc_type, only.note), (None, "no attachment model configured"))

    def test_missing_tesseract_gets_a_clear_note(self) -> None:
        class NoOcr:
            name = "ocr"

            def predict(self, attachment):
                raise RuntimeError("tesseract is not installed or it's not in your PATH")

        [only] = classify_attachments([AttachmentInput("scan.jpg", "image/jpeg", b"jpg")], NoOcr())
        self.assertEqual(only.note, "OCR program Tesseract is not installed")

    def test_model_input_types(self) -> None:
        self.assertTrue(is_model_input(AttachmentInput("scan.jpg", "image/jpeg")))
        self.assertTrue(is_model_input(AttachmentInput("letter.docx", "application/octet-stream")))
        self.assertFalse(is_model_input(AttachmentInput("rows.csv", "text/csv")))
        self.assertEqual(model_content_type(AttachmentInput("Scan.JPG", "application/octet-stream")), "image/jpeg")
        self.assertEqual(model_content_type(AttachmentInput("x", "image/jpg")), "image/jpeg")


class BodyStepTests(unittest.TestCase):
    def test_body_label(self) -> None:
        self.assertEqual(classify_body("Your account statement", "attached", FakeBodyModel()), body("statement", 0.93))

    def test_empty_body_and_no_model(self) -> None:
        self.assertEqual(classify_body("", "  ", FakeBodyModel()).note, "empty email body")
        self.assertEqual(classify_body("Hi", "text", NoBodyModel()).note, "no body model configured")

    def test_model_error_is_contained(self) -> None:
        class Broken:
            name = "broken"

            def predict(self, subject, body):
                raise ValueError("tokenizer exploded")

        self.assertEqual(classify_body("Hi", "text", Broken()).note, "ValueError: tokenizer exploded")


class CombineRuleTests(unittest.TestCase):
    # Rule 1a: attachment above 0.8 decides alone.
    def test_confident_invoice_attachment_decides_alone(self) -> None:
        d = combine(body("other", 0.95), [att("invoice", 0.95, "inv.jpg")])
        self.assertEqual((d.decision, d.doc_type, d.tier, d.confidence), (DECISION_PAYMENT, "invoice", TIER_ATTACHMENT, 0.95))
        self.assertIn("payment: attachment model above 0.80", d.reason)

    def test_confident_non_invoice_attachment_decides_alone_even_if_body_says_statement(self) -> None:
        d = combine(body("statement", 0.97), [att("not_invoice", 1.0, "statement.pdf")])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_NONE, "other", TIER_ATTACHMENT))
        self.assertIn("attachment model above 0.80 says not an invoice", d.reason)

    def test_any_confident_invoice_among_several_attachments_decides(self) -> None:
        d = combine(body(None), [att("not_invoice", 0.6, "a.pdf"), att("invoice", 0.9, "b.pdf")])
        self.assertEqual(d.decision, DECISION_PAYMENT)

    def test_exactly_at_the_bar_is_not_above_it(self) -> None:
        self.assertFalse(attachment_decides([att("invoice", 0.8)]))
        self.assertTrue(attachment_decides([att("invoice", 0.81)]))

    # Rule 1b: attachment at or below 0.8, Laya consulted.
    def test_weak_attachment_and_confident_body_payment_is_payment(self) -> None:
        d = combine(body("statement", 0.93), [att("not_invoice", 0.7)])
        self.assertEqual((d.decision, d.doc_type, d.tier, d.confidence), (DECISION_PAYMENT, "statement", TIER_BOTH, 0.93))

    def test_weak_invoice_attachment_and_body_invoice_is_payment(self) -> None:
        d = combine(body("invoice", 0.9), [att("invoice", 0.7)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_PAYMENT, "invoice"))

    def test_weak_invoice_attachment_without_body_support_is_review(self) -> None:
        d = combine(body("other", 0.9), [att("invoice", 0.7)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_REVIEW, "invoice"))
        self.assertIn("attachment model unsure about invoice", d.reason)

    def test_weak_non_invoice_attachment_and_body_other_is_none(self) -> None:
        self.assertEqual(combine(body("other", 0.9), [att("not_invoice", 0.7)]).decision, DECISION_NONE)

    # Rule 2: no readable attachment, Laya alone.
    def test_body_payment_without_attachments(self) -> None:
        d = combine(body("receipt", 0.91), [])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "receipt", TIER_BODY))

    def test_unreadable_attachment_counts_as_no_attachment(self) -> None:
        d = combine(body("invoice", 0.92), [att(None, None, "rows.csv", "not a PDF, image or DOCX")])
        self.assertEqual((d.decision, d.tier), (DECISION_PAYMENT, TIER_BODY))

    def test_payment_label_below_body_bar_is_review_not_payment(self) -> None:
        d = combine(body("receipt", 0.65), [])
        self.assertEqual((d.decision, d.doc_type, d.confidence), (DECISION_REVIEW, "receipt", 0.65))
        self.assertIn("receipt below the 0.70 payment bar", d.reason)
        self.assertFalse(d.is_payment)

    def test_payment_label_below_review_floor_is_none(self) -> None:
        self.assertEqual(combine(body("statement", 0.45), []).decision, DECISION_NONE)

    def test_low_score_on_non_payment_label_is_never_review(self) -> None:
        for label in ("other", "purchase_order", "quotation"):
            with self.subTest(label=label):
                self.assertEqual(combine(body(label, 0.45), []).decision, DECISION_NONE)

    def test_plain_mail(self) -> None:
        d = combine(body("other", 0.80), [])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_NONE, "other", TIER_BODY))

    def test_nothing_classified(self) -> None:
        d = combine(body(None, None, "no body model configured"), [att(None, None, "scan.jpg", "OCR program Tesseract is not installed")])
        self.assertEqual((d.decision, d.tier, d.doc_type, d.confidence), (DECISION_NONE, TIER_NONE, None, None))
        self.assertEqual(d.reason, 'body: no body model configured; "scan.jpg": OCR program Tesseract is not installed')

    # Settings.
    def test_bars_are_configurable(self) -> None:
        self.assertEqual(combine(body("invoice", 0.92), [], body_min_confidence=0.95).decision, DECISION_REVIEW)
        self.assertEqual(combine(body("invoice", 0.96), [], body_min_confidence=0.95).decision, DECISION_PAYMENT)
        self.assertEqual(combine(body(None), [att("invoice", 0.85)], decides_above=0.9).decision, DECISION_REVIEW)
        self.assertEqual(combine(body(None), [att("invoice", 0.85)]).decision, DECISION_PAYMENT)  # default bar 0.80

    # Rule 3 output.
    def test_inherited_decision(self) -> None:
        d = inherited_decision(2)
        self.assertTrue(d.is_payment and d.inherited)
        self.assertFalse(d.matched_directly)
        self.assertEqual((len(d.attachments), d.doc_type, d.tier), (2, None, TIER_NONE))
        self.assertIn("thread already classified as payment", d.reason)

    def test_inherited_decision_carries_attachment_labels(self) -> None:
        d = inherited_decision([att("invoice", 0.97, "inv.pdf"), att("not_invoice", 0.9, "terms.pdf")])
        self.assertEqual((d.decision, d.doc_type, d.confidence, d.tier), (DECISION_PAYMENT, "invoice", 0.97, TIER_ATTACHMENT))
        self.assertIn('attachments labelled: "inv.pdf": invoice (0.97), "terms.pdf": not_invoice (0.90)', d.reason)
        self.assertFalse(d.matched_directly)

    def test_classifier_inherit_runs_only_the_attachment_model(self) -> None:
        body_model, doc_model = FakeBodyModel(), FakeDocumentModel()
        d = fake_classifier(body_model, doc_model).inherit(EmailInput("Re: x", "thanks", (pdf("invoice_7.pdf"),)))
        self.assertEqual((d.decision, d.doc_type, d.inherited), (DECISION_PAYMENT, "invoice", True))
        self.assertEqual((body_model.calls, doc_model.calls), ([], ["invoice_7.pdf"]))

    def test_with_note(self) -> None:
        d = combine(body("other", 0.8), []).with_note("backfilled into a payment thread")
        self.assertTrue(d.reason.endswith("; backfilled into a payment thread"))


class StoredPredictionTests(unittest.TestCase):
    def test_body_prediction_is_read_back_from_the_reason(self) -> None:
        reason = combine(body("statement", 0.93), [att("not_invoice", 0.9, "s.pdf")]).reason
        self.assertEqual(body_decision_from_reason(reason), body("statement", 0.93))

    def test_body_note_is_read_back(self) -> None:
        reason = combine(body(None, None, "empty email body"), [att("invoice", 0.9)]).reason
        self.assertEqual(body_decision_from_reason(reason), body(None, None, "empty email body"))
        self.assertEqual(body_decision_from_reason(None).note, "no stored body prediction")

    def test_redecide_gives_the_same_answer_as_a_fresh_run(self) -> None:
        cases = [(body("receipt", 0.65), []), (body("invoice", 0.92), [att("not_invoice", 0.7, "x.pdf")]),
                 (body("other", 0.45), [att("invoice", 0.7, "inv.pdf")])]
        for b, atts in cases:
            with self.subTest(body=b):
                first = combine(b, atts)
                again = combine(body_decision_from_reason(first.reason), atts)
                self.assertEqual((again.decision, again.doc_type, again.reason), (first.decision, first.doc_type, first.reason))


class GraphTests(unittest.TestCase):
    def test_confident_attachment_skips_the_body_model(self) -> None:
        body_model, doc_model = FakeBodyModel(), FakeDocumentModel()
        clf = fake_classifier(body_model, doc_model)

        d = clf.classify(EmailInput("Invoice #42", "Please pay.", (pdf("invoice_42.pdf"), pdf("terms.pdf"))))

        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "invoice", TIER_ATTACHMENT))
        self.assertEqual([a.doc_type for a in d.attachments], ["invoice", "not_invoice"])
        self.assertEqual(doc_model.calls, ["invoice_42.pdf", "terms.pdf"])
        self.assertEqual(body_model.calls, [])  # Laya not run
        self.assertIn("body: not run: attachment model decided", d.reason)

    def test_unsure_attachment_consults_the_body_model(self) -> None:
        body_model, doc_model = FakeBodyModel(), FakeDocumentModel()
        d = fake_classifier(body_model, doc_model).classify(EmailInput("Your account statement", "attached", (pdf("sep.pdf"),)))
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "statement", TIER_BOTH))
        self.assertEqual(body_model.calls, ["Your account statement"])

    def test_no_attachment_uses_the_body_model_only(self) -> None:
        body_model, doc_model = FakeBodyModel(), FakeDocumentModel()
        d = fake_classifier(body_model, doc_model).classify(EmailInput("Lunch?", "1pm?", ()))
        self.assertEqual((d.decision, d.tier), (DECISION_NONE, TIER_BODY))
        self.assertEqual(doc_model.calls, [])

    def test_graph_with_no_models(self) -> None:
        clf = EmailClassifier(NoBodyModel(), NoModel())
        d = clf.classify(EmailInput("Hi", "text", (pdf("a.pdf"),)))
        self.assertEqual((d.decision, d.tier), (DECISION_NONE, TIER_NONE))
        self.assertFalse(clf.has_any_model)
        self.assertEqual(clf.name, "body: none, attachments: none")

    def test_graph_shape(self) -> None:
        diagram = fake_classifier().mermaid()
        for text in ("classify_body", "classify_attachments", "combine", "attachment decided", "consult body"):
            self.assertIn(text, diagram)

    def test_override_prediction(self) -> None:
        body_model = FakeBodyModel()
        body_model.overrides["Quote"] = Prediction("quotation", 0.9)
        d = fake_classifier(body_model).classify(EmailInput("Quote", "see attached", ()))
        self.assertEqual((d.decision, d.doc_type), (DECISION_NONE, "quotation"))


if __name__ == "__main__":
    unittest.main()
