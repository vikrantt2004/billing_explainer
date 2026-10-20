"""Schema-level behavior: foreign keys, CHECK constraints, persistence, atomicity."""
import sqlite3
from decimal import Decimal

import pytest

from billing_explainer import repository as repo
from billing_explainer.database import connect, transaction
from billing_explainer.models import (
    ChargeType, DocumentStatus, ItemStatus, LineItem, ReconciliationStatus, to_money,
)
from conftest import make_doc


def test_foreign_keys_are_enforced(conn):
    pragma = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert pragma == 1
    with pytest.raises(sqlite3.IntegrityError):  # user 999 does not exist
        conn.execute(
            "INSERT INTO documents (user_id, original_filename, storage_path, sha256, file_type,"
            " file_size_bytes, uploaded_at) VALUES (999, 'x.csv', 'p', ?, 'csv', 1, 't')", ("a" * 64,))


def test_cannot_delete_user_who_owns_documents(conn, alice):
    make_doc(conn, alice)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM users WHERE id = ?", (alice.id,))


def test_deleting_document_cascades_to_items_and_summary(conn, alice):
    doc = make_doc(conn, alice)
    repo.replace_line_items(conn, alice.id, doc.id, [
        LineItem(cost_amount=Decimal("1.00"), currency="USD", source_locator="row 2")])
    repo.save_invoice_summary(conn, alice.id, repo.InvoiceSummary(document_id=doc.id))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc.id,))
    assert conn.execute("SELECT COUNT(*) FROM billing_line_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM invoice_summaries").fetchone()[0] == 0


def test_check_constraints_reject_bad_values(conn, alice):
    doc = make_doc(conn, alice)
    with pytest.raises(sqlite3.IntegrityError):  # status not in allowed list
        conn.execute("UPDATE documents SET status = 'bogus' WHERE id = ?", (doc.id,))
    with pytest.raises(sqlite3.IntegrityError):  # period ends before it starts
        repo.set_document_metadata(conn, alice.id, doc.id, account_alias=None,
                                   billing_period_start="2026-02-01", billing_period_end="2026-01-01")
    with pytest.raises(sqlite3.IntegrityError):  # lowercase / wrong-length currency
        repo.replace_line_items(conn, alice.id, doc.id, [
            LineItem(cost_amount=Decimal("1"), currency="usd", source_locator="row 2")])


def test_enum_values_match_schema(conn, alice):
    """If someone adds an enum member but forgets the CHECK list (or vice versa), fail."""
    doc = make_doc(conn, alice)
    for status in DocumentStatus:
        repo.set_document_status(conn, alice.id, doc.id, status)
    for ct in ChargeType:
        for st in ItemStatus:
            repo.replace_line_items(conn, alice.id, doc.id, [
                LineItem(cost_amount=Decimal("1"), currency="USD", source_locator="r",
                         charge_type=ct, status=st)])
    for rs in ReconciliationStatus:
        repo.save_invoice_summary(conn, alice.id,
                                  repo.InvoiceSummary(document_id=doc.id, reconciliation_status=rs))


def test_data_persists_across_restart(db_path):
    c1 = connect(db_path)
    user = repo.get_or_create_user(c1, "google", "sub-alice", "a@example.com")
    doc = make_doc(c1, user, billing_period_start="2026-01-01", billing_period_end="2026-01-31")
    repo.replace_line_items(c1, user.id, doc.id, [
        LineItem(cost_amount=Decimal("12.3456"), currency="USD", source_locator="row 2",
                 service="EC2", region="us-east-1")])
    c1.close()  # "app stops"

    c2 = connect(db_path)  # "app restarts"
    user2 = repo.get_or_create_user(c2, "google", "sub-alice")
    assert user2.id == user.id
    docs = repo.list_documents(c2, user2.id)
    assert [d.id for d in docs] == [doc.id]
    assert docs[0].billing_period_start == "2026-01-01"
    assert repo.list_line_items(c2, user2.id, doc.id)[0].cost_amount == Decimal("12.3456")
    c2.close()


def test_failed_batch_rolls_back_everything(conn, alice):
    doc = make_doc(conn, alice)
    good = LineItem(cost_amount=Decimal("5"), currency="USD", source_locator="row 2")
    bad = LineItem(cost_amount=Decimal("5"), currency="usd", source_locator="row 3")
    repo.replace_line_items(conn, alice.id, doc.id, [good])
    with pytest.raises(sqlite3.IntegrityError):
        repo.replace_line_items(conn, alice.id, doc.id, [good, good, bad])
    # the earlier state survives: still exactly one item, and no open transaction
    assert len(repo.list_line_items(conn, alice.id, doc.id)) == 1
    assert not conn.in_transaction


def test_outer_transaction_rolls_back_inner_repository_calls(conn, alice):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            make_doc(conn, alice)
            raise RuntimeError("boom")
    assert repo.list_documents(conn, alice.id) == []


# ---- money ----

def test_decimal_sum_is_exact(conn, alice):
    doc = make_doc(conn, alice)
    repo.replace_line_items(conn, alice.id, doc.id, [
        LineItem(cost_amount=to_money("0.1"), currency="USD", source_locator="row 2"),
        LineItem(cost_amount=to_money("0.2"), currency="USD", source_locator="row 3"),
        LineItem(cost_amount=to_money("0.0000166667"), currency="USD", source_locator="row 4")])
    total = sum((i.cost_amount for i in repo.list_line_items(conn, alice.id, doc.id)), Decimal(0))
    assert total == Decimal("0.3000166667")  # floats would give 0.30001666670000003-ish noise


def test_floats_are_rejected_as_money():
    with pytest.raises(TypeError):
        to_money(0.1)
    with pytest.raises(ValueError):
        to_money("12,50 USD")
    with pytest.raises(ValueError):
        to_money("NaN")


def test_v1_database_is_migrated_without_losing_data(tmp_path):
    """A database created before cloud_provider existed gains the column on init_db()."""
    from billing_explainer.database import SCHEMA_PATH, init_db
    old_schema = "\n".join(l for l in SCHEMA_PATH.read_text().splitlines() if "cloud_provider" not in l)
    c = connect(tmp_path / "old.db")
    c.executescript(old_schema)
    c.execute("PRAGMA user_version = 1")
    assert "cloud_provider" not in [r["name"] for r in c.execute("PRAGMA table_info(documents)")]
    # rows written by the OLD application version (raw SQL: new repository code needs the new column)
    c.execute("INSERT INTO users (auth_provider, provider_subject, created_at) VALUES ('google','sub-old','t')")
    c.execute("INSERT INTO documents (user_id, original_filename, storage_path, sha256, file_type,"
              " file_size_bytes, uploaded_at) VALUES (1, 'bill.csv', ?, ?, 'csv', 5, 't')",
              ("0" * 32 + ".csv", "a" * 64))

    init_db(c)
    init_db(c)  # idempotent
    assert "cloud_provider" in [r["name"] for r in c.execute("PRAGMA table_info(documents)")]
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    got = repo.get_document(c, 1, 1)
    assert got.original_filename == "bill.csv" and got.cloud_provider is None
    c.close()
