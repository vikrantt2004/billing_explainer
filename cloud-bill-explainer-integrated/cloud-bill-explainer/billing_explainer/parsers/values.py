"""Cell-level parsing: amounts, timestamps, currencies, charge types.

Policy: be strict and say so. Anything ambiguous is rejected (returns None / raises)
so the caller can flag it for review, rather than guessing a number into an invoice.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from billing_explainer.models import ChargeType

_SYMBOLS = "$€£¥₹"
_STRIP = re.compile(r"[\s" + re.escape(_SYMBOLS) + r"]")
# US/ISO style only: optional comma thousands in groups of exactly 3, '.' decimal.
_NUMBER = re.compile(r"(?P<int>\d{1,3}(?:,\d{3})+|\d*)(?P<frac>\.\d+)?(?P<exp>[eE][+-]?\d+)?")


class AmountParseError(ValueError):
    pass


def parse_amount(value: "str | int | Decimal | None") -> Decimal:
    """'$1,234.50' -> 1234.50; '(12.00)' -> -12.00; '1e-5' -> 0.00001.

    Rejects: blanks, '1.234,56' and '12,5' (European format is ambiguous with US
    thousands separators), '12.5.3', '(-5)', NaN/Infinity, absurd exponents.
    Never goes through float, so no precision is lost.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AmountParseError("amount must be finite")
        return value
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise AmountParseError(f"cannot read an amount from {value!r}")
    s = _STRIP.sub("", str(value))
    if not s:
        raise AmountParseError("empty amount")
    if len(s) > 40:
        raise AmountParseError("amount text is implausibly long")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s and s[0] in "+-":
        if negative:
            raise AmountParseError(f"ambiguous sign in {value!r}")
        negative, s = s[0] == "-", s[1:]
    elif s.endswith("-"):
        negative, s = True, s[:-1]

    m = _NUMBER.fullmatch(s)
    if not m or not (m["int"] or m["frac"]):
        raise AmountParseError(f"not a valid amount: {value!r}")
    number = Decimal((m["int"] or "0").replace(",", "") + (m["frac"] or "") + (m["exp"] or ""))
    if abs(number.adjusted()) > 30:
        raise AmountParseError(f"implausible magnitude: {value!r}")
    result = -number if negative else number
    return abs(result) if result.is_zero() else result  # avoid Decimal('-0')


_TIMESTAMP = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?)?")


def parse_timestamp(text: str) -> tuple[date, bool] | None:
    """Parse ISO-like dates/timestamps. Returns (date, is_midnight_time) or None.

    `is_midnight_time` is True only when a time part is present and equals 00:00:00;
    cost exports use that as an EXCLUSIVE end ('2026-02-01T00:00:00Z' ends January).
    Timezone offsets are ignored (the date is taken as written). Day-first/month-first
    formats like 01/02/2026 are refused because they are ambiguous.
    """
    m = _TIMESTAMP.fullmatch((text or "").strip())
    if not m:
        return None
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    has_time = m[4] is not None
    midnight = has_time and int(m[4]) == 0 and int(m[5]) == 0 and int(m[6] or 0) == 0
    return d, midnight


def period_end_date(end_text: str, start: date | None) -> date | None:
    """Inclusive last day for an end timestamp, honoring the exclusive-midnight convention."""
    parsed = parse_timestamp(end_text)
    if parsed is None:
        return None
    end, midnight = parsed
    if midnight:
        end -= timedelta(days=1)
        if start is not None and end < start:
            end = start
    return end


def normalize_currency(text: str | None) -> str | None:
    """'usd' -> 'USD'. Symbols ('$') are NOT mapped: '$' could be USD, CAD, AUD..."""
    t = (text or "").strip().upper()
    return t if re.fullmatch(r"[A-Z]{3}", t) else None


def normalize_charge_type(text: str) -> ChargeType | None:
    """Map vendor wording to our enum; None means 'unrecognized' (caller flags it)."""
    t = re.sub(r"[^a-z]", "", text.lower())
    if "credit" in t or "promo" in t:
        return ChargeType.CREDIT
    if "tax" in t or "vat" in t:
        return ChargeType.TAX
    if "refund" in t or "adjust" in t:
        return ChargeType.ADJUSTMENT
    if "fee" in t:
        return ChargeType.FEE
    if "usage" in t:
        return ChargeType.USAGE
    return None
