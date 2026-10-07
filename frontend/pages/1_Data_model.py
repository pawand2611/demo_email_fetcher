"""Streamlit page: the six-table data model, served by the backend.

The diagram, column tables and DDL all come from the backend's ``/schema``
endpoints, which read the live SQLAlchemy models, so this page always matches
the code that writes the data.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiClient, ApiError

st.set_page_config(page_title="Data model", page_icon="🗂️", layout="wide")

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
    "attachments": "Tied to the message it arrived on. `blob_key` is a reference only; the file lives in the store.",
    "sync_state": "The bookmark, one row per folder: where the next incremental sync resumes.",
    "decision_log": "The audit trail: what the document model decided, and why.",
}


@st.cache_resource(show_spinner=False)
def get_api() -> ApiClient:
    return ApiClient()


def main() -> None:
    api = get_api()
    st.title("🗂️ The data model")
    st.caption("Six tables, clear ownership. Served by the backend from the live model code.")

    dialect_label = st.radio("Show types and DDL for", ["PostgreSQL", "SQLite"], horizontal=True)
    dialect = dialect_label.lower()
    try:
        tables = api.schema(dialect)
        ddl = api.ddl(dialect)
        live = {t["name"]: t for t in api.tables()}
    except ApiError as exc:
        st.error(str(exc))
        st.stop()

    left, right = st.columns([3, 2])
    with left:
        st.graphviz_chart(render_dot(tables), width="stretch")
    with right:
        st.subheader("Ownership")
        for t in tables:
            st.markdown(f"**{t['name']}**: {OWNERSHIP.get(t['name'], '')}")
        st.subheader("Rows in the database")
        st.markdown("\n".join(f"- `{name}`: {info['rows']}" for name, info in live.items()))

    st.divider()
    st.subheader("Columns and constraints")
    for t in tables:
        with st.expander(f"{t['name']}  ·  {len(t['columns'])} columns", expanded=t["name"] in ("threads", "emails")):
            st.caption(OWNERSHIP.get(t["name"], ""))
            st.dataframe(
                [
                    {
                        "column": c["name"],
                        "type": c["type"],
                        "nullable": "yes" if c["nullable"] else "no",
                        "key": _marks(c),
                        "references": ", ".join(c["references"]),
                        "indexed": "yes" if c["indexed"] else "",
                    }
                    for c in t["columns"]
                ],
                hide_index=True,
                width="stretch",
            )
            notes = t["constraints"] + [f"INDEX {i}" for i in t["indexes"]]
            if notes:
                st.markdown("\n".join(f"- `{n}`" for n in notes))

    st.divider()
    st.subheader(f"DDL ({dialect_label})")
    st.code(ddl, language="sql")


def render_dot(tables: list[dict]) -> str:
    lines = [
        "digraph schema {",
        "  rankdir=LR; nodesep=0.5; ranksep=0.9; bgcolor=transparent;",
        '  node [shape=plaintext fontname="Helvetica" fontsize=11];',
        '  edge [color="#555555" arrowsize=0.7 fontname="Helvetica" fontsize=9];',
    ]
    for t in tables:
        fill, fg = STYLE.get(t["name"], ("#e8e6e1", "#1f1f1f"))
        rows = []
        for c in t["columns"]:
            marks = _marks(c)
            label = _escape(c["name"]) + (f" ({marks})" if marks else "")
            if c["primary_key"] or c["unique"]:
                label = f"<b>{label}</b>"
            rows.append(f'<tr><td align="left" port="{_escape(c["name"])}">{label}</td></tr>')
        header = f'<tr><td bgcolor="{fill}" align="left"><font color="{fg}"><b>{t["name"]}</b></font></td></tr>'
        lines.append(
            f'  {t["name"]} [label=<<table border="1" cellborder="0" cellspacing="0" cellpadding="4" color="#999999" bgcolor="#fdf6e3">'
            f"{header}{''.join(rows)}</table>>];"
        )
    for t in tables:
        for c in t["columns"]:
            for ref in c["references"]:
                target_table, target_col = ref.split(".", 1)
                lines.append(f'  {t["name"]}:{c["name"]} -> {target_table}:{target_col} [label="n → 1"];')
    lines.append("}")
    return "\n".join(lines)


def _marks(c: dict) -> str:
    marks = []
    if c["primary_key"]:
        marks.append("key")
    if c["references"]:
        marks.append("FK")
    if c["unique"]:
        marks.append("unique")
    return ", ".join(marks)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


main()
