"""User identity, duplicate detection, and cross-user isolation."""
import hashlib
from decimal import Decimal

import pytest

from billing_explainer import repository as repo
from billing_explainer.models import DocumentStatus, InvoiceSummary, LineItem
from conftest import make_doc


def test_get_or_create_user_is_idempotent_and_keyed_on_subject(conn):
    u1 = repo.get_or_create_user(conn, "google", "sub-1", "old@example.com")
    u2 = repo.get_or_create_user(conn, "google", "sub-1", "new@example.com")  # email changed
    assert u1.id == u2.id
    assert u2.email == "new@example.com"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_same_subject_from_other_provider_is_a_different_user(conn):
    a = repo.get_or_create_user(conn, "google", "123")
    b = repo.get_or_create_user(conn, "microsoft", "123")
    assert a.id != b.id


def test_same_email_different_subject_is_not_merged(conn):
    a = repo.get_or_create_user(conn, "google", "sub-a", "shared@example.com")
    b = repo.get_or_create_user(conn, "google", "sub-b", "shared@example.com")
    assert a.id != b.id


def test_missing_subject_is_rejected(conn):
    with pytest.raises(ValueError):
        repo.get_or_create_user(conn, "google", "", "x@example.com")


def test_duplicate_upload_same_user_is_blocked(conn, alice):
    first = make_doc(conn, alice, b"same bytes")
    with pytest.raises(repo.DuplicateDocumentError) as err:
        make_doc(conn, alice, b"same bytes", storage_path="other-name.csv")
    assert err.value.existing_document_id == first.id
    assert len(repo.list_documents(conn, alice.id)) == 1


def test_same_file_by_different_users_is_not_a_duplicate(conn, alice, bob):
    make_doc(conn, alice, b"same bytes")
    make_doc(conn, bob, b"same bytes", storage_path="bobs-copy.csv")
    assert repo.find_duplicate(conn, bob.id, hashlib.sha256(b"same bytes").hexdigest())


def test_user_only_sees_own_documents(conn, alice, bob):
    a = make_doc(conn, alice, b"alice file")
    b = make_doc(conn, bob, b"bob file")
    assert [d.id for d in repo.list_documents(conn, alice.id)] == [a.id]
    assert [d.id for d in repo.list_documents(conn, bob.id)] == [b.id]


def test_user_cannot_touch_another_users_document_id(conn, alice, bob):
    doc = make_doc(conn, alice, b"alice file")
    repo.replace_line_items(conn, alice.id, doc.id, [
        LineItem(cost_amount=Decimal("9.99"), currency="USD", source_locator="row 2")])
    repo.save_invoice_summary(conn, alice.id, InvoiceSummary(document_id=doc.id, invoice_number="INV-1"))

    item = LineItem(cost_amount=Decimal("1"), currency="USD", source_locator="row 2")
    attempts = [
        lambda: repo.get_document(conn, bob.id, doc.id),
        lambda: repo.list_line_items(conn, bob.id, doc.id),
        lambda: repo.get_invoice_summary(conn, bob.id, doc.id),
        lambda: repo.replace_line_items(conn, bob.id, doc.id, [item]),
        lambda: repo.save_invoice_summary(conn, bob.id, InvoiceSummary(document_id=doc.id)),
        lambda: repo.set_document_status(conn, bob.id, doc.id, DocumentStatus.FAILED),
        lambda: repo.set_document_extraction(conn, bob.id, doc.id, "hacked"),
        lambda: repo.set_document_metadata(conn, bob.id, doc.id, account_alias="x",
                                           billing_period_start=None, billing_period_end=None),
        lambda: repo.mark_document_verified(conn, bob.id, doc.id),
    ]
    for attempt in attempts:
        with pytest.raises(repo.DocumentNotFoundError):
            attempt()

    # ...and Alice's data is completely untouched
    after = repo.get_document(conn, alice.id, doc.id)
    assert after.status == DocumentStatus.UPLOADED and after.extracted_text is None
    assert [i.cost_amount for i in repo.list_line_items(conn, alice.id, doc.id)] == [Decimal("9.99")]
    assert repo.get_invoice_summary(conn, alice.id, doc.id).invoice_number == "INV-1"


def test_nonexistent_and_foreign_documents_look_identical(conn, alice, bob):
    doc = make_doc(conn, alice)
    with pytest.raises(repo.DocumentNotFoundError):
        repo.get_document(conn, bob.id, doc.id)
    with pytest.raises(repo.DocumentNotFoundError):
        repo.get_document(conn, bob.id, 424242)


def test_corrected_invoice_keeps_the_original(conn, alice, bob):
    old = make_doc(conn, alice, b"invoice v1")
    new = make_doc(conn, alice, b"invoice v2", storage_path="v2.csv", supersedes_document_id=old.id)
    assert new.supersedes_document_id == old.id
    assert repo.get_document(conn, alice.id, old.id).id == old.id  # still there
    # cannot claim to supersede someone else's document
    with pytest.raises(repo.DocumentNotFoundError):
        make_doc(conn, bob, b"bob file", supersedes_document_id=old.id)


def test_failed_status_keeps_document_and_notes(conn, alice):
    doc = make_doc(conn, alice)
    repo.set_document_status(conn, alice.id, doc.id, DocumentStatus.FAILED,
                             notes=["Could not parse: file is corrupt"])
    got = repo.get_document(conn, alice.id, doc.id)
    assert got.status == DocumentStatus.FAILED
    assert got.validation_notes == ["Could not parse: file is corrupt"]
    assert got.storage_path  # original reference still present for retry


def test_invoice_summary_keeps_unknown_fields_as_none(conn, alice):
    doc = make_doc(conn, alice)
    repo.save_invoice_summary(conn, alice.id, InvoiceSummary(
        document_id=doc.id, usage_total=Decimal("100.00"), uncertain_fields=["invoice_total", "tax_total"]))
    s = repo.get_invoice_summary(conn, alice.id, doc.id)
    assert s.invoice_total is None and s.tax_total is None and s.currency is None
    assert s.uncertain_fields == ["invoice_total", "tax_total"]
    assert s.usage_total == Decimal("100.00")
