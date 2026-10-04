#!/usr/bin/env python3
"""Inspect a CSV without changing it; values in previews are opt-in."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import sys

from tidy_csv import InputError

DEFAULT_MAX_ROWS = 0
MAX_PREVIEW_ROWS = 20
MAX_PREVIEW_COLUMNS = 20
MAX_PREVIEW_CELL_CHARS = 80
MAX_WIDTH_EXAMPLES = 5
NOTES = [
    "Cell/column counts use only structurally valid records in the scanned portion; malformed rows are excluded.",
    "Empty means zero characters; whitespace-only means nonempty and str.strip() is empty. Outer whitespace overlaps whitespace-only.",
    "Formula-like means a =, +, - or @ prefix after leading Unicode whitespace, ASCII controls or U+FEFF; trimming can expose prefixes.",
    "Formula-like counts are warnings, include negative numbers, and never certify spreadsheet safety, even when zero.",
    "Maximum cell length counts Unicode characters, not bytes or visual width.",
    "A row limit bounds parsed data records, not bytes, time or record size; all source bytes are hashed and text decoding may read ahead.",
    "Headers and the source basename are included; raw data values appear only when include_values is true.",
]


@dataclass
class CellCounts:
    """Counts of present fields; categories can overlap."""

    present_cells: int = 0
    empty_cells: int = 0
    whitespace_only_cells: int = 0
    outer_whitespace_cells: int = 0
    formula_like_cells: int = 0
    maximum_cell_length: int = 0

    def add(self, value: str) -> None:
        self.present_cells += 1
        self.maximum_cell_length = max(self.maximum_cell_length, len(value))
        stripped = value.strip()
        self.empty_cells += value == ""
        self.whitespace_only_cells += bool(value) and not stripped
        self.outer_whitespace_cells += value != stripped
        # A conservative signal, not a formula parser or safety guarantee.
        # Leading whitespace, ASCII controls and U+FEFF can hide a prefix.
        start = 0
        while start < len(value) and (value[start].isspace()
                                     or ord(value[start]) < 32
                                     or value[start] in "\x7f\ufeff"):
            start += 1
        self.formula_like_cells += value[start:start + 1] in ("=", "+", "-", "@")


def _duplicate_groups(header: list[str]) -> list[dict]:
    indexes: dict[str, list[int]] = {}
    for index, name in enumerate(header, 1):
        indexes.setdefault(name, []).append(index)
    return [{"name": name, "columns": positions}
            for name, positions in indexes.items() if len(positions) > 1]


def _preview(record: int, row: list[str], width: int, include_values: bool) -> dict:
    result = {"source_record": record, "cell_count": len(row),
              "matches_header_width": len(row) == width}
    if include_values:
        shown = row[:MAX_PREVIEW_COLUMNS]
        result["values"] = [cell[:MAX_PREVIEW_CELL_CHARS] for cell in shown]
        result["truncated_value_columns"] = [
            index for index, cell in enumerate(shown, 1)
            if len(cell) > MAX_PREVIEW_CELL_CHARS]
        result["omitted_cells"] = max(0, len(row) - MAX_PREVIEW_COLUMNS)
    return result


def profile(source: Path | str, *, delimiter: str = ",",
            max_rows: int = DEFAULT_MAX_ROWS, preview_rows: int = 5,
            include_values: bool = False) -> dict:
    """Return exact observed counts; max_rows=0 explicitly scans to EOF.

    The row limit excludes the header. At the limit no additional record is
    parsed to prove EOF, so a file with exactly max_rows data records remains
    incomplete until a larger limit or full scan is requested. Text buffering
    may read ahead. This is not a byte, time or per-record memory limit.
    """
    source = Path(source)
    if len(delimiter) != 1 or delimiter in "\r\n\0":
        raise ValueError("Delimiter must be one character, other than a line break or NUL.")
    if type(max_rows) is not int or max_rows < 0:
        raise ValueError("max_rows must be a non-negative integer; 0 means a full scan.")
    if type(preview_rows) is not int or not 0 <= preview_rows <= MAX_PREVIEW_ROWS:
        raise ValueError(f"preview_rows must be an integer from 0 to {MAX_PREVIEW_ROWS}.")
    if type(include_values) is not bool:
        raise ValueError("include_values must be a boolean; raw previews require explicit True.")
    if not source.is_file():
        raise InputError(f"Source is not a file: {source.name}")

    try:
        with source.open("rb") as raw:
            before = os.fstat(raw.fileno())
            digest = hashlib.file_digest(raw, "sha256").hexdigest()
            raw.seek(0)
            stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise InputError("The CSV is empty; a header row is required.") from exc
            width = len(header)
            column_counts = [CellCounts() for _ in header]
            totals = CellCounts()
            rows_scanned = matching = short = wide = blank = 0
            max_width = 0
            missing_cells = extra_cells = 0
            width_examples: list[dict] = []
            preview: list[dict] = []
            complete = False
            while not max_rows or rows_scanned < max_rows:
                try:
                    row = next(reader)
                except StopIteration:
                    complete = True
                    break
                rows_scanned += 1
                record = rows_scanned + 1
                row_width = len(row)
                max_width = max(max_width, row_width)
                blank += row_width == 0
                if row_width == width:
                    matching += 1
                    for counts, value in zip(column_counts, row):
                        totals.add(value)
                        counts.add(value)
                else:
                    short += row_width < width
                    wide += row_width > width
                    if len(width_examples) < MAX_WIDTH_EXAMPLES:
                        width_examples.append({"source_record": record,
                                               "cell_count": row_width})
                missing_cells += max(0, width - row_width)
                extra_cells += max(0, row_width - width)
                if len(preview) < preview_rows:
                    preview.append(_preview(record, row, width, include_values))
            after = os.fstat(raw.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise InputError("Source changed during inspection; no report produced. Retry with a stable file.")
    except (csv.Error, UnicodeError) as exc:
        # Do not include raw source values (including decoder context) in errors.
        reason = "invalid UTF-8" if isinstance(exc, UnicodeError) else "invalid CSV quoting or field size"
        raise InputError(f"Could not parse the requested scan: {reason}; no report produced.") from exc

    stripped_header = [name.strip() for name in header]
    header_issues = {
        "no_columns": not header,
        "empty_name_columns": [i for i, name in enumerate(header, 1) if not name],
        "whitespace_only_name_columns": [i for i, name in enumerate(header, 1)
                                         if name and not name.strip()],
        "outer_whitespace_columns": [i for i, name in enumerate(header, 1)
                                     if name != name.strip()],
        "duplicate_names": _duplicate_groups(header),
        "duplicate_names_after_trim": _duplicate_groups(stripped_header),
    }
    return {
        "schema_version": 1,
        "source": source.name,
        "source_sha256": digest,
        "fingerprint_scope": "complete_file_bytes",
        "settings": {"encoding": "utf-8-sig", "delimiter": delimiter,
                     "strict_csv": True, "max_rows": max_rows,
                     "preview_rows": preview_rows, "include_values": include_values},
        "scan": {"complete": complete, "stop_reason": "end_of_file" if complete else "row_limit",
                 "rows_scanned": rows_scanned,
                 "total_rows": rows_scanned if complete else None,
                 "counts_scope": "complete_file" if complete else "scanned_prefix",
                 "counts_are_estimates": False,
                 "column_counts_denominator_rows": matching},
        "header": {"column_count": width, **header_issues},
        "columns": [{"index": i + 1, "name": name, **asdict(counts)}
                    for i, (name, counts) in enumerate(zip(header, column_counts))],
        "rows": {"matching_width": matching, "short": short, "wide": wide,
                 "malformed_width": short + wide, "blank_records": blank,
                 "max_observed_width": max_width, "missing_cells": missing_cells,
                 "extra_cells": extra_cells, "width_examples": width_examples,
                 "omitted_width_examples": max(0, short + wide - len(width_examples))},
        "cells": asdict(totals),
        "preview": preview,
        "preview_limits": {"max_rows": MAX_PREVIEW_ROWS, "max_columns": MAX_PREVIEW_COLUMNS,
                           "max_cell_characters": MAX_PREVIEW_CELL_CHARS},
        "notes": NOTES.copy(),
    }


def render_text(result: dict) -> str:
    """Use JSON string escaping for untrusted names and opt-in sample values."""
    quote = lambda value: json.dumps(value, ensure_ascii=True)
    scan, rows, cells, header = (result[key] for key in ("scan", "rows", "cells", "header"))
    lines = [f"CSV preflight: {quote(result['source'])}",
             "Read-only inspection. No cleanup or output files were created.",
             f"Source SHA-256 (all bytes): {result['source_sha256']}"]
    if scan["complete"]:
        lines.append(f"Scan: complete; {scan['rows_scanned']:,} data records (header excluded).")
    else:
        lines.append(f"Scan: first {scan['rows_scanned']:,} data records only; row limit reached.")
        lines.append("Counts are exact for this prefix, not whole-file estimates. Total rows unknown.")
        lines.append("Use --max-rows 0 to scan to EOF and check the remaining records.")
        lines.append("All source bytes were hashed; the row limit is not a byte or time limit.")
    lines.extend([
        f"Columns: {header['column_count']:,}; input UTF-8, delimiter {quote(result['settings']['delimiter'])}.",
        f"Rows: {rows['matching_width']:,} matching width; {rows['short']:,} short; {rows['wide']:,} wide; "
        f"{rows['blank_records']:,} blank records.",
        f"Cells in {scan['column_counts_denominator_rows']:,} structurally valid records: "
        f"{cells['empty_cells']:,} empty; {cells['whitespace_only_cells']:,} whitespace-only; "
        f"{cells['outer_whitespace_cells']:,} with outer whitespace; {cells['formula_like_cells']:,} formula-like.",
        f"Missing cells: {rows['missing_cells']:,}; extra cells: {rows['extra_cells']:,}.",
        "Column/cell counts exclude wrong-width records. Categories can overlap.",
        "Formula-like means a =, +, - or @ prefix after leading whitespace/ASCII controls/U+FEFF.",
        "This heuristic includes negative numbers and does not certify spreadsheet safety.",
        "Header checks:",
    ])
    if header["no_columns"]:
        lines.append("  No columns: the header record is blank.")
    found_issue = header["no_columns"]
    for key, label in (("empty_name_columns", "Empty names"),
                       ("whitespace_only_name_columns", "Whitespace-only names"),
                       ("outer_whitespace_columns", "Outer whitespace")):
        if header[key]:
            found_issue = True
            lines.append(f"  {label} at columns: {', '.join(map(str, header[key]))}.")
    for key, label in (("duplicate_names", "Duplicate name"),
                       ("duplicate_names_after_trim", "Duplicate name if trimmed")):
        for group in header[key]:
            found_issue = True
            lines.append(f"  {label} {quote(group['name'])}: columns {', '.join(map(str, group['columns']))}.")
    if not found_issue:
        lines.append("  No empty, duplicated or whitespace-padded names observed.")
    lines.append("Per-column counts (structurally valid records only):")
    for column in result["columns"]:
        lines.append(f"  {column['index']}. {quote(column['name'])}: present={column['present_cells']}, "
                     f"empty={column['empty_cells']}, "
                     f"whitespace-only={column['whitespace_only_cells']}, "
                     f"outer-whitespace={column['outer_whitespace_cells']}, formula-like={column['formula_like_cells']}, "
                     f"max-characters={column['maximum_cell_length']}")
    if rows["width_examples"]:
        lines.append("Wrong-width examples (CSV record numbers, header = 1):")
        for example in rows["width_examples"]:
            lines.append(f"  Record {example['source_record']}: {example['cell_count']} cells.")
        if rows["omitted_width_examples"]:
            lines.append(f"  {rows['omitted_width_examples']} more wrong-width records omitted.")
    if result["preview"]:
        values = result["settings"]["include_values"]
        lines.append("Preview (values explicitly requested):" if values else "Preview (values hidden; use --include-values to show):")
        for item in result["preview"]:
            lines.append(f"  Record {item['source_record']}: {item['cell_count']} cells; "
                         f"{'matches' if item['matches_header_width'] else 'differs from'} header width.")
            if values:
                lines.append(f"    Values: {quote(item['values'])}")
                if item["truncated_value_columns"] or item["omitted_cells"]:
                    lines.append(f"    Truncated columns: {quote(item['truncated_value_columns'])}; "
                                 f"omitted cells: {item['omitted_cells']}.")
    lines.append("Headers and the source basename are included; data values stay private unless --include-values is used.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--delimiter", default=",", help="Input delimiter; use 'tab' for TSV. No dialect guessing.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                        help="Data records to parse; 0 scans the whole file (default). All bytes are hashed regardless.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Report format on stdout")
    parser.add_argument("--preview-rows", type=int, default=5, help="Preview records, 0 to 20 (default: 5)")
    parser.add_argument("--include-values", action="store_true", help="Opt in to bounded raw data values in previews")
    args = parser.parse_args(argv)
    try:
        result = profile(args.source, delimiter="\t" if args.delimiter == "tab" else args.delimiter,
                         max_rows=args.max_rows, preview_rows=args.preview_rows,
                         include_values=args.include_values)
    except (OSError, ValueError) as exc:
        # OS exceptions can embed full private paths. Keep those out of reports.
        message = (f"Could not read source: {os.strerror(exc.errno) if exc.errno else type(exc).__name__}."
                   if isinstance(exc, OSError) else str(exc))
        print(f"Error: {ascii(message)}", file=sys.stderr)
        return 2
    output = (json.dumps(result, ensure_ascii=True, indent=2) + "\n"
              if args.format == "json" else render_text(result))
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
