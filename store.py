"""
Local SQLite persistence for run history and per-document results, shared by
the CLI (pipeline.py) and the web dashboard (app.py). This is what lets the
dashboard show results from scheduled/Task Scheduler runs too, and lets a
flagged item be reviewed and fixed later (its original attachment content is
saved to disk so "fix and retry" can still attach the source PDF to Odoo).

This is local-only (SQLite file next to the script) — no network exposure.
"""

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "app_data.db")
ATTACH_DIR = os.path.join(BASE_DIR, "review_attachments")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(ATTACH_DIR, exist_ok=True)
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            source TEXT,            -- 'cli' or 'web'
            since_date TEXT,
            until_date TEXT,
            message_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            message_id TEXT,
            subject TEXT,
            sender TEXT,
            source_label TEXT,
            status TEXT,             -- excluded | skipped | needs_review | created | error
            reason TEXT,
            order_id INTEGER,
            partner TEXT,
            match_score REAL,
            extraction_json TEXT,
            content_text TEXT,
            attachment_path TEXT,
            attachment_filename TEXT,
            error_detail TEXT,
            created_at TEXT,
            resolved INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def start_run(source, since_date=None, until_date=None):
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO runs (started_at, source, since_date, until_date, message_count) VALUES (?,?,?,?,0)",
        (_now(), source, str(since_date) if since_date else None, str(until_date) if until_date else None),
    )
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    return run_id


def finish_run(run_id, message_count):
    conn = _conn()
    conn.execute(
        "UPDATE runs SET finished_at=?, message_count=? WHERE id=?",
        (_now(), message_count, run_id),
    )
    conn.commit()
    conn.close()


def _save_attachment(doc):
    if not doc or not doc.get("source_bytes") or not doc.get("source_filename"):
        return None, None
    os.makedirs(ATTACH_DIR, exist_ok=True)
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{doc['source_filename']}"
    path = os.path.join(ATTACH_DIR, safe_name)
    with open(path, "wb") as f:
        f.write(doc["source_bytes"])
    return path, doc["source_filename"]


def record_item(run_id, *, message_id, subject, sender, source_label, status,
                 reason=None, order_id=None, partner=None, match_score=None,
                 extraction=None, content_text=None, doc=None, error_detail=None):
    attach_path, attach_filename = _save_attachment(doc) if status == "needs_review" else (None, None)
    conn = _conn()
    conn.execute(
        """INSERT INTO items
           (run_id, message_id, subject, sender, source_label, status, reason,
            order_id, partner, match_score, extraction_json, content_text,
            attachment_path, attachment_filename, error_detail, created_at, resolved)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (run_id, message_id, subject, sender, source_label, status, reason,
         order_id, partner, match_score,
         json.dumps(extraction, ensure_ascii=False) if extraction else None,
         content_text, attach_path, attach_filename, error_detail, _now()),
    )
    conn.commit()
    conn.close()


def list_runs(limit=50):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_items(run_id=None, status=None, limit=200):
    conn = _conn()
    q = "SELECT * FROM items WHERE 1=1"
    params = []
    if run_id is not None:
        q += " AND run_id=?"
        params.append(run_id)
    if status is not None:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("extraction_json"):
            try:
                d["extraction"] = json.loads(d["extraction_json"])
            except Exception:
                d["extraction"] = None
        items.append(d)
    return items


def get_item(item_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get("extraction_json"):
        try:
            d["extraction"] = json.loads(d["extraction_json"])
        except Exception:
            d["extraction"] = None
    return d


def get_attachment_bytes(item_id):
    item = get_item(item_id)
    if not item or not item.get("attachment_path") or not os.path.exists(item["attachment_path"]):
        return None, None
    with open(item["attachment_path"], "rb") as f:
        return f.read(), item.get("attachment_filename")


def mark_resolved(item_id, order_id):
    conn = _conn()
    conn.execute(
        "UPDATE items SET resolved=1, status='created', order_id=? WHERE id=?",
        (order_id, item_id),
    )
    conn.commit()
    conn.close()
