"""Data access layer. The ONLY place SQL for application data is written.

Isolation rule: every function that touches a document (or its line items / summary)
takes `user_id` and enforces ownership *inside the SQL* (WHERE user_id = ?), so there
is no gap between "check" and "use". A document that doesn't exist and a document
owned by someone else look identical to callers: DocumentNotFoundError.
`user_id` must always come from the authenticated session, never from form input.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Iterable

from billing_explainer.database import transaction
from billing_explainer.models import (
    ChargeType, Document, DocumentStatus, InvoiceSummary, ItemStatus, LineItem,
    ReconciliationStatus, User, money_from_db, money_to_db, to_iso_date,
    utc_now_iso,
)


class DocumentNotFoundError(LookupError):
    """Document missing OR not owned by this user (deliberately indistinguishable)."""


class DuplicateDocumentError(Exception):
    """This user already uploaded a file with the same SHA-256."""

    def __init__(self, existing_document_id: int) -> None:
        super().__init__(f"Duplicate of document {existing_document_id}")
        self.existing_document_id = existing_document_id


# ------------------------------------------------------------------ users

def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["auth_provider"], row["provider_subject"],
                row["email"], row["created_at"])


def get_or_create_user(conn: sqlite3.Connection, auth_provider: str,
                       provider_subject: str, email: str | None = None) -> User:
    """Identity key is (provider, subject). Email is stored but never used to match."""
    if not provider_subject:
        raise ValueError("provider_subject is required (the OIDC 'sub' claim).")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM users WHERE auth_provider = ? AND provider_subject = ?",
            (auth_provider, provider_subject)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO users (auth_provider, provider_subject, email, created_at)"
                " VALUES (?, ?, ?, ?)",
                (auth_provider, provider_subject, email, utc_now_iso()))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        elif email and email != row["email"]:  # emails change; keep ours current
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, row["id"]))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return _user(row)


# -------------------------------------------------------------- documents

def _document(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"], user_id=row["user_id"], original_filename=row["original_filename"],
        storage_path=row["storage_path"], sha256=row["sha256"], file_type=row["file_type"],
        file_size_bytes=row["file_size_bytes"], uploaded_at=row["uploaded_at"],
        status=DocumentStatus(row["status"]), account_alias=row["account_alias"],
        billing_period_start=row["billing_period_start"],
        billing_period_end=row["billing_period_end"], extracted_text=row["extracted_text"],
        validation_notes=json.loads(row["validation_notes"]),
        supersedes_document_id=row["supersedes_document_id"], verified_at=row["verified_at"],
        cloud_provider=row["cloud_provider"])


def find_duplicate(conn: sqlite3.Connection, user_id: int, sha256: str) -> Document | None:
    """Duplicate detection, scoped to this user only."""
    row = conn.execute("SELECT * FROM documents WHERE user_id = ? AND sha256 = ?",
                       (user_id, sha256)).fetchone()
    return _document(row) if row else None


def create_document(conn: sqlite3.Connection, user_id: int, *, original_filename: str,
                    storage_path: str, sha256: str, file_type: str, file_size_bytes: int,
                    account_alias: str | None = None,
                    billing_period_start: "date | str | None" = None,
                    billing_period_end: "date | str | None" = None,
                    supersedes_document_id: int | None = None) -> Document:
    """Insert a new document row in status 'uploaded'.

    `supersedes_document_id` links a corrected invoice to the one it replaces. The old
    document and its evidence are kept untouched.
    """
    with transaction(conn):
        existing = find_duplicate(conn, user_id, sha256)
        if existing:
            raise DuplicateDocumentError(existing.id)
        if supersedes_document_id is not None:
            get_document(conn, user_id, supersedes_document_id)  # must be yours
        cur = conn.execute(
            """INSERT INTO documents (user_id, original_filename, storage_path, sha256,
                   file_type, file_size_bytes, uploaded_at, account_alias,
                   billing_period_start, billing_period_end, supersedes_document_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, original_filename, storage_path, sha256, file_type, file_size_bytes,
             utc_now_iso(), account_alias, to_iso_date(billing_period_start),
             to_iso_date(billing_period_end), supersedes_document_id))
        return get_document(conn, user_id, cur.lastrowid)


def get_document(conn: sqlite3.Connection, user_id: int, document_id: int) -> Document:
    row = conn.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?",
                       (document_id, user_id)).fetchone()
    if row is None:
        raise DocumentNotFoundError(document_id)
    return _document(row)


def list_documents(conn: sqlite3.Connection, user_id: int) -> list[Document]:
    rows = conn.execute("SELECT * FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC, id DESC",
                        (user_id,)).fetchall()
    return [_document(r) for r in rows]


def _update_owned(conn: sqlite3.Connection, user_id: int, document_id: int,
                  assignments: str, params: tuple) -> None:
    """UPDATE ... WHERE id AND user_id; zero rows touched means 'not yours / not found'."""
    cur = conn.execute(f"UPDATE documents SET {assignments} WHERE id = ? AND user_id = ?",
                       (*params, document_id, user_id))
    if cur.rowcount == 0:
        raise DocumentNotFoundError(document_id)


def set_document_status(conn: sqlite3.Connection, user_id: int, document_id: int,
                        status: DocumentStatus, notes: list[str] | None = None) -> None:
    """Change status; if `notes` is given it replaces the stored findings/errors."""
    with transaction(conn):
        if notes is None:
            _update_owned(conn, user_id, document_id, "status = ?", (status.value,))
        else:
            _update_owned(conn, user_id, document_id, "status = ?, validation_notes = ?",
                          (status.value, json.dumps(notes)))


def set_document_extraction(conn: sqlite3.Connection, user_id: int, document_id: int,
                            extracted_text: str | None) -> None:
    with transaction(conn):
        _update_owned(conn, user_id, document_id, "extracted_text = ?", (extracted_text,))


def set_document_metadata(conn: sqlite3.Connection, user_id: int, document_id: int, *,
                          account_alias: str | None,
                          billing_period_start: "date | str | None",
                          billing_period_end: "date | str | None",
                          cloud_provider: str | None = None) -> None:
    """Overwrite alias/period/provider (callers pass the final values they want stored)."""
    with transaction(conn):
        _update_owned(conn, user_id, document_id,
                      "account_alias = ?, billing_period_start = ?, billing_period_end = ?,"
                      " cloud_provider = ?",
                      (account_alias, to_iso_date(billing_period_start),
                       to_iso_date(billing_period_end), cloud_provider))


def reset_verification(conn: sqlite3.Connection, user_id: int, document_id: int) -> None:
    """Called when extracted data is replaced: old human verification no longer applies."""
    with transaction(conn):
        _update_owned(conn, user_id, document_id, "verified_at = NULL", ())


def mark_document_verified(conn: sqlite3.Connection, user_id: int, document_id: int) -> None:
    with transaction(conn):
        _update_owned(conn, user_id, document_id, "verified_at = ?, status = ?",
                      (utc_now_iso(), DocumentStatus.PROCESSED.value))


# ------------------------------------------------------------- line items

def _item(row: sqlite3.Row) -> LineItem:
    return LineItem(
        id=row["id"], document_id=row["document_id"], service=row["service"],
        region=row["region"], resource_id=row["resource_id"],
        charge_type=ChargeType(row["charge_type"]),
        usage_quantity=money_from_db(row["usage_quantity"]), usage_unit=row["usage_unit"],
        cost_amount=money_from_db(row["cost_amount"]), currency=row["currency"],
        period_start=row["period_start"], period_end=row["period_end"],
        source_locator=row["source_locator"], raw_record=row["raw_record"],
        status=ItemStatus(row["status"]))


def replace_line_items(conn: sqlite3.Connection, user_id: int, document_id: int,
                       items: Iterable[LineItem]) -> int:
    """Atomically replace all line items of one of the user's documents.

    Replace (not append) makes re-processing and re-saving after review idempotent.
    If any item is invalid, nothing changes: the transaction rolls back.
    """
    with transaction(conn):
        get_document(conn, user_id, document_id)  # ownership check
        conn.execute("DELETE FROM billing_line_items WHERE document_id = ?", (document_id,))
        count = 0
        for it in items:
            conn.execute(
                """INSERT INTO billing_line_items (document_id, service, region, resource_id,
                       charge_type, usage_quantity, usage_unit, cost_amount, currency,
                       period_start, period_end, source_locator, raw_record, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, it.service, it.region, it.resource_id, it.charge_type.value,
                 money_to_db(it.usage_quantity), it.usage_unit, money_to_db(it.cost_amount),
                 it.currency, to_iso_date(it.period_start), to_iso_date(it.period_end),
                 it.source_locator, it.raw_record, it.status.value))
            count += 1
    return count


def list_line_items(conn: sqlite3.Connection, user_id: int, document_id: int) -> list[LineItem]:
    # JOIN on documents.user_id so line items can never leak across users.
    rows = conn.execute(
        """SELECT li.* FROM billing_line_items li
           JOIN documents d ON d.id = li.document_id
           WHERE li.document_id = ? AND d.user_id = ? ORDER BY li.id""",
        (document_id, user_id)).fetchall()
    if not rows:
        get_document(conn, user_id, document_id)  # raise if it isn't yours
    return [_item(r) for r in rows]


# -------------------------------------------------------- invoice summary

def save_invoice_summary(conn: sqlite3.Connection, user_id: int, summary: InvoiceSummary) -> None:
    with transaction(conn):
        get_document(conn, user_id, summary.document_id)  # ownership check
        conn.execute(
            """INSERT OR REPLACE INTO invoice_summaries (document_id, invoice_number, currency,
                   usage_total, credit_total, tax_total, other_adjustments, invoice_total,
                   reconciliation_status, reconciliation_notes, uncertain_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (summary.document_id, summary.invoice_number, summary.currency,
             money_to_db(summary.usage_total), money_to_db(summary.credit_total),
             money_to_db(summary.tax_total), money_to_db(summary.other_adjustments),
             money_to_db(summary.invoice_total), summary.reconciliation_status.value,
             summary.reconciliation_notes, json.dumps(summary.uncertain_fields)))


def get_invoice_summary(conn: sqlite3.Connection, user_id: int,
                        document_id: int) -> InvoiceSummary | None:
    get_document(conn, user_id, document_id)  # ownership check first
    r = conn.execute("SELECT * FROM invoice_summaries WHERE document_id = ?",
                     (document_id,)).fetchone()
    if r is None:
        return None
    return InvoiceSummary(
        document_id=r["document_id"], invoice_number=r["invoice_number"], currency=r["currency"],
        usage_total=money_from_db(r["usage_total"]), credit_total=money_from_db(r["credit_total"]),
        tax_total=money_from_db(r["tax_total"]),
        other_adjustments=money_from_db(r["other_adjustments"]),
        invoice_total=money_from_db(r["invoice_total"]),
        reconciliation_status=ReconciliationStatus(r["reconciliation_status"]),
        reconciliation_notes=r["reconciliation_notes"],
        uncertain_fields=json.loads(r["uncertain_fields"]))
