"""
Invoice email -> Odoo draft quotation pipeline (CLI / Task Scheduler entry point).

Run manually:         python pipeline.py
Check one day:        python pipeline.py --date 2026-07-26
Reprocess a window:   python pipeline.py --date 2026-07-26 --force
Schedule it (Task Scheduler / cron) once a manual run looks correct.

This is a thin CLI wrapper around core.run_pipeline() — the same function the
local web dashboard (app.py) calls, so runs from either place share the same
processed_emails.json dedup log and the same SQLite history/needs-review
queue (app_data.db). needs_review.csv is still written for a quick
human-readable log; the dashboard is the primary place to review and fix
flagged items now.
"""

import argparse
from datetime import datetime, timedelta

import core


def parse_args():
    p = argparse.ArgumentParser(description="Invoice email -> Odoo draft quotation pipeline")
    p.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Only check mail from this single day (e.g. yesterday). Shortcut for --since DATE with a 1-day window.",
    )
    p.add_argument(
        "--since", metavar="YYYY-MM-DD",
        help="Only check mail on/after this date. Overrides config's lookback_days.",
    )
    p.add_argument(
        "--until", metavar="YYYY-MM-DD",
        help="Only check mail before this date (exclusive). Optional, pairs with --since.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Reprocess messages even if already recorded in processed_emails.json. "
             "Useful with --date/--since for re-testing a specific email. Note: this "
             "will attempt to re-create a quotation for orders that were already "
             "created successfully — only use this for messages you know need a redo "
             "(e.g. one that errored or was wrongly matched before a fix).",
    )
    return p.parse_args()


def resolve_date_window(args):
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        return d, d + timedelta(days=1)
    since = datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
    until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else None
    return since, until


def print_event(event):
    t = event["type"]
    if t == "info":
        print(event["message"])
    elif t == "excluded":
        print(f"[excluded] {event['subject']}")
    elif t in ("skipped", "needs_review"):
        print(f"[{t}] {event['subject']} — {event.get('reason')}")
    elif t == "created":
        print(f"[created] Quotation id {event['order_id']} for {event['partner']} — {event['subject']}")
    elif t == "error":
        print(f"[error] {event['subject']}: {event['detail']}")


def main():
    args = parse_args()
    since_date, until_date = resolve_date_window(args)

    if since_date:
        window = f"{since_date}" + (f" to {until_date}" if until_date and until_date != since_date + timedelta(days=1) else "")
        print(f"Date window: {window}")

    summary = core.run_pipeline(
        source="cli", since_date=since_date, until_date=until_date,
        force=args.force, on_event=print_event,
    )
    print(
        f"Done. {summary['created']} created, {summary['needs_review']} need review, "
        f"{summary['skipped']} skipped, {summary['excluded']} excluded, {summary['errors']} errors."
    )


if __name__ == "__main__":
    main()
