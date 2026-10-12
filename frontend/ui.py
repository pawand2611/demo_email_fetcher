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
TIERS = "0 nothing classified · 1 attachment model · 2 body model · 3 both models"


@st.cache_resource(show_spinner=False)
def get_api() -> ApiClient:
    return ApiClient()


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


def render_sidebar(api: ApiClient) -> None:
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

        job = st.session_state.get("last_job")
        if job is not None:
            show_job(job)


def run_job(api: ApiClient, label: str, start) -> None:
    try:
        job = start()
        with st.spinner(label, show_time=True):
            while job["state"] == "running":
                time.sleep(1)
                job = api.job(job["id"])
        st.session_state["last_job"] = job
    except ApiError as exc:
        st.warning(str(exc))


def show_job(job: dict) -> None:
    if job["state"] == "failed":
        st.error(f"{job['kind'].capitalize()} failed: {job['error']}")
        return
    result = job.get("result") or {}
    if job["kind"] == "sync":
        status = result.get("status")
        text = f"Synced in {result.get('duration_seconds', 0)}s: {result.get('summary', '')}"
        (st.success if status == "ok" else st.warning if status == "partial" else st.error)(text)
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
