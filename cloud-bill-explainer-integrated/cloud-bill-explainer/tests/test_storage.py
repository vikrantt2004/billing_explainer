"""File validation, filename handling, and safe storage."""
import re

import pytest

from billing_explainer import storage
from billing_explainer.storage import IntakeError, detect_file_type, sanitize_display_name
from samples import CSV_BYTES, JPG_BYTES, PDF_BYTES, PNG_BYTES, make_xlsx_bytes

MAX = 1024 * 1024


def code_of(filename, data, max_bytes=MAX):
    with pytest.raises(IntakeError) as err:
        detect_file_type(filename, data, max_bytes)
    return err.value.code


# ---- accepted types ----

def test_each_supported_type_is_detected():
    assert detect_file_type("a.pdf", PDF_BYTES, MAX) == "pdf"
    assert detect_file_type("a.csv", CSV_BYTES, MAX) == "csv"
    assert detect_file_type("a.xlsx", make_xlsx_bytes(), MAX) == "xlsx"
    assert detect_file_type("a.png", PNG_BYTES, MAX) == "png"
    assert detect_file_type("a.jpg", JPG_BYTES, MAX) == "jpg"


def test_extension_case_and_jpeg_alias():
    assert detect_file_type("BILL.CSV", CSV_BYTES, MAX) == "csv"
    assert detect_file_type("shot.JPEG", JPG_BYTES, MAX) == "jpg"


# ---- rejected files ----

def test_unsupported_extensions_are_rejected():
    for name in ("run.exe", "notes.txt", "macro.xlsm", "noextension", ".csv", "a.pdf.exe"):
        assert code_of(name, CSV_BYTES) == "unsupported_type", name


def test_empty_and_oversized_files_are_rejected():
    assert code_of("a.csv", b"") == "empty"
    assert code_of("a.csv", b"x" * 101, max_bytes=100) == "too_large"


def test_renamed_files_are_caught_by_content_check():
    assert code_of("fake.pdf", b"MZ\x90\x00 this is an exe") == "content_mismatch"
    assert code_of("fake.png", JPG_BYTES) == "content_mismatch"
    assert code_of("fake.jpg", PNG_BYTES) == "content_mismatch"
    assert code_of("fake.xlsx", CSV_BYTES) == "content_mismatch"   # not a zip
    assert code_of("fake.csv", PDF_BYTES) == "content_mismatch"    # a PDF named .csv
    assert code_of("fake.csv", make_xlsx_bytes()) == "content_mismatch"


def test_csv_with_binary_content_is_rejected():
    assert code_of("a.csv", "a,b\n1,2\n".encode("utf-16")) == "content_mismatch"


def test_xlsx_structure_and_macro_checks():
    assert code_of("a.xlsx", make_xlsx_bytes(include_workbook=False)) == "content_mismatch"
    assert code_of("a.xlsx", make_xlsx_bytes(extra_names=("xl/vbaProject.bin",))) == "unsafe_content"


# ---- display names ----

def test_sanitize_display_name():
    assert sanitize_display_name("../../etc/passwd.csv") == "passwd.csv"
    assert sanitize_display_name("C:\\Users\\me\\bill.csv") == "bill.csv"
    assert sanitize_display_name("bad\x00na\nme.csv") == "badname.csv"
    assert sanitize_display_name("invoice\u202efdp.csv") == "invoicefdp.csv"  # bidi override removed
    assert sanitize_display_name("..") == "unnamed"
    assert sanitize_display_name("") == "unnamed"
    long = sanitize_display_name("a" * 400 + ".csv")
    assert len(long) <= 255 and long.endswith(".csv")


# ---- storage paths ----

def test_store_file_uses_generated_name_inside_upload_dir(tmp_path):
    upload_dir = tmp_path / "uploads"
    name1 = storage.store_file(upload_dir, CSV_BYTES, "csv")
    name2 = storage.store_file(upload_dir, CSV_BYTES, "csv")
    assert name1 != name2
    assert re.fullmatch(r"[0-9a-f]{32}\.csv", name1)
    assert storage.resolve_storage_path(upload_dir, name1).read_bytes() == CSV_BYTES
    assert sorted(p.name for p in upload_dir.iterdir()) == sorted([name1, name2])  # no .part leftovers


def test_store_file_rejects_unknown_type(tmp_path):
    with pytest.raises(ValueError):
        storage.store_file(tmp_path / "uploads", b"x", "exe")


def test_resolve_storage_path_refuses_anything_but_our_names(tmp_path):
    for bad in ("../x.csv", "sub/a.csv", "/etc/passwd", "", "evil.csv",
                "0" * 32 + ".exe", "0" * 32 + ".csv/../../x"):
        with pytest.raises(ValueError):
            storage.resolve_storage_path(tmp_path, bad)
    storage.resolve_storage_path(tmp_path, "0" * 32 + ".csv")  # a well-formed name is fine
