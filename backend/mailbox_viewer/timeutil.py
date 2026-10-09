"""Display time zone: Indian Standard Time.

The database stores naive UTC (see ``models.py``); every timestamp shown to a
person (API responses, Swagger, Streamlit, command-line output) is converted
to IST here. IST has no daylight saving, so a fixed +05:30 offset is exact
and needs no time-zone database (Windows ships none for ``zoneinfo``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def to_ist(value: datetime | None) -> datetime | None:
    """Convert to IST. Naive values are taken to be UTC, as stored."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def fmt_ist(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    converted = to_ist(value)
    return f"{converted.strftime(pattern)} IST" if converted else "unknown"
