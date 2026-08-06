"""
IMAP fetching for the invoice-to-quotation pipeline.

Deliberately does NOT rely on the \\Seen flag to decide what's "new" — a human may
also read this mailbox in Outlook. Instead we search a rolling window (lookback_days)
and de-duplicate using the Message-ID header against processed_emails.json, which the
pipeline maintains. \\Seen is only set afterwards as a convenience marker for humans,
controlled by config["mark_as_read"].
"""

import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta


def _decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def _connect(cfg):
    imap = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    imap.login(cfg["username"], cfg["password"])
    return imap


def _imap_date(dt):
    # IMAP SEARCH SINCE wants "DD-Mon-YYYY"
    return dt.strftime("%d-%b-%Y")


def fetch_new_messages(cfg, processed_ids, lookback_days=14, since_date=None, until_date=None, force=False):
    """Returns a list of dicts: message_id, subject, sender, date, body_html,
    body_text, attachments (list of {filename, content}), imap_num.

    since_date/until_date (datetime.date) let you target a specific window
    (e.g. "just yesterday") instead of the rolling lookback_days window.
    IMAP SINCE/BEFORE are day-granularity and BEFORE is exclusive."""
    imap = _connect(cfg)
    imap.select(cfg.get("folder", "INBOX"))

    since = _imap_date(since_date) if since_date else _imap_date(
        (datetime.utcnow() - timedelta(days=lookback_days)).date()
    )
    search_parts = [f'SINCE "{since}"']
    if until_date:
        search_parts.append(f'BEFORE "{_imap_date(until_date)}"')

    status, data = imap.search(None, "(" + " ".join(search_parts) + ")")
    if status != "OK":
        imap.logout()
        raise RuntimeError(f"IMAP search failed: {status}")

    ids = data[0].split()
    messages = []

    for num in ids:
        status, msg_data = imap.fetch(num, "(RFC822)")
        if status != "OK" or not msg_data or msg_data[0] is None:
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        msg_id = msg.get("Message-ID") or f"noid-{num.decode()}-{msg.get('Date')}"
        if msg_id in processed_ids and not force:
            continue

        subject = _decode_str(msg.get("Subject"))
        sender = _decode_str(msg.get("From"))
        date_hdr = msg.get("Date")

        body_html, body_text = "", ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                filename = part.get_filename()

                # Key off the filename, not Content-Disposition: some forwarded
                # messages carry a PDF without ever setting "attachment" on the
                # disposition header, which previously caused the PDF to be
                # silently dropped (and the email misread as having no content).
                if filename:
                    fname = _decode_str(filename)
                    if fname.lower().endswith(".pdf"):
                        content = part.get_payload(decode=True)
                        if content:
                            attachments.append({"filename": fname, "content": content})
                    continue

                if ctype == "text/html" and "attachment" not in disp.lower():
                    try:
                        body_html += part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    except Exception:
                        pass
                elif ctype == "text/plain" and "attachment" not in disp.lower():
                    try:
                        body_text += part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    except Exception:
                        pass
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    body_html = text
                else:
                    body_text = text

        messages.append({
            "message_id": msg_id,
            "subject": subject,
            "sender": sender,
            "date": date_hdr,
            "body_html": body_html,
            "body_text": body_text,
            "attachments": attachments,
            "imap_num": num,
        })

    imap.logout()
    return messages


def mark_seen(cfg, message_id):
    """Best-effort: reconnect and flag a specific Message-ID as \\Seen."""
    try:
        imap = _connect(cfg)
        imap.select(cfg.get("folder", "INBOX"))
        # Message-ID may contain characters IMAP HEADER search needs quoted as-is.
        status, data = imap.search(None, f'(HEADER Message-ID "{message_id}")')
        if status == "OK":
            for num in data[0].split():
                imap.store(num, "+FLAGS", "\\Seen")
        imap.logout()
    except Exception:
        # Non-fatal — dedup is handled by processed_emails.json regardless.
        pass
