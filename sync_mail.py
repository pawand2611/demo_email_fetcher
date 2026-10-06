"""Run one sync from the mailbox into the cache and print the counts.

Run it twice to prove de-duplication: the second run reports 0 new and the
table counts do not change.

Usage:
    python sync_mail.py                # incremental sync
    python sync_mail.py --reset-state  # forget the resume point first, then re-walk
                                       # (still adds no duplicate rows)
    python sync_mail.py --reclassify   # re-run the document model over the cache
                                       # only; no mail-server contact

This is a backend-side maintenance tool; the Streamlit frontend uses the API.
"""

from __future__ import annotations

import argparse
import logging
import sys

from mailbox_viewer import repository as repo
from mailbox_viewer.attachment_store import build_store
from mailbox_viewer.config import ConfigError, load_settings
from mailbox_viewer.db import init_db, make_engine, make_session_factory
from mailbox_viewer.document_model import load_model
from mailbox_viewer.sync import run_sync


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # subjects may contain emoji; cp1252 cannot print them
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        model = load_model(settings)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    store = build_store(settings)
    print(f"Attachments  : {store.describe()}")
    print(f"Model        : {model.name}")
    print(f"Folders      : {', '.join(settings.imap_folders)}")
    print(f"Keep policy  : {'payment documents and payment threads only' if settings.store_only_payment else 'every message'}")

    engine = make_engine(settings.database_url, settings.db_schema)
    init_db(engine, settings.db_schema)
    session_factory = make_session_factory(engine)

    if args.reclassify:
        with session_factory() as session, session.begin():
            changed = repo.reclassify_all(session, store, model, settings.model_min_confidence)
            by_doc = repo.count_by_doc_type(session)
            by_decision = repo.count_by_decision(session)
        print(f"Model {model.name} re-applied to cached mail: {changed} email(s) changed")
        _print_counts("doc_type", by_doc)
        _print_counts("decision", by_decision)
        engine.dispose()
        return 0

    if args.reset_state:
        with session_factory() as session, session.begin():
            removed = [f for f in settings.imap_folders if repo.delete_sync_state(session, f)]
        print("Sync state cleared for: " + ", ".join(removed) if removed else "No sync state to clear")

    before = _counts(session_factory)
    result = run_sync(settings, session_factory, store=store, model=model)
    after = _counts(session_factory)

    print()
    print(f"Status        : {result.status}" + (f" ({result.error})" if result.error else ""))
    print(f"Candidates    : {result.candidates}")
    print(f"Kept          : {result.kept}  ({result.payment_hits} payment documents, {result.backfilled} backfilled)")
    print(f"Dropped       : {result.dropped}  (judged, not stored)")
    print(f"Skipped       : {result.skipped}  (already judged)")
    print(f"Failed        : {result.failed}")
    print(f"Duration      : {result.duration_seconds}s")
    for fr in result.folders:
        note = "  [full re-walk]" if fr.full_rewalk else ""
        err = f"  ({fr.status}: {fr.error})" if fr.error else ""
        print(f"  folder {fr.folder!r}: {fr.candidates} candidate(s), last seen UID {fr.last_seen_uid}{note}{err}")
    print()
    print(f"{'table':<20}{'before':>8}{'after':>8}")
    for name in before:
        print(f"{name:<20}{before[name]:>8}{after[name]:>8}")

    with session_factory() as session:
        by_doc = repo.count_by_doc_type(session)
        by_decision = repo.count_by_decision(session)
    _print_counts("doc_type", by_doc)
    _print_counts("decision", by_decision)

    engine.dispose()
    return 0 if result.ok else 1


def _counts(session_factory) -> dict[str, int]:
    with session_factory() as session:
        return {
            "threads": repo.count_threads(session),
            "emails": repo.count_emails(session),
            "attachments": repo.count_attachments(session),
            "payment threads": repo.count_payment_threads(session),
            "decision_log": repo.count_decisions(session),
        }


def _print_counts(title: str, counts: dict[str, int]) -> None:
    print()
    print(f"{title:<14}{'emails':>8}")
    for name, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"{name:<14}{count:>8}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset-state", action="store_true", help="clear the stored resume point so the run re-walks the folder")
    parser.add_argument("--reclassify", action="store_true", help="re-run the document model over the cache and exit; does not contact the server")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
