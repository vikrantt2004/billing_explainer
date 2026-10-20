"""PDF parser: extract page text, use OCR for scanned pages, and normalize detectable tables."""
from __future__ import annotations

import json
import re

import fitz

from billing_explainer.models import InvoiceSummary
from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.invoice_text import extract_invoice_fields
from billing_explainer.parsers.tabular import ALIASES, normalize_header, normalize_table

_PREVIEW_CHARS = 100_000
_MAX_PAGES = 500


class PdfParser(BaseParser):
    file_types = ("pdf",)

    def parse(self, data: bytes, options: ParseOptions) -> ParseResult:
        try:
            document = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            return ParseResult(issues=[Issue(Severity.FAILED, "invalid_pdf",
                                            f"PDF could not be opened: {type(exc).__name__}.")])

        all_text: list[str] = []
        issues: list[Issue] = []
        all_items = []
        table_found = False
        try:
            if document.needs_pass:
                return ParseResult(issues=[Issue(Severity.FAILED, "encrypted_pdf",
                                                "PDF is encrypted or password-protected; upload an unlocked copy.")])
            if len(document) == 0:
                return ParseResult(issues=[Issue(Severity.FAILED, "empty_pdf", "PDF contains no pages.")])
            if len(document) > _MAX_PAGES:
                return ParseResult(issues=[Issue(Severity.FAILED, "too_many_pages",
                                                f"PDF has more than {_MAX_PAGES} pages; split it before uploading.")])

            for page_no, page in enumerate(document, start=1):
                page_text = page.get_text("text") or ""
                if len(page_text.strip()) < 20:
                    try:
                        textpage = page.get_textpage_ocr(language="eng", dpi=200, full=True)
                        ocr_text = page.get_text("text", textpage=textpage) or ""
                        if len(ocr_text.strip()) > len(page_text.strip()):
                            page_text = ocr_text
                            issues.append(Issue(Severity.INFO, "pdf_ocr_used",
                                                "OCR was used on a page with little embedded text.", f"page {page_no}"))
                    except Exception:
                        if not page_text.strip():
                            issues.append(Issue(Severity.REVIEW, "pdf_ocr_unavailable",
                                                "This page appears scanned, but OCR could not run. Install Tesseract and verify its language data.",
                                                f"page {page_no}"))
                if page_text.strip():
                    all_text.append(f"--- Page {page_no} ---\n{page_text.strip()}")
                # Table detection is best-effort and only activates when a header row
                # resembles a cloud billing export (e.g. includes a cost column).
                try:
                    tables = page.find_tables().tables
                except Exception:
                    tables = []
                for table_no, table in enumerate(tables, start=1):
                    try:
                        rows = table.extract() or []
                    except Exception:
                        continue
                    if not rows:
                        continue
                    header_idx = _find_header_row(rows)
                    if header_idx is None or header_idx >= len(rows) - 1:
                        continue
                    table_found = True
                    headers = [str(c or "").strip() for c in rows[header_idx]]
                    body = [[str(c or "").strip() for c in row] for row in rows[header_idx + 1:]]
                    normalized = normalize_table(headers, body, options,
                                                  locator_prefix=f"page {page_no} table {table_no} ")
                    all_items.extend(normalized.line_items)
                    issues.extend(normalized.issues)
        finally:
            document.close()

        text = "\n\n".join(all_text).strip()
        if not text:
            return ParseResult(issues=[Issue(Severity.FAILED, "no_pdf_text",
                                            "No readable text was found in the PDF. If it is scanned, check that Tesseract OCR is installed.")])

        summary, start, end, account = extract_invoice_fields(text)
        if not table_found or not all_items:
            issues.append(Issue(Severity.REVIEW, "pdf_invoice_review",
                                "PDF text was extracted, but no reliable service/region line-item table was normalized. "
                                "Confirm the invoice fields and upload a detailed CSV/XLSX export for full attribution."))
        if summary.invoice_total is None:
            issues.append(Issue(Severity.REVIEW, "invoice_total_uncertain",
                                "Could not confidently identify an invoice total from the PDF text; confirm it manually."))

        preview = text[:_PREVIEW_CHARS]
        if len(text) > _PREVIEW_CHARS:
            preview += "\n... [preview truncated]"
        return ParseResult(
            line_items=all_items, extracted_text=preview, issues=issues, account_alias=account,
            currency=summary.currency, period_start=start, period_end=end, summary=summary,
        )


def _find_header_row(rows: list[list[str | None]]) -> int | None:
    """Look in the first few table rows for a recognizable cost/amount header."""
    for idx, row in enumerate(rows[:8]):
        normalized = {normalize_header(str(cell or "")) for cell in row}
        if normalized & ALIASES["cost"]:
            return idx
    return None
