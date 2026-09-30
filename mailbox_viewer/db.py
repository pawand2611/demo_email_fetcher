"""Engine and session factory construction. The URL and schema come from settings."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(database_url: str, schema: str | None = None) -> Engine:
    """Build the engine. On PostgreSQL, ``schema`` becomes the connection's
    ``search_path`` so every unqualified table name resolves there; SQLite
    ignores it."""
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    connect_args: dict[str, object] = {}

    if is_sqlite:
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        # Streamlit serves each script run on a worker thread; the pooled
        # sqlite3 connection must be allowed to cross threads.
        connect_args["check_same_thread"] = False
    elif schema:
        connect_args["options"] = f"-csearch_path={schema}"

    # pool_pre_ping makes a remote PostgreSQL connection that was dropped by
    # the server (idle timeout, restart) reconnect transparently.
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=not is_sqlite)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine, schema: str | None = None) -> None:
    """Create the schema (PostgreSQL) and any missing tables and indexes.
    Safe to call on every start-up."""
    if schema and engine.dialect.name != "sqlite":
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
