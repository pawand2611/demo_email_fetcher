"""File-system attachment store."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mailbox_viewer.attachment_store import AttachmentStoreError, FileSystemStore, blob_key, build_store
from mailbox_viewer.config import Settings

DATA = b"%PDF-1.4 pretend statement"
SHA = hashlib.sha256(DATA).hexdigest()


class FileSystemStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "attachments"
        self.store = FileSystemStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_put_writes_content_addressed_file_and_get_reads_it_back(self) -> None:
        key = self.store.put(sha256=SHA, filename="Statement.pdf", data=DATA)

        self.assertEqual(key, f"{SHA}/Statement.pdf")
        self.assertEqual((self.root / SHA / "Statement.pdf").read_bytes(), DATA)
        self.assertEqual(self.store.get(key), DATA)
        self.assertTrue(self.store.exists(key))
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_same_file_twice_is_stored_once(self) -> None:
        self.store.put(sha256=SHA, filename="a.pdf", data=DATA)
        self.store.put(sha256=SHA, filename="a.pdf", data=DATA)
        self.assertEqual(len(list(self.root.rglob("*"))), 2)  # one folder, one file

    def test_missing_file_is_a_store_error(self) -> None:
        with self.assertRaises(AttachmentStoreError):
            self.store.get(f"{SHA}/nope.pdf")
        self.assertFalse(self.store.exists(f"{SHA}/nope.pdf"))

    def test_keys_that_escape_the_root_are_refused(self) -> None:
        for bad in ("../../etc/passwd", f"{SHA}/../x", "not-a-hash/a.pdf", SHA, f"{SHA}/a/b", "C:/Windows/x", ""):
            with self.subTest(key=bad):
                with self.assertRaises(AttachmentStoreError):
                    self.store.get(bad)
                self.assertFalse(self.store.exists(bad))

    def test_blob_key_is_sha_then_filename(self) -> None:
        self.assertEqual(blob_key("abc", "x.csv"), "abc/x.csv")

    def test_build_store_uses_configured_dir(self) -> None:
        settings = Settings("h", 993, "u", "p", "INBOX", 5, "sqlite://", 200, attachment_dir="C:/tmp/att")
        self.assertEqual(Path(build_store(settings).root), Path("C:/tmp/att"))
        self.assertIn("att", build_store(settings).describe())


if __name__ == "__main__":
    unittest.main()
