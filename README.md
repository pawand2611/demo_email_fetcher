# Mailbox Viewer - Task 1

Two services. A **backend API** (FastAPI) connects to a Gmail inbox over
IMAP, caches conversations, messages, participants and attachments in a
database (SQLite or PostgreSQL), classifies attachments with a trained
document model, and keeps an audit trail. A **Streamlit frontend** shows it
all and talks only to the API, never to the database or the mailbox.

```
Streamlit (localhost:8501)  --HTTP-->  Backend API (127.0.0.1:8000)  -->  PostgreSQL / SQLite
                                                                    -->  Gmail over IMAP
                                                                    -->  data/attachments/
                                                                    -->  document model
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

1. `IMAP_USER` is your Gmail address.
2. `IMAP_PASSWORD` is a Gmail **App Password**, not your account password.
   Google Account -> Security -> 2-Step Verification (must be on) -> App
   passwords -> create one named "mailbox viewer".
3. `DATABASE_URL` selects SQLite (default) or PostgreSQL, see below.

`.env` is git-ignored. Never commit it.

## Run the app

Two terminals, backend first:

```powershell
# 1. backend API
.\.venv\Scripts\python.exe -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 8000

# 2. frontend
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The backend has no login (Task 1 forbids it), so it must stay bound to
`127.0.0.1`. Interactive API docs: http://127.0.0.1:8000/docs.

Pages:

- **Inbox** (`app.py`): one row per conversation with sender(s), message
  count, attachment count and a payment flag. Select a row to read every
  message in the thread, its participants, attachments with download
  buttons, and the model's decision. **Refresh** starts a background sync on
  the backend and shows a spinner until it finishes. **Re-run classifier**
  re-applies the document model to cached mail without contacting the server.
- **Data model**: the six tables, every column and constraint, and the DDL
  for SQLite or PostgreSQL, served by the backend from the live model code.
- **Tables**: the actual rows of every table, through the API.

## Backend API

| Endpoint | Purpose |
|---|---|
| `GET /health` | database status and the loaded model |
| `GET /profiles` | the active business-purpose profile: mailbox, folders, keep policy, model |
| `GET /stats` | counts and per-folder sync bookmarks |
| `POST /sync` | start an incremental sync in the background; returns a job, or the one already running |
| `GET /sync/{job_id}`, `GET /sync/latest` | job state and result |
| `POST /reclassify` | re-run the document model over cached mail |
| `GET /threads?payment_only&search&limit` | conversation list |
| `GET /threads/{id}` | every message with participants, attachments and decision |
| `GET /attachments/{id}` | file download |
| `GET /decisions/{message_id}` | audit-trail entry |
| `GET /schema`, `GET /schema/ddl` | data model as JSON, DDL as text |
| `GET /tables`, `GET /tables/{name}/rows` | raw table browsing |

All timestamps are UTC (ISO 8601 with `Z`).

## Database: SQLite or PostgreSQL

```
DATABASE_URL=sqlite:///data/mailbox_cache.db
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require
```

URL-encode special characters in the password (`@` → `%40`, `:` → `%3A`,
`/` → `%2F`, `%` → `%25`). The database must exist and the user needs
`CREATE` on it. `DB_SCHEMA=poc` puts the tables in that PostgreSQL schema
(created if missing) instead of `public`. Then `python check_db.py` connects
and creates the tables, and the backend is restarted. The SQLite file is left
untouched, so switching back is one line.

## Where attachments are stored

Metadata in the `attachments` table; bytes on disk under
`ATTACHMENT_DIR/<sha256>/<filename>` (default `data/attachments/`), with the
relative path stored as `blob_key`. Identical files are stored once. Pure
Python, no services, nothing leaves the machine. Back up the folder together
with the database.

An earlier iteration used Microsoft's Azurite emulator for Azure Blob
Storage. It was removed because Azurite's default telemetry reads the Windows
machine GUID from the registry at startup, which trips endpoint-security
alerts on managed machines. Do not reintroduce it here.

## Command-line tools

```powershell
python check_db.py                     # connect to DATABASE_URL, create tables, print counts
python create_schema.py --ddl sqlite   # print the schema DDL (or postgresql); --drop rebuilds tables
python fetch_mail.py --limit 10        # M0: connect and print, no database
python sync_mail.py                    # one sync run, prints counts before/after
python sync_mail.py --reset-state      # forget the resume point and re-walk (still 0 new)
python sync_mail.py --reclassify       # re-run the document model over the cache only
python -m unittest -v                  # unit tests, no mailbox needed
```

## Folders and what gets stored

`IMAP_FOLDERS` lists the folders to sync, each with its own bookmark; the
default `INBOX,[Gmail]/Sent Mail` makes threads complete with your own
replies, and messages from all folders land in the same threads.

By default (`STORE_ONLY_PAYMENT=false`) every fetched message is stored.
With `STORE_ONLY_PAYMENT=true` every message is still judged and logged in
`decision_log`, but only payment documents (statements, invoices, receipts)
and the threads they belong to are stored; when a thread turns into a payment
thread, its earlier messages are backfilled from the mailbox. The sidebar
shows how many messages were judged versus stored.

## How the sync stays idempotent and incremental

- **De-dup key** is the RFC 5322 `Message-ID` (UNIQUE). A message without
  one gets `generated-<sha256 of raw bytes>`, which is just as stable.
  `decision_log` is checked before anything else, so a message judged once,
  kept or dropped, is never judged again.
- **Resume point** is `sync_state.last_seen_uid` per folder. The next run asks
  IMAP for `UID last_seen_uid+1:*` only. A changed `UIDVALIDITY` triggers a
  re-walk, and de-dup makes that safe.
- **Threads** are keyed by the root Message-ID from `References` /
  `In-Reply-To`; a reply whose root is outside the sync window joins the
  nearest cached ancestor.
- **Failures are contained.** A malformed message or one failed fetch is
  counted and skipped; only a dropped connection stops the run, and it
  resumes next time from the last UID persisted.

## Classification

Classification comes only from a trained document model (LayoutLMv3); there
are no keyword rules. The model reads each PDF or image attachment and
returns a label and a score. Each attachment stores its own `doc_type` and
`confidence`. The message takes the strongest prediction and is a `payment`
decision when that label is statement, invoice or receipt and the score is at
least `MODEL_MIN_CONFIDENCE`. Everything goes to `decision_log` with tier 1
for a model prediction, or tier 0 when nothing could be classified.

**Current state: no model is loaded.** With `MODEL_PATH` empty, attachments
stay unclassified with the reason "no classification model configured", and
every message is still stored. The LayoutLMv3 adapter is added in
`mailbox_viewer/document_model.py` when the trained model joins the project.
It must load from the local folder with Hugging Face offline mode and
telemetry disabled; weights stay out of git.

Full design and ERD: [docs/SCHEMA.md](docs/SCHEMA.md).

## Layout

```
backend/                    the API service (only process that touches mail, DB, files, model)
  main.py                   FastAPI app factory and routes
  schemas.py                response models
  jobs.py                   background sync jobs, one at a time
frontend/
  api_client.py             HTTP client the Streamlit pages use
app.py                      Streamlit inbox (threads, messages, attachments, decisions)
pages/1_Data_model.py       the six-table model with DDL, from the API
pages/2_Tables.py           browse the rows of every table, from the API
check_db.py                 database connectivity check
create_schema.py            create / drop the schema, print DDL
fetch_mail.py               M0 script: connect, fetch, print
sync_mail.py                run one sync and print counts
mailbox_viewer/
  config.py                 settings from .env / environment, nothing hardcoded
  mail_client.py            IMAP session: search UIDs, fetch raw bytes
  mail_parser.py            raw bytes -> ParsedEmail (headers, participants, bodies, attachments)
  threads.py                conversation key from threading headers
  classifier.py             model-based classification and decision
  document_model.py         loads the trained document model (LayoutLMv3 slot)
  attachment_store.py       file-system store for attachment bytes
  models.py                 SQLAlchemy models: threads, emails, email_participants,
                            attachments, sync_state, decision_log
  db.py                     engine / session factory
  repository.py             every database read and write
  sync.py                   the sync run: fetch, de-dup, thread, classify, persist
docs/SCHEMA.md              schema design, ERD, refresh flow, history
tests/                      unit tests (parser, threads, classifier, store, sync, schema, API, client)
```

## Out of scope by design

No login or authentication of any kind, and no Next.js. Both are later
tasks; the API is the place both attach to.
