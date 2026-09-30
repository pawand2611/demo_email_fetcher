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

    def test_store_only_payment_flag(self) -> None:
        self.assertTrue(load().store_only_payment)
        self.assertFalse(load(STORE_ONLY_PAYMENT="false").store_only_payment)
        with self.assertRaises(ConfigError):
            load(STORE_ONLY_PAYMENT="maybe")

    def test_non_integer_port_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load(IMAP_PORT="abc")


if __name__ == "__main__":
    unittest.main()
