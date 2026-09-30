"""Streamlit page: the six-table data model, rendered live from schema_v2.py.

Nothing here is hand-drawn. The diagram, the column tables and the DDL are all
generated from the SQLAlchemy metadata, so this page always matches the code.
"""

from __future__ import annotations

import streamlit as st
from sqlalchemy import Table, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CheckConstraint, CreateIndex, CreateTable, ForeignKeyConstraint, UniqueConstraint

from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.db import make_engine
from mailbox_viewer.schema_v2 import Base

st.set_page_config(page_title="Data model", page_icon="🗂️", layout="wide")

DIALECTS = {"SQLite": sqlite.dialect(), "PostgreSQL": postgresql.dialect()}

# Fill / text colours per table, echoing the design sketch.
STYLE = {
    "threads": ("#1f1f1f", "#ffffff"),
    "emails": ("#f5a623", "#1f1f1f"),
    "email_participants": ("#6b6b6b", "#ffffff"),
    "attachments": ("#c8c8c8", "#1f1f1f"),
    "sync_state": ("#e8e6e1", "#1f1f1f"),
    "decision_log": ("#e8e6e1", "#1f1f1f"),
}

OWNERSHIP = {
    "threads": "One row per conversation, keyed by the root Message-ID. `has_payment` flips when any message matches.",
    "emails": "One row per message, pointing at its thread. `message_id` is unique: the de-dup guarantee.",
    "email_participants": "One row per person per role, so To and Cc lists that change across a thread stay clean.",
    "attachments": "Tied to the message it arrived on. `blob_key` is a reference only; no bytes in the database.",
    "sync_state": "The bookmark: where the next incremental sync resumes.",
    "decision_log": "The audit trail: what the classifier decided, at which tier, and why.",
}

ORDER = ["threads", "emails", "email_participants", "attachments", "sync_state", "decision_log"]


def main() -> None:
    st.title("🗂️ The data model")
    st.caption("Six tables, clear ownership. Generated live from `mailbox_viewer/schema_v2.py`.")

    dialect_name = st.radio("Show types and DDL for", list(DIALECTS), horizontal=True)
    dialect = DIALECTS[dialect_name]
    tables = {t.name: t for t in Base.metadata.sorted_tables}

    left, right = st.columns([3, 2])
    with left:
        st.graphviz_chart(render_dot(tables, dialect), width="stretch")
    with right:
        st.subheader("Ownership")
        for name in ORDER:
            st.markdown(f"**{name}**: {OWNERSHIP[name]}")
        render_live_status(tables)

    st.divider()
    st.subheader("Columns and constraints")
    for name in ORDER:
        render_table_details(tables[name], dialect)

    st.divider()
    st.subheader(f"DDL ({dialect_name})")
    st.code(render_ddl(tables, dialect), language="sql")


# -- rendering -------------------------------------------------------------------


def render_dot(tables: dict[str, Table], dialect) -> str:
    lines = [
        "digraph schema {",
        "  rankdir=LR; nodesep=0.5; ranksep=0.9; bgcolor=transparent;",
        '  node [shape=plaintext fontname="Helvetica" fontsize=11];',
        '  edge [color="#555555" arrowsize=0.7 fontname="Helvetica" fontsize=9];',
    ]
    for name in ORDER:
        table = tables[name]
        fill, fg = STYLE[name]
        rows = []
        for col in table.columns:
            marks = _marks(col)
            label = _escape(col.name) + (f" ({marks})" if marks else "")
            bold = col.primary_key or col.unique or any(c.unique for c in _unique_constraints(table) if col.name in c.columns.keys())
            if bold:
                label = f"<b>{label}</b>"
            rows.append(f'<tr><td align="left" port="{_escape(col.name)}">{label}</td></tr>')
        header = f'<tr><td bgcolor="{fill}" align="left"><font color="{fg}"><b>{name}</b></font></td></tr>'
        lines.append(
            f'  {name} [label=<<table border="1" cellborder="0" cellspacing="0" cellpadding="4" color="#999999" bgcolor="#fdf6e3">'
            f"{header}{''.join(rows)}</table>>];"
        )
    for name in ORDER:
        for fk in tables[name].foreign_keys:
            target = fk.column.table.name
            lines.append(f'  {name}:{fk.parent.name} -> {target}:{fk.column.name} [label="n → 1"];')
    lines.append("}")
    return "\n".join(lines)


def render_table_details(table: Table, dialect) -> None:
    with st.expander(f"{table.name}  ·  {len(table.columns)} columns", expanded=table.name in ("threads", "emails")):
        st.caption(OWNERSHIP[table.name])
        rows = []
        for col in table.columns:
            fks = [f"→ {fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys]
            rows.append(
                {
                    "column": col.name,
                    "type": str(col.type.compile(dialect=dialect)),
                    "nullable": "yes" if col.nullable else "no",
                    "key": _marks(col) or "",
                    "references": ", ".join(fks),
                    "indexed": "yes" if any(col.name in idx.columns.keys() for idx in table.indexes) else "",
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")

        notes = []
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                notes.append(f"CHECK {constraint.sqltext}")
            elif isinstance(constraint, UniqueConstraint) and constraint.columns:
                notes.append("UNIQUE (" + ", ".join(constraint.columns.keys()) + ")")
            elif isinstance(constraint, ForeignKeyConstraint):
                cols = ", ".join(constraint.column_keys)
                notes.append(f"FOREIGN KEY ({cols}) ON DELETE {constraint.ondelete or 'NO ACTION'}")
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            notes.append(f"INDEX {idx.name} ({', '.join(idx.columns.keys())})")
        if notes:
            st.markdown("\n".join(f"- `{n}`" for n in notes))


def render_ddl(tables: dict[str, Table], dialect) -> str:
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            parts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")
        parts.append("")
    return "\n".join(parts)


def render_live_status(tables: dict[str, Table]) -> None:
    """Which of the six tables already exist in the configured database. Read-only."""
    st.subheader("In the configured database")
    try:
        settings = load_settings()
    except ConfigError as exc:
        st.warning(f"No database configured: {exc}")
        return
    url = make_url(settings.database_url)
    st.caption(url.render_as_string(hide_password=True) + (f" · schema {settings.db_schema}" if settings.db_schema else ""))
    engine = make_engine(settings.database_url, settings.db_schema)
    try:
        inspector = inspect(engine)
        existing = set(inspector.get_table_names())
        live_columns = {n: {c["name"] for c in inspector.get_columns(n)} for n in ORDER if n in existing}
    except Exception as exc:  # unreachable server, bad credentials
        st.error(f"Could not connect: {str(exc).splitlines()[0][:160]}")
        return
    finally:
        engine.dispose()

    matching, old_layout, missing = [], [], []
    for name in ORDER:
        if name not in existing:
            missing.append(name)
        elif live_columns[name] == {c.name for c in tables[name].columns}:
            matching.append(name)
        else:
            old_layout.append(name)
    if matching:
        st.markdown("Present, matching this model: " + ", ".join(f"`{n}`" for n in matching))
    if old_layout:
        st.markdown("Present with the **old layout** (same name, different columns): " + ", ".join(f"`{n}`" for n in old_layout))
    if missing:
        st.markdown("Not created yet: " + ", ".join(f"`{n}`" for n in missing))
    if old_layout or missing:
        st.caption("Build this model with `python create_schema.py` (add `--drop` to replace old-layout tables). The current app still uses the old layout until it is moved over.")


# -- helpers ---------------------------------------------------------------------


def _marks(col) -> str:
    marks = []
    if col.primary_key:
        marks.append("key")
    if col.foreign_keys:
        marks.append("FK")
    if col.unique or any(col.name in c.columns.keys() for c in _unique_constraints(col.table)):
        marks.append("unique")
    return ", ".join(marks)


def _unique_constraints(table: Table) -> list[UniqueConstraint]:
    return [c for c in table.constraints if isinstance(c, UniqueConstraint) and c.columns]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


main()
