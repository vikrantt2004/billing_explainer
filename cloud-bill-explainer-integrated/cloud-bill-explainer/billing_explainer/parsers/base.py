"""The contract every format parser follows.

A parser is a PURE function of (bytes, options): it never touches the database or disk.
That makes it easy to test, and lets us re-run it with different options (e.g. after the
user picks a currency or a column) without side effects. `processing.py` does the saving.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Mapping

from billing_explainer.models import InvoiceSummary, LineItem


class Severity(str, Enum):
    FAILED = "failed"      # file could not be read at all
    BLOCKING = "blocking"  # readable, but user input is needed before any record is usable
    REVIEW = "review"      # records produced, but the user should confirm something
    INFO = "info"          # FYI (e.g. a totals row was skipped)


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str              # stable machine-readable id, e.g. 'missing_currency'
    message: str           # human-readable
    locator: str | None = None  # e.g. 'row 14'

    def __str__(self) -> str:
        where = f" ({self.locator})" if self.locator else ""
        return f"[{self.severity.value}] {self.message}{where}"


@dataclass(frozen=True)
class ParseOptions:
    """User-supplied hints that make a re-parse succeed where the first try needed input."""
    currency: str | None = None  # fallback ISO code when the file does not state one
    column_overrides: Mapping[str, "str | int"] = field(default_factory=dict)
    # ^ canonical field -> exact header text or 0-based column index
    sheet_name: str | None = None  # used by the Excel parser (later stage)


@dataclass
class ParseResult:
    line_items: list[LineItem] = field(default_factory=list)
    extracted_text: str | None = None
    issues: list[Issue] = field(default_factory=list)
    account_alias: str | None = None
    cloud_provider: str | None = None
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    summary: InvoiceSummary | None = None  # PDF invoices fill this in; CSV usually can't

    def has(self, severity: Severity) -> bool:
        return any(i.severity == severity for i in self.issues)


class BaseParser(ABC):
    file_types: ClassVar[tuple[str, ...]]

    @abstractmethod
    def parse(self, data: bytes, options: ParseOptions) -> ParseResult:
        """Turn raw file bytes into normalized records. Must not raise for bad INPUT;
        report it as an Issue instead. (Unexpected bugs may still raise; the caller
        records them as a failed document without losing the file.)"""
