from __future__ import annotations

from io import BytesIO
from decimal import Decimal

import fitz
import pandas as pd
from PIL import Image, ImageDraw, ImageFont
import pytesseract

from billing_explainer.parsers import ParseOptions, Severity
from billing_explainer.parsers.pdf_parser import PdfParser
from billing_explainer.parsers.xlsx_parser import XlsxParser
from billing_explainer.parsers.image_parser import ImageParser


def test_xlsx_parser_normalizes_cloud_billing_table():
    out = BytesIO()
    frame = pd.DataFrame([
        {"Service": "Compute", "Region": "ap-south-1", "Cost": "100.50", "Currency": "INR", "Date": "2026-06-01"},
        {"Service": "Storage", "Region": "ap-southeast-1", "Cost": "25.00", "Currency": "INR", "Date": "2026-06-02"},
    ])
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Billing", index=False)
        pd.DataFrame({"Notes": ["not a billing sheet"]}).to_excel(writer, sheet_name="Notes", index=False)
    result = XlsxParser().parse(out.getvalue(), ParseOptions())
    assert len(result.line_items) == 2
    assert sum((item.cost_amount for item in result.line_items), Decimal("0")) == Decimal("125.50")
    assert any(issue.code == "multiple_sheets" for issue in result.issues)
    assert "Worksheet: Billing" in result.extracted_text


def test_pdf_parser_extracts_text_and_invoice_total():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Invoice Number: INV-2026-06\nBilling Period: 2026-06-01 to 2026-06-30\nCurrency: INR\nUsage Charges: INR 1000.00\nPromotional Credits: INR 100.00\nGST Tax: INR 162.00\nInvoice Total: INR 1062.00")
    data = doc.tobytes()
    doc.close()
    result = PdfParser().parse(data, ParseOptions())
    assert "Invoice Number: INV-2026-06" in (result.extracted_text or "")
    assert result.summary is not None
    assert result.summary.invoice_number == "INV-2026-06"
    assert result.summary.currency == "INR"
    assert result.summary.invoice_total == Decimal("1062.00")
    assert result.period_start == "2026-06-01" and result.period_end == "2026-06-30"
    assert any(issue.severity == Severity.REVIEW for issue in result.issues)


def test_image_parser_extracts_text_with_tesseract():
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        import pytest
        pytest.skip("Tesseract executable is not installed")
    image = Image.new("RGB", (1200, 350), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 38)
    except OSError:
        font = ImageFont.load_default()
    draw.text((30, 30), "Invoice Number: INV-2026-06", fill="black", font=font)
    draw.text((30, 100), "Invoice Total: INR 1062.00", fill="black", font=font)
    data = BytesIO()
    image.save(data, format="PNG")
    result = ImageParser().parse(data.getvalue(), ParseOptions())
    assert result.extracted_text
    assert any(issue.code == "image_ocr_review" for issue in result.issues)
    assert result.summary is not None
