"""Settings validation."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from mailbox_viewer.config import ConfigError, load_settings

BASE_ENV = {
    "IMAP_HOST": "imap.example.com",
    "IMAP_USER": "me@example.com",
    "IMAP_PASSWORD": "secret",
}

# A file that does not exist, so the project's real .env is never read here.
NO_ENV_FILE = "does-not-exist.env"


def load(**extra):
    env = {**BASE_ENV, **extra}
    with mock.patch.dict(os.environ, env, clear=True):
        return load_settings(NO_ENV_FILE)


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = load()

        self.assertEqual(settings.imap_port, 993)
        self.assertEqual(settings.imap_folder, "INBOX")
        self.assertEqual(settings.initial_fetch_limit, 200)
        self.assertEqual(settings.attachment_dir, "data/attachments")
        self.assertEqual(settings.database_url, "sqlite:///data/mailbox_cache.db")

    def test_missing_required_settings_are_listed(self) -> None:
        with mock.patch.dict(os.environ, {"IMAP_HOST": "h"}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_settings(NO_ENV_FILE)
        self.assertIn("IMAP_USER", str(ctx.exception))
        self.assertIn("IMAP_PASSWORD", str(ctx.exception))

    def test_attachment_dir_and_database_url_are_read(self) -> None:
        settings = load(ATTACHMENT_DIR="D:/mail/files", DATABASE_URL="postgresql+psycopg://u:p@h/db")

        self.assertEqual(settings.attachment_dir, "D:/mail/files")
        self.assertEqual(settings.database_url, "postgresql+psycopg://u:p@h/db")

    def test_db_schema_is_optional_and_validated(self) -> None:
        self.assertIsNone(load().db_schema)
        self.assertEqual(load(DB_SCHEMA="poc").db_schema, "poc")
        with self.assertRaises(ConfigError):
            load(DB_SCHEMA="poc; drop table x")

    def test_imap_folders(self) -> None:
        self.assertEqual(load().imap_folders, ("INBOX",))
        s = load(IMAP_FOLDERS="INBOX, [Gmail]/Sent Mail")
        self.assertEqual(s.imap_folders, ("INBOX", "[Gmail]/Sent Mail"))
        self.assertEqual(s.imap_folder, "INBOX")

    def test_store_only_payment_flag(self) -> None:
        self.assertFalse(load().store_only_payment)
        self.assertTrue(load(STORE_ONLY_PAYMENT="true").store_only_payment)
        with self.assertRaises(ConfigError):
            load(STORE_ONLY_PAYMENT="maybe")

    def test_model_settings(self) -> None:
        defaults = load()
        self.assertEqual((defaults.attachment_model_dir, defaults.body_model_dir), ("models/layoutlmv3_invoice", "models/laya"))
        self.assertEqual((defaults.model_min_confidence, defaults.body_min_confidence, defaults.profile_name), (0.5, 0.7, "statement_recon"))
        self.assertEqual(load(BODY_MIN_CONFIDENCE="0.95").body_min_confidence, 0.95)

        custom = load(ATTACHMENT_MODEL_DIR="D:/m/lv3", BODY_MODEL_DIR="D:/m/laya", MODEL_MIN_CONFIDENCE="0.8", PROFILE_NAME="hr_intake")
        self.assertEqual((custom.attachment_model_dir, custom.body_model_dir, custom.model_min_confidence, custom.profile_name),
                         ("D:/m/lv3", "D:/m/laya", 0.8, "hr_intake"))

        for bad in ("1.5", "-0.1", "high"):
            with self.assertRaises(ConfigError):
                load(MODEL_MIN_CONFIDENCE=bad)

    def test_missing_model_folders_give_no_models(self) -> None:
        from mailbox_viewer.document_model import load_classifier

        clf = load_classifier(load(ATTACHMENT_MODEL_DIR="does/not/exist", BODY_MODEL_DIR="does/not/exist"))
        self.assertEqual(clf.name, "body: none, attachments: none")
        self.assertFalse(clf.has_any_model)

    def test_non_integer_port_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load(IMAP_PORT="abc")


if __name__ == "__main__":
    unittest.main()
