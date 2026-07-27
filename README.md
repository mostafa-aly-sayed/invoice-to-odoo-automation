# Invoice email → Odoo draft quotation

Reads new order emails from `salesorder@naturesta.com`, extracts customer / branch / PO
number / product lines (matched by barcode) using Claude, and creates a **draft**
quotation in Odoo for review. Returns and non-order content are skipped and logged —
nothing is auto-created for them.

This runs as a standalone script on your own computer/server (not inside Cowork), since
Cowork's sandbox can't make a direct IMAP connection to a self-hosted mailbox.

## 1. Install

```bash
pip install -r requirements.txt
```

OCR fallback (`pdf2image` + `pytesseract`) additionally needs system packages if you
expect scanned/image PDFs:
- Windows: install [Tesseract-OCR](https://github.com/UB-Mannheim/tesseract/wiki) (include
  the Arabic language pack) and [poppler for Windows](https://github.com/oschwartz10612/poppler-windows),
  then add both `bin` folders to your PATH.
- Linux: `apt install poppler-utils tesseract-ocr tesseract-ocr-ara`

## 2. Set up config.json

This repo ships `config.example.json` (safe to commit — no real secrets) instead of
`config.json` (gitignored, holds real credentials, never pushed). Copy it and fill
in your values:

```bash
cp config.example.json config.json
```

Then edit `config.json`:
- `imap.password` — the salesorder@naturesta.com mailbox password
- `odoo.username` — the Odoo login email tied to the API key below
- `odoo.api_key` — generate one in Odoo: your user's avatar menu → **My Profile** →
  **Account Security** tab → **New API Key**
- `anthropic.api_key` — from console.anthropic.com

## 3. Test manually first

```bash
python pipeline.py
```

Check the console output, then check Odoo for the created draft quotations, and check
`needs_review.csv` for anything skipped (returns, unmatched customers/products). Do this
a few times against real mail before scheduling it unattended — it's much easier to
tune `aliases.json` and the match threshold now than after it's running on a timer.

`aliases.json` is optional: if a customer keeps matching to the wrong (or no) Odoo
partner, add an entry mapping the extracted name text (lowercase) to the exact Odoo
partner name. This is a data lookup, not logic tied to any specific sender.

## 4. Schedule it (keeps running in the background)

**Windows (Task Scheduler):**
1. Open Task Scheduler → Create Task
2. Trigger: e.g. "Daily", repeat every 30–60 minutes
3. Action: `Start a program` → Program: your `python.exe` path → Arguments:
   `pipeline.py` → Start in: this folder

**Linux/Mac (cron):**
```
*/30 * * * * cd /path/to/invoice_to_odoo && /usr/bin/python3 pipeline.py >> run.log 2>&1
```

Runs from the schedule and from the web dashboard (below) share the same dedup log
and history, so nothing gets double-processed regardless of which one triggered it.

## 5. The web dashboard (no command line needed day-to-day)

For anyone who'd rather click buttons than run commands (e.g. Mahmoud), there's a
local dashboard on top of the same pipeline:

```bash
python app.py
```

Then open **http://127.0.0.1:5000** in a browser. It only runs on this computer —
nothing is exposed to the network, so there's no login. Keep the terminal window
open while using it; closing it stops the dashboard (the scheduled Task
Scheduler/cron run keeps working independently either way).

From the dashboard you can:
- Click **Run now**, optionally pick a specific day/date range, and watch results
  live (created / needs review / skipped / excluded / errors).
- See everything flagged under **Needs review**, and fix it right there: search
  for the correct Odoo customer, confirm/adjust each product line, then click
  **Create quotation** — no file editing required.
- Browse **Run history** — every run from the dashboard, the command line, and
  the schedule, all in one place.
- Manage **Customer aliases** — search for the right Odoo customer and save the
  mapping, instead of hand-editing `aliases.json`.

A convenience shortcut: create a `.bat` file (Windows) with:
```bat
cd /d "C:\path\to\invoice_to_odoo"
python app.py
```
Double-clicking it starts the dashboard without opening a terminal manually
(a console window will still appear — that's normal, it's the server running).

## Files

| File | Purpose |
|---|---|
| `config.json` | credentials + settings |
| `aliases.json` | customer-name normalization lookup (editable via the dashboard or by hand) |
| `imap_client.py` | fetches new mail, dedupes via `processed_emails.json` |
| `pdf_utils.py` | PDF text extraction, OCR fallback |
| `llm_extract.py` | Claude-based structured extraction (order/return/other, customer, branch, PO, barcode+qty lines) |
| `odoo_client.py` | Odoo XML-RPC: partner/product search & matching, draft quotation creation, attachments, chatter notes |
| `core.py` | shared run logic used by both `pipeline.py` and `app.py` |
| `store.py` | local SQLite history + needs-review queue (`app_data.db`, `review_attachments/`) |
| `pipeline.py` | command-line entry point (also what Task Scheduler/cron runs) |
| `app.py` | local web dashboard — run this, then open the browser |
| `static/` | dashboard frontend (plain HTML/CSS/JS, no build step) |
| `processed_emails.json` | auto-generated — tracks which emails were already handled |
| `needs_review.csv` | auto-generated plain-text log (the dashboard's Needs Review tab is the main way to act on these now) |
| `app_data.db` | auto-generated SQLite database backing the dashboard |

## Notes on the design

- `partner_id` is set to the parent company; the branch is written into
  `client_order_ref` as `PO - {number}    {customer} - {branch}`, matching the
  existing convention seen in orders S00093–S00096.
- Only barcode + quantity are taken from the invoice — pricing always comes from
  Odoo's own pricelist for that customer.
- Everything lands as a **draft** quotation; nothing gets confirmed automatically.
