"""CSV parsing end-to-end through the shared table normalizer."""
from decimal import Decimal

import pytest

from billing_explainer.models import ChargeType, ItemStatus
from billing_explainer.parsers import ParseOptions, Severity
from billing_explainer.parsers.csv_parser import CsvParser
from billing_explainer.sample_data import expected_totals, write_samples


@pytest.fixture
def samples(tmp_path):
    return write_samples(tmp_path / "samples")


def parse(path_or_bytes, **opts):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else path_or_bytes.read_bytes()
    return CsvParser().parse(data, ParseOptions(**opts))


def codes(result, severity=None):
    return [i.code for i in result.issues if severity is None or i.severity == severity]


def total(items):
    return sum((i.cost_amount for i in items), Decimal(0))


def test_generator_matches_hand_computed_totals():
    t = expected_totals()
    assert t["dec_usage"] == Decimal("3993.7373")
    assert t["jan_usage"] == Decimal("9756.1710")
    assert t["jan_credits"] == Decimal("-750.00")
    assert t["jan_usage_by_region"]["ap-southeast-2"] == Decimal("774.40")


def test_aws_style_january_export(samples):
    r = parse(samples["aws_style_2026-01.csv"])
    assert [i for i in r.issues if i.severity != Severity.INFO] == []
    assert len(r.line_items) == 14
    usage = [i for i in r.line_items if i.charge_type == ChargeType.USAGE]
    credits = [i for i in r.line_items if i.charge_type == ChargeType.CREDIT]
    assert total(usage) == Decimal("9756.1710")           # TC1-style: exact sum
    assert total(credits) == Decimal("-750.00")           # TC2: credits kept separate
    assert len(credits) == 2 and all(i.cost_amount < 0 for i in credits)
    by_region = {}
    for i in usage:
        by_region[i.region] = by_region.get(i.region, Decimal(0)) + i.cost_amount
    assert by_region["ap-southeast-2"] == Decimal("774.40")  # TC3: unexpected region surfaced
    assert set(by_region) == {"us-east-1", "eu-west-1", "ap-southeast-2"}
    # metadata detected from the file
    assert (r.cloud_provider, r.account_alias, r.currency) == ("aws", "123456789012", "USD")
    assert (r.period_start, r.period_end) == ("2026-01-01", "2026-01-31")  # exclusive end handled
    # provenance
    first = r.line_items[0]
    assert first.source_locator == "row 2" and "i-web-01" in first.raw_record
    assert all(i.status == ItemStatus.EXTRACTED for i in r.line_items)


def test_december_period_and_subcent_precision(samples):
    r = parse(samples["aws_style_2025-12.csv"])
    assert (r.period_start, r.period_end) == ("2025-12-01", "2025-12-31")
    assert total(r.line_items) == Decimal("3993.7373")
    lam = [i for i in r.line_items if i.service == "AWSLambda"][0]
    assert lam.cost_amount == Decimal("44.9873") and lam.usage_quantity == Decimal("9000000")


def test_simple_export_skips_total_row_and_uses_header_currency(samples):
    r = parse(samples["simple_export_2026-01.csv"])
    assert len(r.line_items) == 14                      # the 'Total' row was NOT imported
    assert "skipped_total_rows" in codes(r, Severity.INFO)
    assert r.currency == "USD" and r.cloud_provider is None
    assert "missing_column" in codes(r, Severity.REVIEW)  # no date column -> needs review
    assert r.period_start is None


def test_ambiguous_cost_columns_block_until_user_chooses(samples):
    r = parse(samples["ambiguous_columns.csv"])
    assert r.line_items == [] and codes(r, Severity.BLOCKING) == ["ambiguous_column"]
    assert "'Cost'" in r.issues[0].message and "'Unblended Cost'" in r.issues[0].message
    fixed = parse(samples["ambiguous_columns.csv"], column_overrides={"cost": "Unblended Cost"})
    assert len(fixed.line_items) == 2 and not fixed.has(Severity.BLOCKING)
    by_index = parse(samples["ambiguous_columns.csv"], column_overrides={"cost": 2})
    assert len(by_index.line_items) == 2
    wrong = parse(samples["ambiguous_columns.csv"], column_overrides={"cost": "Nope"})
    assert wrong.line_items == [] and codes(wrong, Severity.BLOCKING) == ["bad_override"]


def test_missing_cost_column_is_reported_not_guessed():
    r = parse(b"Service,Region,Notes\nEC2,us-east-1,hello\n")
    assert r.line_items == [] and codes(r, Severity.BLOCKING) == ["missing_column"]
    assert "'Notes'" in r.issues[0].message  # tells the user what headers it saw


def test_missing_currency_blocks_then_option_unblocks(samples):
    r = parse(samples["missing_currency.csv"])
    assert r.line_items == [] and codes(r, Severity.BLOCKING) == ["missing_currency"]
    ok = parse(samples["missing_currency.csv"], currency="usd")
    assert len(ok.line_items) == 2 and not ok.has(Severity.BLOCKING) and ok.currency == "USD"
    bad_option = parse(samples["missing_currency.csv"], currency="$")
    assert "missing_currency" in codes(bad_option) and "invalid_currency_option" in codes(bad_option)


def test_messy_amounts_nothing_is_silently_dropped(samples):
    r = parse(samples["messy_amounts.csv"])
    assert [i.cost_amount for i in r.line_items] == [Decimal("1234.50"), Decimal("-12.00")]
    bad = [i for i in r.issues if i.code == "bad_amount"]
    assert [i.locator for i in bad] == ["row 4", "row 5", "row 6"]  # 3 excluded rows, each located
    assert len(r.line_items) + len(bad) == 5                         # every data row accounted for


def test_mixed_currencies_are_flagged_and_not_merged():
    r = parse(b"Service,Region,Cost,Currency\nEC2,us-east-1,10,USD\nEC2,eu-west-1,9,EUR\n")
    assert "mixed_currencies" in codes(r, Severity.REVIEW)
    assert r.currency is None and {i.currency for i in r.line_items} == {"USD", "EUR"}


def test_credits_column_becomes_separate_negative_items():
    r = parse(b"Service,Region,Cost,Credits,Currency\nEC2,us-east-1,100.00,-20.00,USD\nS3,us-east-1,5,0,USD\n")
    credits = [i for i in r.line_items if i.charge_type == ChargeType.CREDIT]
    assert [(i.cost_amount, i.service, i.source_locator) for i in credits] == \
           [(Decimal("-20.00"), "EC2", "row 2 (credits column)")]
    assert total([i for i in r.line_items if i.charge_type == ChargeType.USAGE]) == Decimal("105.00")
    pos = parse(b"Service,Region,Cost,Credits,Currency\nEC2,us-east-1,100,20,USD\n")
    assert [i.cost_amount for i in pos.line_items if i.charge_type == ChargeType.CREDIT] == [Decimal("-20")]


def test_positive_credit_line_is_stored_negative_and_reported():
    r = parse(b"Service,Region,Charge Type,Cost,Currency\nEC2,us-east-1,Credit,300,USD\n")
    assert r.line_items[0].cost_amount == Decimal("-300")
    assert "credit_sign_normalized" in codes(r, Severity.INFO)
    assert '"300"' in r.line_items[0].raw_record  # original preserved


def test_unknown_charge_type_and_bad_date_mark_items_for_review():
    r = parse(b"Service,Region,Charge Type,Cost,Currency,Date\n"
              b"EC2,us-east-1,SavingsPlanNegation,5,USD,2026-01-01\n"
              b"EC2,us-east-1,Usage,6,USD,01/02/2026\n")
    assert [i.status for i in r.line_items] == [ItemStatus.NEEDS_REVIEW, ItemStatus.NEEDS_REVIEW]
    assert r.line_items[0].charge_type == ChargeType.OTHER
    assert set(codes(r, Severity.REVIEW)) >= {"unknown_charge_type", "bad_date"}


def test_multiple_accounts_leave_alias_unset():
    r = parse(b"Account,Service,Region,Cost,Currency\nA1,EC2,us-east-1,1,USD\nA2,EC2,us-east-1,2,USD\n")
    assert r.account_alias is None and "multiple_accounts" in codes(r, Severity.REVIEW)


def test_delimiters_bom_and_encodings():
    semi = parse(b"Service;Region;Cost;Currency\nEC2;us-east-1;10.5;USD\n")
    assert semi.line_items[0].cost_amount == Decimal("10.5")
    bom = parse(b"\xef\xbb\xbfService,Region,Cost,Currency\nEC2,us-east-1,1,USD\n")
    assert len(bom.line_items) == 1
    cp = parse("Service,Region,Cost,Currency\nCaf\u00e9,us-east-1,1,USD\n".encode("cp1252"))
    assert cp.line_items[0].service == "Caf\u00e9" and "encoding" in codes(cp, Severity.INFO)
    undecodable = parse(b"Service,Region,Cost\nS\x81,us,1\n")
    assert codes(undecodable, Severity.FAILED) == ["decode_error"]


def test_malformed_files_fail_cleanly():
    cases = [
        ("malformed_csv", b'Service,Region,Cost\n"unterminated,us-east-1,5\n'),
        ("malformed_csv", b"Service,Region,Cost\nEC2,us-east-1,5\nEC2,us-east-1,5,EXTRA\n"),
        ("empty_file", b""),
        ("no_data_rows", b"Service,Region,Cost,Currency\n"),
    ]
    for expected, data in cases:
        r = parse(data)
        assert codes(r, Severity.FAILED) == [expected], data
        assert r.line_items == []
    only_bad = parse(b"Service,Region,Cost,Currency\nEC2,us-east-1,abc,USD\n")
    assert codes(only_bad, Severity.FAILED) == ["no_usable_rows"]


def test_many_bad_rows_are_summarized_not_flooded():
    rows = b"".join(b"EC2,us-east-1,bad%d,USD\n" % i for i in range(30))
    r = parse(b"Service,Region,Cost,Currency\n" + rows + b"EC2,us-east-1,1,USD\n")
    bad = [i for i in r.issues if i.code == "bad_amount"]
    assert len(bad) == 11 and "20 more" in bad[-1].message
    assert len(r.line_items) == 1


def test_ragged_short_row_is_reported_not_guessed():
    r = parse(b"Service,Region,Cost,Currency\nEC2,us-east-1,5,USD\nS3,eu-west-1\n")
    assert len(r.line_items) == 1 and [i.locator for i in r.issues if i.code == "bad_amount"] == ["row 3"]


def test_duplicate_headers_are_handled_by_position():
    r = parse(b"Service,Region,Cost,Cost,Currency\nEC2,us-east-1,1,1,USD\n")
    assert codes(r, Severity.BLOCKING) == ["ambiguous_column"]
    assert len(parse(b"Service,Region,Cost,Cost,Currency\nEC2,us-east-1,1,2,USD\n",
                     column_overrides={"cost": 3}).line_items) == 1
