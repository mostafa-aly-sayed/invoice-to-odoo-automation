"""
Structured extraction of purchase-order content using Claude (Haiku by default).

This is intentionally content-driven, not sender-specific: the same prompt handles
table-in-body emails, single or multiple PDF attachments, Arabic/English text, and
must also flag returns/other non-order content rather than guessing an order out of it.
"""

import json
from anthropic import Anthropic

SYSTEM_PROMPT = """You extract structured data from purchase-order documents sent to \
Naturesta (an Egyptian FMCG distributor/manufacturer) by its retail/wholesale customers. \
Naturesta is always the VENDOR/SUPPLIER in these documents, never the customer.

The documents vary a lot in layout and language (Arabic and/or English) and arrive as either:
- An HTML table in the email body (logo + branch text near the top, then a table of items), or
- One or more PDF attachments (a formal "Purchase Order" layout with labeled fields), or
- Plain text in the email body.

A single email may contain MULTIPLE separate orders (e.g. one PDF attachment per branch/store
that is ordering). You will be given ONE document/table at a time — extract only what is in
the content you were given, not what might be in a sibling attachment.

For the given content, determine and extract:

1. document_type: one of "order", "return", "other".
   - "order": a genuine purchase/sales order requesting goods.
   - "return": a return, credit note, or complaint about goods already delivered
     (look for words like "مرتجع", "return", "credit note", "reject", damaged goods, etc.)
   - "other": anything else (delivery confirmation photos requests, general correspondence,
     an empty/irrelevant table, etc.)
   Do NOT guess "order" just because a table of products is present — check the actual intent.

2. customer_name_candidates: an array of every plausible name string for the company placing
   the order — e.g. text near a logo, an "Invoice To" / "Vendor Name" style field referring to
   the BUYER (not Naturesta), the sending company's brand name, names visible in the subject
   line, etc. Include Arabic and English variants if both appear. This will be fuzzy-matched
   against Odoo customer records, so include every reasonable candidate string rather than
   picking just one.
   IMPORTANT: Do NOT include "Naturesta" / "ناتورستا" / "ناتورستا للاستثمار" (or any close
   variant of Naturesta's own name) in this list, even though it will often appear in the
   subject line or body — that text refers to the VENDOR (the recipient of the order), never
   the buyer. Only include names of the actual ordering company/store.

3. branch_text: the specific branch/store/delivery-location text if present — e.g. a
   "Delivered To" field, a store/location name, a region name near the logo, or text that
   also sometimes appears only in an attachment's filename (which will be given to you as
   part of the content if relevant). Empty string if there is no branch-level distinction.

4. po_number: the purchase order / order reference number, however it's labeled — "PO No",
   "PO -", "Order #", "Filter:(Order # = ...)", "Order Number", etc. Extract just the number/
   reference value itself. Empty string if genuinely absent.

5. lines: an array of every product line item, each with:
   - barcode: the barcode/EAN string exactly as printed (this is the ONLY reliable product
     identifier — Naturesta already matches products by barcode, so this must be accurate).
   - description: the product description text (for human reference only).
   - qty: the ordered quantity as a number (ignore unit price, VAT, and total columns —
     Naturesta's own Odoo pricelist sets the price, only quantity matters here).

6. confidence_notes: brief notes on anything ambiguous, illegible, or uncertain that a human
   reviewer should double check (e.g. "branch text unclear", "OCR quality poor on line 3").

Respond only by calling the record_order tool.
"""

RECORD_ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": ["order", "return", "other"]},
        "customer_name_candidates": {
            "type": "array",
            "items": {"type": "string"},
        },
        "branch_text": {"type": "string"},
        "po_number": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "barcode": {"type": "string"},
                    "description": {"type": "string"},
                    "qty": {"type": "number"},
                },
                "required": ["barcode", "qty"],
            },
        },
        "confidence_notes": {"type": "string"},
    },
    "required": ["document_type", "customer_name_candidates", "lines"],
}


def extract_order(cfg, *, subject, sender, source_label, content_text):
    client = Anthropic(api_key=cfg["anthropic"]["api_key"])
    model = cfg["anthropic"].get("model", "claude-haiku-4-5-20251001")

    user_content = (
        f"Email subject: {subject}\n"
        f"Sender: {sender}\n"
        f"Source: {source_label}\n\n"
        f"--- document content ---\n{content_text}\n"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        tools=[{
            "name": "record_order",
            "description": "Record the structured extraction result for one purchase-order document.",
            "input_schema": RECORD_ORDER_SCHEMA,
        }],
        tool_choice={"type": "tool", "name": "record_order"},
        messages=[{"role": "user", "content": user_content}],
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_order":
            return block.input, resp.usage

    raise RuntimeError(f"No structured extraction returned for: {subject}")


BRANCH_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "matched_branch": {
            "type": ["string", "null"],
            "description": "The exact candidate string that refers to the same physical "
                            "location, or null if none plausibly match.",
        },
    },
    "required": ["matched_branch"],
}


def match_branch(cfg, branch_text, candidate_names):
    """Resolves a branch/delivery-location clue (often in a different language or
    transliteration than Odoo's records) against the real branch names on file
    for the matched customer. Returns the exact matching candidate string, or
    None if nothing plausibly matches. Cheap call — small prompt, Haiku."""
    if not branch_text or not branch_text.strip() or not candidate_names:
        return None

    client = Anthropic(api_key=cfg["anthropic"]["api_key"])
    model = cfg["anthropic"].get("model", "claude-haiku-4-5-20251001")

    prompt = (
        f'A purchase order names this delivery/branch location: "{branch_text}"\n\n'
        "Here are the actual branch names on file for this customer in Odoo "
        "(may be Arabic, English, or a mix):\n"
        + "\n".join(f"- {n}" for n in candidate_names)
        + "\n\nWhich one refers to the same physical location? Match by meaning/place, "
          "not spelling — e.g. an English store name and its Arabic branch name can refer "
          "to the same place. If none plausibly match, return null."
    )

    resp = client.messages.create(
        model=model, max_tokens=200,
        tools=[{
            "name": "match_branch",
            "description": "Report which candidate branch name matches the given location, if any.",
            "input_schema": BRANCH_MATCH_SCHEMA,
        }],
        tool_choice={"type": "tool", "name": "match_branch"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "match_branch":
            matched = block.input.get("matched_branch")
            return matched if matched in candidate_names else None
    return None
