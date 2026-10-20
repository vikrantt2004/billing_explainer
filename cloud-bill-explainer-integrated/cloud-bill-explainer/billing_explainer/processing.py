"""Run a parser over a stored document and persist the outcome.

Status rules (computed from the parser's issues):
  any FAILED issue    -> 'failed'        (original kept; earlier saved items untouched)
  any BLOCKING issue  -> 'needs_review'  (user input needed; nothing new saved)
  any REVIEW issue    -> 'needs_review'  (records saved, flagged for confirmation)
  otherwise           -> 'processed'
All database writes for one run happen in ONE transaction, so a crash can't leave a
document half-updated. Re-running is safe: line items are replaced, never appended.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace

from billing_explainer import repository as repo
from billing_explainer.config import Settings
from billing_explainer.database import transaction
from billing_explainer.ingest import read_original_bytes
from billing_explainer.models import Document, DocumentStatus
from billing_explainer.parsers import PARSERS, get_parser
from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, Severity

_MAX_NOTES = 100


@dataclass(frozen=True)
class ProcessResult:
    document: Document
    issues: list[Issue] = field(default_factory=list)
    items_saved: int = 0


def process_document(conn: sqlite3.Connection, settings: Settings, user_id: int,
                     document_id: int, options: ParseOptions | None = None,
                     registry: dict[str, BaseParser] | None = None) -> ProcessResult:
    options = options or ParseOptions()
    doc = repo.get_document(conn, user_id, document_id)  # ownership check
    parser = get_parser(doc.file_type, registry if registry is not None else PARSERS)
    if parser is None:  # format not supported yet: leave the document as it is
        note = Issue(Severity.INFO, "no_parser", f"No parser for '{doc.file_type}' files is available yet.")
        return ProcessResult(doc, [note])

    repo.set_document_status(conn, user_id, document_id, DocumentStatus.PROCESSING)
    try:
        result = parser.parse(read_original_bytes(conn, settings, user_id, document_id), options)
    except Exception as exc:  # a parser bug must not lose the document or leave it 'processing'
        result = None
        issues = [Issue(Severity.FAILED, "parser_error",
                        f"Unexpected error while parsing: {type(exc).__name__}: {exc}")]
    else:
        issues = list(result.issues)

    failed = any(i.severity == Severity.FAILED for i in issues)
    blocking = any(i.severity == Severity.BLOCKING for i in issues)
    review = any(i.severity == Severity.REVIEW for i in issues)

    with transaction(conn):
        saved = 0
        if result is not None and not failed and not blocking:
            # Prefer what the user typed at upload; fill blanks from the file; flag disagreements.
            start = doc.billing_period_start or result.period_start
            end = doc.billing_period_end or result.period_end
            if start and end and end < start:
                start, end = doc.billing_period_start, doc.billing_period_end
                issues.append(Issue(Severity.REVIEW, "period_conflict",
                                    "Dates from the file conflict with the dates you entered; yours were kept."))
                review = True
            for label, entered, detected in (("start", doc.billing_period_start, result.period_start),
                                             ("end", doc.billing_period_end, result.period_end)):
                if entered and detected and entered != detected:
                    issues.append(Issue(Severity.REVIEW, "period_mismatch",
                                        f"Billing period {label} you entered ({entered}) differs from the file ({detected})."))
                    review = True
            repo.set_document_metadata(
                conn, user_id, document_id,
                account_alias=doc.account_alias or result.account_alias,
                billing_period_start=start, billing_period_end=end,
                cloud_provider=result.cloud_provider)
            repo.set_document_extraction(conn, user_id, document_id, result.extracted_text)
            saved = repo.replace_line_items(conn, user_id, document_id, result.line_items)
            repo.reset_verification(conn, user_id, document_id)  # new data -> old sign-off is void
            if result.summary is not None:
                repo.save_invoice_summary(conn, user_id, replace(result.summary, document_id=document_id))
        elif result is not None and result.extracted_text:
            repo.set_document_extraction(conn, user_id, document_id, result.extracted_text)

        if failed:
            status = DocumentStatus.FAILED
        elif blocking or review:
            status = DocumentStatus.NEEDS_REVIEW
        else:
            status = DocumentStatus.PROCESSED
        notes = [str(i) for i in issues][:_MAX_NOTES]
        repo.set_document_status(conn, user_id, document_id, status, notes=notes)

    return ProcessResult(repo.get_document(conn, user_id, document_id), issues, saved)
