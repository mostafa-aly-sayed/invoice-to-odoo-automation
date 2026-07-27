"""
Local web dashboard for the invoice-to-quotation pipeline.

Runs only on this computer (binds to 127.0.0.1) — no login needed since it's
not reachable from anywhere else. Open http://127.0.0.1:5000 after starting
this with `python app.py`.

Shares core.py / store.py / odoo_client.py / config.json / aliases.json with
pipeline.py (the command-line version), so nothing here duplicates that
logic — this file is just routes + a thin layer over the same functions.
"""

import os

from flask import Flask, jsonify, request, send_from_directory, abort

import core
import store
from odoo_client import OdooClient, load_aliases

app = Flask(__name__, static_folder="static", static_url_path="")


def get_odoo():
    cfg = core.load_config()
    return OdooClient(cfg["odoo"], cfg.get("own_company_keywords", []))


# ---------------------------------------------------------------- frontend --

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ------------------------------------------------------------------- runs --

@app.route("/api/run", methods=["POST"])
def api_run():
    from datetime import datetime, timedelta
    body = request.get_json(silent=True) or {}

    since_date, until_date = None, None
    if body.get("date"):
        d = datetime.strptime(body["date"], "%Y-%m-%d").date()
        since_date, until_date = d, d + timedelta(days=1)
    else:
        if body.get("since"):
            since_date = datetime.strptime(body["since"], "%Y-%m-%d").date()
        if body.get("until"):
            until_date = datetime.strptime(body["until"], "%Y-%m-%d").date()

    events = []
    try:
        summary = core.run_pipeline(
            source="web", since_date=since_date, until_date=until_date,
            force=bool(body.get("force")), on_event=events.append,
        )
    except Exception as e:
        return jsonify({"error": str(e), "events": events}), 500

    return jsonify({"summary": summary, "events": events})


@app.route("/api/runs")
def api_runs():
    return jsonify(store.list_runs())


@app.route("/api/runs/<int:run_id>/items")
def api_run_items(run_id):
    return jsonify(store.list_items(run_id=run_id))


# ------------------------------------------------------------------ items --

@app.route("/api/items")
def api_items():
    status = request.args.get("status")
    return jsonify(store.list_items(status=status))


@app.route("/api/items/<int:item_id>")
def api_item(item_id):
    item = store.get_item(item_id)
    if not item:
        abort(404)
    return jsonify(item)


@app.route("/api/items/<int:item_id>/resolve", methods=["POST"])
def api_resolve_item(item_id):
    item = store.get_item(item_id)
    if not item:
        abort(404)

    body = request.get_json(silent=True) or {}
    partner_id = body.get("partner_id")
    po_number = body.get("po_number", "")
    branch_text = body.get("branch_text", "")
    lines = body.get("lines") or []  # [{product_id, qty}]

    if not partner_id:
        return jsonify({"error": "partner_id is required"}), 400
    line_items = [(l["product_id"], l["qty"]) for l in lines if l.get("product_id") and l.get("qty")]
    if not line_items:
        return jsonify({"error": "at least one product line with a quantity is required"}), 400

    attach_bytes, attach_filename = store.get_attachment_bytes(item_id)
    doc = None
    if attach_bytes and attach_filename:
        doc = {"source_bytes": attach_bytes, "source_filename": attach_filename}

    cfg = core.load_config()
    odoo = get_odoo()
    try:
        order_id = core.create_quotation_manual(
            cfg, odoo, item["subject"], item["sender"], int(partner_id),
            po_number, branch_text, line_items, doc=doc,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    store.mark_resolved(item_id, order_id)
    return jsonify({"order_id": order_id})


# ------------------------------------------------------------------ odoo ---

@app.route("/api/odoo/customers")
def api_odoo_customers():
    q = request.args.get("q", "")
    odoo = get_odoo()
    return jsonify(odoo.search_customers(q, limit=25))


@app.route("/api/odoo/products")
def api_odoo_products():
    q = request.args.get("q", "")
    odoo = get_odoo()
    return jsonify(odoo.search_products(q, limit=25))


# --------------------------------------------------------------- aliases ---

def _read_aliases_raw():
    return load_aliases(core.ALIASES_PATH)


def _write_aliases_raw(entries):
    import json
    with open(core.ALIASES_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


@app.route("/api/aliases")
def api_aliases():
    raw = _read_aliases_raw()
    # Hide the internal "_comment"/"_note_*" documentation keys from the UI list.
    entries = {k: v for k, v in raw.items() if not k.startswith("_")}
    return jsonify(entries)


@app.route("/api/aliases", methods=["POST"])
def api_add_alias():
    body = request.get_json(silent=True) or {}
    key, value = body.get("key", "").strip().lower(), body.get("value", "").strip()
    if not key or not value:
        return jsonify({"error": "both key and value are required"}), 400
    raw = _read_aliases_raw()
    raw[key] = value
    _write_aliases_raw(raw)
    return jsonify({"ok": True})


@app.route("/api/aliases/<path:key>", methods=["DELETE"])
def api_delete_alias(key):
    raw = _read_aliases_raw()
    raw.pop(key.strip().lower(), None)
    _write_aliases_raw(raw)
    return jsonify({"ok": True})


if __name__ == "__main__":
    store.init_db()
    # 127.0.0.1 only — not reachable from other devices, matches the "local only" choice.
    app.run(host="127.0.0.1", port=5000, debug=False)
