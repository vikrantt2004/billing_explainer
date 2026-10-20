"""Parser registry for supported billing upload formats."""
from __future__ import annotations

from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.csv_parser import CsvParser
from billing_explainer.parsers.image_parser import ImageParser
from billing_explainer.parsers.pdf_parser import PdfParser
from billing_explainer.parsers.xlsx_parser import XlsxParser

_image_parser = ImageParser()
PARSERS: dict[str, BaseParser] = {
    "csv": CsvParser(),
    "xlsx": XlsxParser(),
    "pdf": PdfParser(),
    "png": _image_parser,
    "jpg": _image_parser,
}


def get_parser(file_type: str, registry: dict[str, BaseParser] | None = None) -> BaseParser | None:
    return (registry if registry is not None else PARSERS).get(file_type)


__all__ = ["PARSERS", "get_parser", "BaseParser", "Issue", "ParseOptions", "ParseResult", "Severity"]
