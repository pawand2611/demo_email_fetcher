"""Overview: what is in the cache, how emails were decided, and when each folder last synced."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiError
from ui import DECISION_ICONS, fmt, get_api

api = get_api()
st.title("📬 Statement Recon · Mailbox Viewer")
st.caption("Finds statements, invoices and receipts in the mailbox with two local models, "
           "and keeps a logged reason for every decision.")

try:
    stats = api.stats()
    profile = api.profiles()[0]
    recent = api.threads(payment_only=True, limit=5)
except ApiError as exc:
    st.error(str(exc))
    st.stop()

# -- headline numbers -----------------------------------------------------------------------
cols = st.columns(6)
cols[0].metric("Conversations", stats["threads"])
cols[1].metric("Emails stored", stats["emails"])
cols[2].metric("Payment threads", stats["payment_threads"])
cols[3].metric("Needs review", stats["needs_review"], help="Payment answers between the review floor and the payment bar")
cols[4].metric("Emails judged", stats["judged"], help="Every email the models looked at, kept or not")
cols[5].metric("Not kept", stats["dropped"], help="Judged as not payment-related; only the decision is logged")

if stats["judged"] == 0:
    st.info("No mail judged yet. Press **🔄 Refresh mail** in the sidebar to run the first sync.")
    st.stop()

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("How emails were decided")
    order = ["payment", "review", "none"]
    decided = pd.DataFrame(
        {"decision": [f"{DECISION_ICONS[d]} {d}" for d in order],
         "emails": [stats["by_decision"].get(d, 0) for d in order]}
    )
    st.bar_chart(decided, x="decision", y="emails", horizontal=True, color="#ffad28", height=220)
    st.caption("From the decision log: every email judged, including those not kept.")

with right:
    st.subheader("Document types stored")
    if stats["by_doc_type"]:
        kinds = pd.DataFrame(sorted(stats["by_doc_type"].items(), key=lambda kv: -kv[1]),
                             columns=["document type", "emails"])
        st.bar_chart(kinds, x="document type", y="emails", horizontal=True, color="#6d706b", height=220)
    else:
        st.info("No stored emails yet.")

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("Latest payment conversations")
    if not recent:
        st.info("No payment conversations yet.")
    for t in recent:
        with st.container(border=True):
            text, action = st.columns([5, 1], vertical_alignment="center")
            text.markdown(f"**{t['subject'] or '(no subject)'}**")
            text.caption(f"{fmt(t['last_message_at'])} · {t['message_count']} message(s) · "
                         f"{t['attachment_count']} file(s) · {t['participants']}")
            if action.button("Open", key=f"open-{t['id']}", width="stretch"):
                st.session_state["open_thread"] = t["id"]
                st.switch_page("views/inbox.py")
    st.page_link("views/inbox.py", label="Open the inbox", icon="📬")

with right:
    st.subheader("Rules in force")
    with st.container(border=True):
        a, b, c = st.columns(3)
        a.metric("Attachment decides", f"> {profile['attachment_decides_confidence']:.2f}")
        b.metric("Email text: payment", f"≥ {profile['body_min_confidence']:.2f}")
        c.metric("Review from", f"{profile['model_min_confidence']:.2f}")
        st.caption("Keeping: " + ("payment and review mail, with their threads"
                                  if profile["keep_policy"] == "payment_only" else "every email"))
        st.caption("Payment types: " + ", ".join(profile["payment_labels"]))
        st.page_link("views/how_it_decides.py", label="See how it decides", icon="🧠")

    st.subheader("Sync status")
    st.dataframe(
        pd.DataFrame(
            [
                {"folder": s["folder_name"], "last sync": fmt(s["last_sync_at"]) if s["last_sync_at"] else "never",
                 "resume after UID": s["last_seen_uid"]}
                for s in stats["sync_states"]
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(f"Attachments are stored in {profile['attachment_store']}.")
