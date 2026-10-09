"""IST conversion for display; storage stays UTC."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from mailbox_viewer.timeutil import IST, fmt_ist, to_ist


class TimeTests(unittest.TestCase):
    def test_naive_values_are_utc_and_become_ist(self) -> None:
        self.assertEqual(to_ist(datetime(2026, 10, 7, 9, 30)).isoformat(), "2026-10-07T15:00:00+05:30")

    def test_aware_values_are_converted(self) -> None:
        self.assertEqual(to_ist(datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc)), datetime(2026, 10, 8, 4, 30, tzinfo=IST))

    def test_format_and_none(self) -> None:
        self.assertEqual(fmt_ist(datetime(2026, 10, 7, 9, 30)), "2026-10-07 15:00 IST")
        self.assertIsNone(to_ist(None))
        self.assertEqual(fmt_ist(None), "unknown")


if __name__ == "__main__":
    unittest.main()
