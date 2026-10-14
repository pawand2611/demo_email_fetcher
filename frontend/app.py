"""Streamlit frontend: overview, inbox, tickets, decision log, the decision rules and the data model.

It talks only to the backend API (``API_URL``, default http://127.0.0.1:8000);
it never opens the database or the mailbox itself.

Run with (backend first, each from its own folder):
    cd backend;  python -m uvicorn api.main:create_app --factory --host 127.0.0.1 --port 8000
    cd frontend; python -m streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from ui import get_api, render_sidebar

st.set_page_config(page_title="Statement Recon · Mailbox Viewer", page_icon="📬", layout="wide")

pages = {
    "Mailbox": [
        st.Page("views/overview.py", title="Overview", icon="🏠", default=True),
        st.Page("views/inbox.py", title="Inbox", icon="📬"),
        st.Page("views/tickets.py", title="Tickets", icon="🎫"),
        st.Page("views/decisions.py", title="Decision log", icon="🧾"),
    ],
    "How it works": [
        st.Page("views/how_it_decides.py", title="How it decides", icon="🧠"),
        st.Page("views/data_model.py", title="Data model", icon="🗂️"),
        st.Page("views/tables.py", title="Tables", icon="📋"),
    ],
}

page = st.navigation(pages)
render_sidebar(get_api())
page.run()
