"""Create the six-table schema (mailbox_viewer/schema_v2.py) in a database.

Usage:
    python create_schema.py                       # create in DATABASE_URL from .env
    python create_schema.py --url sqlite:///x.db  # create somewhere else
    python create_schema.py --drop                # drop the six tables first, then create
    python create_schema.py --ddl postgresql      # print the DDL, touch nothing
    python create_schema.py --ddl sqlite
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateIndex, CreateTable

from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.db import init_db, make_engine
from mailbox_viewer.schema_v2 import Base

DIALECTS = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.ddl:
        print(render_ddl(args.ddl))
        return 0

    url, schema = args.url, None
    if not url:
        try:
            settings = load_settings()
            url, schema = settings.database_url, settings.db_schema
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2

    engine = make_engine(url, schema)
    print(f"Database : {make_url(url).render_as_string(hide_password=True)}")
    print(f"Schema   : {schema or '(server default)'}")
    try:
        if args.drop:
            Base.metadata.drop_all(engine)
            print("Dropped  : " + ", ".join(t.name for t in reversed(Base.metadata.sorted_tables)))
        init_db(engine, schema)
        existing = set(inspect(engine).get_table_names())
        created = [t.name for t in Base.metadata.sorted_tables if t.name in existing]
        print("Tables   : " + ", ".join(created))
    except SQLAlchemyError as exc:
        print(f"Database error: {str(exc.__cause__ or exc).strip().splitlines()[0]}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print("OK")
    return 0


def render_ddl(dialect_name: str) -> str:
    dialect = DIALECTS[dialect_name]
    parts: list[str] = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        parts.append("")
    return "\n".join(parts)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", help="database URL; default is DATABASE_URL from .env")
    parser.add_argument("--drop", action="store_true", help="drop the six tables before creating them")
    parser.add_argument("--ddl", choices=sorted(DIALECTS), help="print CREATE statements for this dialect and exit")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
