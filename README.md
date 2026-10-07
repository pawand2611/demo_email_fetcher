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
pip install -r backend\requirements.txt -r frontend\requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

1. `IMAP_USER` is your Gmail address.
2. `IMAP_PASSWORD` is a Gmail **App Password**, not your account password.
   Google Account -> Security -> 2-Step Verification (must be on) -> App
   passwords -> create one named "mailbox viewer".
3. `DATABASE_URL` selects SQLite (default) or PostgreSQL, see below.

`.env` lives in the repository root and is shared by both services. It is
git-ignored. Never commit it.

## Run the app

Two terminals, backend first, each from its own folder:

```powershell
# 1. backend API
cd backend
..\.venv\Scripts\python.exe -m uvicorn api.main:create_app --factory --host 127.0.0.1 --port 8000

# 2. frontend
cd frontend
..\.venv\Scripts\python.exe -m streamlit run app.py
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
| `GET /jobs/{job_id}`, `GET /sync/latest` | state and result of a background job |
| `POST /reclassify` | re-run both models over cached mail, in the background |
| `POST /redecide` | re-apply the decision rule to stored predictions, in the background |
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
(created if missing) instead of `public`. Then `python check_db.py` (from
`backend/`) connects and creates the tables, and the backend is restarted. The SQLite file is left
untouched, so switching back is one line.

## Where attachments are stored

Metadata in the `attachments` table; bytes on disk under
`ATTACHMENT_DIR/<sha256>/<filename>` (default `data/attachments/`, relative
to `backend/`, so `backend/data/attachments/`), with the
relative path stored as `blob_key`. Identical files are stored once. Pure
Python, no services, nothing leaves the machine. Back up the folder together
with the database.

An earlier iteration used Microsoft's Azurite emulator for Azure Blob
Storage. It was removed because Azurite's default telemetry reads the Windows
machine GUID from the registry at startup, which trips endpoint-security
alerts on managed machines. Do not reintroduce it here.

## Command-line tools

Backend maintenance scripts, run from `backend/`:

```powershell
python check_db.py                     # connect to DATABASE_URL, create tables, print counts
python create_schema.py --ddl sqlite   # print the schema DDL (or postgresql); --drop rebuilds tables
python fetch_mail.py --limit 10        # M0: connect and print, no database
python sync_mail.py                    # one sync run, prints counts before/after
python sync_mail.py --reset-state      # forget the resume point and re-walk (still 0 new)
python sync_mail.py --reclassify       # re-run both models over the cache (slow)
python sync_mail.py --redecide         # re-apply the decision rule to stored predictions (fast)
python -m unittest -v                  # backend tests, no mailbox needed
```

Frontend tests, run from `frontend/`: `python -m unittest -v` (they stub the
backend, so neither the API nor the mailbox is needed).

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

Each email is classified by two local models, run as a LangGraph flow
(`backend/mailbox_viewer/classification_graph.py`):

```
START ─┬─> classify_body (Laya, email body) ─────────────────┬─> combine ─> END
       └─> classify_attachments (LayoutLMv3, each PDF/image) ─┘
```

- **Laya** (`convaiinnovations/laya`, zero-shot) answers "what kind of
  document does this email send?": invoice, receipt, statement, purchase
  order, quotation or other, with a score.
- **LayoutLMv3** (trained by the team, invoice vs not invoice) reads the first
  page of each PDF, JPG, PNG or DOCX attachment.

Decision rule:

| Evidence | Decision |
|---|---|
| Laya says statement, invoice or receipt with score ≥ `BODY_MIN_CONFIDENCE` (0.7) | payment |
| An attachment is an invoice with score ≥ 0.6 | payment |
| Laya says a payment type with score 0.5 to 0.7 | review |
| An attachment is an invoice with score 0.5 to 0.6 | review |
| The two models contradict each other on "invoice" | review |
| Anything else, including low scores on "other" | none |

Each email's decision, tier (0 nothing classified, 1 attachment model, 2 body
model, 3 both), score and reason go to `decision_log`; each attachment keeps
its own label and score.

Both models load only from local folders (`backend/models/`, git-ignored).
Hugging Face offline mode and telemetry and LangSmith tracing are forced off
in `mailbox_viewer/__init__.py`. Model inference runs on one dedicated thread
to keep PyTorch from spawning hundreds of threads. On a laptop CPU, Laya takes
about 6 seconds per real email.

- **Re-run classifier** (sidebar, `POST /reclassify`, `sync_mail.py --reclassify`):
  runs both models again over all cached mail. Slow: about 25 minutes for 250 emails.
- **Re-decide** (`POST /redecide`, `sync_mail.py --redecide`): re-applies the
  decision rule to the stored predictions after changing a threshold. No
  models, takes about a minute.

Model setup, once (weights are not in git):

- LayoutLMv3: the inference code is copied in `backend/attachment_classifier/`
  (see its `SOURCE.md`); weights `model.safetensors` (504 MB, Git LFS) and its
  config/tokenizer files go to `backend/models/layoutlmv3_invoice/`.
- Laya: `model.safetensors`, `rl_agent_api.py`, `rl_common.py`,
  `email_utils.py`, `rl_agent_config.json`, `encoder/config.json`,
  `tokenizer/tokenizer.json`, `tokenizer/tokenizer_config.json` from
  huggingface.co/convaiinnovations/laya go to `backend/models/laya/`.
- Images and scanned PDFs also need the **Tesseract** OCR program, installed
  through your IT-approved route. Without it they are recorded as "OCR program
  Tesseract is not installed" and the body model alone decides. DOCX needs
  LibreOffice.

Caution: Laya's 96 to 100% accuracy came from generated emails. On real mail
it is weaker (for example real invoice replies labelled "purchase order"),
which the 0.7 bar and the review state compensate for until it is fine-tuned.

## Layout

```
backend/                      the API service: the only part that touches mail, database, files, model
  api/
    main.py                   FastAPI app factory and routes
    schemas.py                response models
    jobs.py                   background sync jobs, one at a time
  mailbox_viewer/
    config.py                 settings from .env / environment, nothing hardcoded
    mail_client.py            IMAP session: search UIDs, fetch raw bytes
    mail_parser.py            raw bytes -> ParsedEmail (headers, participants, bodies, attachments)
    threads.py                conversation key from threading headers
    classifier.py             the two classification steps and the decision rule
    classification_graph.py   LangGraph flow: body + attachments in parallel, then combine
    document_model.py         loads Laya and LayoutLMv3 from backend/models/
    attachment_store.py       file-system store for attachment bytes
    models.py                 SQLAlchemy models: threads, emails, email_participants,
                              attachments, sync_state, decision_log
    db.py                     engine / session factory
    repository.py             every database read and write
    sync.py                   the sync run: fetch, de-dup, thread, classify, persist
  check_db.py                 database connectivity check
  create_schema.py            create / drop the schema, print DDL
  fetch_mail.py               M0 script: connect, fetch, print
  sync_mail.py                run one sync and print counts
  tests/                      parser, threads, classifier, store, sync, schema, API
  attachment_classifier/      LayoutLMv3 inference code, copied from the model repo
  models/                     model weights (git-ignored)
  SCHEMA.md                   schema design, ERD, refresh flow, history
  requirements.txt
  data/                       attachment files (git-ignored)
frontend/                     Streamlit, talks to the backend over HTTP only
  app.py                      inbox: threads, messages, attachments, decisions
  pages/1_Data_model.py       the six-table model with DDL
  pages/2_Tables.py           browse the rows of every table
  api_client.py               HTTP client the pages use
  tests/                      client tests against a stubbed backend
  requirements.txt
README.md, .env.example, .gitignore
```

## Out of scope by design

No login or authentication of any kind, and no Next.js. Both are later
tasks; the API is the place both attach to.
