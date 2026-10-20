"""CSV -> headers + string rows (via pandas) -> shared table normalizer."""
from __future__ import annotations

import csv
import io

import pandas as pd

from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.tabular import normalize_table

_PREVIEW_CHARS = 50_000  # how much raw text we keep for the review screen
_DELIMITERS = (",", ";", "\t", "|")


def decode_text(data: bytes) -> tuple[str | None, str | None]:
    """Return (text, note). UTF-8 (with or without BOM) first, then Windows-1252, which is
    what Excel on Windows often writes. (None, reason) if neither works."""
    try:
        return data.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "File is not UTF-8; decoded as Windows-1252. Check accented characters."
    except UnicodeDecodeError:
        return None, "File text encoding could not be determined (not UTF-8 or Windows-1252)."


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter that splits the header line into the most fields (ties -> comma)."""
    first = next((line for line in text.splitlines() if line.strip()), "")

    def fields(d: str) -> int:
        try:
            return len(next(csv.reader([first], delimiter=d)))
        except (StopIteration, csv.Error):
            return 0

    return max(_DELIMITERS, key=fields)


class CsvParser(BaseParser):
    file_types = ("csv",)

    def parse(self, data: bytes, options: ParseOptions) -> ParseResult:
        text, note = decode_text(data)
        if text is None:
            return _failed("decode_error", note)

        try:
            # header=None keeps duplicate column names intact (so we can report them);
            # dtype=str + na_filter=False means NOTHING is converted to float or NaN.
            table = pd.read_csv(io.StringIO(text), sep=sniff_delimiter(text), header=None,
                                dtype=str, na_filter=False, skip_blank_lines=True)
        except pd.errors.EmptyDataError:
            return _failed("empty_file", "File contains no data.")
        except (pd.errors.ParserError, csv.Error) as exc:
            return _failed("malformed_csv", f"File is not well-formed CSV: {str(exc).strip()}")

        if len(table) < 2:
            return _failed("no_data_rows", "File has a header but no data rows.")
        headers = [str(h) for h in table.iloc[0].tolist()]
        rows = [[str(c) for c in r] for r in table.iloc[1:].values.tolist()]

        result = normalize_table(headers, rows, options)
        result.extracted_text = text[:_PREVIEW_CHARS]
        if len(text) > _PREVIEW_CHARS:
            result.extracted_text += "\n... [preview truncated]"
        if note:
            result.issues.append(Issue(Severity.INFO, "encoding", note))
        return result


def _failed(code: str, message: str) -> ParseResult:
    return ParseResult(issues=[Issue(Severity.FAILED, code, message)])
