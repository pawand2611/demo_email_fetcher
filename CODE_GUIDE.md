# Code guide: how the Mailbox Viewer works

This guide explains the code from the start: what runs first, how one email
travels through the system, what every file and function is for, and which
libraries are used where. Read it top to bottom the first time; after that,
use the reference sections (5 to 8) to look things up.

Related documents:
- `README.md`: setup, running, API list.
- `backend/SCHEMA.md`: the database tables in detail, the ERD and the
  history of design decisions.

---

## 1. What the app does, in one paragraph

The app connects to a Gmail mailbox, fetches new emails, and decides for each
one whether it is **payment-related** (an invoice, receipt or statement).
It decides with two AI models that run on this laptop: **LayoutLMv3** reads
attachments and **Laya** reads the email text. Payment emails and their
conversations are stored in a **PostgreSQL** database on AWS; every decision
is logged with its reason. Each payment conversation gets an **aging ticket**
(open, in progress, closed). A **Streamlit** web page shows all of it.

---

## 2. The big picture

```
                 ┌────────────────────────── backend/ (FastAPI, port 8000) ──────────────────────────┐
                 │                                                                                    │
 Gmail  ──IMAP──▶│ mail_client ─▶ mail_parser ─▶ sync ─▶ classification_graph ─▶ repository ─▶ tickets │──▶ PostgreSQL (AWS RDS)
 (INBOX,         │   fetch raw     turn bytes    the       LayoutLMv3 + Laya,     save rows   open /   │    8 tables
  Sent Mail)     │   bytes         into fields   loop      decision rules                     close    │
                 │                                                     │                              │──▶ data/attachments/
                 │                                                     ▼                              │    (attachment files)
                 │                                           api/main.py: HTTP endpoints              │
                 └─────────────────────────────────────────────────▲──────────────────────────────────┘
                                                                   │ HTTP / JSON
                 ┌──────────────── frontend/ (Streamlit, port 8501) ┴─────────────────────────────────┐
                 │ app.py ─▶ ui.py (sidebar, cache) ─▶ views/*.py (pages) ─▶ api_client.py              │
                 └────────────────────────────────────────────────────────────────────────────────────┘
```

Two programs run side by side:

| Program | Folder | Started with | Job |
|---|---|---|---|
| **Backend** | `backend/` | `uvicorn main:app` | Talks to Gmail, the models and the database. The only part that touches data. |
| **Frontend** | `frontend/` | `streamlit run app.py` | The web page. It only calls the backend's HTTP API. |

This split means another frontend, or another business process, could use the
same backend without changes.

---

## 3. What happens at start-up

### 3.1 Backend start-up

```
uvicorn main:app
 └─ backend/main.py                      app = create_app()
     └─ api/main.py  create_app()
         └─ build_services()
             ├─ config.load_settings()          read .env → Settings
             ├─ db.make_engine()                connection pool to PostgreSQL (schema "poc")
             ├─ db.init_db()                    create any missing tables (safe every start)
             ├─ attachment_store.build_store()  folder for attachment files
             └─ document_model.load_classifier()
                 ├─ LayaBodyModel(models/laya)                    ~1 min, ~1.5 GB RAM
                 └─ LayoutLMv3InvoiceModel(models/layoutlmv3_invoice)
         └─ registers every HTTP route (/health, /sync, /threads, /tickets, …)
```

Before any of this, importing the package runs `mailbox_viewer/__init__.py`,
which **switches off all telemetry** (Hugging Face, LangSmith) and forces the
models to load **offline** from local folders. Nothing is sent out.

If the database cannot be reached, `init_db` fails and the backend stops with
an `OperationalError`. That is the "connection timeout" or "server closed the
connection" you see when the network blocks AWS.

### 3.2 Frontend start-up

```
streamlit run app.py
 └─ frontend/app.py
     ├─ st.navigation(...)        the seven pages in the left menu
     ├─ ui.render_sidebar()       health dots, Refresh mail, Reload data, Maintenance
     └─ page.run()                runs the chosen file in views/
```

Streamlit **reruns the whole page script on every click**. That is why the
frontend caches backend answers for 60 seconds (`ui.CachedApi`).

---

## 4. The journey of one email (start to finish)

This is the most important section. It follows one email from the moment you
press **🔄 Refresh mail** until it appears in the Inbox.

### Step 1: the click (frontend)
| Where | What |
|---|---|
| `ui.render_sidebar()` | The Refresh button calls `run_job(api, …, api.start_sync)`. |
| `ui.run_job()` | Starts the job, then polls `api.job(id)` every second under a spinner. Clears the cache when done. |
| `api_client.ApiClient.start_sync()` | Sends `POST /sync` to the backend. |

### Step 2: a background job starts (backend)
| Where | What |
|---|---|
| `api/main.py` route `start_sync()` | Calls `start_job(KIND_SYNC, run_sync_job)`. |
| `api/jobs.py` `Jobs.start()` | Runs the work in a background thread and returns a job id at once. Only **one job at a time**: a second click gets the running job back. |
| `run_sync_job()` | Calls `sync.run_sync(...)` with the settings, database, store and models. |

### Step 3: which emails are new? (`sync.py`)
| Function | What |
|---|---|
| `run_sync()` | Loops over the folders in `IMAP_FOLDERS` (INBOX and Sent Mail). |
| `_sync_folder()` | Reads the folder's **bookmark** (`sync_state.last_seen_uid`), then asks Gmail for UIDs above it. |
| `_resume_point()` | Decides where to resume. If Gmail's `UIDVALIDITY` changed, re-walks the folder (de-duplication makes that safe). |
| `mail_client.MailClient` | Opens the folder **read-only** over IMAP/SSL. `search_uids()` lists new UIDs; `fetch_message()` downloads raw bytes with `BODY.PEEK[]`, so the email is never marked as read. |

The first sync of a folder takes the latest `INITIAL_FETCH_LIMIT` emails.
After that, only new ones.

### Step 4: turn bytes into an email (`mail_parser.py`)
`parse_message(uid, raw_bytes)` returns a `ParsedEmail`:
- subject, sender, participants (from / to / cc / bcc)
- `Message-ID`, `In-Reply-To`, `References` (used for threading)
- received time (from the `Date` header, else Gmail's own time), in UTC
- plain-text and HTML bodies
- attachments, each with bytes, size and sha256

Two details worth knowing:
- A missing `Message-ID` is replaced by `generated-<sha256 of the raw email>`, so de-duplication still works.
- **Inline images**: small ones (under 20 KB, logos) are ignored; larger ones (a phone photo of an invoice) count as attachments (`_is_small_inline_image`).

### Step 5: judge the email (`sync._Processor._judge_and_store`)
For each email, in this order:

1. **Already judged?** `repository.is_judged()` checks `decision_log`. If yes, skip: an email is never judged twice.
2. **Which conversation?** `repository.find_thread_id()` uses `threads.conversation_key()` (the root Message-ID of the reply chain) and `threads.ancestor_ids()`.
3. **Payment thread already?** `repository.thread_has_payment()`. **Open ticket?** `tickets.active_ticket()`.
4. **Classify:**
   - In a payment thread: `EmailClassifier.inherit()`. The email is payment by inheritance; only its attachments are labelled.
   - Otherwise: `EmailClassifier.classify()` runs the LangGraph flow (step 6).
5. **Keep or drop?** Kept if payment, review, in a payment thread, or `STORE_ONLY_PAYMENT=false`. Dropped emails leave only their `decision_log` line.

### Step 6: the decision flow (`classification_graph.py` + `classifier.py`)
```
START ─▶ classify_attachments ─┬─ attachment decided (score > 0.80) ─▶ combine ─▶ END
                               └─ otherwise ─▶ classify_body (Laya) ─▶ combine
```
| Function | What |
|---|---|
| `classifier.classify_attachments()` | Runs LayoutLMv3 on every PDF / image / Word attachment → `invoice` or `not_invoice` with a score. |
| `classifier.attachment_decides()` | True when an attachment scored above `ATTACHMENT_DECIDES_CONFIDENCE` (0.80): the attachment settles it, Laya is skipped. |
| `classifier.classify_body()` | Asks Laya "what kind of document does this email send or discuss?" (invoice, receipt, purchase order, quotation, statement, other). |
| `classifier.combine()` | **The decision rules.** Payment / review / none, plus a tier (0 nothing, 1 attachment, 2 body, 3 both) and a reason in plain words. |

The rules in `combine()`:
- Confident invoice attachment (> 0.80) → **payment**. Confident non-invoice → **none**.
- Otherwise Laya: statement / invoice / receipt at **0.70+** → **payment**; 0.50–0.70 → **review**.
- A weak invoice attachment the text doesn't support → **review**.
- Anything else → **none**.

All model calls run on **one dedicated thread** (`EmailClassifier._model_thread`).
Without it PyTorch started hundreds of threads and slowed to a crawl.

### Step 7: save it (`sync._Processor._store`)
1. Attachment files go to disk first: `attachment_store.FileSystemStore.put()` writes `data/attachments/<sha256>/<filename>`. The same file is stored once.
2. One database transaction: `repository.insert_email()` creates or joins the thread, then inserts the email, participants, attachment rows (with only a file reference, `blob_key`) and the `decision_log` line.
3. In the same transaction, `_update_ticket()` → `tickets.apply()` updates the conversation's ticket (step 8).
4. If this email just made the thread a payment thread, `_backfill_ancestors()` fetches the earlier, dropped emails of that conversation from Gmail, so the whole conversation is stored.

### Step 8: the ticket (`tickets.py`)
`tickets.apply(session, facts, close_at)` decides what this email does to the ticket:

| Situation | Result |
|---|---|
| No ticket yet, email is payment on its own content | **Open** (age counts from this email's time) |
| Ticket open, email sent from our own mailbox | **In progress** |
| Ticket active, Laya ≥ 0.80 sure the email **confirms a payment** | **Closed** (`auto`) |
| Email carries a new invoice | never closes; **reopens** a closed ticket |

The confirmation score comes from `EmailClassifier.payment_confirmation()` →
`LayaBodyModel.confirms_payment()`, a yes/no question to Laya. It is asked only
while a ticket is active. Every change is written to `ticket_events`.

### Step 9: move the bookmark
`repository.save_sync_state()` stores the highest UID handled. **The bookmark
is read first and moved last**, so a crash in the middle never skips mail.

### Step 10: show it (frontend)
| Where | What |
|---|---|
| `views/inbox.py` | `api.threads()` → table of conversations. Selecting one calls `api.thread(id)` and draws each email with badges, text, attachments and the decision. |
| `ui.CachedApi` | Serves repeated reads from a 60-second cache; any change clears it. |
| `api/main.py get_thread()` | `repository.get_thread_messages()` loads every email, participant, attachment and decision of the thread in **4 queries**. |
| `api/schemas.py` | Shapes the JSON answer. All times are converted to **IST** (`timeutil.to_ist`). |

---

## 5. Folder map

```
demo_email_fetcher/
├── .env                      credentials and settings (never committed)
├── .env.example              every setting, explained
├── README.md                 setup and usage
├── CODE_GUIDE.md             this file
├── backend/
│   ├── main.py               start here: `uvicorn main:app`
│   ├── api/                  HTTP layer (FastAPI)
│   ├── mailbox_viewer/       the core: mail, models, rules, database, tickets
│   ├── attachment_classifier/ LayoutLMv3 prediction code, copied from Roctotpal's repo
│   ├── models/               model weights (git-ignored)
│   ├── data/                 attachment files and backups (git-ignored)
│   ├── tests/                backend tests
│   ├── check_db.py, create_schema.py, fetch_mail.py, sync_mail.py   command-line tools
│   ├── SCHEMA.md             database design
│   └── requirements.txt
└── frontend/
    ├── app.py                start here: `streamlit run app.py`
    ├── ui.py                 shared sidebar, cache, badges, formatting
    ├── api_client.py         every HTTP call to the backend
    ├── views/                one file per page
    ├── tests/                frontend tests
    └── requirements.txt
```

---

## 6. File-by-file reference: backend

### `backend/main.py`
The short entry point: `app = create_app()`, so the backend starts with
`uvicorn main:app`. Same as `uvicorn api.main:create_app --factory`.

### `backend/api/main.py`: the HTTP endpoints
| Function | Purpose |
|---|---|
| `Services` | Everything the routes need: settings, database engine, session factory, file store, classifier, mail client factory. Tests pass fakes here. |
| `build_services()` | Builds `Services` from `.env` at start-up (section 3.1). |
| `create_app()` | Creates the FastAPI app and registers every route below. |
| `run_sync_job`, `run_reclassify_job`, `run_redecide_job`, `run_tickets_job` | The work behind the four background jobs. |
| `start_job()` | Starts a job; refuses with **409** if a different job is running. |
| `health()` · `GET /health` | Database reachable? Which models are loaded? |
| `profiles()` · `GET /profiles` | Mailbox, folders, keep policy, every threshold. |
| `stats()` · `GET /stats` | Counts, decisions, document types, sync bookmarks, ticket counts. Kept to 5 queries. |
| `start_sync()` · `POST /sync` | Starts a sync job. |
| `reclassify()` · `POST /reclassify` | Re-runs the models over stored mail (`?only_with_attachments=true` for LayoutLMv3 only). |
| `redecide()` · `POST /redecide` | Re-applies the decision rules to stored scores. No models. |
| `rebuild_tickets()` · `POST /tickets/rebuild` | Recreates every ticket from stored mail. |
| `latest_sync()`, `job_status()` · `GET /sync/latest`, `GET /jobs/{id}` | Job state and result. |
| `list_threads()` · `GET /threads` | Conversation list; filters `payment_only`, `search`. |
| `get_thread()` · `GET /threads/{id}` | One conversation with every message. |
| `download_attachment()` · `GET /attachments/{id}` | The file itself. |
| `get_decision()` · `GET /decisions/{message_id}` | One `decision_log` line, also for emails not kept. |
| `list_tickets()`, `get_ticket()` · `GET /tickets`, `GET /tickets/{id}` | Tickets with age, bucket and history. |
| `close_ticket()`, `reopen_ticket()` · `POST /tickets/{id}/close`, `/reopen` | Manual actions, with an optional reason. |
| `schema()`, `schema_ddl()`, `tables()`, `table_rows()` | The data model and raw rows, for the Data model and Tables pages. |
| `_ist`, `_thread_out`, `_ticket_out`, `_message_out`, `_decision_out`, `_job_out`, `_result_out`, `_schema_table` | Turn internal objects into the JSON shapes in `schemas.py`; times to IST. |

### `backend/api/schemas.py`
Pydantic classes that define every JSON answer (`HealthOut`, `StatsOut`,
`ThreadOut`, `MessageOut`, `TicketOut`, `JobOut`, …). FastAPI uses them to
validate output and to build the Swagger page at `/docs`.

### `backend/api/jobs.py`
| Name | Purpose |
|---|---|
| `Job` | One background job: id, kind, state (running / succeeded / failed), times, result, error. |
| `Jobs.start()` | Runs a function in a background thread; only one job at a time. |
| `Jobs.get()`, `Jobs.latest()`, `Jobs.running` | Look up jobs. |
| `KIND_SYNC`, `KIND_RECLASSIFY`, `KIND_REDECIDE`, `KIND_TICKETS` | Job kinds. |

### `backend/mailbox_viewer/__init__.py`
Runs first. Forces Hugging Face **offline** mode and switches off Hugging Face
and LangSmith **telemetry**, before any library is imported.

### `backend/mailbox_viewer/config.py`
| Name | Purpose |
|---|---|
| `Settings` | Every setting as one frozen object: IMAP, database, folders, model folders, thresholds, ticket bar. |
| `load_settings()` | Reads the environment and `.env`; validates numbers and fractions; raises `ConfigError` with a clear message. |
| `_get`, `_get_bool`, `_get_fraction`, `_get_int` | Read and validate one value. |

### `backend/mailbox_viewer/mail_client.py`: talking to Gmail
| Name | Purpose |
|---|---|
| `MailClient` | IMAP session for one folder, opened **read-only**. Used as `with MailClient(...) as c:`. |
| `connect()`, `close()` | Log in with the app password and select the folder; log out. |
| `search_uids(after_uid)` | UIDs in the folder, only those above `after_uid` for incremental sync. |
| `find_uid_by_message_id()` | Finds an email by its Message-ID (used by backfill). |
| `fetch_message(uid)` | Raw bytes and Gmail's own received time, without marking the email read. |
| `MailClientError`, `MailConnectionLost` | Errors; a lost connection stops the run as "partial". |

### `backend/mailbox_viewer/mail_parser.py`: raw bytes → email
| Name | Purpose |
|---|---|
| `ParsedEmail`, `ParsedAttachment`, `Participant` | The parsed result. |
| `parse_message()` | The entry point (section 4, step 4). |
| `_participants`, `_parse_address` | From / To / Cc / Bcc, lower-cased. |
| `_message_ids`, `_first_message_id` | Threading headers. |
| `_parse_date`, `_to_utc` | Dates to UTC. |
| `_generated_message_id` | Substitute id when `Message-ID` is missing. |
| `_extract_bodies`, `_body_part` | Plain-text and HTML bodies, with safe charset handling. |
| `_extract_attachments`, `_looks_like_attachment`, `_is_small_inline_image` | Which parts are attachments; logos skipped, phone photos kept. |
| `_safe_filename` | Removes dangerous characters and paths from file names. |
| `_is_bulk` | Marks mailing-list and marketing mail. |

Every step that reads untrusted email content is wrapped, so one malformed
email never stops the whole sync.

### `backend/mailbox_viewer/threads.py`
| Function | Purpose |
|---|---|
| `conversation_key()` | The root Message-ID of the reply chain: first of `References`, else `In-Reply-To`, else the email's own id. |
| `ancestor_ids()` | Every Message-ID this email replies to, oldest first. |

### `backend/mailbox_viewer/classifier.py`: inputs, outputs and the rules
| Name | Purpose |
|---|---|
| `AttachmentInput`, `EmailInput` | What the models receive. `EmailInput.from_parsed()` builds it from a `ParsedEmail`. |
| `Prediction` | A model's answer: label and score. |
| `DocumentModel`, `BodyModel` | The interfaces a model must have (`predict`, and `confirms_payment` for tickets). |
| `NoModel`, `NoBodyModel` | Stand-ins when a model folder is missing; they classify nothing. |
| `AttachmentDecision`, `BodyDecision`, `Decision` | Results. `Decision` has `is_payment`, `needs_review`, `matched_directly` (payment on its own content, not inherited). |
| `is_model_input()`, `model_content_type()` | Which files LayoutLMv3 can read (PDF, PNG, JPEG, DOCX). |
| `classify_attachments()` | Runs the attachment model on every readable file; errors become a short reason, never a crash. |
| `classify_body()` | Runs Laya on the email text. |
| `attachment_decides()` | Is an attachment score above the 0.80 bar? |
| `combine()` | **The decision rules** (section 4, step 6). |
| `inherited_decision()` | The decision for a reply in a payment thread. |
| `body_decision_from_reason()` | Reads Laya's label and score back from a stored reason, so rules can be re-applied without the model. |
| `PAYMENT_LABELS`, `BODY_LABELS`, `ATTACHMENT_DECIDES`, `BODY_REVIEW_FLOOR` | Constants. |

### `backend/mailbox_viewer/classification_graph.py`: the LangGraph flow
| Name | Purpose |
|---|---|
| `EmailClassifier` | Wraps both models and the rules. Built once at start-up. |
| `classify(email)` | Runs the graph: attachments → (maybe) body → combine. |
| `inherit(email)` | Payment by inheritance; labels attachments only. |
| `payment_confirmation(subject, body)` | Laya's yes/no "does this confirm a payment?" score, for tickets. |
| `combine(body, attachments)` | The rules alone, for re-deciding from stored scores. |
| `_build`, `_route_after_attachments`, `_attachments_node`, `_body_node`, `_combine_node` | The graph's nodes and the conditional edge. |
| `_on_model_thread()` | Runs every model call on the single model thread. |
| `mermaid()` | The graph as a diagram. |

### `backend/mailbox_viewer/document_model.py`: loading the real models
| Name | Purpose |
|---|---|
| `LayoutLMv3InvoiceModel` | Wraps `attachment_classifier.AttachmentClassifier`; returns `invoice` or `not_invoice` with a score. |
| `LayaBodyModel` | Loads Laya with its own loader (`rl_agent_api.RLAgent`). `predict()` asks the document-type question; `confirms_payment()` asks the yes/no payment question. |
| `LAYA_QUESTION`, `LAYA_CONFIRMATION_QUESTION` | The exact questions Laya is asked. |
| `load_classifier()` | Builds the `EmailClassifier` from settings; a missing model folder means that model is skipped with a warning. |

### `backend/attachment_classifier/`: LayoutLMv3 prediction code
Copied unchanged from Roctotpal's repo (see `SOURCE.md`; currently the **v2**
model). Do not edit; copy again to update.
| File | Purpose |
|---|---|
| `config.py` | `Config`: OCR language (`eng+fra`), PDF render DPI, 512-token limit, threshold, training settings. |
| `extraction/extract.py` | `extract_page()`: words and their positions on page 1. PDFs via `pdfplumber`; images via **Tesseract OCR**; Word files converted to PDF with LibreOffice (not installed here). |
| `extraction/cache.py` | Optional feature cache on disk (we switch it off). |
| `inference/predict.py` | `AttachmentClassifier.predict()` → probability that the file is an invoice. |

### `backend/mailbox_viewer/attachment_store.py`
| Name | Purpose |
|---|---|
| `FileSystemStore` | Files under `ATTACHMENT_DIR/<sha256>/<filename>`. `put()`, `get()`, `exists()`, `describe()`. Refuses any path that would escape its folder. |
| `blob_key()` | Builds the `<sha256>/<filename>` reference stored in the database. |
| `build_store()` | Creates the store from settings. |

### `backend/mailbox_viewer/models.py`: the database tables
SQLAlchemy classes, one per table:
`Thread`, `Email`, `EmailParticipant`, `Attachment`, `SyncState`,
`DecisionLog`, `Ticket`, `TicketEvent`. Check constraints guard the allowed
values (decisions, ticket statuses, roles). `utcnow_naive()` and
`to_naive_utc()`: the database stores UTC without a time zone.
Full details: `backend/SCHEMA.md`.

### `backend/mailbox_viewer/db.py`
| Function | Purpose |
|---|---|
| `make_engine()` | Connection pool. Sets PostgreSQL's `search_path` to `DB_SCHEMA` (`poc`); checks a connection before use (`pool_pre_ping`). |
| `init_db()` | Creates the schema and any missing tables; safe on every start. |
| `_upgrade_decision_check()` | Upgrades an old check constraint in place (adds `review`). |
| `make_session_factory()` | Creates database sessions. |

### `backend/mailbox_viewer/repository.py`: every database read and write
| Group | Functions |
|---|---|
| Lookups during sync | `find_email_id`, `is_judged`, `find_thread_id`, `thread_has_payment` |
| Writes | `insert_email` (thread, email, participants, attachments, decision, roll-ups), `log_decision` (for emails not kept), `_thread_for`, `_log_decision` |
| Re-judging | `reclassify_all` (re-run models), `redecide_all` (re-apply rules), both through `_replay`: oldest email first so the thread rule holds, and only changed rows are written. `_recompute_thread_flags` updates thread roll-ups. |
| Reads for the UI | `list_threads`, `get_thread`, `get_thread_messages` (a whole conversation in 4 queries), `get_email_detail`, `list_thread_emails`, `get_attachment`, `get_attachment_files`, `get_decision` |
| Counts | `mailbox_counts` (5 counts in one query), `count_by_doc_type`, `count_by_decision`, and single counts |
| Bookmarks | `get_sync_state`, `get_sync_states`, `save_sync_state`, `delete_sync_state` |
| Result types | `ThreadSummary`, `EmailSummary`, `EmailDetail`, `ParticipantInfo`, `AttachmentInfo`, `DecisionInfo`, `SyncStateInfo` |

### `backend/mailbox_viewer/sync.py`: the sync loop
| Name | Purpose |
|---|---|
| `run_sync()` | Syncs every folder; returns a `SyncResult` (kept, payment, dropped, skipped, backfilled, failed, tickets opened / closed). |
| `_sync_folder()` | One folder: bookmark → new UIDs → process each → move the bookmark. |
| `_resume_point()` | Where to resume, or re-walk if `UIDVALIDITY` changed. |
| `_Processor.process()` | Fetch, parse, judge, store one email; counts the outcome; one bad email never stops the run. |
| `_Processor._judge_and_store()` | Section 4, steps 5 to 8. |
| `_Processor._store()`, `_update_ticket()`, `_backfill_ancestors()` | Save, update the ticket, fetch earlier emails of a new payment thread. |
| `MailSource` | The interface a mail client must have (tests use a fake). |

### `backend/mailbox_viewer/tickets.py`: aging tickets
| Name | Purpose |
|---|---|
| `apply()` | **The ticket rules** (section 4, step 8). |
| `MailFacts` | What the rules need to know about one email. |
| `carries_invoice()`, `is_from()`, `active_ticket()` | Helpers for the rules. |
| `close_manually()`, `reopen_manually()` | The UI's Close and Reopen buttons; raise `TicketError` if not allowed. |
| `rebuild()` | Recreates every ticket from stored mail (manual changes are not kept). |
| `list_tickets()`, `get_ticket()`, `counts()` | Reads for the API: active oldest first; history; counts and age buckets. |
| `age_days()`, `bucket()` | Age: open tickets until now, closed ones until they closed. Buckets 0–2, 3–7, 8+ days. |
| `TicketView`, `EventView` | Result types. |

### `backend/mailbox_viewer/timeutil.py`
`to_ist()` and `fmt_ist()`: convert stored UTC times to Indian Standard Time
(+05:30) for display.

### `backend/mailbox_viewer/schema_v2.py`
An alias of `models.py`, kept so older scripts still import.

### Command-line tools (run from `backend/`)
| Script | Purpose |
|---|---|
| `check_db.py` | Checks the database connection and prints what is stored and the last sync time. |
| `create_schema.py` | Creates or drops the tables, or prints the SQL (`--ddl postgresql`). |
| `fetch_mail.py` | Prints recent mail without storing anything; a connection test. |
| `sync_mail.py` | Runs one sync from the terminal. `--reset-state`, `--reclassify` (`--attachments-only`), `--redecide`. |

### Tests (`backend/tests/`), run with `python -m pytest`
`fakes.py` holds stand-ins: a fake mail server, a fake LayoutLMv3 (decides by
file name) and a fake Laya (decides by words), so tests run in seconds
without the real models. One test file per area: `test_mail_parser`,
`test_threads`, `test_classifier`, `test_sync`, `test_attachment_store`,
`test_db`, `test_config`, `test_api`, `test_tickets`, `test_timeutil`.

---

## 7. File-by-file reference: frontend

### `frontend/app.py`
Sets the page title, defines the menu with `st.navigation` (Overview, Inbox,
Tickets, Decision log, How it decides, Data model, Tables), draws the sidebar
and runs the chosen page.

### `frontend/ui.py`: shared pieces
| Name | Purpose |
|---|---|
| `get_api()` | The API client every page uses: a `CachedApi`. |
| `CachedApi` | Read calls (`stats`, `threads`, `thread`, `tickets`, …) cached for `CACHE_SECONDS` (60); write calls (`start_sync`, `close_ticket`, …) clear the cache. |
| `_client()`, `_cached_call()`, `clear_cache()` | The cache machinery (`st.cache_resource`, `st.cache_data`). |
| `render_sidebar()` | Health dots, **Refresh mail**, **Reload data**, and Maintenance: **Re-apply rules**, **Re-run models**, **Rebuild tickets**. |
| `run_job()`, `show_job()` | Start a backend job, wait with a spinner, show the result. |
| `which_check()` | From a decision's tier and reason: which of the 3 checks decided it. |
| `fmt()`, `person()`, `human_size()`, `escape_md()` | Formatting helpers. |
| `DOC_COLORS`, `DECISION_COLORS`, `CHECKS`, `CHECK_BADGES`, `TICKET_STATUS`, `TICKET_EVENTS` | Labels and colours for badges. |

### `frontend/api_client.py`
`ApiClient`: one method per backend endpoint (`health`, `stats`, `threads`,
`thread`, `tickets`, `close_ticket`, `start_sync`, `job`, …). Turns HTTP errors
and an unreachable backend into a readable `ApiError`. `parse_time()`
converts the API's IST timestamps for display.

### `frontend/views/`: one file per page
| Page | What it shows |
|---|---|
| `overview.py` | Headline numbers, ticket numbers, decision and document-type charts, latest payment conversations with **Open**, rules in force, sync status. |
| `inbox.py` | Conversation table; the selected conversation's emails with decision badges, text, attachments (downloaded only on click) and decision details; the ticket badge. |
| `tickets.py` | Ticket counts and age buckets, the list (active / open / in progress / closed / all), history, **Close** and **Reopen** with a reason, link to the conversation. |
| `decisions.py` | Every judged email, kept or not, with filters by decision, check and stored. |
| `how_it_decides.py` | The three checks as a diagram drawn from the live thresholds, why each exists, and scenarios. |
| `data_model.py` | The tables as a diagram, every column and constraint, and the SQL. |
| `tables.py` | Raw rows of any table. |

### Tests (`frontend/tests/`), run with `python -m unittest`
`test_api_client.py` tests the HTTP client against a stubbed backend.
`test_pages.py` renders every page against a fake API with Streamlit's
`AppTest`, including closing and reopening a ticket and the cache.

---

## 8. Libraries and what they are used for

### Backend
| Library | Used for | Where |
|---|---|---|
| **FastAPI** | The HTTP API and the Swagger page at `/docs` | `api/main.py` |
| **Pydantic** (comes with FastAPI) | Shapes and validates the JSON answers | `api/schemas.py` |
| **Uvicorn** | Runs the FastAPI app as a web server | started from the terminal |
| **SQLAlchemy** 2 | Database tables as Python classes, queries, transactions | `models.py`, `db.py`, `repository.py`, `tickets.py` |
| **psycopg** 3 | The PostgreSQL driver underneath SQLAlchemy | via `DATABASE_URL` |
| **python-dotenv** | Reads `.env` | `config.py` |
| **LangGraph** | The classification flow as a graph with a conditional step | `classification_graph.py` |
| **PyTorch** (`torch`) | Runs both neural networks on the CPU | the models |
| **Transformers** (Hugging Face) | Loads LayoutLMv3 and Laya's ModernBERT encoder | `attachment_classifier/`, `models/laya/` |
| **safetensors** | Safe, fast model-weight file format | the model folders |
| **NumPy** | Number arrays inside the models | Laya's loader |
| **pdfplumber** | Reads words and positions from PDFs | `attachment_classifier/extraction/` |
| **pytesseract** + the **Tesseract OCR** program | Reads text from images and scans (English and French) | `attachment_classifier/extraction/` |
| **Pillow** | Image handling | extraction, tests |
| datasets, accelerate, scikit-learn, pandas | Only for training the attachment model, not for running the app | Roctotpal's repo |
| **pytest**, **httpx** | Tests; httpx powers FastAPI's test client | `tests/` |

Python's standard library does the rest: `imaplib` (Gmail), `email` (parsing),
`hashlib` (sha256), `threading` and `concurrent.futures` (jobs and the model
thread), `dataclasses`.

### Frontend
| Library | Used for |
|---|---|
| **Streamlit** | The whole web page: menu, tables, charts, buttons, caching, and `AppTest` for the tests |
| **pandas** (comes with Streamlit) | Tables and charts |
| **httpx** | HTTP calls to the backend |
| **python-dotenv** | Reads `API_URL` if set |

### The two AI models
| Model | Reads | Question | Source |
|---|---|---|---|
| **LayoutLMv3** (v2) | Attachments: words plus where they sit on the page | "Is this an invoice?" | Roctotpal's repo; full fine-tune on 3,800 documents |
| **Laya** (ModernBERT-large) | The email text | "What kind of document is this about?" and, for tickets, "Does this confirm a payment?" | Hugging Face `convaiinnovations/laya`, used zero-shot |

---

## 9. Settings (`.env`)

| Key | Meaning |
|---|---|
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | Gmail and its app password |
| `IMAP_FOLDERS` | Folders to sync, e.g. `INBOX,[Gmail]/Sent Mail` |
| `INITIAL_FETCH_LIMIT` | How many emails the first sync of a folder takes |
| `DATABASE_URL`, `DB_SCHEMA` | PostgreSQL connection and schema (`poc`) |
| `STORE_ONLY_PAYMENT` | `true`: keep only payment-related mail |
| `ATTACHMENT_DIR` | Where attachment files go |
| `ATTACHMENT_MODEL_DIR`, `BODY_MODEL_DIR` | Model folders |
| `ATTACHMENT_DECIDES_CONFIDENCE` (0.8) | Above this, the attachment decides alone |
| `BODY_MIN_CONFIDENCE` (0.7) | Laya's payment bar |
| `MODEL_MIN_CONFIDENCE` (0.5) | Floor for the review band |
| `TICKET_CLOSE_CONFIDENCE` (0.8) | Laya's bar for auto-closing a ticket |
| `API_URL` (frontend) | Where the backend runs; default `http://127.0.0.1:8000` |

---

## 10. Where to start reading the code

1. `backend/main.py` → `backend/api/main.py` `create_app()`: how the backend is wired.
2. `backend/mailbox_viewer/sync.py`: the loop at the heart of the app.
3. `backend/mailbox_viewer/classifier.py` `combine()`: the decision rules.
4. `backend/mailbox_viewer/tickets.py` `apply()`: the ticket rules.
5. `frontend/app.py` → `frontend/ui.py` → `frontend/views/inbox.py`: how a page asks for data and draws it.

---

## 11. Glossary

| Term | Meaning |
|---|---|
| **UID** | Gmail's number for an email within a folder; always increases. Used as the bookmark. |
| **UIDVALIDITY** | Gmail's version number for a folder's UIDs; if it changes, the UIDs were reset. |
| **Message-ID** | The email's unique id header. Our de-duplication key. |
| **Thread / conversation** | All emails linked by `References` / `In-Reply-To`. |
| **Inherited** | Kept as payment because its thread already is. |
| **Tier** | Which models decided: 0 none, 1 attachment, 2 body, 3 both. |
| **Review** | Uncertain payment answer, kept for a person to check. |
| **Backfill** | Fetching the earlier emails of a conversation once it becomes payment-related. |
| **Job** | Long work (sync, re-run, rebuild) running in the background on the backend. |
| **IST** | Indian Standard Time, UTC+05:30. Shown everywhere; the database stores UTC. |
