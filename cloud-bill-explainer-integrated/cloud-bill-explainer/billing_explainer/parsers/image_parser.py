"""OCR parser for JPG/PNG screenshots and scanned billing images."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError
import pytesseract

from billing_explainer.models import InvoiceSummary
from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.invoice_text import extract_invoice_fields

_MAX_PIXELS = 40_000_000
_PREVIEW_CHARS = 100_000


class ImageParser(BaseParser):
    file_types = ("png", "jpg")

    def parse(self, data: bytes, options: ParseOptions) -> ParseResult:
        try:
            with Image.open(BytesIO(data)) as opened:
                if opened.width * opened.height > _MAX_PIXELS:
                    return ParseResult(issues=[Issue(Severity.FAILED, "image_too_large",
                                                    "Image resolution is too large to process safely.")])
                image = ImageOps.exif_transpose(opened).convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return ParseResult(issues=[Issue(Severity.FAILED, "invalid_image",
                                            f"Image could not be opened: {type(exc).__name__}.")])

        try:
            # Keep OCR deterministic and plain-text only; structured rows must be reviewed.
            text = pytesseract.image_to_string(image, lang="eng", config="--psm 6")
        except Exception as exc:
            return ParseResult(issues=[Issue(Severity.FAILED, "ocr_unavailable",
                                            "Image OCR failed. Ensure Tesseract OCR is installed and available on PATH "
                                            f"(details: {type(exc).__name__}).")])

        text = (text or "").strip()
        if not text:
            return ParseResult(issues=[Issue(Severity.FAILED, "empty_ocr",
                                            "OCR did not find readable text in this image.")])
        summary, start, end, account = extract_invoice_fields(text)
        issue = Issue(Severity.REVIEW, "image_ocr_review",
                      "OCR text was extracted, but screenshot amounts and table rows must be reviewed by a person before use.")
        preview = text[:_PREVIEW_CHARS]
        if len(text) > _PREVIEW_CHARS:
            preview += "\n... [preview truncated]"
        return ParseResult(
            line_items=[], extracted_text=preview, issues=[issue], account_alias=account,
            currency=summary.currency, period_start=start, period_end=end, summary=summary,
        )
