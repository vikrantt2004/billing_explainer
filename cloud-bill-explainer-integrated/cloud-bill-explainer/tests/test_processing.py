"""process_document: parser output -> database, status rules, failure handling."""
from decimal import Decimal

import pytest

from billing_explainer import repository as repo
from billing_explainer.ingest import ingest_upload, read_original_bytes
from billing_explainer.models import ChargeType, DocumentStatus
from billing_explainer.parsers import BaseParser, ParseOptions, Severity
from billing_explainer.processing import process_document
from billing_explainer.sample_data import write_samples


@pytest.fixture
def samples(tmp_path):
    return write_samples(tmp_path / "samples")


def upload(conn, settings, user, path, **kw):
    return ingest_upload(conn, settings, user.id, path.name, path.read_bytes(), **kw).document


class ExplodingParser(BaseParser):
    file_types = ("csv",)

    def parse(self, data, options):
        raise RuntimeError("kaboom")


def test_clean_file_becomes_processed_with_exact_totals(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    res = process_document(conn, settings, alice.id, doc.id)
    d = res.document
    assert d.status == DocumentStatus.PROCESSED and res.items_saved == 14
    assert (d.account_alias, d.cloud_provider) == ("123456789012", "aws")
    assert (d.billing_period_start, d.billing_period_end) == ("2026-01-01", "2026-01-31")
    assert d.extracted_text.startswith("lineItem/UsageAccountId") and d.verified_at is None
    items = repo.list_line_items(conn, alice.id, doc.id)
    usage = sum((i.cost_amount for i in items if i.charge_type == ChargeType.USAGE), Decimal(0))
    credits = sum((i.cost_amount for i in items if i.charge_type == ChargeType.CREDIT), Decimal(0))
    assert (usage, credits) == (Decimal("9756.1710"), Decimal("-750.00"))


def test_review_issues_save_records_but_flag_the_document(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["simple_export_2026-01.csv"])
    res = process_document(conn, settings, alice.id, doc.id)
    assert res.document.status == DocumentStatus.NEEDS_REVIEW and res.items_saved == 14
    assert any("period" in n or "date" in n for n in res.document.validation_notes)
    assert any(n.startswith("[info]") for n in res.document.validation_notes)  # totals row note


def test_blocking_issue_saves_nothing_then_retry_with_option_succeeds(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["missing_currency.csv"])
    first = process_document(conn, settings, alice.id, doc.id)
    assert first.document.status == DocumentStatus.NEEDS_REVIEW and first.items_saved == 0
    assert repo.list_line_items(conn, alice.id, doc.id) == []
    second = process_document(conn, settings, alice.id, doc.id, ParseOptions(currency="USD"))
    assert second.items_saved == 2
    assert {i.currency for i in repo.list_line_items(conn, alice.id, doc.id)} == {"USD"}


def test_malformed_file_fails_but_original_is_kept(conn, settings, alice):
    bad = b'Service,Region,Cost\n"unterminated,us-east-1,5\n'
    doc = ingest_upload(conn, settings, alice.id, "bad.csv", bad).document
    res = process_document(conn, settings, alice.id, doc.id)
    assert res.document.status == DocumentStatus.FAILED
    assert res.document.validation_notes and "not well-formed" in res.document.validation_notes[0]
    assert read_original_bytes(conn, settings, alice.id, doc.id) == bad
    assert repo.list_line_items(conn, alice.id, doc.id) == []


def test_unexpected_parser_exception_is_recorded_not_lost(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    res = process_document(conn, settings, alice.id, doc.id, registry={"csv": ExplodingParser()})
    assert res.document.status == DocumentStatus.FAILED      # not stuck in 'processing'
    assert "RuntimeError: kaboom" in res.document.validation_notes[0]
    assert read_original_bytes(conn, settings, alice.id, doc.id)


def test_failed_retry_does_not_destroy_earlier_good_data(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    process_document(conn, settings, alice.id, doc.id)
    process_document(conn, settings, alice.id, doc.id, registry={"csv": ExplodingParser()})
    assert len(repo.list_line_items(conn, alice.id, doc.id)) == 14


def test_reprocessing_replaces_items_and_voids_verification(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    process_document(conn, settings, alice.id, doc.id)
    repo.mark_document_verified(conn, alice.id, doc.id)
    assert repo.get_document(conn, alice.id, doc.id).verified_at is not None
    res = process_document(conn, settings, alice.id, doc.id)
    assert len(repo.list_line_items(conn, alice.id, doc.id)) == 14  # replaced, not doubled
    assert res.document.verified_at is None


def test_user_entered_period_wins_and_disagreement_is_flagged(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"],
                 billing_period_start="2026-01-01", billing_period_end="2026-01-30")
    res = process_document(conn, settings, alice.id, doc.id)
    assert res.document.billing_period_end == "2026-01-30"           # yours kept
    assert res.document.status == DocumentStatus.NEEDS_REVIEW
    assert any("2026-01-31" in n for n in res.document.validation_notes)


def test_malformed_pdf_fails_but_original_is_left_stored(conn, settings, alice):
    # PDF is a supported input type now; this minimal header is not a valid readable PDF.
    pdf = ingest_upload(conn, settings, alice.id, "inv.pdf", b"%PDF-1.4\n%%EOF\n").document
    res = process_document(conn, settings, alice.id, pdf.id)
    assert res.document.status == DocumentStatus.FAILED
    assert res.document.validation_notes
    assert read_original_bytes(conn, settings, alice.id, pdf.id) == b"%PDF-1.4\n%%EOF\n"


def test_cannot_process_another_users_document(conn, settings, alice, bob, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    with pytest.raises(repo.DocumentNotFoundError):
        process_document(conn, settings, bob.id, doc.id)
    assert repo.get_document(conn, alice.id, doc.id).status == DocumentStatus.UPLOADED


def test_processing_persists_across_restart(conn, settings, alice, samples, db_path):
    from billing_explainer.database import connect
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    process_document(conn, settings, alice.id, doc.id)
    conn.close()
    fresh = connect(db_path)
    got = repo.get_document(fresh, alice.id, doc.id)
    assert got.status == DocumentStatus.PROCESSED and len(repo.list_line_items(fresh, alice.id, doc.id)) == 14
    fresh.close()


def test_blocked_reparse_keeps_earlier_good_items(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    process_document(conn, settings, alice.id, doc.id)
    res = process_document(conn, settings, alice.id, doc.id,
                           ParseOptions(column_overrides={"cost": "No such column"}))
    assert res.document.status == DocumentStatus.NEEDS_REVIEW and res.items_saved == 0
    assert len(repo.list_line_items(conn, alice.id, doc.id)) == 14  # evidence not wiped


def test_user_entered_start_date_also_wins(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"],
                 billing_period_start="2026-01-02", billing_period_end="2026-01-31")
    res = process_document(conn, settings, alice.id, doc.id)
    assert (res.document.billing_period_start, res.document.billing_period_end) == ("2026-01-02", "2026-01-31")
    assert any("2026-01-01" in n for n in res.document.validation_notes)


def test_blank_period_is_filled_from_file_without_a_warning(conn, settings, alice, samples):
    doc = upload(conn, settings, alice, samples["aws_style_2026-01.csv"])
    res = process_document(conn, settings, alice.id, doc.id)
    assert (res.document.billing_period_start, res.document.billing_period_end) == ("2026-01-01", "2026-01-31")
    assert not any("differs" in n for n in res.document.validation_notes)
