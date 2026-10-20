"""Command-line tour of the pipeline (no UI yet).

    python -m billing_explainer.demo            # uses a throwaway temp database
"""
from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from billing_explainer import repository as repo
from billing_explainer.config import Settings
from billing_explainer.database import connect, init_db
from billing_explainer.ingest import ingest_upload
from billing_explainer.models import ChargeType
from billing_explainer.processing import process_document
from billing_explainer.sample_data import write_samples


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        settings = Settings(root / "demo.db", root / "uploads", 25 * 1024 * 1024)
        conn = connect(settings.db_path)
        init_db(conn)
        user = repo.get_or_create_user(conn, "demo", "demo-user", "demo@example.com")

        for name, path in write_samples(root / "samples").items():
            ing = ingest_upload(conn, settings, user.id, name, path.read_bytes())
            res = process_document(conn, settings, user.id, ing.document.id)
            d = res.document
            print(f"\n=== {name}\n    status={d.status.value}  provider={d.cloud_provider}  account={d.account_alias}"
                  f"  period={d.billing_period_start}..{d.billing_period_end}  items={res.items_saved}")
            items = repo.list_line_items(conn, user.id, d.id)
            usage = sum((i.cost_amount for i in items if i.charge_type == ChargeType.USAGE), Decimal(0))
            credit = sum((i.cost_amount for i in items if i.charge_type == ChargeType.CREDIT), Decimal(0))
            if items:
                print(f"    usage total={usage}  credits={credit}")
            for note in d.validation_notes:
                print("    -", note)

        dup = ingest_upload(conn, settings, user.id, "again.csv", (root / "samples" / "aws_style_2026-01.csv").read_bytes())
        print(f"\n=== re-upload of the January file -> {dup.outcome.value}: {dup.message}")
        conn.close()


if __name__ == "__main__":
    main()
