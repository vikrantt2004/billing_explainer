"""Conservative text-level invoice field extraction shared by PDF and image parsers.

This module intentionally extracts only explicit, label-adjacent values. It is not an
accounting engine: uncertain fields stay NULL and are shown to the user for review.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from billing_explainer.models import InvoiceSummary, ReconciliationStatus
from billing_explainer.parsers.values import AmountParseError, normalize_currency, parse_amount

_MAX_TEXT = 500_000
_AMOUNT_RE = re.compile(
    r"(?<![\w/])(?:\(?\s*-?\s*(?:USD|EUR|GBP|INR|CAD|AUD|JPY|CHF)\s*)?"
    r"(?:[$€£₹]\s*)?\d[\d,]*(?:\.\d+)?\s*\)?-?",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(r"\b(USD|EUR|GBP|INR|CAD|AUD|JPY|CHF|NZD|SGD)\b", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"(?:billing|invoice|usage)\s+period\s*[:\-]?\s*"
    r"(\d{4}-\d{2}-\d{2})\s*(?:to|through|until|–|—|\s+-\s+)\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_INVOICE_NO_RE = re.compile(r"\b(?:invoice\s*(?:number|no\.?|#|id))\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/_\-]{2,})", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"\b(?:account\s*(?:id|name|alias|number))\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/_\-.]{2,})", re.IGNORECASE)

_LABELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("invoice_total", re.compile(r"\b(?:invoice\s+total|amount\s+due|total\s+due|grand\s+total|total\s+payable|balance\s+due|total\s+amount)\b", re.I)),
    ("credit_total", re.compile(r"\b(?:promotional\s+credits?|credits?\s+(?:applied|total)|total\s+credits?)\b", re.I)),
    ("tax_total", re.compile(r"\b(?:tax(?:es)?|vat|gst|sales\s+tax)\b", re.I)),
    ("other_adjustments", re.compile(r"\b(?:other\s+adjustments?|adjustments?|other\s+charges|fees)\b", re.I)),
    ("usage_total", re.compile(r"\b(?:usage\s+(?:subtotal|total|charges)|total\s+usage|charges\s+before\s+tax)\b", re.I)),
)


def _amount_after_label(line: str, label_match: re.Match[str]) -> Decimal | None:
    # Prefer the amount after the label, avoiding invoice numbers or dates earlier on
    # the same line. If the document uses "$120.00 usage total", try the whole line.
    candidates = list(_AMOUNT_RE.finditer(line[label_match.end():]))
    if not candidates:
        candidates = list(_AMOUNT_RE.finditer(line[:label_match.start()]))
    if not candidates:
        return None
    raw = candidates[-1].group(0).strip()
    # Ignore ISO currency token if the regex included it; parse_amount handles symbols.
    raw = re.sub(r"^(?:USD|EUR|GBP|INR|CAD|AUD|JPY|CHF)\s*", "", raw, flags=re.I)
    try:
        return parse_amount(raw)
    except AmountParseError:
        return None


def extract_invoice_fields(text: str, document_id: int = 0) -> tuple[InvoiceSummary, str | None, str | None, str | None]:
    """Return (summary, period_start, period_end, account_alias) with unknowns left blank."""
    text = (text or "")[:_MAX_TEXT]
    currency_matches = _CURRENCY_RE.findall(text)
    currencies = {normalize_currency(x) for x in currency_matches}
    currencies.discard(None)
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    period_start = period_end = account_alias = invoice_number = None
    pm = _PERIOD_RE.search(text)
    if pm:
        try:
            start, end = date.fromisoformat(pm.group(1)), date.fromisoformat(pm.group(2))
            if end >= start:
                period_start, period_end = start.isoformat(), end.isoformat()
        except ValueError:
            pass
    im = _INVOICE_NO_RE.search(text)
    if im:
        invoice_number = im.group(1).strip()
    am = _ACCOUNT_RE.search(text)
    if am:
        account_alias = am.group(1).strip()

    found: dict[str, Decimal] = {}
    for line in text.splitlines():
        # A line can contain multiple labels; process the first matching label by type.
        for field, pattern in _LABELS:
            match = pattern.search(line)
            if not match:
                continue
            amount = _amount_after_label(line, match)
            if amount is not None:
                # Credits are represented as negative line items/summaries downstream.
                if field == "credit_total":
                    amount = -abs(amount)
                found[field] = amount
            break

    uncertain = [key for key in ("usage_total", "credit_total", "tax_total", "other_adjustments", "invoice_total") if key not in found]
    if currency is None:
        uncertain.append("currency")
    if period_start is None or period_end is None:
        uncertain.append("billing_period")
    summary = InvoiceSummary(
        document_id=document_id,
        invoice_number=invoice_number,
        currency=currency,
        usage_total=found.get("usage_total"),
        credit_total=found.get("credit_total"),
        tax_total=found.get("tax_total"),
        other_adjustments=found.get("other_adjustments"),
        invoice_total=found.get("invoice_total"),
        reconciliation_status=ReconciliationStatus.NOT_CHECKED,
        reconciliation_notes="Extracted from document text; confirm fields before relying on them.",
        uncertain_fields=uncertain,
    )
    return summary, period_start, period_end, account_alias
