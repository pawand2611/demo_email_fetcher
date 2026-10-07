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
    body_decision_from_reason,
    combine,
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
                         [("invoice", 0.95), ("not_invoice", 0.90), (None, None), (None, None), (None, None)])
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
    def test_both_agree_on_invoice(self) -> None:
        d = combine(body("invoice", 0.92), [att("invoice", 0.95, "inv.pdf")])
        self.assertEqual((d.decision, d.doc_type, d.tier, d.confidence), (DECISION_PAYMENT, "invoice", TIER_BOTH, 0.95))
        self.assertIn('body: invoice (0.92); "inv.pdf": invoice (0.95) -> payment', d.reason)

    def test_body_statement_with_non_invoice_attachment_is_payment_not_conflict(self) -> None:
        d = combine(body("statement", 0.93), [att("not_invoice", 0.90)])
        self.assertEqual((d.decision, d.doc_type, d.confidence), (DECISION_PAYMENT, "statement", 0.93))

    def test_attachment_invoice_alone_is_payment(self) -> None:
        d = combine(body(None, None, "no body model configured"), [att("invoice", 0.88)])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "invoice", TIER_ATTACHMENT))

    def test_body_payment_without_attachments(self) -> None:
        d = combine(body("receipt", 0.91), [])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "receipt", TIER_BODY))

    def test_body_says_invoice_attachment_says_not_is_review(self) -> None:
        d = combine(body("invoice", 0.92), [att("not_invoice", 0.90)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_REVIEW, "invoice"))
        self.assertIn("models disagree on invoice", d.reason)
        self.assertTrue(d.needs_review)
        self.assertFalse(d.is_payment)

    def test_attachment_invoice_but_body_confidently_says_other_is_review(self) -> None:
        d = combine(body("purchase_order", 0.95), [att("invoice", 0.95)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_REVIEW, "invoice"))
        self.assertIn("models disagree on invoice", d.reason)

    def test_attachment_invoice_with_unsure_body_other_is_payment(self) -> None:
        d = combine(body("other", 0.55), [att("invoice", 0.95)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_PAYMENT, "invoice"))

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

    def test_borderline_invoice_attachment_is_review(self) -> None:
        d = combine(body(None, None, "empty email body"), [att("invoice", 0.55)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_REVIEW, "invoice"))
        self.assertIn("attachment model is unsure about invoice", d.reason)

    def test_unsure_invoice_body_with_decisive_non_invoice_attachment_is_review(self) -> None:
        d = combine(body("invoice", 0.55), [att("not_invoice", 0.90)])
        self.assertEqual(d.decision, DECISION_REVIEW)
        self.assertIn("invoice below the 0.70 payment bar", d.reason)

    def test_plain_mail(self) -> None:
        d = combine(body("other", 0.80), [])
        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_NONE, "other", TIER_BODY))

    def test_attachment_only_not_invoice_maps_to_other(self) -> None:
        d = combine(body(None, None, "empty email body"), [att("not_invoice", 0.9)])
        self.assertEqual((d.decision, d.doc_type), (DECISION_NONE, "other"))

    def test_nothing_classified(self) -> None:
        d = combine(body(None, None, "no body model configured"), [att(None, None, "scan.jpg", "OCR program Tesseract is not installed")])
        self.assertEqual((d.decision, d.tier, d.doc_type, d.confidence), (DECISION_NONE, TIER_NONE, None, None))
        self.assertEqual(d.reason, 'body: no body model configured; "scan.jpg": OCR program Tesseract is not installed')

    def test_body_bar_is_configurable(self) -> None:
        self.assertEqual(combine(body("invoice", 0.92), [], body_min_confidence=0.95).decision, DECISION_REVIEW)
        self.assertEqual(combine(body("invoice", 0.96), [], body_min_confidence=0.95).decision, DECISION_PAYMENT)
        self.assertEqual(combine(body("invoice", 0.92), []).decision, DECISION_PAYMENT)  # default bar 0.70

    def test_attachment_bar_is_configurable(self) -> None:
        self.assertEqual(combine(body(None, None), [att("invoice", 0.75)], min_confidence=0.8).decision, DECISION_NONE)

    def test_with_note(self) -> None:
        d = combine(body("other", 0.8), []).with_note("kept as part of a payment thread")
        self.assertTrue(d.reason.endswith("; kept as part of a payment thread"))


class StoredPredictionTests(unittest.TestCase):
    def test_body_prediction_is_read_back_from_the_reason(self) -> None:
        reason = combine(body("statement", 0.93), [att("not_invoice", 0.9, "s.pdf")]).reason
        self.assertEqual(body_decision_from_reason(reason), body("statement", 0.93))

    def test_body_note_is_read_back(self) -> None:
        reason = combine(body(None, None, "empty email body"), [att("invoice", 0.9)]).reason
        self.assertEqual(body_decision_from_reason(reason), body(None, None, "empty email body"))
        self.assertEqual(body_decision_from_reason(None).note, "no stored body prediction")

    def test_redecide_gives_the_same_answer_as_a_fresh_run(self) -> None:
        cases = [(body("receipt", 0.75), []), (body("invoice", 0.92), [att("not_invoice", 0.9, "x.pdf")]),
                 (body("other", 0.45), [att("invoice", 0.95, "inv.pdf")])]
        for b, atts in cases:
            with self.subTest(body=b):
                first = combine(b, atts)
                again = combine(body_decision_from_reason(first.reason), atts)
                self.assertEqual((again.decision, again.doc_type, again.reason), (first.decision, first.doc_type, first.reason))


class GraphTests(unittest.TestCase):
    def test_graph_runs_both_models_and_combines(self) -> None:
        body_model, doc_model = FakeBodyModel(), FakeDocumentModel()
        clf = fake_classifier(body_model, doc_model)

        d = clf.classify(EmailInput("Invoice #42", "Please pay.", (pdf("invoice_42.pdf"), pdf("terms.pdf"))))

        self.assertEqual((d.decision, d.doc_type, d.tier), (DECISION_PAYMENT, "invoice", TIER_BOTH))
        self.assertEqual([a.doc_type for a in d.attachments], ["invoice", "not_invoice"])
        self.assertEqual(body_model.calls, ["Invoice #42"])
        self.assertEqual(doc_model.calls, ["invoice_42.pdf", "terms.pdf"])

    def test_graph_with_no_models(self) -> None:
        clf = EmailClassifier(NoBodyModel(), NoModel())
        d = clf.classify(EmailInput("Hi", "text", (pdf("a.pdf"),)))
        self.assertEqual((d.decision, d.tier), (DECISION_NONE, TIER_NONE))
        self.assertFalse(clf.has_any_model)
        self.assertEqual(clf.name, "body: none, attachments: none")

    def test_graph_shape(self) -> None:
        diagram = fake_classifier().mermaid()
        for node in ("classify_body", "classify_attachments", "combine"):
            self.assertIn(node, diagram)

    def test_override_prediction(self) -> None:
        body_model = FakeBodyModel()
        body_model.overrides["Quote"] = Prediction("quotation", 0.9)
        d = fake_classifier(body_model).classify(EmailInput("Quote", "see attached", ()))
        self.assertEqual((d.decision, d.doc_type), (DECISION_NONE, "quotation"))


if __name__ == "__main__":
    unittest.main()
