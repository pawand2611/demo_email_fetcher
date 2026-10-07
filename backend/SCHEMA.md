# Cache schema

Status: the six-table model below was adopted on 2026-09-30 from a design
sketch and is the live schema (`mailbox_viewer/models.py`). It replaced the
original three-table design, whose history is at the end of this file.

Database: SQLite or PostgreSQL via SQLAlchemy 2.x ORM, selected by
`DATABASE_URL`. The same models produce `DATETIME` on SQLite and `TIMESTAMP`
on PostgreSQL. `python create_schema.py --ddl postgresql|sqlite` prints the
DDL; the **Data model** page in the app renders it live.

## ERD

```mermaid
erDiagram
    THREADS ||--o{ EMAILS : "1..n"
    EMAILS ||--o{ EMAIL_PARTICIPANTS : "0..n"
    EMAILS ||--o{ ATTACHMENTS : "0..n"

    THREADS {
        int      id               PK
        text     conversation_key UK "root Message-ID of the reply chain"
        text     subject
        int      message_count
        datetime last_message_at
        bool     has_payment      "true once any message matches"
        datetime created_at
    }
    EMAILS {
        int      id                PK
        int      thread_id         FK "-> threads.id, ON DELETE RESTRICT"
        text     message_id        UK "RFC 5322 Message-ID; the de-dup key"
        text     in_reply_to
        text     references_header
        text     subject
        text     body_text
        datetime received_at
        bool     has_attachments
        bool     matched_directly  "this message itself is a payment document"
        text     doc_type          "statement | invoice | receipt | other | NULL"
        float    confidence        "0..1"
        text     decision_reason
        datetime created_at
    }
    EMAIL_PARTICIPANTS {
        int  id       PK
        int  email_id FK "-> emails.id, ON DELETE CASCADE"
        text role     "from | to | cc | bcc"
        text name
        text address
    }
    ATTACHMENTS {
        int   id           PK
        int   email_id     FK "-> emails.id, ON DELETE CASCADE"
        text  filename
        text  content_type
        int   size_bytes
        text  blob_key     "reference only: <sha256>/<filename> in ATTACHMENT_DIR"
        text  doc_type
        float confidence
    }
    SYNC_STATE {
        text     folder_name   PK
        int      uid_validity
        int      last_seen_uid
        datetime last_sync_at
    }
    DECISION_LOG {
        text     message_id PK
        int      tier       "0 nothing, 1 attachment model, 2 body model, 3 both"
        text     decision   "payment | none | review"
        float    confidence
        text     reason
        datetime decided_at
    }
```

## Ownership

- **threads**: one row per conversation, keyed by the root Message-ID
  (first entry of `References`, else `In-Reply-To`, else the message's own
  id). A reply whose root is not cached joins the nearest cached ancestor.
  `message_count`, `last_message_at` and `has_payment` are rolled up on
  insert and recomputed by `--reclassify`.
- **emails**: one row per message. `message_id` UNIQUE is the de-dup
  guarantee; a missing header is replaced by `generated-<sha256 of raw>`.
- **email_participants**: one row per person per role, so To and Cc lists
  that change across a thread stay clean and are searchable.
- **attachments**: metadata tied to the message it arrived on. `blob_key` is
  a reference into the file store; no bytes in the database.
- **sync_state**: the bookmark, one row per folder (`IMAP_FOLDERS`). The next run asks IMAP for
  `UID last_seen_uid+1:*`. A changed `uid_validity` triggers a re-walk, and
  de-dup makes that safe.
- **decision_log**: the audit trail, one row per message, rewritten each time
  the rules run.

## How data moves on one refresh

The bookmark is read first and moved last.

| Step | Action | Reads | Writes |
|---|---|---|---|
| 1 | Refresh clicked | `sync_state` | |
| 2 | Fetch UIDs above `last_seen_uid` over IMAP | | |
| 3 | **Already judged?** If `decision_log` has the Message-ID, skip. A rescan never re-judges. | `decision_log`, `emails` | |
| 4 | **Find the thread.** A reply into a thread with `has_payment` is kept regardless of its own content. | `threads`, `emails` | |
| 5 | **Classify** with both models (LangGraph: Laya on the body, LayoutLMv3 on attachments, then the decision rule) | | `decision_log` |
| 6 | **Keep?** Payment document, or payment thread, or `STORE_ONLY_PAYMENT=false` (the default) → save. Otherwise drop: only the log line is written. | | |
| 7 | **Save in one transaction**: thread (found or created), email, participants, attachments. Files were written to the store just before. When a thread turns into a payment thread, its earlier mail that was dropped is backfilled from the mailbox by Message-ID. | | `threads`, `emails`, `email_participants`, `attachments`, files |
| 8 | Move the bookmark | | `sync_state` |

Streamlit reads `threads`, `emails`, `email_participants`, `attachments` and
`decision_log`. Never the mailbox.

## Classification

Two local models, run as a LangGraph flow (`classification_graph.py`).
LayoutLMv3 (trained) labels each PDF, image or DOCX attachment invoice /
not_invoice first; above 0.8 (`ATTACHMENT_DECIDES_CONFIDENCE`) it decides the
email on its own and Laya is skipped. Otherwise Laya (zero-shot) reads the
body and counts as payment at 0.7 or more (`BODY_MIN_CONFIDENCE`), with 0.5
to 0.7, or a weak invoice call Laya does not support, going to `review`.
Without a readable attachment Laya decides alone. Once an email of a thread
is payment, later emails of that thread inherit it without running Laya;
LayoutLMv3 still labels their attachments (`matched_directly` false, reason "part of a thread already classified
as payment"). Re-classify and re-decide replay emails oldest first so the
thread rule sees earlier emails first. See the README table.

`decision_log.decision` allows `payment`, `none` and `review` (the `review`
value was added on 2026-10-07; `init_db` upgrades the PostgreSQL check
constraint in place). `tier`: 0 nothing classified, 1 attachment model only,
2 body model only, 3 both. The reason always starts with the body model's
label and score (`body: receipt (0.75) ...`), which is what lets `--redecide`
re-apply a changed rule without running the models again.

## Attachments

Bytes live under `ATTACHMENT_DIR/<sha256>/<filename>` (default
`data/attachments/`). Content-addressed keys mean identical files are stored
once and retries are idempotent. Files are written between database
transactions, so the database write lock is never held during disk I/O. The
store refuses any key that could escape its root.

## History

- 2026-09-25: original three tables (`emails`, `attachments` with inline
  BLOBs, `sync_state`) plus an eight-way category label. Reviewed and built.
- 2026-09-25: Azure Blob Storage tried via the Azurite emulator; removed the
  same week after its default telemetry raised an endpoint-security alert.
- 2026-09-30: six-table model adopted; attachments moved to the local file
  system; categories dropped in favour of `doc_type` / tier / decision log.
- 2026-10-06: keyword rules removed; classification comes only from the
  trained document model. The app split into a backend API (FastAPI) and a
  Streamlit frontend that talks to it over HTTP.
- 2026-10-07: classification by two local models through LangGraph: Laya on
  the body (zero-shot), LayoutLMv3 on attachments (trained). `review` added
  as a decision. Laya counts as payment only at 0.9 or more.
- 2026-10-07: payment bar for Laya lowered from 0.9 to 0.7 at the user's request.
- 2026-10-07: rule set by the user: attachment model decides alone above
  0.8, otherwise Laya is consulted (0.7 bar); without attachments Laya
  decides; later emails of a payment thread inherit payment.
- 2026-10-07: inherited emails get their attachments labelled by LayoutLMv3.
  Storage switched to payment-related mail only (`STORE_ONLY_PAYMENT=true`);
  review emails are kept so they can be checked.
