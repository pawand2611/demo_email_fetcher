"""Model-based classification with a fake document model; no keyword rules."""

from __future__ import annotations

import unittest

from mailbox_viewer.classifier import (
    DECISION_NONE,
    DECISION_PAYMENT,
    TIER_MODEL,
    TIER_NONE,
    AttachmentInput,
    NoModel,
    Prediction,
    classify,
    is_model_input,
)

from .fakes import FakeDocumentModel


def pdf(name: str, data: bytes = b"%PDF-1.4") -> AttachmentInput:
    return AttachmentInput(name, "application/pdf", data)


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = FakeDocumentModel()

    def test_payment_label_above_threshold_is_payment(self) -> None:
        result = classify([pdf("statement_aug.pdf")], self.model)

        self.assertEqual((result.doc_type, result.tier, result.confidence, result.decision), ("statement", TIER_MODEL, 0.95, DECISION_PAYMENT))
        self.assertEqual(result.reason, 'model fake: "statement_aug.pdf" is statement (0.95)')
        self.assertEqual([(a.doc_type, a.confidence) for a in result.attachments], [("statement", 0.95)])

    def test_strongest_payment_prediction_wins_and_each_file_keeps_its_label(self) -> None:
        self.model.overrides["a.pdf"] = Prediction("invoice", 0.70)
        self.model.overrides["b.pdf"] = Prediction("statement", 0.90)
        result = classify([pdf("a.pdf"), pdf("b.pdf"), AttachmentInput("rows.csv", "text/csv", b"a,b")], self.model)

        self.assertEqual((result.doc_type, result.confidence), ("statement", 0.90))
        self.assertEqual([a.doc_type for a in result.attachments], ["invoice", "statement", None])

    def test_payment_label_below_threshold_is_recorded_but_not_payment(self) -> None:
        self.model.overrides["x.pdf"] = Prediction("invoice", 0.40)
        result = classify([pdf("x.pdf")], self.model, min_confidence=0.5)

        self.assertEqual((result.doc_type, result.tier, result.decision), ("invoice", TIER_MODEL, DECISION_NONE))
        self.assertIn("below the 0.50 threshold", result.reason)

    def test_non_payment_label_is_not_payment(self) -> None:
        result = classify([pdf("holiday.pdf")], self.model)

        self.assertEqual((result.doc_type, result.decision), ("other", DECISION_NONE))
        self.assertIn("not a payment document", result.reason)

    def test_no_model_configured_leaves_documents_unclassified(self) -> None:
        result = classify([pdf("statement_aug.pdf")], NoModel())

        self.assertEqual((result.doc_type, result.tier, result.confidence, result.decision), (None, TIER_NONE, None, DECISION_NONE))
        self.assertEqual(result.reason, "no classification model configured; 1 document(s) left unclassified")

    def test_no_document_attachment(self) -> None:
        result = classify([AttachmentInput("rows.csv", "text/csv", b"a,b")], self.model)
        self.assertEqual((result.doc_type, result.decision, result.reason), (None, DECISION_NONE, "no PDF or image attachment for the model"))
        self.assertEqual(classify([], self.model).attachments, ())

    def test_missing_bytes_are_skipped(self) -> None:
        result = classify([AttachmentInput("statement.pdf", "application/pdf", None)], self.model)
        self.assertEqual(result.tier, TIER_NONE)
        self.assertEqual(self.model.calls, [])

    def test_model_error_on_one_file_does_not_stop_the_others(self) -> None:
        self.model.fail_on.add("broken.pdf")
        result = classify([pdf("broken.pdf"), pdf("invoice_9.pdf")], self.model)

        self.assertEqual((result.doc_type, result.decision), ("invoice", DECISION_PAYMENT))
        self.assertEqual([a.doc_type for a in result.attachments], [None, "invoice"])

    def test_only_errors_reports_them(self) -> None:
        self.model.fail_on.add("broken.pdf")
        result = classify([pdf("broken.pdf")], self.model)
        self.assertEqual(result.reason, "model fake could not classify 1 document(s) (1 error(s))")

    def test_labels_are_normalised_and_scores_clamped(self) -> None:
        self.model.overrides["x.pdf"] = Prediction("  Statement ", 1.7)
        result = classify([pdf("x.pdf")], self.model)
        self.assertEqual((result.doc_type, result.confidence), ("statement", 1.0))

    def test_with_note_appends_to_reason(self) -> None:
        result = classify([pdf("holiday.pdf")], self.model).with_note("kept as part of a payment thread")
        self.assertTrue(result.reason.endswith("; kept as part of a payment thread"))

    def test_model_reads_pdfs_and_images_only(self) -> None:
        self.assertTrue(is_model_input(AttachmentInput("scan.jpg", "image/jpeg")))
        self.assertTrue(is_model_input(AttachmentInput("doc", "application/pdf")))
        self.assertTrue(is_model_input(AttachmentInput("Scan.PNG", "application/octet-stream")))
        self.assertFalse(is_model_input(AttachmentInput("rows.csv", "text/csv")))
        self.assertFalse(is_model_input(AttachmentInput("book.xlsx", "application/vnd.ms-excel")))


if __name__ == "__main__":
    unittest.main()
