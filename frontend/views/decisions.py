"""Decision log: every email the models judged, kept or not, with the check that decided it."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiError, parse_time
from ui import CHECKS, DECISION_ICONS, get_api, which_check

api = get_api()
st.title("🧾 Decision log")
st.caption("Every email judged, including those not kept. For mail that was not kept only the "
           "decision is stored, never the content, so its subject is not shown.")

try:
    log = api.table_rows("decision_log", limit=2000, order_by="decided_at", descending=True)
    emails = api.table_rows("emails", limit=2000)
except ApiError as exc:
    st.error(str(exc))
    st.stop()

if not log["rows"]:
    st.info("Nothing judged yet. Press **🔄 Refresh mail** in the sidebar.")
    st.stop()

subjects = {e["message_id"]: e.get("subject") or "(no subject)" for e in emails["rows"]}
frame = pd.DataFrame(
    {
        "Decided": [parse_time(r["decided_at"]) for r in log["rows"]],
        "Decision": [f"{DECISION_ICONS.get(r['decision'], '')} {r['decision']}" for r in log["rows"]],
        "decision": [r["decision"] for r in log["rows"]],
        "Decided by": [CHECKS[which_check(r["tier"], r["reason"])] for r in log["rows"]],
        "Confidence": [r["confidence"] for r in log["rows"]],
        "Kept": [r["message_id"] in subjects for r in log["rows"]],
        "Subject": [subjects.get(r["message_id"], "— not kept —") for r in log["rows"]],
        "Reason": [r["reason"] or "" for r in log["rows"]],
        "Message-ID": [r["message_id"] for r in log["rows"]],
    }
)

# -- summary ------------------------------------------------------------------------------
counts = frame["decision"].value_counts()
c = st.columns(4)
c[0].metric("Judged", len(frame))
c[1].metric("🟢 Payment", int(counts.get("payment", 0)))
c[2].metric("🟠 Review", int(counts.get("review", 0)))
c[3].metric("⚪ None", int(counts.get("none", 0)))

by_check = frame.groupby(["Decided by", "decision"]).size().unstack(fill_value=0)
with st.expander("Which check decided, by outcome", expanded=False):
    st.bar_chart(by_check, horizontal=True, height=220)

# -- filters ------------------------------------------------------------------------------
f1, f2, f3, f4 = st.columns([2, 2, 1, 3])
pick = f1.multiselect("Decision", ["payment", "review", "none"], default=["payment", "review", "none"])
checks = f2.multiselect("Decided by", list(CHECKS.values()), default=list(CHECKS.values()))
kept = f3.selectbox("Stored", ["all", "kept", "not kept"])
text = f4.text_input("Search reason or subject", placeholder="e.g. receipt, invoice-0-4.pdf")

shown = frame[frame["decision"].isin(pick) & frame["Decided by"].isin(checks)]
if kept != "all":
    shown = shown[shown["Kept"] == (kept == "kept")]
if text:
    needle = text.lower()
    shown = shown[shown["Reason"].str.lower().str.contains(needle, regex=False)
                  | shown["Subject"].str.lower().str.contains(needle, regex=False)]

st.caption(f"Showing {len(shown)} of {len(frame)} decisions, newest first."
           + (f" Only the latest {len(frame)} are loaded." if log["total"] > len(frame) else ""))
st.dataframe(
    shown.drop(columns=["decision"]),
    hide_index=True,
    width="stretch",
    height=min(40 + 35 * max(len(shown), 1), 640),
    column_config={
        "Decided": st.column_config.DatetimeColumn("Decided (IST)", format="DD MMM, HH:mm", width="small"),
        "Decision": st.column_config.TextColumn(width="small"),
        "Decided by": st.column_config.TextColumn(width="medium"),
        "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f", width="small"),
        "Kept": st.column_config.CheckboxColumn("Stored", width="small"),
        "Subject": st.column_config.TextColumn(width="medium"),
        "Reason": st.column_config.TextColumn(width="large"),
        "Message-ID": st.column_config.TextColumn(width="small"),
    },
)
