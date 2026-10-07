"""Streamlit page: browse the rows of every table, served by the backend."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiClient, ApiError

st.set_page_config(page_title="Tables", page_icon="📋", layout="wide")


@st.cache_resource(show_spinner=False)
def get_api() -> ApiClient:
    return ApiClient()


def main() -> None:
    api = get_api()
    st.title("📋 Tables")
    st.caption(f"Live rows from the backend at `{api.base_url}`, read only.")

    try:
        tables = api.tables()
        schema = {t["name"]: t for t in api.schema()}
    except ApiError as exc:
        st.error(str(exc))
        st.stop()

    st.subheader("Overview")
    st.dataframe(
        pd.DataFrame(
            {
                "table": [t["name"] for t in tables],
                "rows": [t["rows"] for t in tables],
                "columns": [len(t["columns"]) for t in tables],
                "column names": [", ".join(t["columns"]) for t in tables],
            }
        ),
        hide_index=True,
        width="stretch",
    )

    st.divider()
    counts = {t["name"]: t["rows"] for t in tables}
    col_pick, col_limit, col_order = st.columns([2, 1, 1])
    chosen = col_pick.selectbox("Table", [t["name"] for t in tables], format_func=lambda n: f"{n} ({counts[n]} rows)")
    limit = col_limit.number_input("Rows to show", min_value=10, max_value=2000, value=100, step=50)
    columns = next(t["columns"] for t in tables if t["name"] == chosen)
    order_by = col_order.selectbox("Sort by", ["(primary key)"] + columns)
    descending = st.toggle("Newest / largest first", value=True)

    try:
        page = api.table_rows(chosen, limit=int(limit), order_by=None if order_by == "(primary key)" else order_by, descending=descending)
    except ApiError as exc:
        st.error(str(exc))
        st.stop()

    frame = pd.DataFrame(page["rows"], columns=page["columns"])
    st.caption(f"Showing {len(frame)} of {page['total']} rows in `{chosen}`.")
    st.dataframe(frame, hide_index=True, width="stretch", height=min(40 + 35 * max(len(frame), 1), 700))

    with st.expander("Column definitions"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "column": c["name"],
                        "type": c["type"],
                        "nullable": "yes" if c["nullable"] else "no",
                        "primary key": "yes" if c["primary_key"] else "",
                        "references": ", ".join(c["references"]),
                    }
                    for c in schema[chosen]["columns"]
                ]
            ),
            hide_index=True,
            width="stretch",
        )


main()
