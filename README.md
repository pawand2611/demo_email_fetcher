# Mailbox Viewer (Streamlit) - Task 1

Streamlit app that connects to a Gmail inbox over IMAP, caches conversations,
messages, participants and attachments in a database (SQLite or PostgreSQL),
detects payment documents (statements, invoices, receipts) with an audit
trail, and renders everything from the cache rather than the mail server.

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

```powershell
streamlit run app.py
```

Pages:

- **Inbox** (`app.py`): one row per conversation with sender(s), message
  count, attachment count and a payment flag. Select a row to read every
  message in the thread, its participants, attachments with download
  buttons, and the classifier's decision. **Refresh** runs an incremental sync
  behind a spinner. **Re-run classification rules** re-labels the cache
  without contacting the server.
- **Data model**: the six tables drawn from the live model code, every
  column and constraint, and the DDL for SQLite or PostgreSQL.
- **Tables**: the actual rows of every table in the configured database.

## Database: SQLite or PostgreSQL

```
DATABASE_URL=sqlite:///data/mailbox_cache.db
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require
```

URL-encode special characters in the password (`@` → `%40`, `:` → `%3A`,
`/` → `%2F`, `%` → `%25`). The database must exist and the user needs
`CREATE` on it. `DB_SCHEMA=poc` puts the tables in that PostgreSQL schema
(created if missing) instead of `public`. Then `python check_db.py` connects and creates the tables,
`python sync_mail.py` fills them, and the app is restarted. The SQLite file
is left untouched, so switching back is one line.

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
python sync_mail.py --reclassify       # re-run payment-document rules over the cache only
python -m unittest -v                  # unit tests, no mailbox needed
```

## What gets stored

By default (`STORE_ONLY_PAYMENT=true`) every fetched message is judged and
logged in `decision_log`, but only payment documents (statements, invoices,
receipts) and the threads they belong to are stored. When a thread turns into
a payment thread, its earlier messages are backfilled from the mailbox. Set
`STORE_ONLY_PAYMENT=false` to keep every message. The sidebar shows how many
messages were judged versus stored.

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

Each message gets a `doc_type` (statement, invoice, receipt, other, or none),
a tier (1 attachment filename, 2 subject, 3 body, 0 nothing), a confidence
(0.95 / 0.80 / 0.60) and a `payment` / `none` decision, all recorded in
`decision_log`. A payment document requires a PDF, CSV, XLS or XLSX
attachment; a subject alone is a notice. Rules live in
`mailbox_viewer/classifier.py`; after editing them run `--reclassify` or use
the sidebar button.

Full design and ERD: [docs/SCHEMA.md](docs/SCHEMA.md).

## Layout

```
app.py                      Streamlit inbox (threads, messages, attachments, decisions)
pages/1_Data_model.py       the six-table model rendered from code, with DDL
pages/2_Tables.py           browse the rows of every table
check_db.py                 database connectivity check
create_schema.py            create / drop the schema, print DDL
fetch_mail.py               M0 script: connect, fetch, print
sync_mail.py                run one sync and print counts
mailbox_viewer/
  config.py                 settings from .env / environment, nothing hardcoded
  mail_client.py            IMAP session: search UIDs, fetch raw bytes
  mail_parser.py            raw bytes -> ParsedEmail (headers, participants, bodies, attachments)
  threads.py                conversation key from threading headers
  classifier.py             tiered payment-document rules
  attachment_store.py       file-system store for attachment bytes
  models.py                 SQLAlchemy models: threads, emails, email_participants,
                            attachments, sync_state, decision_log
  db.py                     engine / session factory
  repository.py             every database read and write
  sync.py                   the sync run: fetch, de-dup, thread, classify, persist
docs/SCHEMA.md              schema design, ERD, classification tiers, history
tests/                      unit tests (parser, threads, classifier, store, sync, schema)
```

## Out of scope by design

No login or authentication of any kind, and no Next.js. Both are later tasks.
