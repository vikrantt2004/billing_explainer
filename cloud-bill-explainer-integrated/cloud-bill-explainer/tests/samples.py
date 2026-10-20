"""Tiny synthetic files with valid headers. Enough for intake checks; parsers get
realistic samples in later stages."""
import io
import zipfile

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
CSV_BYTES = b"service,region,cost\nEC2,us-east-1,10.50\nS3,eu-west-1,3.20\n"


def make_xlsx_bytes(include_workbook: bool = True, extra_names: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        if include_workbook:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        for name in extra_names:
            zf.writestr(name, b"x")
    return buf.getvalue()
