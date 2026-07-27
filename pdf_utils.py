"""
PDF text extraction with an OCR fallback, since invoices arrive as a mix of
digitally generated PDFs and scanned images (per naturesta.com sample set).
"""

import io
import pdfplumber


def extract_text_from_pdf(pdf_bytes, ocr_lang="ara+eng"):
    """Returns (text, mode) where mode is 'digital', 'ocr', or 'ocr_failed:<error>'."""
    text_parts = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
    except Exception as e:
        return "", f"digital_failed:{e}"

    text = "\n".join(text_parts).strip()
    if len(text) >= 20:
        return text, "digital"

    # Likely a scanned/image PDF — fall back to OCR.
    try:
        from pdf2image import convert_from_bytes
        import pytesseract

        images = convert_from_bytes(pdf_bytes)
        ocr_text = "\n".join(
            pytesseract.image_to_string(img, lang=ocr_lang) for img in images
        )
        return ocr_text.strip(), "ocr"
    except Exception as e:
        return text, f"ocr_failed:{e}"
