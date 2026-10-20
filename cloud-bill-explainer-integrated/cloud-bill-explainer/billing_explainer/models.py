"""Plain data classes and money helpers shared by every layer.

Why Decimal? 0.1 + 0.2 != 0.3 in binary floating point. Invoice reconciliation must
add up to the cent (or sub-cent: cloud unit prices go to ~10 decimal places), so all
money is Decimal in Python and exact decimal TEXT in SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    PROCESSED = "processed"
    FAILED = "failed"


class ChargeType(str, Enum):
    USAGE = "usage"
    CREDIT = "credit"
    TAX = "tax"
    ADJUSTMENT = "adjustment"
    FEE = "fee"
    OTHER = "other"


class ItemStatus(str, Enum):
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"


class ReconciliationStatus(str, Enum):
    NOT_CHECKED = "not_checked"
    RECONCILED = "reconciled"
    MISMATCH = "mismatch"
    INCOMPLETE = "incomplete"


# ---------- money / date helpers ----------

def to_money(value: "Decimal | int | str") -> Decimal:
    """Convert to a finite Decimal. Floats are rejected on purpose: a float has
    already lost precision before it reaches us, so we refuse rather than guess."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"Refusing {type(value).__name__} for money; use str, int or Decimal.")
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid decimal amount: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"Amount must be finite: {value!r}")
    return d


def money_to_db(value: "Decimal | int | str | None") -> str | None:
    """Decimal -> plain text without scientific notation ('1E+2' -> '100')."""
    return None if value is None else format(to_money(value), "f")


def money_from_db(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso_date(value: "date | str | None") -> str | None:
    """Accept a date or an ISO string; always return 'YYYY-MM-DD' or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(value).isoformat()  # raises ValueError if malformed


# ---------- records ----------

@dataclass(frozen=True)
class User:
    id: int
    auth_provider: str
    provider_subject: str
    email: str | None
    created_at: str


@dataclass(frozen=True)
class Document:
    id: int
    user_id: int
    original_filename: str
    storage_path: str
    sha256: str
    file_type: str
    file_size_bytes: int
    uploaded_at: str
    status: DocumentStatus
    account_alias: str | None = None
    billing_period_start: str | None = None
    billing_period_end: str | None = None
    extracted_text: str | None = None
    validation_notes: list[str] = field(default_factory=list)
    supersedes_document_id: int | None = None
    verified_at: str | None = None
    cloud_provider: str | None = None


@dataclass
class LineItem:
    """Normalized billing record. Optional fields are None when the source lacks them."""
    cost_amount: Decimal
    currency: str
    source_locator: str
    service: str | None = None
    region: str | None = None
    resource_id: str | None = None
    charge_type: ChargeType = ChargeType.USAGE
    usage_quantity: Decimal | None = None
    usage_unit: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    raw_record: str | None = None
    status: ItemStatus = ItemStatus.EXTRACTED
    id: int | None = None
    document_id: int | None = None


@dataclass
class InvoiceSummary:
    document_id: int
    invoice_number: str | None = None
    currency: str | None = None
    usage_total: Decimal | None = None
    credit_total: Decimal | None = None
    tax_total: Decimal | None = None
    other_adjustments: Decimal | None = None
    invoice_total: Decimal | None = None
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.NOT_CHECKED
    reconciliation_notes: str | None = None
    uncertain_fields: list[str] = field(default_factory=list)
