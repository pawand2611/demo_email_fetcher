"""Shared pieces for every page: the API client, the sidebar, badges and formatting."""

from __future__ import annotations

import time
from datetime import datetime

import streamlit as st

from api_client import ApiClient, ApiError, parse_time

DOC_COLORS = {"statement": "green", "invoice": "violet", "receipt": "blue", "other": "gray",
              "purchase_order": "orange", "quotation": "orange", "not_invoice": "gray"}
DECISION_COLORS = {"payment": "green", "review": "orange", "none": "gray"}
DECISION_ICONS = {"payment": "🟢", "review": "🟠", "none": "⚪"}
PAYMENT_TYPES = ("statement", "invoice", "receipt")

CHECKS = {
    1: "Check 1 · thread",
    2: "Check 2 · attachment",
    3: "Check 3 · email text",
    None: "Not classified",
}
CHECK_BADGES = {1: ("inherited", "blue"), 2: ("attachment", "violet"), 3: ("email text", "blue"),
                None: ("unclassified", "gray")}
TICKET_STATUS = {"open": ("🔴 Open", "red"), "in_progress": ("🟠 In progress", "orange"), "closed": ("🟢 Closed", "green")}
TICKET_EVENTS = {"opened": "Opened", "in_progress": "In progress", "closed": "Closed", "reopened": "Reopened"}
TIERS = "0 nothing classified · 1 attachment model · 2 body model · 3 both models"

INTENT_LABELS = {
    "new_invoice": "New invoice", "payment_reminder": "Payment reminder", "payment_confirmation": "Payment confirmation",
    "credit_note": "Credit note", "dispute_query": "Dispute / query", "partial_payment": "Partial payment",
    "cancellation": "Cancellation", "other": "Other",
}
INTENT_COLORS = {"new_invoice": "violet", "payment_reminder": "orange", "payment_confirmation": "green",
                 "credit_note": "blue", "dispute_query": "red", "partial_payment": "orange", "cancellation": "gray",
                 "other": "gray"}
INTENT_DECIDED_BY = {"tier1": "Laya (tier 1)", "tier2": "DeepSeek (tier 2)", "tier1_fallback": "Laya, tier 2 failed"}


# -- API access with a short cache ------------------------------------------------------------
#
# Every Streamlit click reruns the whole page, and the backend's database is
# far away (each query is a network round trip), so read calls are cached for
# CACHE_SECONDS. Anything that changes data clears the cache: Refresh mail,
# the maintenance jobs, closing or reopening a ticket, and the Reload button.

CACHE_SECONDS = 60
_READS = {"health", "profiles", "stats", "threads", "thread", "tickets", "ticket", "table_rows", "tables",
          "schema", "ddl", "latest_sync", "intent_summary"}
_WRITES = {"start_sync", "reclassify", "redecide", "rebuild_tickets", "close_ticket", "reopen_ticket",
           "resolve_intents"}


@st.cache_resource(show_spinner=False)
def _client() -> ApiClient:
    return ApiClient()


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _cached_call(name: str, args: tuple, kwargs: tuple):
    return getattr(_client(), name)(*args, **dict(kwargs))


def clear_cache() -> None:
    _cached_call.clear()


class CachedApi:
    """The API client, with read calls cached and write calls clearing the cache."""

    def __init__(self, client: ApiClient) -> None:
        self._client = client

    def __getattr__(self, name: str):
        target = getattr(self._client, name)
        if name in _READS:
            return lambda *args, **kwargs: _cached_call(name, args, tuple(sorted(kwargs.items())))
        if name in _WRITES:
            def write(*args, **kwargs):
                try:
                    return target(*args, **kwargs)
                finally:
                    clear_cache()
            return write
        return target  # job polling, attachment downloads, base_url: always live


def get_api() -> CachedApi:
    return CachedApi(_client())


def which_check(tier: int | None, reason: str | None) -> int | None:
    """Which of the three checks produced a decision, read from the logged tier and reason."""
    if "already classified as payment" in (reason or ""):
        return 1
    if tier == 1:
        return 2
    if tier in (2, 3):
        return 3
    return None


# -- sidebar ------------------------------------------------------------------------------


def render_sidebar(api: CachedApi) -> None:
    with st.sidebar:
        st.markdown("### 📬 Statement Recon")
        try:
            health = api.health()
            profile = api.profiles()[0]
        except ApiError as exc:
            st.error("Backend not reachable")
            st.caption(str(exc))
            return

        db_ok = health["database"] == "ok"
        models_ok = health["model"] not in ("none", "")
        st.markdown(
            f"{'🟢' if db_ok else '🔴'} Database  \n"
            f"{'🟢' if models_ok else '🟠'} Models: `{health['model']}`  \n"
            f"📁 {', '.join(profile['folders'])}"
        )
        if not db_ok and health.get("database_error"):
            st.caption(health["database_error"])

        st.divider()
        if st.button("🔄 Refresh mail", type="primary", width="stretch",
                     help="Fetch only mail newer than the last sync, then classify it"):
            run_job(api, "Syncing and classifying new mail…", api.start_sync)
        if st.button("♻️ Reload data", width="stretch",
                     help=f"Pages keep data for {CACHE_SECONDS} seconds to stay fast. This fetches it fresh now."):
            clear_cache()
            st.rerun()

        with st.expander("Maintenance"):
            st.caption("Use these after changing a model or a threshold. Neither contacts the mailbox.")
            if st.button("⚖️ Re-apply rules", width="stretch",
                         help="Re-run the decision rules on stored scores. No models; about a minute."):
                run_job(api, "Re-applying the decision rules…", api.redecide)
            only_files = st.toggle("Attachments only", value=True,
                                   help="Re-run only LayoutLMv3 on emails with attachments: about 20 seconds")
            if st.button("🧠 Re-run models", width="stretch",
                         help="Re-run the models over cached mail. A full run takes many minutes."):
                run_job(api, "Re-running the models over cached mail…",
                        lambda: api.reclassify(only_with_attachments=only_files))
            if st.button("🎫 Rebuild tickets", width="stretch",
                         help="Recreate every ticket from the stored payment conversations. "
                              "Manual closes and reopens are not kept."):
                run_job(api, "Rebuilding tickets from stored mail…", api.rebuild_tickets)

        if profile.get("tier2", "off") != "off":
            with st.expander("Intent tier 2"):
                st.caption("Payment emails whose intent Laya was unsure about wait for DeepSeek. "
                           "Each takes about 100 seconds on CPU; other jobs wait meanwhile.")
                retry = st.toggle("Also retry failed", value=False,
                                  help="Retry emails whose tier-2 call timed out or found no server")
                if st.button("🤖 Resolve unsure intents", width="stretch",
                             help="Run DeepSeek on the next batch of emails waiting for intent tier 2"):
                    run_job(api, "DeepSeek is classifying the unsure intents…",
                            lambda: api.resolve_intents(retry_failed=retry))

        job = st.session_state.get("last_job")
        if job is not None:
            show_job(job)


def run_job(api: CachedApi, label: str, start) -> None:
    try:
        job = start()
        with st.spinner(label, show_time=True):
            while job["state"] == "running":
                time.sleep(1)
                job = api.job(job["id"])
        st.session_state["last_job"] = job
    except ApiError as exc:
        st.warning(str(exc))
    finally:
        clear_cache()  # the job changed the data


def show_job(job: dict) -> None:
    if job["state"] == "failed":
        st.error(f"{job['kind'].capitalize()} failed: {job['error']}")
        return
    result = job.get("result") or {}
    if job["kind"] == "sync":
        status = result.get("status")
        text = f"Synced in {result.get('duration_seconds', 0)}s: {result.get('summary', '')}"
        (st.success if status == "ok" else st.warning if status == "partial" else st.error)(text)
    elif job["kind"] == "intents":
        st.success(f"Intent tier 2: {result.get('tier2', 0)} decided by DeepSeek, {result.get('fallback', 0)} kept "
                   f"Laya's answer for review; {result.get('remaining', 0)} still waiting.")
    elif job["kind"] == "tickets":
        st.success(f"Tickets rebuilt: {result.get('opened', 0)} opened, {result.get('in_progress', 0)} in progress, "
                   f"{result.get('closed', 0)} closed.")
    elif "changed" in result:
        st.success(f"{job['kind'].capitalize()} done: {result['changed']} email(s) changed.")


# -- formatting ---------------------------------------------------------------------------


def fmt(value: datetime | str | None) -> str:
    if isinstance(value, str):
        value = parse_time(value)
    return value.strftime("%d %b %Y, %H:%M IST") if value else "unknown"


def person(p: dict) -> str:
    return f"{p['name']} <{p['address']}>" if p["name"] else p["address"]


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def escape_md(text: str) -> str:
    return text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
