# Demo code walkthrough: how the frontend shows the data

Use this while presenting the code. It follows **one click in the browser**
all the way to the database and back, with the real code at each step, and
says which file to open at each moment of the demo.

For the full reference of every file and function, see `CODE_GUIDE.md`.

---

## 1. The flow in one picture

```
 You click a conversation in the Inbox
            │
            ▼
 frontend/app.py                 st.navigation → runs views/inbox.py
            │
            ▼
 frontend/views/inbox.py         api.thread(thread_id)
            │
            ▼
 frontend/ui.py  CachedApi       in the 60-second cache?  ── yes ──▶ answer at once
            │ no
            ▼
 frontend/api_client.py          HTTP GET http://127.0.0.1:8000/threads/{id}
            │
            ▼
 backend/api/main.py             route get_thread()
            │
            ▼
 backend/mailbox_viewer/
   repository.py                 get_thread_messages(): 4 SQL queries
            │
            ▼
 PostgreSQL on AWS (schema poc)  threads · emails · participants · attachments · decision_log
            │
            ▼  rows → Python objects → JSON (api/schemas.py, times in IST)
            ▼
 frontend/views/inbox.py         render_message(): badges, tabs, download button
```

Three ideas to explain while showing it:
1. **The frontend never touches the database.** It only calls the backend's
   HTTP API. That's why another frontend could reuse the same backend.
2. **Streamlit reruns the whole page on every click.** Without a cache,
   every click would go back to the database in AWS. Hence `CachedApi`.
3. **The backend shapes the data** (`api/schemas.py`), so the page receives
   clean JSON with times already in IST.

---

## 2. Step by step, with the code

### Step 1: the app and its pages (`frontend/app.py`)

The entry point. It builds the left-hand menu, draws the sidebar and runs the
chosen page.

```python
pages = {
    "Mailbox": [
        st.Page("views/overview.py", title="Overview", icon="🏠", default=True),
        st.Page("views/inbox.py", title="Inbox", icon="📬"),
        st.Page("views/tickets.py", title="Tickets", icon="🎫"),
        st.Page("views/decisions.py", title="Decision log", icon="🧾"),
    ],
    "How it works": [ ... how it decides, data model, tables ... ],
}

page = st.navigation(pages)
render_sidebar(get_api())
page.run()
```

> Say: "Each page is its own file in `views/`. `app.py` only wires them together."

### Step 2: one API object for every page, with a cache (`frontend/ui.py`)

Every page starts with `api = get_api()`. That returns a `CachedApi`, a thin
wrapper around the HTTP client:

```python
CACHE_SECONDS = 60
_READS  = {"health", "profiles", "stats", "threads", "thread", "tickets", "ticket", ...}
_WRITES = {"start_sync", "reclassify", "redecide", "rebuild_tickets", "close_ticket", "reopen_ticket"}

@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def _cached_call(name: str, args: tuple, kwargs: tuple):
    return getattr(_client(), name)(*args, **dict(kwargs))

class CachedApi:
    def __getattr__(self, name: str):
        target = getattr(self._client, name)
        if name in _READS:                      # reads: served from the cache
            return lambda *args, **kwargs: _cached_call(name, args, tuple(sorted(kwargs.items())))
        if name in _WRITES:                     # writes: run, then clear the cache
            def write(*args, **kwargs):
                try:
                    return target(*args, **kwargs)
                finally:
                    clear_cache()
            return write
        return target                           # job polling, downloads: always live
```

> Say: "Reads are remembered for 60 seconds, so clicking around is instant.
> Anything that changes data, such as Refresh or closing a ticket, clears the
> cache, so you never see stale data after your own change."

### Step 3: the HTTP call (`frontend/api_client.py`)

One method per backend endpoint; all of them go through `_request`:

```python
def thread(self, thread_id: int) -> dict[str, Any]:
    return self._json("GET", f"/threads/{thread_id}")

def _request(self, method, path, params=None, body=None) -> httpx.Response:
    try:
        response = self._http.request(method, path, params=params, json=body)
    except httpx.TransportError as exc:
        raise ApiError(f"Backend not reachable at {self.base_url} ... Start it with: {START_HINT}") from exc
    if response.status_code >= 400:
        ...
        raise ApiError(f"{method} {path} failed ({response.status_code}): {detail}", response.status_code)
    return response
```

> Say: "If the backend is down, the page shows a clear message with the
> command to start it, instead of crashing."

### Step 4: the backend answers (`backend/api/main.py` and `repository.py`)

The route the Inbox calls when you open a conversation:

```python
@app.get("/threads/{thread_id}", response_model=s.ThreadDetailOut, tags=["mail"])
def get_thread(thread_id: int) -> s.ThreadDetailOut:
    with svc.session_factory() as session:
        thread = repo.get_thread(session, thread_id)
        if thread is None:
            raise HTTPException(404, f"no thread {thread_id}")
        messages = [_message_out(detail, decision) for detail, decision in repo.get_thread_messages(session, thread_id)]
    return s.ThreadDetailOut(**_thread_out(thread).model_dump(), messages=messages)
```

And the query behind it: the whole conversation in **four queries**, however
long it is:

```python
def get_thread_messages(session, thread_id):
    rows = session.scalars(
        select(Email)
        .options(selectinload(Email.participants), selectinload(Email.attachments))
        .where(Email.thread_id == thread_id)
        .order_by(Email.received_at.desc().nulls_last(), Email.id.desc())
    ).all()
    ids = [r.message_id for r in rows]
    logged = session.scalars(select(DecisionLog).where(DecisionLog.message_id.in_(ids))).all() if ids else []
    decisions = {d.message_id: _decision_info(d) for d in logged}
    return [(_email_detail(r), decisions.get(r.message_id)) for r in rows]
```

> Say: "The database is in AWS Virginia, so every query is a network round
> trip of about half a second. Loading a conversation used to take about 20
> queries; now it takes 4."

### Step 5: the Inbox page draws it (`frontend/views/inbox.py`)

**a) The conversation list.** One call, turned into a table you can click:

```python
threads = api.threads(payment_only=payment_only, search=search or None)

table = pd.DataFrame({
    "id": [t["id"] for t in threads],
    "Last message": [parse_time(t["last_message_at"]) for t in threads],
    "Subject": [t["subject"] or "(no subject)" for t in threads],
    "Payment": [t["has_payment"] for t in threads],
    ...
})
event = st.dataframe(table.drop(columns=["id"]), on_select="rerun", selection_mode="single-row",
                     key="thread_table", ...)
```

**b) Which conversation is open.** Either the row you clicked, or one opened
with **Open** on the Overview page (remembered in `st.session_state`):

```python
rows = event.selection.rows
if rows:
    thread_id = int(table.iloc[rows[0]]["id"])
    st.session_state["open_thread"] = thread_id
elif st.session_state.get("open_thread") in set(table["id"]):   # opened from the Overview
    thread_id = int(st.session_state["open_thread"])
else:
    st.caption("Select a conversation to open it.")
    st.stop()

thread = api.thread(thread_id)
```

**c) The ticket badge** next to the subject:

```python
ticket = next((t for t in api.tickets() if t["thread_id"] == thread["id"]), None)
if ticket:
    label, color = TICKET_STATUS[ticket["status"]]
    ticket_badge.badge(f"Ticket: {label.split(' ', 1)[1]}", color=color)
```

**d) Each email.** `render_message()` draws one expander per email: three
badges, then three tabs.

```python
def render_message(m: dict, expanded: bool) -> None:
    d = m["decision"] or {}
    check = which_check(d.get("tier"), d.get("reason"))       # which of the 3 checks decided
    with st.expander(head, expanded=expanded):
        badges = st.columns([1, 1, 1, 5])
        badges[0].badge(d["decision"], color=DECISION_COLORS[...])   # payment / review / none
        badges[1].badge(m["doc_type"], color=DOC_COLORS[...])        # invoice, receipt, ...
        badges[2].badge(*CHECK_BADGES[check])                         # inherited / attachment / email text

        tab_text, tab_files, tab_decision = st.tabs(["Text", f"Attachments ({len(m['attachments'])})", "Decision"])
        with tab_files:
            for att in m["attachments"]:
                # fetched only when Download is clicked, not on every page load
                c3.download_button("Download", data=lambda aid=att["id"]: api.attachment_bytes(aid), ...)
        with tab_decision:
            st.code(f"decision   : {d['decision']}\n" f"decided by : {CHECKS[check]}\n" ...)
```

> Say: "Every email shows *why* it was kept: the decision, the document type,
> which check decided it, and the full reason in the Decision tab."

### Step 6: which check decided (`ui.which_check`)

A small function that reads the logged decision and tells the UI which of the
three checks made it:

```python
def which_check(tier, reason):
    if "already classified as payment" in (reason or ""):
        return 1          # Check 1: the thread was already payment (inherited)
    if tier == 1:
        return 2          # Check 2: the attachment model decided
    if tier in (2, 3):
        return 3          # Check 3: Laya read the email text
    return None
```

### Step 7: closing a ticket by hand (`frontend/views/tickets.py`)

The one place the user changes data from a page. Follow it end to end:

```python
reason = st.text_input("Reason (optional)", key=f"close-reason-{ticket_id}")
if st.button("✅ Close ticket", key=f"close-{ticket_id}", type="primary"):
    api.close_ticket(ticket_id, reason)    # a WRITE: POST /tickets/{id}/close, then the cache is cleared
    st.rerun()                             # redraw with fresh data
```

On the backend, `POST /tickets/{id}/close` calls `tickets.close_manually()`,
which sets the status to closed, records who closed it and why, and adds an
entry to `ticket_events`. The page then reruns, and because the cache was
cleared, it shows the ticket as closed straight away, with the new line in
its history.

### Step 8: the Refresh button (`ui.render_sidebar` and `ui.run_job`)

```python
if st.button("🔄 Refresh mail", type="primary"):
    run_job(api, "Syncing and classifying new mail…", api.start_sync)

def run_job(api, label, start):
    job = start()                                  # POST /sync → returns a job id at once
    with st.spinner(label, show_time=True):
        while job["state"] == "running":
            time.sleep(1)
            job = api.job(job["id"])               # GET /jobs/{id}, never cached
    st.session_state["last_job"] = job
    ...
    clear_cache()                                  # the sync changed the data
```

> Say: "The sync runs in the background on the backend; the page just polls
> once a second and shows a spinner, then the result."

---

## 3. Which file to open at each moment of the demo

| When you show… | Open this file | Point at |
|---|---|---|
| The menu and pages | `frontend/app.py` | `st.navigation(pages)` |
| Why clicking is fast | `frontend/ui.py` | `CachedApi`, `CACHE_SECONDS = 60` |
| How the page talks to the backend | `frontend/api_client.py` | `thread()`, `_request()` |
| Opening a conversation | `frontend/views/inbox.py` | `event.selection.rows` → `api.thread()` → `render_message()` |
| The badges on each email | `frontend/ui.py` | `which_check()`, `CHECK_BADGES` |
| The backend side of the same click | `backend/api/main.py` | `get_thread()` |
| The SQL behind it | `backend/mailbox_viewer/repository.py` | `get_thread_messages()` |
| Closing a ticket | `frontend/views/tickets.py` | the **Close ticket** button → `api.close_ticket()` |
| The ticket rules | `backend/mailbox_viewer/tickets.py` | `close_manually()`, `apply()` |
| The Refresh button | `frontend/ui.py` | `render_sidebar()`, `run_job()` |
| The live API | browser: `http://127.0.0.1:8000/docs` | try `GET /threads/{id}` |

**Tip:** keep the Swagger page (`/docs`) open in a second tab. Calling
`GET /threads/{id}` there shows exactly the JSON that `render_message()`
draws from.

---

## 4. Frontend functions at a glance

| File | Function / object | What it does for the display |
|---|---|---|
| `app.py` | `st.navigation(pages)` | The menu; runs the chosen page |
| `ui.py` | `get_api()` · `CachedApi` | Every page's API access, with the 60-second cache |
| `ui.py` | `_cached_call()` · `clear_cache()` | The cache itself, and emptying it after a change |
| `ui.py` | `render_sidebar()` | Health dots, Refresh mail, Reload data, Maintenance |
| `ui.py` | `run_job()` · `show_job()` | Start a backend job, show a spinner, show the result |
| `ui.py` | `which_check()` | Which of the three checks decided an email |
| `ui.py` | `fmt()` · `person()` · `human_size()` | Dates in IST, "Name <address>", file sizes |
| `api_client.py` | `ApiClient.threads()` · `thread()` · `tickets()` · `stats()` … | One method per endpoint |
| `api_client.py` | `_request()` · `ApiError` | The HTTP call and readable errors |
| `api_client.py` | `parse_time()` | API timestamps to IST for tables |
| `views/overview.py` | page script | Metrics, charts, latest payment conversations with **Open** |
| `views/inbox.py` | page script · `render_message()` | Conversation table; one email with badges, tabs and download |
| `views/tickets.py` | page script | Ticket counts, list, history, **Close** / **Reopen** |
| `views/decisions.py` | page script | Every judged email, kept or not, with filters |
| `views/how_it_decides.py` | page script | The three checks as a live diagram |
| `views/data_model.py` · `views/tables.py` | `main()` | The tables, their columns and raw rows |

---

## 5. Questions you may get about the code

| Question | Answer |
|---|---|
| Why not query the database from Streamlit directly? | Separation: the backend owns all data, the frontend only displays. Another frontend (the planned Next.js one) can reuse the same API. |
| Why is there a cache? | Streamlit reruns the whole page on every click, and each database query to AWS costs about 0.5 s. The cache makes repeat clicks instant; writes clear it, so data is never stale after your own change. |
| What if the backend is down? | `ApiClient` turns it into an `ApiError` with the start command, and the page shows that message. |
| How are times shown in IST? | The database stores UTC; the backend converts to IST before sending (`timeutil.to_ist`), and `parse_time()` keeps it for tables. |
| How is it tested? | `frontend/tests/test_pages.py` renders every page against a fake API with Streamlit's `AppTest`, including closing and reopening a ticket and the cache. |
