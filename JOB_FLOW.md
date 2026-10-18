# Job flow: what runs when you press a button

This file follows the code from a click on **🔄 Refresh mail** to the page
redrawing, function by function, with the file for each step. The other three
buttons (**⚖️ Re-apply rules**, **🧠 Re-run models**, **🎫 Rebuild tickets**)
follow the same pattern, so only where they differ is shown.

Related guides: `CODE_GUIDE.md` (every file and function), `API_REFERENCE.md`
(every endpoint), `DEMO_CODE_WALKTHROUGH.md` (how the pages show data).

---

## 1. Refresh: the whole journey

Three phases:
- **A** (frontend): the click sends the request.
- **B** (backend): the sync runs in the background.
- **C** (frontend): the page waits for it to finish, then redraws.

### Phase A: the click (frontend)

```
frontend/app.py
  render_sidebar(get_api())                         ← every page run draws the sidebar first
   │
   └─ frontend/ui.py · render_sidebar()
        st.button("🔄 Refresh mail")  → True on the click
        └─ run_job(api, "Syncing and classifying new mail…", api.start_sync)
             │
             └─ api.start_sync()
                  frontend/ui.py · CachedApi.__getattr__("start_sync")
                     "start_sync" is a WRITE → runs it, then clear_cache()
                  └─ frontend/api_client.py · ApiClient.start_sync()
                       └─ _json("POST", "/sync") → _request() → httpx POST http://127.0.0.1:8000/sync
```

### Phase B: the backend runs the sync (in the background)

**B1. Accept the request and start a job** (answers at once with `202`):
```
backend/api/main.py · start_sync()                   ← route POST /sync
  └─ start_job(KIND_SYNC, run_sync_job)
       └─ backend/api/jobs.py · Jobs.start(kind, run)
            creates Job(id, state="running"), starts threading.Thread(target=_execute)
            returns at once → the route replies 202 + the job (id)
```

**B2. The background thread does the work:**
```
backend/api/jobs.py · Jobs._execute(job, run)
  └─ run() = backend/api/main.py · run_sync_job()
       └─ backend/mailbox_viewer/sync.py · run_sync(settings, session_factory, client_factory, store, classifier)
            for each folder in IMAP_FOLDERS (INBOX, then Sent Mail):
            └─ _sync_folder(...)
```

**B3. One folder** (`sync.py · _sync_folder`):
```
_sync_folder
 ├─ repository.py · get_sync_state(session, folder)          read the bookmark (last_seen_uid)
 ├─ mail_client.py · MailClient(settings, folder)  → __enter__ → connect()
 │                                                          log in to Gmail, open folder read-only
 ├─ sync.py · _resume_point(state, uidvalidity)               where to continue from
 ├─ mail_client.py · search_uids(after_uid)                   UIDs newer than the bookmark
 │                                                          (logged as "N candidate(s)")
 ├─ sync.py · _Processor(...)                                 one helper for this folder
 ├─ for each uid:  _Processor.process(uid)                    ← B4, once per new email
 ├─ MailClient.__exit__ → close()                             log out
 └─ repository.py · save_sync_state(...)                      move the bookmark (always last)
```

**B4. One email** (`sync.py · _Processor.process`). This only runs when there's new mail:
```
_Processor.process(uid)
 ├─ mail_client.py · fetch_message(uid)                       raw bytes (BODY.PEEK: stays unread)
 ├─ mail_parser.py · parse_message(uid, raw, date)            → ParsedEmail
 └─ _judge_and_store(mail)
     ├─ repository.py · is_judged() / find_email_id()         seen before? → skip
     ├─ repository.py · find_thread_id(mail)                  which conversation
     │     └─ threads.py · conversation_key(), ancestor_ids()
     ├─ repository.py · thread_has_payment(thread_id)         payment thread already?
     ├─ tickets.py · active_ticket(thread_id)                 open ticket?
     │
     ├─ CLASSIFY  (classification_graph.py · EmailClassifier)
     │    in a payment thread → inherit(email)
     │         └─ classifier.py · classify_attachments()  →  inherited_decision()
     │    otherwise           → classify(email) = LangGraph graph.invoke()
     │         ├─ _attachments_node → classifier.py · classify_attachments()
     │         │       └─ document_model.py · LayoutLMv3InvoiceModel.predict()
     │         │             └─ attachment_classifier/inference/predict.py · AttachmentClassifier.predict()
     │         │                   └─ extraction/extract.py · extract_page()   (pdfplumber / Tesseract OCR)
     │         ├─ _route_after_attachments → classifier.py · attachment_decides()   (> 0.80?)
     │         ├─ _body_node (only if not decided) → classifier.py · classify_body()
     │         │       └─ document_model.py · LayaBodyModel.predict()
     │         └─ _combine_node → classifier.py · combine()           ← the decision rules
     │       (every model call runs on one thread: EmailClassifier._on_model_thread)
     │
     ├─ keep?  payment / review / payment thread / keep-all setting
     │    NO  → repository.py · log_decision()                   only the audit line → "dropped"
     │    YES ↓
     ├─ if ticket active: classification_graph.py · payment_confirmation()
     │       └─ document_model.py · LayaBodyModel.confirms_payment()     yes/no score
     ├─ _store(mail, decision, confirmation, update_ticket=True)
     │    ├─ attachment_store.py · FileSystemStore.put()            files to disk first
     │    └─ one DB transaction:
     │         ├─ repository.py · insert_email()                    thread, email, people, attachments, decision log
     │         │     └─ _thread_for(), _log_decision()
     │         └─ sync.py · _update_ticket() → tickets.py · apply()  open / in progress / close / reopen
     └─ if it just became a payment thread: _backfill_ancestors()
          └─ mail_client.find_uid_by_message_id() → fetch → parse → _classify() → _store()
```

When `run_sync` returns its `SyncResult`, `Jobs._execute` sets
`state="succeeded"` (or `failed`, with the error) and records `finished_at`.

### Phase C: the page waits, then redraws (frontend)

```
frontend/ui.py · run_job()   (still inside the click, under a spinner)
  loop every 1 s while job["state"] == "running":
     api.job(id)                     ← NOT cached ("always live")
       └─ api_client.py · ApiClient.job() → GET /jobs/{id}
            └─ backend/api/main.py · job_status() → jobs.py · Jobs.get() → _job_out() (+ _result_out())
  st.session_state["last_job"] = job
  clear_cache()                       ← data changed, forget cached reads
  ...
  ui.py · show_job(job)               green "Synced in 10s: …" message

frontend/app.py → page.run()          the current page (e.g. views/inbox.py) now runs:
  api.stats()          → GET /stats              (cache was cleared)
  api.threads(...)     → GET /threads?…
  api.thread(259)      → GET /threads/259
  api.tickets()        → GET /tickets            (ticket badge)
```

So the backend log for one Refresh reads:

```
POST /sync                         ← Phase A
GET  /jobs/<id>   × ~10            ← Phase C, waiting (one per second)
sync INBOX: 0 candidate(s), …      ← Phase B, in the backend
sync [Gmail]/Sent Mail: …
sync finished: …
GET /stats                         ← Phase C, redraw
GET /threads?payment_only=true&limit=1000
GET /threads/259
GET /tickets
```

Even with no new mail a sync takes about 10 s: each folder needs a secure
login to Gmail, plus a read and a write of its bookmark in the AWS database.

---

## 2. The other three jobs

Each one follows the same Phase A → B1 → C path. Only the button, the route
and the work function in B2 differ.

### ⚖️ Re-apply rules
```
ui.render_sidebar → run_job(api.redecide) → ApiClient.redecide() → POST /redecide
backend/api/main.py · redecide() → start_job(KIND_REDECIDE, run_redecide_job)
  run_redecide_job()
   └─ repository.py · redecide_all(session, classifier)
        └─ _replay(...)                     every stored email, oldest first
             for each email:
               classifier.py · body_decision_from_reason()   Laya's stored label + score, read from the reason
               + the stored attachment labels
               └─ classification_graph.py · EmailClassifier.combine()  → classifier.py · combine()
               write only if the decision changed; thread rule applied via the inherit callback
             _recompute_thread_flags()       update threads.has_payment etc.
```
No models and no Gmail, so it takes about a minute.

### 🧠 Re-run models
```
ui.render_sidebar → run_job(api.reclassify(only_with_attachments=…)) → POST /reclassify?only_with_attachments=…
backend/api/main.py · reclassify() → start_job(KIND_RECLASSIFY, run_reclassify_job)
  run_reclassify_job()
   └─ repository.py · reclassify_all(session, store, classifier, only_with_attachments)
        └─ _replay(...)                     oldest first
             for each email: rebuild its input from the DB text + attachment files (store.get)
               └─ EmailClassifier.classify()  or .inherit()   ← the same LangGraph flow as in sync
             write only what changed
```
This uses the models but not Gmail.

### 🎫 Rebuild tickets
```
ui.render_sidebar → run_job(api.rebuild_tickets) → POST /tickets/rebuild
backend/api/main.py · rebuild_tickets() → start_job(KIND_TICKETS, run_tickets_job)
  run_tickets_job()
   └─ tickets.py · rebuild(session, classifier.payment_confirmation, mailbox, close_at)
        delete all tickets + events
        for each payment thread → its emails oldest first:
            build MailFacts (payment? new invoice? from us? Laya confirmation if a ticket is active)
            └─ tickets.py · apply()        open / in progress / close / reopen
```

---

## 3. Cheat sheet

| Button | Route | Work function | Core function |
|---|---|---|---|
| 🔄 Refresh mail | `POST /sync` | `run_sync_job` | `sync.run_sync` → `_sync_folder` → `_Processor.process` |
| ⚖️ Re-apply rules | `POST /redecide` | `run_redecide_job` | `repository.redecide_all` → `_replay` → `classifier.combine` |
| 🧠 Re-run models | `POST /reclassify` | `run_reclassify_job` | `repository.reclassify_all` → `_replay` → `EmailClassifier.classify` |
| 🎫 Rebuild tickets | `POST /tickets/rebuild` | `run_tickets_job` | `tickets.rebuild` → `tickets.apply` |
| (all of them) | then `GET /jobs/{id}` | `job_status` | `Jobs.get` |

**Good places to set a breakpoint, or open the file, to follow it live:**
- `backend/mailbox_viewer/sync.py · _judge_and_store`: where every decision
  about an email is made.
- `backend/mailbox_viewer/classifier.py · combine`: the decision rules.
- `backend/mailbox_viewer/tickets.py · apply`: the ticket rules.
- `backend/api/jobs.py · Jobs._execute`: where every background job runs and
  where a crash is caught and recorded.
