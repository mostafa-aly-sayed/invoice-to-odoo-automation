"""
Odoo XML-RPC client for the invoice-to-quotation pipeline.

Talks directly to Odoo's /xmlrpc/2/* endpoints (not the Odoo MCP connector — this
script runs standalone, outside the Cowork session, so it needs its own credentials).
"""

import base64
import difflib
import json
import os
import re
import xmlrpc.client


class OdooClient:
    def __init__(self, cfg, own_company_keywords=None):
        self.url = cfg["url"].rstrip("/")
        self.db = cfg["db"]
        self.username = cfg["username"]
        self.api_key = cfg["api_key"]
        # Names containing any of these are never eligible to match as a customer —
        # prevents the vendor's own company (mentioned in most PO subjects/bodies as
        # the recipient) from ever being mistaken for the buyer.
        self.own_company_keywords = [k.lower() for k in (own_company_keywords or [])]

        self.common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self.uid = self.common.authenticate(self.db, self.username, self.api_key, {})
        if not self.uid:
            raise RuntimeError(
                "Odoo authentication failed — check odoo.username/api_key/db/url in config.json"
            )
        self.models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

        self._partner_cache = None

    def execute(self, model, method, *args, **kwargs):
        return self.models.execute_kw(
            self.db, self.uid, self.api_key, model, method, list(args), kwargs
        )

    # ---- Customer matching -------------------------------------------------

    def _is_own_company(self, name):
        n = (name or "").lower()
        return any(kw in n for kw in self.own_company_keywords)

    def _all_customers(self):
        if self._partner_cache is None:
            partners = self.execute(
                "res.partner",
                "search_read",
                ["|", ["customer_rank", ">", 0], ["parent_id", "=", False]],
                fields=["id", "name", "parent_id"],
            )
            self._partner_cache = [p for p in partners if not self._is_own_company(p["name"])]
        return self._partner_cache

    def find_best_partner(self, name_candidates, aliases=None, threshold=0.5):
        """Fuzzy-matches candidate name strings against Odoo customer records.
        Checks an optional alias map first (exact text -> exact Odoo partner name)."""
        aliases = aliases or {}
        partners = self._all_customers()

        for cand in name_candidates:
            key = cand.strip().lower()
            if key in aliases:
                target_name = aliases[key]
                for p in partners:
                    if p["name"].strip().lower() == target_name.strip().lower():
                        return p, 1.0

        best, best_score = None, 0.0
        for cand in name_candidates:
            cand_norm = cand.strip().lower()
            if not cand_norm:
                continue
            for p in partners:
                score = difflib.SequenceMatcher(
                    None, cand_norm, p["name"].strip().lower()
                ).ratio()
                if score > best_score:
                    best_score, best = score, p

        if best and best_score >= threshold:
            return best, best_score
        return None, best_score

    def search_customers(self, query, limit=20):
        """Name-substring search for the UI's customer picker dropdown."""
        if not query or not query.strip():
            return self._all_customers()[:limit]
        results = self.execute(
            "res.partner", "search_read",
            ["&", "|", ["customer_rank", ">", 0], ["parent_id", "=", False],
             ["name", "ilike", query.strip()]],
            fields=["id", "name", "parent_id"], limit=limit,
        )
        return [p for p in results if not self._is_own_company(p["name"])]

    def get_children(self, parent_id):
        return self.execute(
            "res.partner", "search_read", [["parent_id", "=", parent_id]],
            fields=["id", "name"],
        )

    # ---- Product matching ---------------------------------------------------

    def find_product_by_barcode(self, barcode):
        res = self.execute(
            "product.product", "search_read", [["barcode", "=", barcode]],
            fields=["id", "name"],
        )
        return res[0] if res else None

    def find_product_by_name(self, description, threshold=0.55, limit=30, lang="ar_001"):
        """Fallback when barcode matching fails: fuzzy-matches the invoice line's
        description against Odoo product names. Uses the Arabic-translated name
        (Odoo has these set up) since invoice descriptions are typically Arabic
        while the base product name is English. Only considers products that
        actually have a barcode, to avoid matching label/internal SKUs."""
        if not description or not description.strip():
            return None, 0.0
        query = description.strip()

        words = [w for w in re.split(r"[\s\-,،]+", query) if len(w) >= 3]
        candidates = {}
        for w in words[:4]:
            res = self.execute(
                "product.product", "search_read",
                ["&", ["barcode", "!=", False], ["name", "ilike", w]],
                fields=["id", "name", "barcode"], limit=limit,
                context={"lang": lang},
            )
            for c in res:
                candidates[c["id"]] = c

        if not candidates:
            return None, 0.0

        best, best_score = None, 0.0
        q_norm = query.lower()
        for c in candidates.values():
            score = difflib.SequenceMatcher(None, q_norm, c["name"].lower()).ratio()
            if score > best_score:
                best_score, best = score, c

        if best and best_score >= threshold:
            return best, best_score
        return None, best_score

    def search_products(self, query, limit=20):
        """Name/barcode substring search for the UI's product picker dropdown."""
        if not query or not query.strip():
            return []
        q = query.strip()
        return self.execute(
            "product.product", "search_read",
            ["|", ["name", "ilike", q], ["barcode", "ilike", q]],
            fields=["id", "name", "barcode"], limit=limit,
        )

    def get_partner(self, partner_id):
        res = self.execute(
            "res.partner", "search_read", [["id", "=", partner_id]],
            fields=["id", "name"],
        )
        return res[0] if res else None

    # ---- Quotation creation ---------------------------------------------------

    def create_draft_quotation(self, partner_id, client_order_ref, order_lines):
        """order_lines: list of (product_id, qty). Leaves state as default draft quotation."""
        vals = {
            "partner_id": partner_id,
            "client_order_ref": client_order_ref,
            "order_line": [
                (0, 0, {"product_id": pid, "product_uom_qty": qty})
                for pid, qty in order_lines
            ],
        }
        return self.execute("sale.order", "create", vals)

    def post_note(self, model, res_id, body_html):
        self.execute(model, "message_post", res_id, **{"body": body_html})

    def attach_file(self, model, res_id, filename, content_bytes, mimetype="application/pdf"):
        self.execute("ir.attachment", "create", {
            "name": filename,
            "res_model": model,
            "res_id": res_id,
            "type": "binary",
            "datas": base64.b64encode(content_bytes).decode("ascii"),
            "mimetype": mimetype,
        })


def load_aliases(path="aliases.json"):
    """aliases.json maps a lowercase extracted-name string to the exact Odoo partner
    name it should resolve to. Optional, grows over time as mismatches are corrected —
    this is a data lookup, not per-sender code logic."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k.strip().lower(): v for k, v in raw.items()}
    return {}
