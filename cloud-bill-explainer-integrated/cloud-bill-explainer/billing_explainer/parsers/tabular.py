"""Shared logic for table-shaped sources (CSV now, Excel sheets later).

Input: a header row and string cells. Output: a ParseResult of normalized LineItems.
The CSV/Excel parsers only differ in HOW they get headers+rows; everything about
mapping columns, parsing values and flagging doubt lives here, once.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from billing_explainer.models import ChargeType, ItemStatus, LineItem
from billing_explainer.parsers.base import Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.values import (
    AmountParseError, normalize_charge_type, normalize_currency, parse_amount,
    parse_timestamp, period_end_date,
)

_PER_CODE_DETAIL_LIMIT = 10  # list this many bad rows individually, then summarize


def normalize_header(text: str) -> str:
    """'lineItem/Unblended Cost' and 'unblended_cost' both -> comparable lowercase text."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# canonical field -> accepted header spellings (normalized on load). Extend freely.
_ALIASES_RAW: dict[str, tuple[str, ...]] = {
    "cost": ("cost", "unblended cost", "lineItem/UnblendedCost", "blended cost", "net cost",
             "effective cost", "amortized cost", "cost in billing currency", "pretax cost",
             "amount", "charge", "total cost", "net unblended cost"),
    "service": ("service", "service name", "product", "product name", "lineItem/ProductCode",
                "product/ProductName", "meter category", "service description", "cloud service"),
    "region": ("region", "region name", "location", "resource location", "product/region"),
    "resource_id": ("resource id", "resourceid", "lineItem/ResourceId", "instance id",
                    "resource", "resource name"),
    "charge_type": ("charge type", "line item type", "lineItem/LineItemType", "type"),
    "usage_quantity": ("usage quantity", "usage amount", "quantity", "lineItem/UsageAmount",
                       "consumed quantity"),
    "usage_unit": ("usage unit", "unit", "unit of measure", "pricing/unit", "pricing unit"),
    "currency": ("currency", "currency code", "billing currency", "lineItem/CurrencyCode"),
    "credit": ("credits", "credit", "promotional credits", "promo credits", "credit amount",
               "total credits"),
    "period_start": ("date", "usage date", "start date", "usage start date",
                     "lineItem/UsageStartDate", "billing period start", "period start"),
    "period_end": ("end date", "usage end date", "lineItem/UsageEndDate",
                   "billing period end", "period end"),
    "account": ("account", "account id", "account name", "account alias", "linked account",
                "linked account id", "lineItem/UsageAccountId", "subscription",
                "subscription id", "subscription name", "project", "project id"),
}
ALIASES = {f: frozenset(normalize_header(a) for a in names) for f, names in _ALIASES_RAW.items()}

REQUIRED = ("cost",)
# Missing -> user is warned (REVIEW). Everything else missing is just INFO.
WARN_IF_MISSING = ("service", "region", "period_start")

# Names shown to users instead of internal field ids.
_LABEL = {"cost": "cost/amount", "service": "service", "region": "region",
          "period_start": "usage date (billing period)", "period_end": "usage end date",
          "currency": "currency", "account": "account"}

_HEADER_CURRENCY = re.compile(r"\(\s*([A-Za-z]{3})\s*\)\s*$")  # 'Cost (USD)'


def detect_provider(normalized_headers: set[str]) -> str | None:
    """Heuristic from header style. Returns None rather than guessing when unsure."""
    if any(h.startswith("lineitem") for h in normalized_headers) or "productregion" in normalized_headers:
        return "aws"
    if {"costinbillingcurrency", "metercategory"} & normalized_headers:
        return "azure"
    if {"servicedescription", "skudescription"} <= normalized_headers:
        return "gcp"
    return None


@dataclass
class _Columns:
    index: dict[str, int]
    currency_hint: str | None


def _resolve_columns(headers: list[str], options: ParseOptions, issues: list[Issue]) -> _Columns | None:
    """Map canonical fields to column indexes. Returns None if a REQUIRED field can't be
    resolved (BLOCKING issue already recorded). Never silently picks between candidates."""
    cleaned = [_HEADER_CURRENCY.sub("", h).strip() for h in headers]
    norm = [normalize_header(h) for h in cleaned]
    hint = None
    for h in headers:
        m = _HEADER_CURRENCY.search(h)
        if m and normalize_header(_HEADER_CURRENCY.sub("", h)) in ALIASES["cost"]:
            hint = m[1].upper()

    index: dict[str, int] = {}
    blocked = False
    for fld, aliases in ALIASES.items():
        override = options.column_overrides.get(fld)
        if override is not None:
            if isinstance(override, int) and 0 <= override < len(headers):
                index[fld] = override
                continue
            matches = [i for i, h in enumerate(headers) if h.strip().lower() == str(override).strip().lower()]
            if len(matches) == 1:
                index[fld] = matches[0]
                continue
            issues.append(Issue(Severity.BLOCKING if fld in REQUIRED else Severity.REVIEW, "bad_override",
                                f"Chosen column {override!r} for '{fld}' was not found exactly once."))
            blocked = blocked or fld in REQUIRED
            continue

        candidates = [i for i, n in enumerate(norm) if n in aliases]
        if len(candidates) == 1:
            index[fld] = candidates[0]
        elif len(candidates) > 1:
            names = ", ".join(repr(headers[i]) for i in candidates)
            sev = Severity.BLOCKING if fld in REQUIRED else Severity.REVIEW
            issues.append(Issue(sev, "ambiguous_column",
                                f"More than one column could be the {_LABEL.get(fld, fld)}: {names}. Please choose one."))
            blocked = blocked or fld in REQUIRED
        elif fld in REQUIRED:
            issues.append(Issue(Severity.BLOCKING, "missing_column",
                                f"No {_LABEL.get(fld, fld)} column found. Headers seen: {', '.join(map(repr, headers))}."))
            blocked = True
        elif fld in WARN_IF_MISSING:
            issues.append(Issue(Severity.REVIEW, "missing_column", f"No {_LABEL.get(fld, fld)} column found."))
    return None if blocked else _Columns(index, hint)


def normalize_table(headers: list[str], rows: list[list[str]], options: ParseOptions,
                    *, locator_prefix: str = "") -> ParseResult:
    """headers: first row. rows: data rows (strings). Row numbers in locators count
    records with the header as row 1 (blank lines are not counted)."""
    headers = [h.strip() for h in headers]
    result = ParseResult(cloud_provider=detect_provider({normalize_header(h) for h in headers}))
    cols = _resolve_columns(headers, options, result.issues)
    if cols is None:
        return result
    ix = cols.index
    fallback_currency = cols.currency_hint or normalize_currency(options.currency)
    if options.currency and normalize_currency(options.currency) is None:
        result.issues.append(Issue(Severity.REVIEW, "invalid_currency_option",
                                   f"Currency {options.currency!r} is not a 3-letter code; ignored."))

    def cell(cells: list[str], fld: str) -> str:
        i = ix.get(fld)
        return cells[i] if i is not None and i < len(cells) else ""

    row_problems: dict[str, list[Issue]] = {}  # code -> issues, capped when emitted

    def problem(code: str, message: str, locator: str, severity: Severity = Severity.REVIEW) -> None:
        row_problems.setdefault(code, []).append(Issue(severity, code, message, locator))

    items: list[LineItem] = []
    starts: list[date] = []
    ends: list[date] = []
    accounts: set[str] = set()
    missing_currency_rows: list[str] = []
    positive_credit_rows = skipped_total_rows = 0
    skipped_total_amount = Decimal(0)

    for number, raw_row in enumerate(rows, start=2):  # row 1 is the header
        cells = [c.strip() for c in raw_row] + [""] * (len(headers) - len(raw_row))
        if not any(cells):
            continue
        locator = f"{locator_prefix}row {number}"
        raw_record = json.dumps(dict(zip(headers, cells)), ensure_ascii=False)
        service = cell(cells, "service") or None

        # Totals rows would double-count if imported as line items: skip, but say so.
        label = service if "service" in ix else (cells[0] if cells else "")
        if normalize_header(label or "").startswith(("total", "grandtotal", "subtotal")):
            skipped_total_rows += 1
            try:
                skipped_total_amount += parse_amount(cell(cells, "cost"))
            except AmountParseError:
                pass
            continue

        needs_review = False

        # --- cost (mandatory per row) ---
        try:
            cost = parse_amount(cell(cells, "cost"))
        except AmountParseError as exc:
            problem("bad_amount", f"Row excluded, cost unreadable: {exc}", locator)
            continue

        # --- currency: row value, else header hint, else user option ---
        cur_raw = cell(cells, "currency")
        currency = normalize_currency(cur_raw) if cur_raw else None
        if cur_raw and currency is None:
            problem("invalid_currency", f"Unrecognized currency {cur_raw!r}", locator)
        currency = currency or fallback_currency
        if currency is None:
            missing_currency_rows.append(locator)
            continue

        # --- charge type ---
        charge_type = ChargeType.USAGE
        ct_raw = cell(cells, "charge_type")
        if ct_raw:
            parsed_ct = normalize_charge_type(ct_raw)
            if parsed_ct is None:
                charge_type, needs_review = ChargeType.OTHER, True
                problem("unknown_charge_type", f"Unrecognized charge type {ct_raw!r}; stored as 'other'", locator)
            else:
                charge_type = parsed_ct
        if charge_type == ChargeType.CREDIT and cost > 0:
            cost, positive_credit_rows = -cost, positive_credit_rows + 1  # credits are stored negative

        # --- quantity ---
        quantity = None
        if cell(cells, "usage_quantity"):
            try:
                quantity = parse_amount(cell(cells, "usage_quantity"))
            except AmountParseError as exc:
                needs_review = True
                problem("bad_quantity", f"Usage quantity unreadable (left empty): {exc}", locator)

        # --- dates ---
        p_start = p_end = None
        for fld in ("period_start", "period_end"):
            raw = cell(cells, fld)
            if not raw:
                continue
            if fld == "period_start":
                parsed = parse_timestamp(raw)
                p_start = parsed[0] if parsed else None
                if p_start is None:
                    needs_review = True
                    problem("bad_date", f"Unrecognized date {raw!r} (use YYYY-MM-DD)", locator)
            else:
                p_end = period_end_date(raw, p_start)
                if p_end is None:
                    needs_review = True
                    problem("bad_date", f"Unrecognized end date {raw!r} (use YYYY-MM-DD)", locator)
        if p_start:
            starts.append(p_start)
        if p_end or p_start:
            ends.append(p_end or p_start)

        if cell(cells, "account"):
            accounts.add(cell(cells, "account"))

        common = dict(service=service, region=cell(cells, "region") or None,
                      resource_id=cell(cells, "resource_id") or None, currency=currency,
                      period_start=p_start.isoformat() if p_start else None,
                      period_end=(p_end or p_start).isoformat() if (p_end or p_start) else None,
                      raw_record=raw_record)
        status = ItemStatus.NEEDS_REVIEW if needs_review else ItemStatus.EXTRACTED
        items.append(LineItem(cost_amount=cost, source_locator=locator, charge_type=charge_type,
                              usage_quantity=quantity, usage_unit=cell(cells, "usage_unit") or None,
                              status=status, **common))

        # A separate credits column becomes its own negative CREDIT line item, so credits
        # always appear separately from usage (test case TC2).
        credit_raw = cell(cells, "credit")
        if credit_raw:
            try:
                credit = parse_amount(credit_raw)
                if not credit.is_zero():
                    items.append(LineItem(cost_amount=-abs(credit), charge_type=ChargeType.CREDIT,
                                          source_locator=f"{locator} (credits column)",
                                          status=ItemStatus.EXTRACTED, **common))
            except AmountParseError as exc:
                problem("bad_amount", f"Credits value unreadable (excluded): {exc}", locator)

    # ---------- document-level findings ----------
    if missing_currency_rows:
        result.issues.append(Issue(
            Severity.BLOCKING, "missing_currency",
            f"{len(missing_currency_rows)} row(s) have no currency and the file does not state one. "
            "Choose the currency to continue.", missing_currency_rows[0]))
        items = []  # nothing is saved until the currency is known; records need one
    for code, found in row_problems.items():
        result.issues.extend(found[:_PER_CODE_DETAIL_LIMIT])
        if len(found) > _PER_CODE_DETAIL_LIMIT:
            result.issues.append(Issue(found[0].severity, code,
                                       f"...and {len(found) - _PER_CODE_DETAIL_LIMIT} more rows with this problem."))
    if skipped_total_rows:
        result.issues.append(Issue(
            Severity.INFO, "skipped_total_rows",
            f"Skipped {skipped_total_rows} totals row(s) (sum {skipped_total_amount}) to avoid double counting."))
    if positive_credit_rows:
        result.issues.append(Issue(
            Severity.INFO, "credit_sign_normalized",
            f"{positive_credit_rows} credit row(s) had positive amounts; stored as negative. Originals kept in raw_record."))

    result.line_items = items
    currencies = {i.currency for i in items}
    if len(currencies) == 1:
        result.currency = next(iter(currencies))
    elif len(currencies) > 1:
        result.issues.append(Issue(Severity.REVIEW, "mixed_currencies",
                                   f"File mixes currencies ({', '.join(sorted(currencies))}); totals are never combined across currencies."))
    if starts or ends:
        result.period_start = min(starts or ends).isoformat()
        result.period_end = max(ends or starts).isoformat()
    elif "period_start" in ix and items:
        result.issues.append(Issue(Severity.REVIEW, "missing_period", "No billing period dates could be read; please enter them."))
    if len(accounts) == 1:
        result.account_alias = next(iter(accounts))
    elif len(accounts) > 1:
        result.issues.append(Issue(Severity.REVIEW, "multiple_accounts",
                                   f"File contains {len(accounts)} accounts; no single account alias was set."))
    if not items and not result.has(Severity.BLOCKING):
        result.issues.append(Issue(Severity.FAILED, "no_usable_rows", "No usable rows were found in the file."))
    return result
