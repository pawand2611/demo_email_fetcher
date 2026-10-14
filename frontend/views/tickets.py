"""Tickets: one per payment thread, with its age. Opened and closed by the mail itself, or by hand."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiError, parse_time
from ui import TICKET_EVENTS, TICKET_STATUS, fmt, get_api

api = get_api()
st.title("🎫 Tickets")
st.caption("One ticket per payment conversation. It **opens** with the first payment email, moves to "
           "**in progress** when we reply, and **closes** when an email confirms the payment, or when you close it.")

try:
    stats = api.stats()["tickets"]
    profile = api.profiles()[0]
except ApiError as exc:
    st.error(str(exc))
    st.stop()

c = st.columns(4)
c[0].metric("🔴 Open", stats["open"])
c[1].metric("🟠 In progress", stats["in_progress"])
c[2].metric("🟢 Closed", stats["closed"])
oldest = stats["oldest_active_days"]
c[3].metric("Oldest active", f"{oldest:.0f} days" if oldest is not None else "–",
            help="Age of the oldest ticket that is still open or in progress")

if not (stats["open"] or stats["in_progress"] or stats["closed"]):
    st.info("No tickets yet. They open as payment emails arrive. For payment conversations that were stored "
            "before tickets existed, use **🎫 Rebuild tickets** under Maintenance in the sidebar.")
    st.stop()

with st.expander("Active tickets by age", expanded=False):
    buckets = pd.DataFrame(list(stats["active_by_bucket"].items()), columns=["age", "tickets"])
    st.bar_chart(buckets, x="age", y="tickets", color="#ffad28", height=200)

VIEWS = {"Active": None, "Open": "open", "In progress": "in_progress", "Closed": "closed", "All": None}
view = st.radio("Show", list(VIEWS), horizontal=True, label_visibility="collapsed")
try:
    rows = api.tickets(VIEWS[view])
except ApiError as exc:
    st.error(str(exc))
    st.stop()
if view == "Active":
    rows = [t for t in rows if t["status"] != "closed"]

if not rows:
    st.info(f"No {view.lower()} tickets.")
    st.stop()

table = pd.DataFrame(
    {
        "id": [t["id"] for t in rows],
        "Status": [TICKET_STATUS[t["status"]][0] for t in rows],
        "Conversation": [t["subject"] for t in rows],
        "Age (days)": [t["age_days"] for t in rows],
        "Bucket": [t["bucket"] for t in rows],
        "Opened": [parse_time(t["opened_at"]) for t in rows],
        "Closed": [parse_time(t["closed_at"]) for t in rows],
        "Closed by": [t["closed_by"] or "" for t in rows],
    }
)
event = st.dataframe(
    table.drop(columns=["id"]),
    hide_index=True,
    width="stretch",
    on_select="rerun",
    selection_mode="single-row",
    key="ticket_table",
    column_config={
        "Status": st.column_config.TextColumn(width="small"),
        "Conversation": st.column_config.TextColumn(width="large"),
        "Age (days)": st.column_config.NumberColumn(format="%.1f", width="small"),
        "Bucket": st.column_config.TextColumn(width="small"),
        "Opened": st.column_config.DatetimeColumn("Opened (IST)", format="DD MMM, HH:mm", width="small"),
        "Closed": st.column_config.DatetimeColumn("Closed (IST)", format="DD MMM, HH:mm", width="small"),
        "Closed by": st.column_config.TextColumn(width="small"),
    },
)
st.caption(f"Active tickets oldest first. Auto-close needs a payment confirmation scored "
           f"{profile['ticket_close_confidence']:.2f} or higher; an email carrying a new invoice never closes a ticket.")

selected = event.selection.rows
if selected:
    ticket_id = int(table.iloc[selected[0]]["id"])
    st.session_state["open_ticket"] = ticket_id
elif st.session_state.get("open_ticket") in set(table["id"]):
    ticket_id = int(st.session_state["open_ticket"])
else:
    st.caption("Select a ticket to see its history and close or reopen it.")
    st.stop()

try:
    t = api.ticket(ticket_id)
except ApiError as exc:
    st.warning(str(exc))
    st.stop()

st.divider()
label, color = TICKET_STATUS[t["status"]]
head, badge = st.columns([5, 1], vertical_alignment="center")
head.subheader(t["subject"])
badge.badge(label.split(" ", 1)[1], color=color)
st.caption(f"Opened {fmt(t['opened_at'])} · age {t['age_days']:.1f} days ({t['bucket']}) · "
           f"{t['message_count']} message(s) in the conversation")
if t["status"] == "closed":
    conf = f" · confidence {t['close_confidence']:.2f}" if t["close_confidence"] is not None else ""
    st.success(f"Closed {fmt(t['closed_at'])} by **{t['closed_by']}**: {t['close_reason']}{conf}")

left, right = st.columns([3, 2])
with left:
    st.markdown("**History**")
    st.dataframe(
        pd.DataFrame(
            {
                "When": [parse_time(e["at"]) for e in t["events"]],
                "Event": [TICKET_EVENTS.get(e["event"], e["event"]) for e in t["events"]],
                "By": [e["actor"] for e in t["events"]],
                "Reason": [e["reason"] or "" for e in t["events"]],
                "Confidence": [e["confidence"] for e in t["events"]],
            }
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "When": st.column_config.DatetimeColumn("When (IST)", format="DD MMM YYYY, HH:mm", width="small"),
            "Confidence": st.column_config.NumberColumn(format="%.2f", width="small"),
        },
    )

with right:
    with st.container(border=True):
        if t["status"] == "closed":
            st.markdown("**Reopen this ticket**")
            reason = st.text_input("Reason (optional)", key=f"reopen-reason-{ticket_id}",
                                   placeholder="e.g. payment bounced")
            if st.button("↩️ Reopen", key=f"reopen-{ticket_id}", width="stretch"):
                try:
                    api.reopen_ticket(ticket_id, reason)
                    st.rerun()
                except ApiError as exc:
                    st.warning(str(exc))
        else:
            st.markdown("**Close this ticket by hand**")
            reason = st.text_input("Reason (optional)", key=f"close-reason-{ticket_id}",
                                   placeholder="e.g. paid by bank transfer, confirmed on the phone")
            if st.button("✅ Close ticket", key=f"close-{ticket_id}", type="primary", width="stretch"):
                try:
                    api.close_ticket(ticket_id, reason)
                    st.rerun()
                except ApiError as exc:
                    st.warning(str(exc))
    if st.button("📬 Open the conversation", key=f"thread-{ticket_id}", width="stretch"):
        st.session_state["open_thread"] = t["thread_id"]
        st.switch_page("views/inbox.py")
