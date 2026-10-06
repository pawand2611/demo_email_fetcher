"""Check the DATABASE_URL in .env: connect, create missing tables, print counts.

Run this after changing DATABASE_URL (for example when switching from SQLite to
PostgreSQL) and before the first sync. It never contacts the mail server.

Usage:
    python check_db.py
"""

from __future__ import annotations

import sys

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from mailbox_viewer import repository as repo
from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    url = make_url(settings.database_url)
    print(f"Database     : {url.render_as_string(hide_password=True)}")
    print(f"Schema       : {settings.db_schema or '(server default)'}")

    engine = make_engine(settings.database_url, settings.db_schema)
    try:
        with engine.connect() as conn:
            version = conn.execute(text(_version_sql(url.get_backend_name()))).scalar()
        print(f"Connected    : {engine.dialect.name} ({_short(version)})")

        init_db(engine, settings.db_schema)
        tables = sorted(inspect(engine).get_table_names())
        print(f"Tables       : {', '.join(tables)}")

        with make_session_factory(engine)() as session:
            print(f"Threads      : {repo.count_threads(session)}")
            print(f"Emails       : {repo.count_emails(session)}")
            print(f"Attachments  : {repo.count_attachments(session)}")
            states = {f: repo.get_sync_state(session, f) for f in settings.imap_folders}
        for folder, state in states.items():
            if state and state.last_sync_at:
                print(f"Last sync    : {folder!r} {state.last_sync_at:%Y-%m-%d %H:%M} UTC, last seen UID {state.last_seen_uid}")
            else:
                print(f"Last sync    : {folder!r} never (run `python sync_mail.py` to fill the cache)")
    except SQLAlchemyError as exc:
        print(f"Database error: {_short(str(exc.__cause__ or exc))}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print("OK")
    return 0


def _version_sql(backend: str) -> str:
    return "select sqlite_version()" if backend == "sqlite" else "select version()"


def _short(value: object) -> str:
    text_value = str(value).strip()
    return text_value.splitlines()[0][:120] if text_value else ""


if __name__ == "__main__":
    sys.exit(main())
