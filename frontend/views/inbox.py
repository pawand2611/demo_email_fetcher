"""Inbox: conversations from the cache, each message with its decision and attachments."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiError, parse_time
from ui import (CHECK_BADGES, CHECKS, DECISION_COLORS, DOC_COLORS, PAYMENT_TYPES, TIERS, escape_md, fmt, get_api,
                human_size, person, which_check)

api = get_api()
st.title("📬 Inbox")

f1, f2 = st.columns([1, 3])
payment_only = f1.toggle("Payment threads only", value=False)
search = f2.text_input("Search subject or participant", placeholder="e.g. invoice, bank, a name",
                       label_visibility="collapsed")

try:
    stats = api.stats()
    threads = api.threads(payment_only=payment_only, search=search or None)
except ApiError as exc:
    st.error(str(exc))
    st.stop()

if stats["threads"] == 0:
    if stats["judged"]:
        st.info(f"{stats['judged']} email(s) were judged and none was kept: only payment-related mail is stored. "
                "See the Decision log for what was judged.")
    else:
        st.info("No cached mail yet. Press **🔄 Refresh mail** in the sidebar to run the first sync.")
    st.stop()

st.caption(f"Showing {len(threads)} of {stats['threads']} conversations.")
if not threads:
    st.warning("No conversations match the filter.")
    st.stop()

table = pd.DataFrame(
    {
        "id": [t["id"] for t in threads],
        "Last message": [parse_time(t["last_message_at"]) for t in threads],
        "From": [t["participants"] for t in threads],
        "Subject": [t["subject"] or "(no subject)" for t in threads],
        "Msgs": [t["message_count"] for t in threads],
        "Files": [t["attachment_count"] for t in threads],
        "Payment": [t["has_payment"] for t in threads],
    }
)
event = st.dataframe(
    table.drop(columns=["id"]),
    hide_index=True,
    width="stretch",
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Last message": st.column_config.DatetimeColumn("Last message (IST)", format="DD MMM YYYY, HH:mm", width="small"),
        "From": st.column_config.TextColumn(width="medium"),
        "Subject": st.column_config.TextColumn(width="large"),
        "Msgs": st.column_config.NumberColumn("💬", width="small", help="messages in the thread"),
        "Files": st.column_config.NumberColumn("📎", width="small", help="attachments in the thread"),
        "Payment": st.column_config.CheckboxColumn("💳", width="small", help="has a payment document"),
    },
    key="thread_table",
)
rows = event.selection.rows
if rows:
    thread_id = int(table.iloc[rows[0]]["id"])
    st.session_state["open_thread"] = thread_id
elif st.session_state.get("open_thread") in set(table["id"]):  # opened from the Overview
    thread_id = int(st.session_state["open_thread"])
else:
    st.caption("Select a conversation to open it.")
    st.stop()

try:
    thread = api.thread(thread_id)
except ApiError as exc:
    st.warning(str(exc))
    st.stop()

st.divider()
st.subheader(thread["subject"] or "(no subject)")
st.caption(f"{thread['message_count']} message(s) in this conversation, newest first.")


def render_message(m: dict, expanded: bool) -> None:
    people = {role: [p for p in m["participants"] if p["role"] == role] for role in ("from", "to", "cc", "bcc")}
    sender = people["from"][0] if people["from"] else None
    d = m["decision"] or {}
    check = which_check(d.get("tier"), d.get("reason"))
    head = f"{fmt(m['received_at'])} · {person(sender) if sender else '(unknown sender)'}"
    if m["doc_type"]:
        head += f" · {m['doc_type']}"

    with st.expander(head, expanded=expanded):
        badges = st.columns([1, 1, 1, 5])
        if d.get("decision"):
            badges[0].badge(d["decision"], color=DECISION_COLORS.get(d["decision"], "gray"))
        if m["doc_type"]:
            badges[1].badge(m["doc_type"], color=DOC_COLORS.get(m["doc_type"], "gray"))
        badge_label, badge_color = CHECK_BADGES[check]
        badges[2].badge(badge_label, color=badge_color)
        conf = f"confidence {m['confidence']:.2f} · " if m["confidence"] is not None else ""
        badges[3].caption(f"{CHECKS[check]} · {conf}{m['decision_reason'] or ''}")

        for role, label in (("from", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
            if people[role]:
                st.text(f"{label}: " + ", ".join(person(p) for p in people[role]))

        tab_text, tab_files, tab_decision = st.tabs(["Text", f"Attachments ({len(m['attachments'])})", "Decision"])
        with tab_text:
            if m["body_text"].strip():
                st.text(m["body_text"])
            else:
                st.info("This email has no plain-text body.")
        with tab_files:
            if not m["attachments"]:
                st.info("No attachments.")
            for att in m["attachments"]:
                c1, c2, c3 = st.columns([5, 3, 2])
                icon = "📄" if att["doc_type"] in PAYMENT_TYPES else "📎"
                c1.markdown(f"{icon} **{escape_md(att['filename'])}**")
                if att["doc_type"]:
                    c1.caption(f"LayoutLMv3: {att['doc_type']} · {att['confidence']:.2f}")
                c2.caption(f"{att['content_type']} · {human_size(att['size_bytes'])}  \nstored once as {att['blob_key'][:12]}…")
                try:
                    c3.download_button("Download", data=api.attachment_bytes(att["id"]), file_name=att["filename"],
                                       mime=att["content_type"], key=f"download-{att['id']}", width="stretch")
                except ApiError as exc:
                    c3.error(str(exc))
        with tab_decision:
            if not d:
                st.info("No decision logged.")
            else:
                st.code(
                    f"decision   : {d['decision']}\n"
                    f"decided by : {CHECKS[check]}\n"
                    f"tier       : {d['tier']}  ({TIERS})\n"
                    f"confidence : {d['confidence'] if d['confidence'] is not None else '-'}\n"
                    f"reason     : {d['reason']}\n"
                    f"decided_at : {fmt(d['decided_at'])}\n"
                    f"message_id : {d['message_id']}\n"
                    f"in_reply_to: {m['in_reply_to'] or '-'}",
                    language=None,
                )


for index, message in enumerate(thread["messages"]):
    render_message(message, expanded=index == 0)
