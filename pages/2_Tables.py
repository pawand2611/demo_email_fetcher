"""Streamlit page: browse the rows of every table in the configured database.

Works on SQLite and PostgreSQL and on whichever tables exist (the current
layout or the six-table model), because it reflects the live schema instead
of importing models. Binary columns are shown as their size, never dumped.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import LargeBinary

from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.db import make_engine

st.set_page_config(page_title="Tables", page_icon="📋", layout="wide")

PREFERRED_ORDER = ["threads", "emails", "email_participants", "attachments", "sync_state", "decision_log"]


@st.cache_resource(show_spinner=False)
def get_engine():
    settings = load_settings()
    return make_engine(settings.database_url, settings.db_schema)


def main() -> None:
    st.title("📋 Tables")
    try:
        settings = load_settings()
        engine = get_engine()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()

    schema_note = f", schema `{settings.db_schema}`" if settings.db_schema else ""
    st.caption(f"Database: `{make_url(settings.database_url).render_as_string(hide_password=True)}`{schema_note} — live rows, read only.")

    try:
        inspector = inspect(engine)
        names = inspector.get_table_names()
    except SQLAlchemyError as exc:
        st.error(f"Could not connect: {str(exc.__cause__ or exc).splitlines()[0][:160]}")
        st.stop()

    if not names:
        st.info("The database has no tables yet. Run `python check_db.py` or `python create_schema.py`.")
        return

    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=engine) for name in names}
    ordered = [n for n in PREFERRED_ORDER if n in tables] + sorted(n for n in tables if n not in PREFERRED_ORDER)

    with engine.connect() as conn:
        counts = {n: conn.execute(select(func.count()).select_from(tables[n])).scalar() or 0 for n in ordered}

    st.subheader("Overview")
    st.dataframe(
        pd.DataFrame(
            {
                "table": ordered,
                "rows": [counts[n] for n in ordered],
                "columns": [len(tables[n].columns) for n in ordered],
                "column names": [", ".join(c.name for c in tables[n].columns) for n in ordered],
            }
        ),
        hide_index=True,
        width="stretch",
    )

    st.divider()
    col_pick, col_limit, col_order = st.columns([2, 1, 1])
    chosen = col_pick.selectbox("Table", ordered, format_func=lambda n: f"{n} ({counts[n]} rows)")
    limit = col_limit.number_input("Rows to show", min_value=10, max_value=2000, value=100, step=50)
    table = tables[chosen]
    order_options = ["(insertion order)"] + [c.name for c in table.columns]
    order_by = col_order.selectbox("Sort by", order_options)
    descending = st.toggle("Newest / largest first", value=True)

    stmt = select(*_display_columns(table)).limit(int(limit))
    if order_by != "(insertion order)":
        col = table.c[order_by]
        stmt = stmt.order_by(col.desc().nulls_last() if descending else col.asc())
    elif descending and "id" in table.c:
        stmt = stmt.order_by(table.c.id.desc())

    with engine.connect() as conn:
        frame = pd.read_sql(stmt, conn)

    st.caption(f"Showing {len(frame)} of {counts[chosen]} rows in `{chosen}`.")
    st.dataframe(frame, hide_index=True, width="stretch", height=min(40 + 35 * max(len(frame), 1), 700))

    with st.expander("Column definitions"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "column": c.name,
                        "type": str(c.type),
                        "nullable": "yes" if c.nullable else "no",
                        "primary key": "yes" if c.primary_key else "",
                        "references": ", ".join(f"{fk.column.table.name}.{fk.column.name}" for fk in c.foreign_keys),
                    }
                    for c in table.columns
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def _display_columns(table: Table) -> list:
    """Every column, except binary ones are replaced by their byte length."""
    columns = []
    for col in table.columns:
        if isinstance(col.type, LargeBinary):
            columns.append(func.length(col).label(f"{col.name} (bytes)"))
        else:
            columns.append(col)
    return columns


main()
