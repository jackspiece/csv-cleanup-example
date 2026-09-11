#!/usr/bin/env python3
"""Clean a CSV into a new folder, with an audit trail and rows for review."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


class InputError(ValueError):
    """The source cannot be read without guessing its structure."""


@dataclass
class Summary:
    source: str
    source_sha256: str
    columns: list[str]
    input_rows: int = 0
    output_rows: int = 0
    duplicate_rows: int = 0
    review_rows: int = 0
    changed_cells: int = 0
    trim: bool = False
    deduplicate: bool = False


def _report(summary: Summary) -> str:
    s = summary
    e = html.escape
    cards = [("Rows read", s.input_rows), ("Ready", s.output_rows),
             ("Duplicates removed", s.duplicate_rows), ("Need review", s.review_rows)]
    counts = "".join(f'<div class="card"><strong>{value:,}</strong><span>{label}</span></div>' for label, value in cards)
    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV cleanup: {e(s.source)}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f5f2;color:#192d35;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:900px;margin:64px auto;padding:0 24px}}.eyebrow{{color:#45666a;font-size:13px;letter-spacing:.1em;text-transform:uppercase}}
h1{{font-size:clamp(32px,6vw,52px);line-height:1.12;letter-spacing:-.04em;margin:14px 0 24px}}h2{{font-size:20px;margin-top:36px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:32px 0}}.card{{background:white;border:1px solid #dce2dc;border-radius:12px;padding:20px}}
.card strong{{display:block;font-size:38px;line-height:1.2;font-variant-numeric:tabular-nums}}.card span{{display:block;font-size:13px;margin-top:8px;color:#4c6268}}
a{{color:#17605d}}code{{font-size:13px;overflow-wrap:anywhere}}.note{{border-left:3px solid #598e79;padding:8px 16px;background:#e8eee7}}
dt{{font-weight:650;margin-top:12px}}dd{{margin-left:0;color:#42575e}}footer{{margin-top:42px;border-top:1px solid #dce2dc;padding-top:18px;color:#627478;font-size:12px}}
@media(max-width:620px){{main{{margin:32px auto}}.cards{{grid-template-columns:repeat(2,1fr)}}.card{{padding:16px}}}}
</style><main><div class="eyebrow">CSV cleanup / checked output</div>
<h1>Every row accounted for.</h1><p>Source: <strong>{e(s.source)}</strong>. The source file was read, never edited.</p>
<div class="cards">{counts}</div>
<p class="note">{s.input_rows:,} input rows = {s.output_rows:,} ready + {s.duplicate_rows:,} duplicates + {s.review_rows:,} for review.</p>
<h2>Files to use</h2><dl>
<dt><a href="cleaned.csv">cleaned.csv</a></dt><dd>Rows with the expected number of columns. Values stay text, including IDs with leading zeroes.</dd>
<dt><a href="review.csv">review.csv</a></dt><dd>Rows with missing or extra cells, with their original values and source record number.</dd>
<dt><a href="audit.jsonl">audit.jsonl</a></dt><dd>One event for each changed, removed or quarantined record.</dd>
<dt><a href="summary.json">summary.json</a></dt><dd>Counts, settings and the source fingerprint.</dd></dl>
<h2>What was applied</h2><p>Trim outer whitespace: <strong>{'yes' if s.trim else 'no'}</strong>.
Remove exact duplicate rows: <strong>{'yes' if s.deduplicate else 'no'}</strong>.
Cells changed: <strong>{s.changed_cells:,}</strong>.</p>
<p>Duplicates use every cell, after optional trimming. The first matching record is kept. No names, dates, numbers or currencies were inferred.</p>
<footer>Source SHA-256<br><code>{e(s.source_sha256)}</code></footer></main></html>'''


def clean(source: Path | str, output: Path | str, *, trim: bool = False,
          deduplicate: bool = False, delimiter: str = ",") -> Summary:
    source, output = Path(source), Path(output)
    if len(delimiter) != 1 or delimiter in "\r\n\0":
        raise ValueError("Delimiter must be one character, other than a line break or NUL.")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists: {output}")
    if not source.is_file():
        raise InputError(f"Source is not a file: {source}")
    with source.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".csv-tidy-", dir=output.parent))
    try:
        with source.open(encoding="utf-8-sig", newline="") as src, \
             (stage / "cleaned.csv").open("w", encoding="utf-8", newline="") as dst, \
             (stage / "review.csv").open("w", encoding="utf-8", newline="") as review, \
             (stage / "audit.jsonl").open("w", encoding="utf-8") as audit:
            reader = csv.reader(src, delimiter=delimiter, strict=True)
            try:
                original_header = next(reader)
            except StopIteration as exc:
                raise InputError("The CSV is empty; a header row is required.") from exc
            header = [cell.strip() if trim else cell for cell in original_header]
            if not header or any(not cell.strip() for cell in header):
                raise InputError("Column names must not be empty.")
            if len(set(header)) != len(header):
                raise InputError("Column names would be duplicated; fix the header first.")
            result = Summary(source.name, digest, header, trim=trim, deduplicate=deduplicate)
            writer, review_writer = csv.writer(dst), csv.writer(review)
            writer.writerow(header)
            review_writer.writerow(["source_record", "reason", "original_cells_json"])

            def event(record: int, action: str, **details: object) -> None:
                audit.write(json.dumps({"source_record": record, "action": action, **details}, ensure_ascii=False) + "\n")

            if header != original_header:
                event(1, "header_trimmed", original=original_header, cleaned=header)
            seen: dict[tuple[str, ...], int] = {}
            for record, row in enumerate(reader, 2):
                result.input_rows += 1
                if len(row) != len(header):
                    reason = f"Expected {len(header)} cells, found {len(row)}"
                    review_writer.writerow([record, reason, json.dumps(row, ensure_ascii=False)])
                    event(record, "quarantined", reason=reason)
                    result.review_rows += 1
                    continue
                cleaned = [cell.strip() for cell in row] if trim else row
                changes = [header[i] for i, (before, after) in enumerate(zip(row, cleaned)) if before != after]
                if changes:
                    result.changed_cells += len(changes)
                    event(record, "trimmed", columns=changes)
                key = tuple(cleaned)
                if deduplicate and key in seen:
                    event(record, "duplicate_removed", kept_source_record=seen[key])
                    result.duplicate_rows += 1
                    continue
                if deduplicate:
                    seen[key] = record
                writer.writerow(cleaned)
                result.output_rows += 1
        if result.input_rows != result.output_rows + result.duplicate_rows + result.review_rows:
            raise RuntimeError("Row accounting failed; no output published.")
        (stage / "summary.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "report.html").write_text(_report(result), encoding="utf-8")
        # Reserve a new directory. Existing output is never intentionally replaced.
        output.mkdir()
        try:
            os.replace(stage, output)
        except OSError:
            output.rmdir()  # This succeeds only while the reserved directory is empty.
            raise
        return result
    except (csv.Error, UnicodeError) as exc:
        raise InputError(f"Could not parse the complete CSV: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, help="New output folder; must not already exist")
    parser.add_argument("--trim", action="store_true", help="Trim whitespace at cell and header edges")
    parser.add_argument("--deduplicate", action="store_true", help="Keep the first of exact matching rows")
    parser.add_argument("--delimiter", default=",", help="Input delimiter; use 'tab' for TSV. Output is comma-separated.")
    args = parser.parse_args(argv)
    try:
        result = clean(args.source, args.output, trim=args.trim, deduplicate=args.deduplicate,
                       delimiter="\t" if args.delimiter == "tab" else args.delimiter)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
