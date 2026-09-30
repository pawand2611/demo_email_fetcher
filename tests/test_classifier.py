"""Tiered payment-document detection on hand-built MailFacts."""

from __future__ import annotations

import unittest

from mailbox_viewer.classifier import (
    DECISION_NONE,
    DECISION_PAYMENT,
    DOC_INVOICE,
    DOC_OTHER,
    DOC_RECEIPT,
    DOC_STATEMENT,
    TIER_BODY,
    TIER_FILENAME,
    TIER_NONE,
    TIER_SUBJECT,
    AttachmentFacts,
    KeywordMatcher,
    MailFacts,
    classify,
    is_document,
)


def att(filename: str, content_type: str) -> AttachmentFacts:
    return AttachmentFacts(filename=filename, content_type=content_type)


def facts(subject: str = "", body: str = "", attachments: tuple[AttachmentFacts, ...] = ()) -> MailFacts:
    return MailFacts(subject=subject, body_text=body, attachments=attachments)


class TierTests(unittest.TestCase):
    def test_tier1_filename_names_the_type_and_flags_only_that_file(self) -> None:
        result = classify(facts(subject="Documents", attachments=(att("holiday.pdf", "application/pdf"), att("Card_Statement_Sep.pdf", "application/pdf"))))

        self.assertEqual((result.doc_type, result.tier, result.confidence, result.decision), (DOC_STATEMENT, TIER_FILENAME, 0.95, DECISION_PAYMENT))
        self.assertIn('attachment "Card_Statement_Sep.pdf" contains "card statement"', result.reason)
        self.assertEqual([a.doc_type for a in result.attachments], [DOC_OTHER, DOC_STATEMENT])
        self.assertEqual([a.confidence for a in result.attachments], [0.30, 0.95])

    def test_tier2_subject_labels_every_document(self) -> None:
        result = classify(facts(subject="Your invoice for March", attachments=(att("a.pdf", "application/pdf"), att("b.csv", "text/csv"), att("logo.png", "image/png"))))

        self.assertEqual((result.doc_type, result.tier, result.confidence), (DOC_INVOICE, TIER_SUBJECT, 0.80))
        self.assertEqual([a.doc_type for a in result.attachments], [DOC_INVOICE, DOC_INVOICE, None])
        self.assertTrue(result.is_payment)

    def test_tier3_body(self) -> None:
        result = classify(facts(subject="September", body="Please find the payment receipt attached.", attachments=(att("doc.pdf", "application/pdf"),)))

        self.assertEqual((result.doc_type, result.tier, result.confidence), (DOC_RECEIPT, TIER_BODY, 0.60))
        self.assertIn("body contains", result.reason)

    def test_document_without_keyword_is_other_and_not_payment(self) -> None:
        result = classify(facts(subject="Holiday photos list", attachments=(att("list.pdf", "application/pdf"),)))

        self.assertEqual((result.doc_type, result.tier, result.confidence, result.decision), (DOC_OTHER, TIER_NONE, 0.30, DECISION_NONE))
        self.assertEqual(result.attachments[0].doc_type, DOC_OTHER)

    def test_keyword_without_document_is_a_notice_not_a_document(self) -> None:
        result = classify(facts(subject="Your statement is ready to view online"))

        self.assertEqual((result.doc_type, result.tier, result.confidence, result.decision), (None, TIER_NONE, None, DECISION_NONE))
        self.assertIn("no document is attached", result.reason)

    def test_plain_mail(self) -> None:
        result = classify(facts(subject="Lunch tomorrow?"))

        self.assertEqual((result.doc_type, result.decision), (None, DECISION_NONE))
        self.assertEqual(result.attachments, ())

    def test_image_attachment_does_not_count_as_document(self) -> None:
        result = classify(facts(subject="invoice", attachments=(att("shot.png", "image/png"),)))

        self.assertEqual(result.decision, DECISION_NONE)
        self.assertEqual(result.attachments[0].doc_type, None)

    def test_family_precedence_statement_before_invoice(self) -> None:
        result = classify(facts(subject="Invoice and account statement", attachments=(att("x.pdf", "application/pdf"),)))
        self.assertEqual(result.doc_type, DOC_STATEMENT)

    def test_word_boundaries(self) -> None:
        self.assertEqual(classify(facts(subject="Billion dollar footprint", attachments=(att("x.pdf", "application/pdf"),))).doc_type, DOC_OTHER)
        self.assertEqual(classify(facts(subject="Bills for March", attachments=(att("x.pdf", "application/pdf"),))).doc_type, DOC_INVOICE)

    def test_is_document_falls_back_to_extension(self) -> None:
        self.assertTrue(is_document(att("Statement.XLSX", "application/octet-stream")))
        self.assertTrue(is_document(att("noext", "application/pdf")))
        self.assertFalse(is_document(att("archive.zip", "application/zip")))


class KeywordMatcherTests(unittest.TestCase):
    def test_reports_canonical_keyword_for_plurals_and_separators(self) -> None:
        matcher = KeywordMatcher(("card statement", "statement", "bill"))

        self.assertEqual(matcher.find("Card_Statement_Sep.pdf"), "card statement")
        self.assertEqual(matcher.find("Your statements are ready"), "statement")
        self.assertEqual(matcher.find("Bills due"), "bill")
        self.assertIsNone(matcher.find("Billion"))
        self.assertIsNone(matcher.find(""))


if __name__ == "__main__":
    unittest.main()
