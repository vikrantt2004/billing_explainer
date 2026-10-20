"""Excel workbook parser using the common tabular normalizer."""
from __future__ import annotations

from io import BytesIO

import pandas as pd

from billing_explainer.parsers.base import BaseParser, Issue, ParseOptions, ParseResult, Severity
from billing_explainer.parsers.tabular import ALIASES, normalize_header, normalize_table

_PREVIEW_CHARS = 50_000


class XlsxParser(BaseParser):
    file_types = ("xlsx",)

    def parse(self, data: bytes, options: ParseOptions) -> ParseResult:
        try:
            workbook = pd.ExcelFile(BytesIO(data), engine="openpyxl")
        except Exception as exc:
            return ParseResult(issues=[Issue(Severity.FAILED, "invalid_xlsx",
                                            f"Excel workbook could not be opened: {type(exc).__name__}.")])

        names = list(workbook.sheet_names)
        if not names:
            return ParseResult(issues=[Issue(Severity.FAILED, "empty_workbook", "Workbook contains no worksheets.")])
        if options.sheet_name:
            if options.sheet_name not in names:
                return ParseResult(issues=[Issue(Severity.BLOCKING, "unknown_sheet",
                                                f"Worksheet {options.sheet_name!r} was not found. Available sheets: {', '.join(names)}.")])
            selected = options.sheet_name
            selected_by_hint = True
        else:
            candidates: list[str] = []
            for name in names:
                try:
                    preview = pd.read_excel(workbook, sheet_name=name, header=None, dtype=str,
                                            keep_default_na=False, nrows=25)
                except Exception:
                    continue
                if _header_row(preview) is not None:
                    candidates.append(name)
            selected = candidates[0] if candidates else names[0]
            selected_by_hint = False

        try:
            table = pd.read_excel(workbook, sheet_name=selected, header=None, dtype=str,
                                  keep_default_na=False)
        except Exception as exc:
            return ParseResult(issues=[Issue(Severity.FAILED, "xlsx_read_error",
                                            f"Could not read worksheet {selected!r}: {type(exc).__name__}.")])
        if table.empty:
            return ParseResult(extracted_text=f"Worksheet: {selected}\n(empty)",
                               issues=[Issue(Severity.FAILED, "empty_sheet", f"Worksheet {selected!r} is empty.")])

        header_index = _header_row(table)
        if header_index is None:
            header_index = 0
        headers = [str(v).strip() for v in table.iloc[header_index].tolist()]
        rows = [[str(v).strip() for v in row] for row in table.iloc[header_index + 1:].values.tolist()]
        result = normalize_table(headers, rows, options, locator_prefix=f"sheet {selected}! ")

        preview = table.fillna("").astype(str).to_csv(index=False, header=False, sep="\t")
        result.extracted_text = f"Worksheet: {selected}\n" + preview[:_PREVIEW_CHARS]
        if len(preview) > _PREVIEW_CHARS:
            result.extracted_text += "\n... [preview truncated]"
        if len(candidates) > 1 and not selected_by_hint:
            result.issues.append(Issue(Severity.REVIEW, "multiple_candidate_sheets",
                                       f"Multiple worksheets look like billing exports ({', '.join(candidates)}). Parsed {selected!r}; confirm that this is the intended sheet."))
        elif len(names) > 1 and not selected_by_hint:
            result.issues.append(Issue(Severity.INFO, "multiple_sheets",
                                       f"Workbook contains {len(names)} worksheets. Parsed {selected!r}; choose a different sheet when reprocessing if needed."))
        return result


def _header_row(table: pd.DataFrame) -> int | None:
    cost_aliases = ALIASES["cost"]
    for row_idx in range(min(len(table), 25)):
        normalized = {normalize_header(str(cell)) for cell in table.iloc[row_idx].tolist()}
        if normalized & cost_aliases:
            return row_idx
    return None
