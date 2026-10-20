"""End-to-end intake: validation + hashing + duplicates + storage + database."""
import sqlite3

import pytest

from billing_explainer import repository as repo
from billing_explainer.config import Settings
from billing_explainer.ingest import Outcome, ingest_many, ingest_upload, read_original_bytes
from billing_explainer.models import DocumentStatus
from billing_explainer.storage import sha256_hex
from samples import CSV_BYTES, JPG_BYTES, PDF_BYTES, PNG_BYTES, make_xlsx_bytes


def files_on_disk(settings):
    return sorted(settings.upload_dir.iterdir()) if settings.upload_dir.exists() else []


def test_accepted_upload_creates_row_and_file(conn, settings, alice):
    r = ingest_upload(conn, settings, alice.id, "Jan bill.csv", CSV_BYTES,
                      account_alias="prod", billing_period_start="2026-01-01",
                      billing_period_end="2026-01-31")
    assert r.outcome == Outcome.ACCEPTED
    d = r.document
    assert d.status == DocumentStatus.UPLOADED
    assert d.original_filename == "Jan bill.csv" and d.file_type == "csv"
    assert d.sha256 == sha256_hex(CSV_BYTES) and d.file_size_bytes == len(CSV_BYTES)
    assert (d.account_alias, d.billing_period_start, d.billing_period_end) == ("prod", "2026-01-01", "2026-01-31")
    assert read_original_bytes(conn, settings, alice.id, d.id) == CSV_BYTES
    assert len(files_on_disk(settings)) == 1


def test_hostile_filename_never_influences_storage_path(conn, settings, alice, tmp_path):
    r = ingest_upload(conn, settings, alice.id, "../../evil.csv", CSV_BYTES)
    assert r.outcome == Outcome.ACCEPTED
    assert r.document.original_filename == "evil.csv"
    assert "/" not in r.document.storage_path and ".." not in r.document.storage_path
    assert [p.name for p in files_on_disk(settings)] == [r.document.storage_path]
    assert not (tmp_path / "evil.csv").exists() and not (tmp_path.parent / "evil.csv").exists()


def test_duplicate_upload_is_reported_and_not_stored_twice(conn, settings, alice):
    first = ingest_upload(conn, settings, alice.id, "a.csv", CSV_BYTES)
    second = ingest_upload(conn, settings, alice.id, "renamed.csv", CSV_BYTES)
    assert second.outcome == Outcome.DUPLICATE
    assert second.existing_document_id == first.document.id
    assert "a.csv" in second.message
    assert len(files_on_disk(settings)) == 1
    assert len(repo.list_documents(conn, alice.id)) == 1


def test_same_file_from_two_users_is_accepted_for_both(conn, settings, alice, bob):
    assert ingest_upload(conn, settings, alice.id, "a.csv", CSV_BYTES).outcome == Outcome.ACCEPTED
    assert ingest_upload(conn, settings, bob.id, "a.csv", CSV_BYTES).outcome == Outcome.ACCEPTED
    assert len(files_on_disk(settings)) == 2


def test_rejected_files_leave_no_row_and_no_file(conn, settings, alice):
    for name, data in [("x.exe", b"MZ"), ("empty.csv", b""), ("fake.pdf", b"not a pdf")]:
        r = ingest_upload(conn, settings, alice.id, name, data)
        assert r.outcome == Outcome.REJECTED and r.document is None and r.message
    assert repo.list_documents(conn, alice.id) == []
    assert files_on_disk(settings) == []


def test_oversized_file_is_rejected(conn, tmp_path, alice):
    small = Settings(db_path=tmp_path / "x.db", upload_dir=tmp_path / "up", max_upload_bytes=10)
    r = ingest_upload(conn, small, alice.id, "a.csv", CSV_BYTES)
    assert r.outcome == Outcome.REJECTED and "limit" in r.message


def test_batch_returns_one_result_per_file_in_order(conn, settings, alice):
    batch = [("a.pdf", PDF_BYTES), ("b.csv", CSV_BYTES), ("bad.exe", b"MZ"),
             ("c.xlsx", make_xlsx_bytes()), ("dup.csv", CSV_BYTES),
             ("d.png", PNG_BYTES), ("e.jpeg", JPG_BYTES)]
    results = ingest_many(conn, settings, alice.id, batch)
    assert [r.outcome for r in results] == [
        Outcome.ACCEPTED, Outcome.ACCEPTED, Outcome.REJECTED,
        Outcome.ACCEPTED, Outcome.DUPLICATE, Outcome.ACCEPTED, Outcome.ACCEPTED]
    assert [r.filename for r in results][2] == "bad.exe"
    assert len(repo.list_documents(conn, alice.id)) == 5
    assert len(files_on_disk(settings)) == 5


def test_invalid_billing_period_is_rejected_before_storing(conn, settings, alice):
    bad_order = ingest_upload(conn, settings, alice.id, "a.csv", CSV_BYTES,
                              billing_period_start="2026-02-01", billing_period_end="2026-01-01")
    not_a_date = ingest_upload(conn, settings, alice.id, "b.csv", CSV_BYTES + b"x",
                               billing_period_start="last month")
    assert bad_order.outcome == not_a_date.outcome == Outcome.REJECTED
    assert files_on_disk(settings) == [] and repo.list_documents(conn, alice.id) == []


def test_database_failure_leaves_no_orphan_file(conn, settings):
    with pytest.raises(sqlite3.IntegrityError):  # user 999 doesn't exist -> FK violation
        ingest_upload(conn, settings, 999, "a.csv", CSV_BYTES)
    assert files_on_disk(settings) == []
    assert not conn.in_transaction


def test_original_is_kept_when_processing_later_fails(conn, settings, alice):
    broken_csv = b"service,region,cost\n\"unterminated,us-east-1,5\n"  # valid intake, parser will choke
    doc = ingest_upload(conn, settings, alice.id, "broken.csv", broken_csv).document
    repo.set_document_status(conn, alice.id, doc.id, DocumentStatus.FAILED,
                             notes=["CSV parse error on row 2"])
    got = repo.get_document(conn, alice.id, doc.id)
    assert got.status == DocumentStatus.FAILED and got.validation_notes
    assert read_original_bytes(conn, settings, alice.id, doc.id) == broken_csv  # still retrievable


def test_user_cannot_read_another_users_original(conn, settings, alice, bob):
    doc = ingest_upload(conn, settings, alice.id, "a.csv", CSV_BYTES).document
    with pytest.raises(repo.DocumentNotFoundError):
        read_original_bytes(conn, settings, bob.id, doc.id)


def test_corrected_invoice_links_to_original(conn, settings, alice):
    old = ingest_upload(conn, settings, alice.id, "inv.csv", CSV_BYTES).document
    new = ingest_upload(conn, settings, alice.id, "inv_corrected.csv", CSV_BYTES + b"IAM,global,1.00\n",
                        supersedes_document_id=old.id).document
    assert new.supersedes_document_id == old.id
    assert read_original_bytes(conn, settings, alice.id, old.id) == CSV_BYTES  # untouched
