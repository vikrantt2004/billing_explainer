"""Synthetic billing exports for demos and tests. No real data.  Run:

    python -m billing_explainer.sample_data samples

Scenario: Dec 2025 -> Jan 2026. January's bill rises because (1) two forgotten batch
instances appear in us-east-1, (2) new spend appears in ap-southeast-2, a region outside
the usual us-east-1/eu-west-1 filter (test case TC3), and (3) promotional credits arrive
as separate Credit lines (TC2). Costs are strings so they stay exact.
"""
from __future__ import annotations

import csv
import sys
from decimal import Decimal
from pathlib import Path

ACCOUNT = "123456789012"
AWS_HEADER = ["lineItem/UsageAccountId", "lineItem/ProductCode", "product/region",
              "lineItem/ResourceId", "lineItem/LineItemType", "lineItem/UsageStartDate",
              "lineItem/UsageEndDate", "lineItem/UsageAmount", "pricing/unit",
              "lineItem/UnblendedCost", "lineItem/CurrencyCode"]

# (service, region, resource, type, quantity, unit, cost)
DEC_ROWS = [
    ("AmazonEC2", "us-east-1", "i-web-01", "Usage", "744", "Hrs", "1100.00"),
    ("AmazonEC2", "us-east-1", "i-web-02", "Usage", "744", "Hrs", "1100.00"),
    ("AmazonEC2", "eu-west-1", "i-api-01", "Usage", "744", "Hrs", "900.50"),
    ("AmazonRDS", "us-east-1", "db-main", "Usage", "744", "Hrs", "650.25"),
    ("AmazonS3", "us-east-1", "bucket-logs", "Usage", "5200", "GB-Mo", "119.60"),
    ("AmazonS3", "eu-west-1", "bucket-assets", "Usage", "800", "GB-Mo", "18.40"),
    ("AWSLambda", "us-east-1", "fn-ingest", "Usage", "9000000", "Requests", "44.9873"),
    ("AmazonCloudWatch", "us-east-1", "", "Usage", "120", "GB", "60.0000"),
]
JAN_ROWS = [
    ("AmazonEC2", "us-east-1", "i-web-01", "Usage", "744", "Hrs", "1100.00"),
    ("AmazonEC2", "us-east-1", "i-web-02", "Usage", "744", "Hrs", "1100.00"),
    ("AmazonEC2", "us-east-1", "i-batch-07", "Usage", "744", "Hrs", "2480.00"),   # spike
    ("AmazonEC2", "us-east-1", "i-batch-08", "Usage", "744", "Hrs", "2480.00"),   # spike
    ("AmazonEC2", "eu-west-1", "i-api-01", "Usage", "744", "Hrs", "905.10"),
    ("AmazonRDS", "us-east-1", "db-main", "Usage", "744", "Hrs", "650.25"),
    ("AmazonS3", "us-east-1", "bucket-logs", "Usage", "5600", "GB-Mo", "128.80"),
    ("AmazonS3", "eu-west-1", "bucket-assets", "Usage", "820", "GB-Mo", "18.90"),
    ("AWSLambda", "us-east-1", "fn-ingest", "Usage", "9800000", "Requests", "47.2210"),
    ("AmazonCloudWatch", "us-east-1", "", "Usage", "143", "GB", "71.5000"),
    ("AmazonEC2", "ap-southeast-2", "i-orphan-3", "Usage", "744", "Hrs", "612.40"),   # TC3
    ("AWSDataTransfer", "ap-southeast-2", "", "Usage", "1800", "GB", "162.00"),       # TC3
    ("AmazonEC2", "us-east-1", "", "Credit", "", "", "-500.00"),                      # TC2
    ("AmazonS3", "us-east-1", "", "Credit", "", "", "-250.00"),                       # TC2
]


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows([header, *rows])


def _aws_rows(data: list[tuple], start: str, end_exclusive: str) -> list[list[str]]:
    return [[ACCOUNT, svc, region, res, typ, f"{start}T00:00:00Z", f"{end_exclusive}T00:00:00Z", qty, unit, cost, "USD"]
            for svc, region, res, typ, qty, unit, cost in data]


def expected_totals() -> dict:
    """Ground truth for tests/demos, computed from the rows above with Decimal."""
    def usage(rows): return sum((Decimal(r[6]) for r in rows if r[3] != "Credit"), Decimal(0))
    by_region: dict[str, Decimal] = {}
    for r in JAN_ROWS:
        if r[3] != "Credit":
            by_region[r[1]] = by_region.get(r[1], Decimal(0)) + Decimal(r[6])
    credits = sum((Decimal(r[6]) for r in JAN_ROWS if r[3] == "Credit"), Decimal(0))
    return {"dec_usage": usage(DEC_ROWS), "jan_usage": usage(JAN_ROWS), "jan_credits": credits,
            "jan_net": usage(JAN_ROWS) + credits, "jan_usage_by_region": by_region}


def write_samples(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = {k: directory / k for k in (
        "aws_style_2025-12.csv", "aws_style_2026-01.csv", "simple_export_2026-01.csv",
        "ambiguous_columns.csv", "missing_currency.csv", "messy_amounts.csv")}
    _write(files["aws_style_2025-12.csv"], AWS_HEADER, _aws_rows(DEC_ROWS, "2025-12-01", "2026-01-01"))
    _write(files["aws_style_2026-01.csv"], AWS_HEADER, _aws_rows(JAN_ROWS, "2026-01-01", "2026-02-01"))

    simple = [[s, r, res, t, q, u, c] for s, r, res, t, q, u, c in JAN_ROWS]
    simple.append(["Total", "", "", "", "", "", str(expected_totals()["jan_net"])])  # must be skipped
    _write(files["simple_export_2026-01.csv"],
           ["Service", "Region", "Resource ID", "Charge Type", "Usage Quantity", "Usage Unit", "Cost (USD)"], simple)

    _write(files["ambiguous_columns.csv"], ["Service", "Region", "Cost", "Unblended Cost", "Currency"],
           [["AmazonEC2", "us-east-1", "10.00", "10.00", "USD"], ["AmazonS3", "eu-west-1", "3.20", "3.20", "USD"]])
    _write(files["missing_currency.csv"], ["Service", "Region", "Cost"],
           [["AmazonEC2", "us-east-1", "10.00"], ["AmazonS3", "eu-west-1", "3.20"]])
    _write(files["messy_amounts.csv"], ["Service", "Region", "Cost", "Currency"],
           [["AmazonEC2", "us-east-1", "$1,234.50", "USD"], ["AmazonS3", "eu-west-1", "(12.00)", "USD"],
            ["AWSLambda", "us-east-1", "abc", "USD"], ["AmazonRDS", "us-east-1", "", "USD"],
            ["AmazonEC2", "eu-west-1", "1.234,56", "USD"]])
    return files


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    for name in write_samples(target).values():
        print("wrote", name)
    t = expected_totals()
    print(f"\nExpected: Dec usage {t['dec_usage']} | Jan usage {t['jan_usage']} | "
          f"Jan credits {t['jan_credits']} | Jan net {t['jan_net']}")
    print("Jan usage by region:", {k: str(v) for k, v in t["jan_usage_by_region"].items()})
