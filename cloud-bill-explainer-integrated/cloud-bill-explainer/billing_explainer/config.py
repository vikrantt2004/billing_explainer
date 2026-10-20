"""Central settings, overridable by environment variables (no secrets live here)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path
    upload_dir: Path
    max_upload_bytes: int


def load_settings() -> Settings:
    return Settings(
        db_path=Path(os.environ.get("BILLING_DB_PATH", PROJECT_ROOT / "billing.db")),
        upload_dir=Path(os.environ.get("BILLING_UPLOAD_DIR", PROJECT_ROOT / "uploads")),
        max_upload_bytes=int(float(os.environ.get("BILLING_MAX_UPLOAD_MB", "25")) * 1024 * 1024),
    )
