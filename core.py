"""
Shared processing logic used by both the CLI (pipeline.py) and the local web
dashboard (app.py). Neither entry point duplicates this — they call into it
and decide separately how to display/persist the results.
"""

import csv
import json
import os
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

import imap_client
import pdf_utils
import llm_extract
import store
from odoo_client import OdooClient, load_aliases


def html_to_text(html):
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)


def compile_exclude_patterns(cfg):
    return [re.compile(p, re.IGNORECASE) for p in cfg.get("subject_exclude_patterns", [])]


def is_excluded_subject(subject, exclude_patterns):
    return any(p.search(subject or "") for p in exclude_patterns)


def build_client_order_ref(po_number, customer_name, branch_text):
    po = (po_number or "").strip()
    if not po:
        # No PO number on the invoice — fall back to "PO - {branch name}"
        # instead of leaving the number blank.
        return f"PO - {branch_text}" if branch_text else "PO -"
    ref = f"PO - {po}"
    tail = " - ".join([p for p in [customer_name, branch_text] if p])
    if tail:
        ref += f"    {tail}"
    return ref


def documents_for_message(msg, skip_plain_text=False):
    """Splits one email into a list of documents to run extraction on
    separately (a PDF-heavy email may contain several branch orders).

    When skip_plain_text is on, plain-text body emails (no PDF attachment,
    no HTML table) are still returned but tagged with skip_reason so the
    pipeline logs them as skipped instead of running extraction — PDFs and
    Seoudi-style table-in-body orders are unaffected."""
    docs = []
    html = msg["body_html"]
    has_table = "<table" in (html or "").lower()

    if msg["attachments"]:
        for att in msg["attachments"]:
            text, mode = pdf_utils.extract_text_from_pdf(att["content"])
            combined = f"Attachment filename: {att['filename']}\n\n{text}"
            docs.append({
                "source_label": f"pdf_attachment ({mode})",
                "content_text": combined,
                "source_bytes": att["content"],
                "source_filename": att["filename"],
            })
    elif has_table:
        docs.append({
            "source_label": "email_body_table",
            "content_text": html_to_text(html),
            "source_bytes": None,
            "source_filename": None,
        })
    else:
        body_text = html_to_text(html) if html else msg["body_text"]
        docs.append({
            "source_label": "email_body_text",
            "content_text": body_text,
            "source_bytes": None,
            "source_filename": None,
            "skip_reason": "plain_text_body_disabled" if skip_plain_text else None,
        })
    return docs


def extract_document(cfg, subject, sender, doc):
    """Calls Claude to classify + extract one document. Returns (extraction_dict, usage)."""
    return llm_extract.extract_order(
        cfg,
        subject=subject,
        sender=sender,
        source_label=doc["source_label"],
        content_text=doc["content_text"],
    )


def match_and_create(cfg, odoo, aliases, subject, sender, extraction, doc):
    """Attempts customer + product matching and Odoo quotation creation for
    one already-extracted document. Returns a result dict:
      status: skipped | needs_review | created
      reason: (skipped/needs_review only) return | other | customer_not_matched | no_products_matched
      order_id, partner, match_score, unmatched: (created only)
    """
    if extraction["document_type"] != "order":
        return {"status": "skipped", "reason": extraction["document_type"]}

    threshold = cfg["matching"].get("customer_match_threshold", 0.5)
    flag_below = cfg["matching"].get("customer_confidence_flag_below", 0.75)

    partner, score = odoo.find_best_partner(
        extraction.get("customer_name_candidates", []), aliases=aliases, threshold=threshold
    )
    if not partner:
        return {"status": "needs_review", "reason": "customer_not_matched"}

    branch_text = extraction.get("branch_text") or ""
    resolved_branch = _resolve_branch(cfg, odoo, partner, branch_text)
    client_order_ref = build_client_order_ref(extraction.get("po_number"), partner["name"], resolved_branch)

    # Duplicate guard: if a quotation with this exact order reference already
    # exists in Odoo for this customer, don't create it again. This checks Odoo
    # directly, so re-runs (even with a cleared log or --force) only add NEW
    # orders. Deleting the order in Odoo makes it eligible for recreation.
    existing_id = odoo.find_existing_quotation(client_order_ref, partner["id"])
    if existing_id:
        return {"status": "skipped", "reason": "already_exists",
                "existing_order_id": existing_id, "client_order_ref": client_order_ref}

    order_lines, unmatched, name_matched = [], [], []
    for line in extraction.get("lines", []):
        barcode = str(line.get("barcode") or "").strip()
        description = line.get("description") or ""
        product = odoo.find_product_by_barcode(barcode) if barcode else None
        matched_by_name_score = None

        if not product:
            product, matched_by_name_score = odoo.find_product_by_name(description)

        if product:
            order_lines.append((product["id"], line.get("qty") or 0))
            if matched_by_name_score is not None:
                name_matched.append({
                    "barcode": barcode, "description": description,
                    "matched_product": product["name"], "score": matched_by_name_score,
                })
        else:
            unmatched.append(line)

    if not order_lines:
        return {"status": "needs_review", "reason": "no_products_matched"}

    order_id = odoo.create_draft_quotation(partner["id"], client_order_ref, order_lines)
    _post_creation_note(odoo, order_id, subject, sender, unmatched, score, flag_below, extraction, name_matched)
    _attach_source(odoo, order_id, doc)

    return {
        "status": "created", "order_id": order_id, "partner": partner["name"],
        "match_score": score, "unmatched": unmatched,
    }


def create_quotation_manual(cfg, odoo, subject, sender, partner_id, po_number, branch_text,
                             line_items, doc=None):
    """Used by the web UI's 'fix and retry' flow: partner and each product were
    picked explicitly by a person, so no fuzzy matching happens here.
    line_items: list of (product_id, qty)."""
    partner = odoo.get_partner(partner_id)
    if not partner:
        raise ValueError(f"Odoo partner id {partner_id} not found")
    if not line_items:
        raise ValueError("At least one product line is required")

    client_order_ref = build_client_order_ref(po_number, partner["name"], branch_text)
    order_id = odoo.create_draft_quotation(partner["id"], client_order_ref, line_items)
    note = [f"Manually resolved from email: <b>{subject}</b> (from {sender}) via the review dashboard."]
    odoo.post_note("sale.order", order_id, "<br/>".join(note))
    if doc:
        _attach_source(odoo, order_id, doc)
    return order_id


def _resolve_branch(cfg, odoo, partner, branch_text):
    """Resolves the extracted branch clue against the matched customer's real
    Odoo branches (child contacts). Falls back to the parent company's own
    name when nothing matches — per design, never leaves the branch blank."""
    children = odoo.get_children(partner["id"])
    child_names = [c["name"] for c in children]
    if branch_text and child_names:
        matched = llm_extract.match_branch(cfg, branch_text, child_names)
        if matched:
            return matched
    return partner["name"]


def _post_creation_note(odoo, order_id, subject, sender, unmatched, score, flag_below, extraction, name_matched=None):
    note = [f"Auto-created from email: <b>{subject}</b> (from {sender})"]
    if name_matched:
        for m in name_matched:
            note.append(
                f"Matched by product name instead of barcode (barcode '{m['barcode'] or '—'}' not found): "
                f"\"{m['description']}\" → {m['matched_product']} (confidence {m['score']:.2f}) — please verify."
            )
    if unmatched:
        codes = ", ".join(str(l.get("barcode") or "?") for l in unmatched)
        note.append(f"Unmatched barcodes (NOT added to the order — please check): {codes}")
    if score < flag_below:
        note.append(f"Customer match confidence: {score:.2f} — please verify the partner is correct.")
    if extraction.get("confidence_notes"):
        note.append(f"Extraction notes: {extraction['confidence_notes']}")
    odoo.post_note("sale.order", order_id, "<br/>".join(note))


def _attach_source(odoo, order_id, doc):
    if doc and doc.get("source_bytes") and doc.get("source_filename"):
        odoo.attach_file("sale.order", order_id, doc["source_filename"], doc["source_bytes"])


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
ALIASES_PATH = os.path.join(os.path.dirname(__file__), "aliases.json")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_processed(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_processed(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_review_log(path, row):
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["timestamp", "message_id", "subject", "sender", "status", "detail"])
        writer.writerow(row)


def run_pipeline(source, since_date=None, until_date=None, force=False, on_event=None):
    """The single entry point both pipeline.py (CLI) and app.py (web) call.
    source: 'cli' or 'web', recorded on the run for the dashboard's history.
    on_event(dict) is called once per line of "what happened" — pipeline.py
    prints it, app.py collects it into the JSON response. Returns a summary dict.
    """
    def emit(event):
        if on_event:
            on_event(event)

    cfg = load_config()
    processed_path = cfg["paths"]["processed_log"]
    review_path = cfg["paths"]["review_log"]

    store.init_db()
    processed = load_processed(processed_path)
    aliases = load_aliases(ALIASES_PATH)
    exclude_patterns = compile_exclude_patterns(cfg)

    odoo = OdooClient(cfg["odoo"], cfg.get("own_company_keywords", []))
    messages = imap_client.fetch_new_messages(
        cfg["imap"],
        processed,
        lookback_days=cfg["imap"].get("lookback_days", 14),
        since_date=since_date,
        until_date=until_date,
        force=force,
    )
    emit({"type": "info", "message": f"Found {len(messages)} new message(s) to process."})

    run_id = store.start_run(source, since_date, until_date)
    created, needs_review, skipped, excluded, errors = 0, 0, 0, 0, 0

    for msg in messages:
        mid, subject, sender = msg["message_id"], msg["subject"], msg["sender"]

        if is_excluded_subject(subject, exclude_patterns):
            excluded += 1
            emit({"type": "excluded", "subject": subject})
            store.record_item(run_id, message_id=mid, subject=subject, sender=sender,
                               source_label="n/a", status="excluded")
            processed[mid] = {"processed_at": datetime.now(timezone.utc).isoformat(), "results": ["excluded"]}
            if cfg["imap"].get("mark_as_read"):
                imap_client.mark_seen(cfg["imap"], mid)
            continue

        try:
            docs = documents_for_message(msg, skip_plain_text=cfg.get("skip_plain_text_body", False))
            results = []
            for doc in docs:
                # Plain-text body orders are disabled for now (config flag).
                # Log as skipped without spending an AI call.
                if doc.get("skip_reason"):
                    res = {"status": "skipped", "reason": doc["skip_reason"]}
                    results.append(res)
                    store.record_item(
                        run_id, message_id=mid, subject=subject, sender=sender,
                        source_label=doc["source_label"], status="skipped",
                        reason=doc["skip_reason"],
                    )
                    skipped += 1
                    append_review_log(review_path, [
                        datetime.now(timezone.utc).isoformat(), mid, subject, sender,
                        "skipped", doc["skip_reason"],
                    ])
                    emit({"type": "skipped", "subject": subject, "reason": doc["skip_reason"]})
                    continue

                extraction, usage = extract_document(cfg, subject, sender, doc)
                res = match_and_create(cfg, odoo, aliases, subject, sender, extraction, doc)
                results.append(res)

                store.record_item(
                    run_id, message_id=mid, subject=subject, sender=sender,
                    source_label=doc["source_label"], status=res["status"],
                    reason=res.get("reason"), order_id=res.get("order_id"),
                    partner=res.get("partner"), match_score=res.get("match_score"),
                    extraction=extraction, content_text=doc["content_text"], doc=doc,
                )

                if res["status"] in ("skipped", "needs_review"):
                    if res["status"] == "needs_review":
                        needs_review += 1
                    else:
                        skipped += 1
                    append_review_log(review_path, [
                        datetime.now(timezone.utc).isoformat(), mid, subject, sender,
                        res["status"], res.get("reason", ""),
                    ])
                    emit({"type": res["status"], "subject": subject, "reason": res.get("reason")})
                else:
                    created += 1
                    emit({"type": "created", "subject": subject, "order_id": res["order_id"], "partner": res["partner"]})

            processed[mid] = {
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "results": [r["status"] for r in results],
            }
            if cfg["imap"].get("mark_as_read"):
                imap_client.mark_seen(cfg["imap"], mid)

        except Exception as e:
            errors += 1
            store.record_item(run_id, message_id=mid, subject=subject, sender=sender,
                               source_label="n/a", status="error", error_detail=str(e))
            append_review_log(review_path, [
                datetime.now(timezone.utc).isoformat(), mid, subject, sender, "error", str(e),
            ])
            emit({"type": "error", "subject": subject, "detail": str(e)})

    save_processed(processed_path, processed)
    store.finish_run(run_id, len(messages))

    return {
        "run_id": run_id, "message_count": len(messages),
        "created": created, "needs_review": needs_review,
        "skipped": skipped, "excluded": excluded, "errors": errors,
    }
