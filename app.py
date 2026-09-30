"""Streamlit UI: inbox by thread, message detail, attachments, decisions.

Every render reads only from the database. The mail server is contacted in
exactly one place: the Refresh button, which runs an incremental sync behind
a spinner.

Run with:  streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from mailbox_viewer import repository as repo
from mailbox_viewer.attachment_store import FileSystemStore, build_store
from mailbox_viewer.config import ConfigError, Settings, load_settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.sync import STATUS_OK, STATUS_PARTIAL, SyncResult, run_sync

st.set_page_config(page_title="Mailbox Viewer", page_icon="📬", layout="wide")

DOC_COLORS = {"statement": "green", "invoice": "violet", "receipt": "blue", "other": "gray"}


@st.cache_resource(show_spinner=False)
def get_settings() -> Settings:
    return load_settings()


@st.cache_resource(show_spinner=False)
def get_session_factory():
    settings = get_settings()
    engine = make_engine(settings.database_url, settings.db_schema)
    init_db(engine, settings.db_schema)
    return make_session_factory(engine)


@st.cache_resource(show_spinner=False)
def get_store() -> FileSystemStore:
    return build_store(get_settings())


# -- page ------------------------------------------------------------------------------


def main() -> None:
    try:
        settings = get_settings()
        factory = get_session_factory()
        store = get_store()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()

    payment_only, search = render_sidebar(settings, factory, store)

    st.title("📬 Mailbox Viewer")

    with factory() as session:
        total_threads = repo.count_threads(session)
        threads = repo.list_threads(session, payment_only=payment_only, search=search or None)

    if total_threads == 0:
        with factory() as session:
            judged = repo.count_decisions(session)
        if judged:
            st.info(
                f"{judged} message(s) were judged and none was a payment document, so nothing is stored. "
                "Send yourself a PDF named like `statement_sep.pdf` and Refresh, or set "
                "`STORE_ONLY_PAYMENT=false` in `.env` to keep every message."
            )
        else:
            st.info("No cached mail yet. Click **Refresh** in the sidebar to run the first sync.")
        return

    thread_id = render_thread_list(threads, total_threads)
    if thread_id is not None:
        render_thread(factory, store, thread_id)


def render_sidebar(settings: Settings, factory, store: FileSystemStore) -> tuple[bool, str]:
    with st.sidebar:
        st.header("Mailbox")
        st.caption(f"{settings.imap_user} · " + ", ".join(settings.imap_folders))
        st.caption(f"Attachments → {store.describe()}")
        st.caption("Keeping: " + ("payment documents and their threads" if settings.store_only_payment else "every message"))

        if st.button("🔄 Refresh", type="primary", width="stretch", help="Pull only mail newer than the last sync"):
            with st.spinner("Syncing with the mail server…", show_time=True):
                result = run_sync(settings, factory, store=store)
            st.session_state["last_sync_result"] = result

        result: SyncResult | None = st.session_state.get("last_sync_result")
        if result is not None:
            _show_sync_result(result)

        if st.button("🏷️ Re-run classification rules", width="stretch", help="Re-label cached mail with the current rules; no mail-server contact."):
            with st.spinner("Re-applying rules…"):
                with factory() as session, session.begin():
                    changed = repo.reclassify_all(session)
            st.info(f"Rules re-applied: {changed} email(s) changed.")

        with factory() as session:
            states = {f: repo.get_sync_state(session, f) for f in settings.imap_folders}
            n_threads = repo.count_threads(session)
            n_emails = repo.count_emails(session)
            n_files = repo.count_attachments(session)
            n_payment = repo.count_payment_threads(session)
            n_judged = repo.count_decisions(session)
            by_doc = repo.count_by_doc_type(session)

        st.divider()
        st.subheader("Cache")
        a, b, c, d = st.columns(4)
        a.metric("Threads", n_threads)
        b.metric("Emails", n_emails)
        c.metric("Files", n_files)
        d.metric("Payment", n_payment, help="threads with at least one payment document")
        st.caption(f"Judged: {n_judged} message(s) in decision_log, {n_judged - n_emails} dropped")
        if by_doc:
            st.caption("Documents: " + ", ".join(f"{k} {v}" for k, v in sorted(by_doc.items())))
        for folder, state in states.items():
            if state is not None and state.last_sync_at is not None:
                st.caption(f"{folder}: last sync {_fmt_local(state.last_sync_at)} · resume after UID {state.last_seen_uid}")
            else:
                st.caption(f"{folder}: never synced.")

        st.divider()
        st.subheader("Filter")
        payment_only = st.toggle("Payment threads only", value=False)
        search = st.text_input("Search subject or participant", placeholder="e.g. bank")

    return payment_only, search


def render_thread_list(threads: list[repo.ThreadSummary], total: int) -> int | None:
    st.caption(f"Showing {len(threads)} of {total} conversations. Read from the local cache, never from the mail server.")
    if not threads:
        st.warning("No conversations match the current filter.")
        return None

    table = pd.DataFrame(
        {
            "id": [t.id for t in threads],
            "Last message": [_to_local_naive(t.last_message_at) for t in threads],
            "From": [t.participants for t in threads],
            "Subject": [t.subject or "(no subject)" for t in threads],
            "Msgs": [t.message_count for t in threads],
            "Files": [t.attachment_count for t in threads],
            "Payment": [t.has_payment for t in threads],
        }
    )
    event = st.dataframe(
        table.drop(columns=["id"]),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Last message": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm", width="small"),
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


def render_thread(factory, store: FileSystemStore, thread_id: int) -> None:
    with factory() as session:
        emails = repo.list_thread_emails(session, thread_id)
    if not emails:
        st.warning("That conversation is no longer in the cache.")
        return

    st.divider()
    st.subheader(emails[0].subject or "(no subject)")
    st.caption(f"{len(emails)} message(s) in this conversation, newest first.")

    for summary in emails:
        with factory() as session:
            detail = repo.get_email_detail(session, summary.id)
            files = repo.get_attachment_files(session, summary.id, store) if detail and detail.attachments else []
            decision = repo.get_decision(session, summary.message_id)
        if detail is None:
            continue
        render_message(detail, files, decision, expanded=summary is emails[0])


def render_message(detail: repo.EmailDetail, files: list[repo.AttachmentFile], decision: repo.DecisionInfo | None, *, expanded: bool) -> None:
    sender = detail.by_role("from")
    head = f"{_fmt_local(detail.received_at)} · {sender[0].display if sender else '(unknown sender)'}"
    if detail.doc_type:
        head += f" · {detail.doc_type}"
    with st.expander(head, expanded=expanded):
        if detail.doc_type:
            badge_col, reason_col = st.columns([1, 5])
            badge_col.badge(detail.doc_type, color=DOC_COLORS.get(detail.doc_type, "gray"))
            if detail.confidence is not None:
                reason_col.caption(f"confidence {detail.confidence:.2f} · {detail.decision_reason}")
        elif detail.decision_reason:
            st.caption(detail.decision_reason)

        for role, label in (("from", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
            people = detail.by_role(role)
            if people:
                st.text(f"{label}: " + ", ".join(p.display for p in people))

        tab_text, tab_files, tab_decision = st.tabs(["Text", f"Attachments ({len(detail.attachments)})", "Decision"])
        with tab_text:
            st.text(detail.body_text) if detail.body_text.strip() else st.info("This email has no plain-text body.")
        with tab_files:
            if not files:
                st.info("No attachments.")
            for file in files:
                c1, c2, c3 = st.columns([5, 3, 2])
                icon = "📄" if file.info.doc_type in ("statement", "invoice", "receipt") else "📎"
                c1.markdown(f"{icon} **{_escape_md(file.info.filename)}**")
                if file.info.doc_type:
                    c1.caption(f"{file.info.doc_type} · confidence {file.info.confidence:.2f}")
                c2.caption(f"{file.info.content_type} · {_human_size(file.info.size_bytes)}  \nblob_key {file.info.blob_key[:12]}…/{file.info.filename}")
                if file.content is None:
                    c3.error(file.error or "unavailable")
                else:
                    c3.download_button("Download", data=file.content, file_name=file.info.filename, mime=file.info.content_type, key=f"download-{file.info.id}", width="stretch")
        with tab_decision:
            if decision is None:
                st.info("No decision logged.")
            else:
                st.code(
                    f"message_id : {decision.message_id}\n"
                    f"tier       : {decision.tier}  (1 filename, 2 subject, 3 body, 0 none)\n"
                    f"decision   : {decision.decision}\n"
                    f"confidence : {decision.confidence if decision.confidence is not None else '-'}\n"
                    f"reason     : {decision.reason}\n"
                    f"decided_at : {_fmt_local(decision.decided_at)}\n"
                    f"in_reply_to: {detail.in_reply_to or '-'}\n"
                    f"references : {detail.references_header or '-'}",
                    language=None,
                )


# -- helpers -----------------------------------------------------------------------------


def _show_sync_result(result: SyncResult) -> None:
    if result.status == STATUS_OK:
        st.success(f"Synced in {result.duration_seconds}s: {result.summary()}")
    elif result.status == STATUS_PARTIAL:
        st.warning(f"Sync interrupted: {result.summary()}")
    else:
        st.error(f"Sync failed: {result.summary()}")


def _to_local_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _fmt_local(value: datetime | None) -> str:
    local = _to_local_naive(value)
    return local.strftime("%d %b %Y, %H:%M") if local else "unknown"


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
