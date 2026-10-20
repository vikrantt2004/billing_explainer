"""Intake workflow: validate -> hash -> duplicate check -> store original -> create row.

Parsing is NOT done here (Stage 3). After a successful intake the document is
'uploaded' and the original file is safely on disk, so a later parsing failure can
never lose the user's file.

Two kinds of problem are handled differently on purpose:
  * REJECTED at intake: wrong/unsupported type, empty, too large, extension lies about
    content. Nothing is stored; there is nothing worth keeping.
  * Valid-looking file that later fails to PARSE: stays stored, status 'failed' with notes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable

from billing_explainer import repository as repo
from billing_explainer.config import Settings
from billing_explainer.models import Document, to_iso_date
from billing_explainer.storage import (
    IntakeError, delete_stored_file, detect_file_type, resolve_storage_path,
    sanitize_display_name, sha256_hex, store_file,
)


class Outcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True)
class IngestResult:
    filename: str                      # sanitized display name
    outcome: Outcome
    message: str
    document: Document | None = None   # set when ACCEPTED
    existing_document_id: int | None = None  # set when DUPLICATE


def _validate_period(start: "date | str | None", end: "date | str | None") -> tuple[str | None, str | None]:
    try:
        s, e = to_iso_date(start or None), to_iso_date(end or None)
    except (ValueError, TypeError):
        raise IntakeError("invalid_period", "Billing period dates must be valid dates (YYYY-MM-DD).")
    if s and e and e < s:
        raise IntakeError("invalid_period", "Billing period end is before its start.")
    return s, e


def ingest_upload(conn: sqlite3.Connection, settings: Settings, user_id: int,
                  filename: str, data: bytes, *, account_alias: str | None = None,
                  billing_period_start: "date | str | None" = None,
                  billing_period_end: "date | str | None" = None,
                  supersedes_document_id: int | None = None) -> IngestResult:
    """Ingest one uploaded file for `user_id` (from the authenticated session)."""
    display = sanitize_display_name(filename)
    try:
        file_type = detect_file_type(display, data, settings.max_upload_bytes)
        start, end = _validate_period(billing_period_start, billing_period_end)
    except IntakeError as err:
        return IngestResult(display, Outcome.REJECTED, err.message)

    digest = sha256_hex(data)
    existing = repo.find_duplicate(conn, user_id, digest)  # cheap check before touching disk
    if existing:
        return _duplicate(display, existing)

    storage_path = store_file(settings.upload_dir, data, file_type)
    try:
        doc = repo.create_document(
            conn, user_id, original_filename=display, storage_path=storage_path,
            sha256=digest, file_type=file_type, file_size_bytes=len(data),
            account_alias=(account_alias or "").strip()[:100] or None,
            billing_period_start=start, billing_period_end=end,
            supersedes_document_id=supersedes_document_id)
    except repo.DuplicateDocumentError as dup:  # two uploads raced; the DB constraint caught it
        delete_stored_file(settings.upload_dir, storage_path)
        return _duplicate(display, repo.get_document(conn, user_id, dup.existing_document_id))
    except BaseException:
        delete_stored_file(settings.upload_dir, storage_path)  # never leave an orphan file
        raise
    return IngestResult(display, Outcome.ACCEPTED, "Uploaded.", document=doc)


def _duplicate(display: str, existing: Document) -> IngestResult:
    return IngestResult(
        display, Outcome.DUPLICATE,
        f"Already uploaded as '{existing.original_filename}' "
        f"(document #{existing.id}, status: {existing.status.value}).",
        existing_document_id=existing.id)


def ingest_many(conn: sqlite3.Connection, settings: Settings, user_id: int,
                files: Iterable[tuple[str, bytes]], **metadata) -> list[IngestResult]:
    """One result per file, in order. A bad file never stops the others."""
    return [ingest_upload(conn, settings, user_id, name, data, **metadata) for name, data in files]


def read_original_bytes(conn: sqlite3.Connection, settings: Settings, user_id: int,
                        document_id: int) -> bytes:
    """Return the stored original. Raises DocumentNotFoundError if it isn't this user's."""
    doc = repo.get_document(conn, user_id, document_id)  # ownership check first
    return resolve_storage_path(settings.upload_dir, doc.storage_path).read_bytes()
