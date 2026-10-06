"""Schema creation on SQLite and dialect compilation for PostgreSQL."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from mailbox_viewer.db import init_db, make_engine
from mailbox_viewer.models import Attachment, Base

EXPECTED_TABLES = {"threads", "emails", "email_participants", "attachments", "sync_state", "decision_log"}


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(f"sqlite:///{(Path(self.tmp.name) / 'x.db').as_posix()}")

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def test_init_db_creates_all_six_tables_and_is_idempotent(self) -> None:
        init_db(self.engine)
        init_db(self.engine)

        inspector = inspect(self.engine)
        self.assertEqual(set(inspector.get_table_names()), EXPECTED_TABLES)
        emails = {c["name"] for c in inspector.get_columns("emails")}
        self.assertTrue({"thread_id", "message_id", "in_reply_to", "references_header", "matched_directly", "doc_type", "confidence", "decision_reason"} <= emails)
        self.assertNotIn("content", {c["name"] for c in inspector.get_columns("attachments")})
        self.assertIn("ix_emails_thread_id", {i["name"] for i in inspector.get_indexes("emails")})

    def test_models_compile_to_postgresql_ddl(self) -> None:
        dialect = postgresql.dialect()
        ddl = {t.name: str(CreateTable(t).compile(dialect=dialect)) for t in Base.metadata.sorted_tables}

        self.assertEqual(set(ddl), EXPECTED_TABLES)
        self.assertIn("SERIAL", ddl["threads"])
        self.assertIn("ON DELETE CASCADE", ddl["attachments"])
        self.assertIn("ON DELETE RESTRICT", ddl["emails"])
        self.assertIn("UNIQUE (message_id)", ddl["emails"])
        self.assertIn("CHECK (role IN ('from', 'to', 'cc', 'bcc'))", ddl["email_participants"])

    def test_models_compile_to_sqlite_ddl(self) -> None:
        ddl = str(CreateTable(Attachment.__table__).compile(dialect=sqlite.dialect()))
        self.assertIn("blob_key TEXT NOT NULL", ddl)

    def test_postgres_engine_url_is_accepted_without_connecting(self) -> None:
        engine = make_engine("postgresql+psycopg://user:secret@db.example.invalid:5432/mailbox")
        try:
            self.assertEqual((engine.dialect.name, engine.dialect.driver), ("postgresql", "psycopg"))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
