# Inspect a CSV before cleanup

The preflight command reads a local export and reports its structure and likely cleanup work. It does not edit the source, run cleanup, evaluate formulas, send data anywhere, or create output files. Python 3.11+ and the standard library are enough.

```sh
python profile_csv.py examples/messy_customers.csv
python profile_csv.py input.csv --format json
python profile_csv.py input.tsv --delimiter tab
python profile_csv.py input.csv --delimiter ';'
```

The original `tidy_csv.py` command is unchanged. Profiling does not choose cleanup flags for you.

## A before-cleanup example

The included `examples/messy_customers.csv` is fictional. Running the first command above reports:

- 8 data records, excluding the header
- 5 columns; 6 records with the expected width
- 1 short record and 1 wide record, at CSV records 5 and 7
- 3 cells with surrounding whitespace across the 6 structurally valid records
- 0 empty, whitespace-only, or formula-like cells in those valid records
- SHA-256 `3355005ffb988694c23b1cee2c72b9c6ab2ae0972395273a13f06d1a0606946a`

This can help decide whether to review the ragged records and opt in to trimming. Zero formula-like detections do **not** mean a file is safe to open in a spreadsheet. The two malformed records are outside the per-column formula counts, and the prefix heuristic cannot establish safety even for valid records.

For a short record-boundary preview with no raw data values:

```sh
python profile_csv.py examples/messy_customers.csv --max-rows 3 --preview-rows 2
```

The command clearly labels this as a prefix scan. It reports exactly 3 observed records and an unknown total; it does not extrapolate.

## What the counts mean

A *structurally valid record* has exactly as many fields as the header. All per-column and aggregate cell counts use only these records in the scanned portion. The explicit denominator is `scan.column_counts_denominator_rows`. Every reported column has that many `present_cells`.

Ragged records are counted separately as short or wide. Their values are never silently padded, truncated, or assigned to the column counters. `missing_cells` and `extra_cells` describe differences in widths, not synthetic cell values. Up to five wrong-width record numbers are shown; `omitted_width_examples` reports the rest. Blank CSV records are empty row lists, not single empty strings; they normally count as short rows. A quoted `""` is a one-cell row.

For each column:

- `empty_cells`: the value is exactly an empty string.
- `whitespace_only_cells`: the nonempty value consists of whitespace as defined by Python's `str.strip()`.
- `outer_whitespace_cells`: stripping surrounding whitespace would change the value. This overlaps with whitespace-only cells. Internal whitespace alone does not count.
- `formula_like_cells`: after ignoring leading Unicode whitespace, ASCII controls (U+0000–U+001F and U+007F), or U+FEFF, the first remaining character is `=`, `+`, `-`, or `@`.
- `maximum_cell_length`: the largest value length in Unicode code points, not UTF-8 bytes or display width. It is zero when no valid records were observed.

These are observations, not type guesses. IDs such as `00017` remain strings. No distinct-value sets or entire dataset are retained.

Headers retain their exact values, identified by both a one-based index and name. Empty names, whitespace-only names, duplicate names, surrounding whitespace, and names that would collide after trimming are diagnosed rather than silently repaired. An entirely blank header record is reported as having no columns. A truly empty file is an error. Header problems may make the cleanup command reject the source even when profiling succeeds.

## Encoding and records

The parser uses the same explicit dialect as cleanup: UTF-8 with optional initial signature, the supplied one-character delimiter (comma by default), ordinary CSV double-quote escaping, and strict parsing. It does not sniff encoding, delimiters, headers, or data types.

A UTF-8 signature at byte zero is removed as an encoding marker. A literal U+FEFF inside a quoted header is data and remains intact. A quoted multiline value is one CSV record. Record numbers include the header as record 1 and are not physical line numbers.

Invalid UTF-8, malformed quoting, or exceeding Python's CSV field-size limit fails with exit code 2 and no successful report on stdout. Header diagnoses and ragged records are successful observations, not parse failures. Exit code 0 means the requested inspection succeeded; it does not certify the input as clean or spreadsheet-safe.

## Full scans and prefix limits

By default, the profiler scans to EOF. `--max-rows N` parses at most N data records after the header; `--max-rows 0` explicitly requests the default full scan.

- Complete scan: `scan.complete` is true, `scan.total_rows` is exact, and `counts_scope` is `complete_file`.
- Prefix scan: `scan.complete` is false, `scan.total_rows` is null, and `counts_scope` is `scanned_prefix`. Counts are exact for the observed prefix, **not estimates** of the whole file.
- Reaching exactly N records does not prove EOF. No extra record is parsed to find out, so a source with exactly N data records is still labeled incomplete at that limit. Use a larger limit or a full scan to confirm EOF.
- The SHA-256 always covers **all source bytes**, even for a prefix scan. Hashing is a streaming first pass over the entire file; parsing is a second pass through the same open file. A row limit therefore does **not** limit bytes read or fingerprinting time. The text decoder may also read ahead.
- Data beyond a parsed prefix has not been structurally checked. Later malformed quoting can remain undetected. Buffered UTF-8 decoding can detect invalid bytes beyond the requested record boundary and fail earlier than expected.
- Memory grows with header width and the largest parsed CSV record, plus bounded previews; it does not grow with the number of data records or distinct values. A row limit is not a byte, time, header-width, or record-size limit. The default Python CSV field-size limit is unchanged.

The source must stay stable during the command. A change in file size or modification/change timestamp is rejected; this is an ordinary concurrent-change guard, not a filesystem snapshot or protection against all external writers. The tool itself opens the source only for reading.

## Privacy and preview limits

Default reports contain column names, source **basename**, counts, source fingerprint, and record numbers. They omit raw data values and absolute source paths. These metadata can still reveal information about a private dataset; keep the report private when the source is private.

`--preview-rows` accepts 0 through 20 and defaults to 5. Without another flag, previews show only record number, cell count, and whether the width matches.

Explicitly opt in when you need local sample values:

```sh
python profile_csv.py input.csv --include-values --preview-rows 3
```

Opt-in previews show the first records, including ragged ones, with at most 20 fields per record and 80 Unicode characters per value. Omitted fields and truncated value positions are explicitly identified; no truncation affects counts or the source. Values are JSON-escaped in both report formats so terminal controls and embedded newlines cannot masquerade as new report lines. Header names and basenames are escaped too. Displaying a value is not a spreadsheet-safe export.

Both formats go to stdout. Redirect to a **different** file if desired, such as `--format json > preflight-report.json`. Never redirect over the source: shell redirection can truncate it before the profiler starts. The profiler does not accept an output path or intentionally write a report file.

## Spreadsheet caution

Formula-like prefixes are conservative warnings. Negative numbers such as `-12` are counted even when they are ordinary data. Trimming can expose a prefix, but the profiler does not trim or sanitize values. Spreadsheet behavior varies; some other prefixes or embedded characters can also matter. Zero detections never mean the file is spreadsheet-safe. Follow the [existing spreadsheet safety guidance](behavior.md#spreadsheet-safety), and import untrusted fields explicitly as text.

## Checks

```sh
python -m unittest discover -s tests -v
```

The new tests cover exact valid-row accounting, privacy defaults and optional bounded previews, full/prefix scan boundaries, leading-zero IDs, quoted multiline and Unicode fields, comma/semicolon/tab, UTF-8 signatures versus literal U+FEFF, ambiguous headers, formula-like prefixes, blank/ragged records, invalid encoding/quoting, escaped terminal controls, before/after source hashes, concurrent-change detection, CLI failures, and a 40,000-row distinct-value fixture with a bounded traced-allocation check. The synthetic fixture is a regression check, not a production performance guarantee.

The complete existing cleanup, generated-model, BOM, and idempotence suites are still required. A dedicated compatibility check verifies the same input produces byte-identical `cleaned.csv`, `review.csv`, `audit.jsonl`, `summary.json`, and `report.html` before and after profiling.
