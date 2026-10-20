"""Cell-level parsing rules."""
from datetime import date
from decimal import Decimal

import pytest

from billing_explainer.models import ChargeType
from billing_explainer.parsers.values import (
    AmountParseError, normalize_charge_type, normalize_currency, parse_amount,
    parse_timestamp, period_end_date,
)


def test_amounts_that_must_parse_exactly():
    assert parse_amount("1234.50") == Decimal("1234.50")
    assert parse_amount("$1,234.50") == Decimal("1234.50")
    assert parse_amount("€ 3.10") == Decimal("3.10")
    assert parse_amount("(12.00)") == Decimal("-12.00")
    assert parse_amount("-5") == Decimal("-5")
    assert parse_amount("5-") == Decimal("-5")
    assert parse_amount(".5") == Decimal("0.5")
    assert parse_amount(" 12 ") == Decimal("12")
    assert parse_amount("1e-5") == Decimal("0.00001")
    assert str(parse_amount("0.0000166667")) == "0.0000166667"  # no precision lost
    assert str(parse_amount("-0")) == "0"                        # never '-0'
    assert parse_amount(7) == Decimal(7)


def test_ambiguous_or_garbage_amounts_are_refused():
    for bad in ["", "   ", "abc", "1.234,56", "12,5", "1,2,3", "12.5.3", "(-5)", "--5",
                "NaN", "Infinity", "1E999", "()", "(", "+", "1" * 50, None, 0.1, True]:
        with pytest.raises(AmountParseError):
            parse_amount(bad)


def test_timestamps():
    assert parse_timestamp("2026-01-31") == (date(2026, 1, 31), False)
    assert parse_timestamp("2026-01-31T23:00:00Z") == (date(2026, 1, 31), False)
    assert parse_timestamp("2026-02-01T00:00:00Z") == (date(2026, 2, 1), True)
    assert parse_timestamp("2026/02/01 00:00") == (date(2026, 2, 1), True)
    assert parse_timestamp("2026-02-01T00:00:00+05:30") == (date(2026, 2, 1), True)
    for bad in ["01/02/2026", "2026-13-01", "2026-02-30", "last month", ""]:
        assert parse_timestamp(bad) is None, bad


def test_exclusive_midnight_end_belongs_to_previous_day():
    assert period_end_date("2026-02-01T00:00:00Z", date(2026, 1, 1)) == date(2026, 1, 31)
    assert period_end_date("2026-01-31T23:00:00Z", None) == date(2026, 1, 31)
    assert period_end_date("2026-01-31", None) == date(2026, 1, 31)  # date-only end is inclusive
    assert period_end_date("2026-01-01T00:00:00Z", date(2026, 1, 1)) == date(2026, 1, 1)  # never before start
    assert period_end_date("garbage", None) is None


def test_currency_and_charge_type_normalization():
    assert normalize_currency("usd") == "USD"
    assert normalize_currency(" EUR ") == "EUR"
    for bad in ["US", "$", "", None, "USDX", "12A"]:
        assert normalize_currency(bad) is None, bad
    assert normalize_charge_type("Usage") == ChargeType.USAGE
    assert normalize_charge_type("DiscountedUsage") == ChargeType.USAGE
    assert normalize_charge_type("Credit") == ChargeType.CREDIT
    assert normalize_charge_type("Promotional Credit") == ChargeType.CREDIT
    assert normalize_charge_type("Tax") == ChargeType.TAX
    assert normalize_charge_type("Refund") == ChargeType.ADJUSTMENT
    assert normalize_charge_type("RIFee") == ChargeType.FEE
    assert normalize_charge_type("SavingsPlanNegation") is None
