"""Streamlit frontend: inbox by thread, message detail, attachments, decisions.

It talks only to the backend API (``API_URL``, default http://127.0.0.1:8000);
it never opens the database or the mailbox itself.

Run with (backend first, each from its own folder):
    cd backend;  python -m uvicorn api.main:create_app --factory --host 127.0.0.1 --port 8000
    cd frontend; python -m streamlit run app.py
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from api_client import ApiClient, ApiError, parse_time

st.set_page_config(page_title="Mailbox Viewer", page_icon="📬", layout="wide")

DOC_COLORS = {"statement": "green", "invoice": "violet", "receipt": "blue", "other": "gray",
              "purchase_order": "orange", "quotation": "orange", "not_invoice": "gray"}
DECISION_COLORS = {"payment": "green", "review": "orange", "none": "gray"}
PAYMENT_TYPES = ("statement", "invoice", "receipt")


@st.cache_resource(show_spinner=False)
def get_api() -> ApiClient:
    return ApiClient()


# -- page ------------------------------------------------------------------------------


def main() -> None:
    api = get_api()
    try:
        profile = api.profiles()[0]
        payment_only, search = render_sidebar(api, profile)
        st.title("📬 Mailbox Viewer")
        stats = api.stats()
        threads = api.threads(payment_only=payment_only, search=search or None)
    except ApiError as exc:
        st.error(str(exc))
        st.stop()

    if stats["threads"] == 0:
        if stats["judged"]:
            st.info(
                f"{stats['judged']} message(s) were judged and none was kept. With `STORE_ONLY_PAYMENT=true` only "
                "payment documents are stored; set it to `false` in `.env` and restart the backend to keep every message."
            )
        else:
            st.info("No cached mail yet. Click **Refresh** in the sidebar to run the first sync.")
        return

    thread_id = render_thread_list(threads, stats["threads"])
    if thread_id is not None:
        render_thread(api, thread_id)


def render_sidebar(api: ApiClient, profile: dict) -> tuple[bool, str]:
    with st.sidebar:
        st.header("Mailbox")
        st.caption(f"{profile['mailbox']} · " + ", ".join(profile["folders"]))
        st.caption(f"Profile: {profile['name']} · models: {profile['model']}")
        st.caption(f"Attachments → {profile['attachment_store']}")
        st.caption("Keeping: " + ("payment documents and their threads" if profile["keep_policy"] == "payment_only" else "every message"))

        if st.button("🔄 Refresh", type="primary", width="stretch", help="Pull only mail newer than the last sync"):
            try:
                job = api.start_sync()
                with st.spinner("Syncing and classifying new mail…", show_time=True):
                    job = _wait(api, job)
                st.session_state["last_sync_job"] = job
            except ApiError as exc:
                st.warning(str(exc))

        job = st.session_state.get("last_sync_job")
        if job is not None:
            _show_sync_job(job)

        if st.button("🏷️ Re-run classifier", width="stretch", help="Re-run both models over all cached mail; no mail-server contact. Takes a few minutes."):
            try:
                job = api.reclassify()
                with st.spinner("Re-running both models over cached mail…", show_time=True):
                    job = _wait(api, job)
                if job["state"] == "failed":
                    st.error(f"Re-classify crashed: {job['error']}")
                else:
                    st.info(f"Models re-applied: {job['result']['changed']} email(s) changed.")
            except ApiError as exc:
                st.warning(str(exc))

        stats = api.stats()
        st.divider()
        st.subheader("Cache")
        a, b, c = st.columns(3)
        a.metric("Threads", stats["threads"])
        b.metric("Emails", stats["emails"])
        c.metric("Files", stats["attachments"])
        d, e = st.columns(2)
        d.metric("Payment", stats["payment_threads"], help="threads with at least one payment document")
        e.metric("Review", stats["needs_review"], help="emails where the two models disagree or are unsure")
        st.caption(f"Judged: {stats['judged']} message(s) in decision_log, {stats['dropped']} dropped")
        if stats["by_doc_type"]:
            st.caption("Documents: " + ", ".join(f"{k} {v}" for k, v in sorted(stats["by_doc_type"].items())))
        for state in stats["sync_states"]:
            when = parse_time(state["last_sync_at"])
            if when:
                st.caption(f"{state['folder_name']}: last sync {_fmt(when)} · resume after UID {state['last_seen_uid']}")
            else:
                st.caption(f"{state['folder_name']}: never synced.")

        st.divider()
        st.subheader("Filter")
        payment_only = st.toggle("Payment threads only", value=False)
        search = st.text_input("Search subject or participant", placeholder="e.g. bank")

    return payment_only, search


def render_thread_list(threads: list[dict], total: int) -> int | None:
    st.caption(f"Showing {len(threads)} of {total} conversations, served by the backend from its cache.")
    if not threads:
        st.warning("No conversations match the current filter.")
        return None

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
    if not rows:
        st.caption("Select a conversation to open it.")
        return None
    return int(table.iloc[rows[0]]["id"])


def render_thread(api: ApiClient, thread_id: int) -> None:
    try:
        thread = api.thread(thread_id)
    except ApiError as exc:
        st.warning(str(exc))
        return

    st.divider()
    st.subheader(thread["subject"] or "(no subject)")
    st.caption(f"{thread['message_count']} message(s) in this conversation, newest first.")
    for index, message in enumerate(thread["messages"]):
        render_message(api, message, expanded=index == 0)


def render_message(api: ApiClient, m: dict, *, expanded: bool) -> None:
    people = {role: [p for p in m["participants"] if p["role"] == role] for role in ("from", "to", "cc", "bcc")}
    sender = people["from"][0] if people["from"] else None
    head = f"{_fmt(parse_time(m['received_at']))} · {_display(sender) if sender else '(unknown sender)'}"
    if m["doc_type"]:
        head += f" · {m['doc_type']}"
    with st.expander(head, expanded=expanded):
        decision = (m["decision"] or {}).get("decision")
        if m["doc_type"] or decision in ("payment", "review"):
            badge_col, reason_col = st.columns([1, 4])
            if m["doc_type"]:
                badge_col.badge(m["doc_type"], color=DOC_COLORS.get(m["doc_type"], "gray"))
            if decision in ("payment", "review"):
                badge_col.badge(decision, color=DECISION_COLORS[decision])
            confidence = f"confidence {m['confidence']:.2f} · " if m["confidence"] is not None else ""
            reason_col.caption(f"{confidence}{m['decision_reason']}")
        elif m["decision_reason"]:
            st.caption(m["decision_reason"])

        for role, label in (("from", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
            if people[role]:
                st.text(f"{label}: " + ", ".join(_display(p) for p in people[role]))

        tab_text, tab_files, tab_decision = st.tabs(["Text", f"Attachments ({len(m['attachments'])})", "Decision"])
        with tab_text:
            st.text(m["body_text"]) if m["body_text"].strip() else st.info("This email has no plain-text body.")
        with tab_files:
            if not m["attachments"]:
                st.info("No attachments.")
            for att in m["attachments"]:
                c1, c2, c3 = st.columns([5, 3, 2])
                icon = "📄" if att["doc_type"] in PAYMENT_TYPES else "📎"
                c1.markdown(f"{icon} **{_escape_md(att['filename'])}**")
                if att["doc_type"]:
                    c1.caption(f"{att['doc_type']} · confidence {att['confidence']:.2f}")
                c2.caption(f"{att['content_type']} · {_human_size(att['size_bytes'])}  \nblob_key {att['blob_key'][:12]}…")
                try:
                    data = api.attachment_bytes(att["id"])
                    c3.download_button("Download", data=data, file_name=att["filename"], mime=att["content_type"], key=f"download-{att['id']}", width="stretch")
                except ApiError as exc:
                    c3.error(str(exc))
        with tab_decision:
            d = m["decision"]
            if d is None:
                st.info("No decision logged.")
            else:
                st.code(
                    f"message_id : {d['message_id']}\n"
                    f"tier       : {d['tier']}  (0 nothing classified, 1 attachment model, 2 body model, 3 both)\n"
                    f"decision   : {d['decision']}\n"
                    f"confidence : {d['confidence'] if d['confidence'] is not None else '-'}\n"
                    f"reason     : {d['reason']}\n"
                    f"decided_at : {_fmt(parse_time(d['decided_at']))}\n"
                    f"in_reply_to: {m['in_reply_to'] or '-'}\n"
                    f"references : {m['references_header'] or '-'}",
                    language=None,
                )


# -- helpers -----------------------------------------------------------------------------


def _wait(api: ApiClient, job: dict) -> dict:
    """Poll a background job until it finishes."""
    while job["state"] == "running":
        time.sleep(1)
        job = api.job(job["id"])
    return job


def _show_sync_job(job: dict) -> None:
    if job["state"] == "failed":
        st.error(f"Sync crashed: {job['error']}")
        return
    result = job["result"]
    if result is None:
        return
    if result["status"] == "ok":
        st.success(f"Synced in {result['duration_seconds']}s: {result['summary']}")
    elif result["status"] == "partial":
        st.warning(f"Sync interrupted: {result['summary']}")
    else:
        st.error(f"Sync failed: {result['summary']}")


def _display(p: dict) -> str:
    return f"{p['name']} <{p['address']}>" if p["name"] else p["address"]


def _fmt(value: datetime | None) -> str:
    return value.strftime("%d %b %Y, %H:%M IST") if value else "unknown"


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _escape_md(text: str) -> str:
    return text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


if __name__ == "__main__":  # Streamlit runs the script as __main__; tests import it
    main()
