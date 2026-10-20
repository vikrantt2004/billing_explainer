"""Upload validation and safe on-disk storage. Pure functions, no database access.

Threat model for an uploaded file:
  * a lying name        -> "../../x", "bill.pdf.exe", RTL-override tricks
  * a lying extension   -> an .exe renamed to .pdf
  * a macro workbook    -> .xlsm content inside a .xlsx name
  * a huge file         -> memory/disk exhaustion
We never execute, import, or render uploaded content here. We only check its first bytes
and structure, then store the bytes under a name WE generate.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
import zipfile
from pathlib import Path

EXTENSION_TO_TYPE = {
    ".pdf": "pdf", ".csv": "csv", ".xlsx": "xlsx",
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpg",
}
ALLOWED_TYPES = frozenset(EXTENSION_TO_TYPE.values())

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_MAX_XLSX_UNCOMPRESSED = 200 * 1024 * 1024  # declared size; see note in _check_xlsx
_STORED_NAME = re.compile(r"[0-9a-f]{32}\.(pdf|csv|xlsx|png|jpg)")


class IntakeError(Exception):
    """A file or form value was refused. `code` is machine-readable, `message` is for humans."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- names

def sanitize_display_name(name: str) -> str:
    """Make a browser-supplied filename safe to SHOW. It is never used to build a path.

    Drops directory parts (both / and \\), control characters, and invisible/bidi
    formatting characters (so 'invoice<RLO>fdp.csv' can't masquerade as another type).
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if name in ("", ".", ".."):
        return "unnamed"
    if len(name) > 255:
        suffix = Path(name).suffix[:16]
        name = name[: 255 - len(suffix)] + suffix
    return name


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------- content checks

def _check_pdf(data: bytes) -> None:
    # The PDF spec tolerates a little junk before the header, so look in the first 1 KB.
    if b"%PDF-" not in data[:1024]:
        raise IntakeError("content_mismatch", "File is named .pdf but does not look like a PDF.")


def _check_png(data: bytes) -> None:
    if not data.startswith(_PNG_SIGNATURE):
        raise IntakeError("content_mismatch", "File is named .png but does not look like a PNG image.")


def _check_jpg(data: bytes) -> None:
    if not data.startswith(_JPEG_SIGNATURE):
        raise IntakeError("content_mismatch", "File is named .jpg/.jpeg but does not look like a JPEG image.")


def _check_csv(data: bytes) -> None:
    # CSV has no magic number, so: it must be text, and must not be a known binary type.
    if b"\x00" in data:
        raise IntakeError(
            "content_mismatch",
            "File contains binary data, so it is not a plain-text CSV "
            "(UTF-16 exports must be re-saved as UTF-8 CSV).")
    for signature in (b"%PDF-", _PNG_SIGNATURE, _JPEG_SIGNATURE, b"PK\x03\x04"):
        if data.startswith(signature):
            raise IntakeError("content_mismatch", "File is named .csv but its contents are another file type.")


def _check_xlsx(data: bytes) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        raise IntakeError("content_mismatch", "File is named .xlsx but is not a valid Excel workbook.")
    with zf:
        names = set(zf.namelist())
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise IntakeError("content_mismatch", "File is a ZIP archive but not an Excel workbook.")
        if any(n.lower().startswith("xl/vbaproject") for n in names):
            raise IntakeError("unsafe_content", "Workbook contains macros; macro-enabled files are not accepted.")
        # NOTE: these sizes are what the archive *claims*. The parser stage must still
        # read workbooks defensively (it never extracts files to disk).
        if sum(i.file_size for i in zf.infolist()) > _MAX_XLSX_UNCOMPRESSED:
            raise IntakeError("unsafe_content", "Workbook expands to an unreasonable size.")


_CHECKERS = {"pdf": _check_pdf, "png": _check_png, "jpg": _check_jpg,
             "csv": _check_csv, "xlsx": _check_xlsx}


def detect_file_type(filename: str, data: bytes, max_bytes: int) -> str:
    """Return 'pdf'|'csv'|'xlsx'|'png'|'jpg', or raise IntakeError.

    The extension picks which check to run; the content must then pass that check.
    """
    if not data:
        raise IntakeError("empty", "File is empty.")
    if len(data) > max_bytes:
        raise IntakeError("too_large", f"File is larger than the {max_bytes / 1024 / 1024:.0f} MB limit.")
    file_type = EXTENSION_TO_TYPE.get(Path(filename).suffix.lower())
    if file_type is None:
        raise IntakeError("unsupported_type",
                          "Unsupported file type. Accepted: PDF, CSV, XLSX, PNG, JPG/JPEG.")
    _CHECKERS[file_type](data)
    return file_type


# -------------------------------------------------------------- storage

def store_file(upload_dir: Path, data: bytes, file_type: str) -> str:
    """Write bytes to `<uuid>.<type>` inside upload_dir; return that relative name.

    The name is generated here from a UUID and a whitelisted type, so nothing the
    user typed can influence the path. Written to a temp file then renamed, so a crash
    never leaves a half-written file under the final name.
    """
    if file_type not in ALLOWED_TYPES:
        raise ValueError(f"Unknown file type: {file_type!r}")
    upload_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{file_type}"
    tmp = upload_dir / f".{name}.part"
    with open(tmp, "xb") as fh:  # 'x' = fail if it already exists
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, upload_dir / name)
    return name


def resolve_storage_path(upload_dir: Path, storage_path: str) -> Path:
    """Turn a stored name back into a real path, refusing anything that isn't exactly
    one of our generated names (defense in depth against a tampered database value)."""
    if not _STORED_NAME.fullmatch(storage_path):
        raise ValueError(f"Invalid storage path: {storage_path!r}")
    base = upload_dir.resolve()
    path = (base / storage_path).resolve()
    if path.parent != base:
        raise ValueError(f"Invalid storage path: {storage_path!r}")
    return path


def delete_stored_file(upload_dir: Path, storage_path: str) -> None:
    resolve_storage_path(upload_dir, storage_path).unlink(missing_ok=True)
