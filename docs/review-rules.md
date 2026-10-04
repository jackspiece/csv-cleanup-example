# Explicit review rules

[Back to CSV Cleanup](../README.md)

A correctly shaped row may still need review. Supply a local JSON file to require
nonblank fields or restrict particular columns to exact string values. This is
optional, uses only the Python standard library, and makes no network requests.

```sh
python tidy_csv.py examples/review-rules/customers.csv new-rules-result \
  --trim --deduplicate --rules examples/review-rules/rules.json
```

The [fictional export](../examples/review-rules/customers.csv) produces **8 input
rows = 3 ready + 4 for review + 1 duplicate**. Open `new-rules-result/report.html`,
or inspect the [saved results](../examples/review-rules/checked). Neither the
source nor the rules file is edited. Use a new output directory each time.

## Check the rules before cleanup

Preview how a configuration targets an export before creating cleanup outputs:

```sh
python check_rules.py examples/review-rules/customers.csv \
  --rules examples/review-rules/rules.json --trim
```

The [saved text report](../examples/review-rules/preflight.txt) shows that `id`
requires a nonblank value, `status` has three allowed exact values, and `note`
has no configured checks. The allowed values themselves are hidden. The command
prints a report on stdout and creates no cleanup directory, CSV, audit, or HTML
file. Source bytes, rule bytes, and existing cleanup outputs are unchanged.
As with other Python scripts, use `python -B` if you also want to suppress
Python's import-bytecode cache files.

**This checks only configuration and header compatibility. Data rows have not
been checked.** A successful result does not verify row widths, data-row quoting,
rule compliance, business validity, or spreadsheet safety. For example, a file
with a valid header and an unterminated quoted data row can pass this command;
the full cleanup still rejects it. Text decoding may read beyond the header, so
invalid UTF-8 near the header can prevent the check even though no data row is
parsed. Use the [profiler](profiling.md) for structural/data observations, and run
cleanup to evaluate rules on actual records.

Use the same `--trim` and `--delimiter` settings as the cleanup you intend to run.
Trimming is off by default. Delimiters are explicit, with `--delimiter tab` for
TSV. The shared cleanup header validator and rules loader/binder are used, so
UTF-8 signatures, literal U+FEFF field data, exact column names, ambiguous headers,
strict JSON, and the 1 MiB configuration limit follow the same contract.

- Exit **0** means the configuration can bind to the effective header.
- Exit **2** means invalid configuration, an invalid/unreadable header, missing
  rule targets, or invalid command options. Reports for file/configuration/header
  failures remain on stdout; command-option errors use stderr.
- A rule targeting a missing column is listed by its exact name. Fix the rules
  or header explicitly, then rerun. There are no guesses or automatic edits.

For a machine-readable report, add `--format json`:

```sh
python check_rules.py examples/review-rules/customers.csv \
  --rules examples/review-rules/rules.json --trim --format json
```

The [saved JSON report](../examples/review-rules/preflight.json) has
`schema_version: 1`, `scope: "rules_and_header"`, a `valid` boolean, and an explicit
`data_rows_checked: false`. Its `columns` list uses one-based `column_index`, the
effective `name`, a `required_nonblank` boolean, and `allowed_value_count` (zero
means no allowed-values check). A column can have both checks. Unconfigured
columns are listed too, without implying that they were validated.

`unmatched_rule_columns` lists missing targets once, in required-column order
followed by allowed-value-column order. Both it and `columns` are `null` if the
configuration or header could not be read and validated. On a binding failure,
the available column coverage and missing targets are still reported, with
`valid: false` and `errors` describing the failure. Rules are loaded first, so an
invalid rules file stops the check before the CSV header is read. Correct that
failure and rerun to check the next stage.

The `errors` list contains a `code` and human-readable `message`. Current codes
are `invalid_rules`, `unreadable_rules`, `unreadable_source`, `invalid_header`,
`unreadable_header`, `source_changed`, and `unbound_rules`. Rule-file existence
and size failures are `invalid_rules`, following the shared rules loader.
Messages are intended for people; use the codes rather than matching prose.

`rules.sha256` identifies exact rule-file bytes and is `null` when configuration
loading failed. There is no CSV fingerprint or full-file scan in this command.
`source` and `rules.source` contain basenames, never parent directory paths.
Header/rule column names are included and can themselves reveal private details.
Raw data cells and configured allowed values are not included in reports or
validation errors. Both text and JSON escape terminal control characters in
names. Nothing is sent over the network.

## Configuration

```json
{
  "required_nonblank": ["id"],
  "allowed_values": {
    "status": ["ready", "批准", "001"]
  }
}
```

These are the only two supported keys. Either may be omitted, but the file must
contain at least one check. There is no schema language, type coercion, inference,
regular expression, or automatic correction.

- `required_nonblank` is an array of unique column-name strings. A value is blank
  if Python's `str.strip()` makes it empty, including spaces, tabs, newlines, NBSP
  and many Unicode whitespace characters. This check never edits the value, even
  without `--trim`. U+200B (zero-width space) and U+FEFF are not stripped and are
  not considered blank by this rule. Decide explicitly whether those characters
  need a separate upstream review.
- `allowed_values` maps each selected column name to a nonempty array of unique
  string values. Matching is exact and case-sensitive, after any requested
  trimming. No number conversion or Unicode normalization occurs: `"001"` differs
  from `"1"`, and composed `"é"` differs from `"e\u0301"`. An empty string is a valid
  allowed value if explicitly listed. Non-string values, such as JSON `1`, `true`
  or `null`, are rejected.
- Column names are exact names from the effective header, after optional trimming.
  Rule names and allowed values are never trimmed. With `--trim`, the header
  `" id "` becomes `"id"`, so the rule must name `"id"`. Without it, the rule must
  name `" id "`. A column may appear once in each check category; both checks apply.

Unknown keys, repeated JSON object keys, repeated required columns, duplicate
allowed values, missing columns, ambiguous CSV headers, empty check sets, and
malformed configuration fail without publishing a result directory. Rule files
must be valid UTF-8 JSON (an optional UTF-8 signature is accepted) and at most
1 MiB. Trailing content, non-finite numbers and invalid Unicode strings are
rejected. Error messages do not echo rule values. These limits are validation,
not a promise to repair arbitrary schemas.

## Processing order

1. Parse the complete CSV strictly and validate its header. Rows with the wrong
   number of cells go directly to review; rules and trimming are not applied to
   those rows.
2. For a correctly shaped row, apply `--trim` if requested. Without that flag,
   spaces remain and can cause an allowed-value mismatch.
3. Run all configured checks and collect every failed check. A failing row goes
   to review exactly once, with its original, untrimmed cells.
4. Only rows passing all checks participate in `--deduplicate`. The first exact
   valid row is retained. Repeated invalid rows are each retained for review,
   rather than removed as duplicates.

`changed_cells` counts candidate cells changed by trimming, including cells in
rows later reviewed or removed as duplicates. The audit therefore may contain a
`trimmed` event followed by a `quarantined` event for the same source record.
Header trimming is recorded separately and is not part of `changed_cells`.
Record numbers include the header and count CSV records, not physical lines; a
quoted multiline field is still part of one record.

## Explainable output

The existing five output filenames are unchanged:

- `cleaned.csv` contains only structurally valid rows passing the configured
  checks, after requested transformations. Passing these checks is not a general
  data-quality, deliverability, business-validity or security guarantee.
- `review.csv` keeps its first three columns: `source_record`, `reason`, and
  `original_cells_json`. When rules are enabled it adds `rules_sha256`. Multiple
  reasons are joined in the `reason` field. The JSON cell array preserves the
  original parsed strings, including surrounding whitespace and embedded
  newlines; it is not a byte-for-byte copy of the original CSV record syntax.
- `audit.jsonl` begins with a `review_rules_applied` event on header record 1.
  Every event includes `rules_sha256`. Rule quarantines include `rule_failures`,
  with the code (`required_nonblank` or `not_allowed_value`), exact column name,
  and one-based `column_index`. Structurally malformed rows have only their
  structural reason; they are not also evaluated against rules.
- `summary.json` adds `review_rules` with the rules basename, SHA-256,
  `required_nonblank`, and `allowed_value_columns`.
- `report.html` identifies the rules file and fingerprint and links all outputs.
  Names are HTML-escaped; raw customer cell values are not rendered.

The fingerprint is SHA-256 of the **exact rule-file bytes**, including an optional
UTF-8 signature and formatting. Semantically equivalent files with different
spacing have different fingerprints. Keep the original rules file alongside the
outputs if you need to reproduce the decisions; the summary lists selected
columns but does not embed allowed values or copy the rules file.

In the fictional example, source record 4 lacks an ID, record 5 has an unlisted
status, record 7 is too short, and record 8 fails both ID and status checks. Record
3 duplicates record 2. Record 6 is trimmed before its Unicode status is checked.
The `001` status remains a string, as do the zero-padded IDs.

Without `--rules`, the four data-file formats and bytes remain unchanged for
the same source and options. In particular, `review.csv` has its original three
columns and no rule fields or events appear in the JSON outputs. The HTML report
has a copy-only revision: singular row/duplicate wording and the spreadsheet
warning before the file links. The separate [preflight profiler](profiling.md)
is unchanged and does not interpret these rules.

## Safety and checks

Rules do not neutralize spreadsheet formulas. Even an explicitly allowed value
could be formula-like. Follow the existing [spreadsheet safety guidance](behavior.md#spreadsheet-safety).
Sources, rule files, review rows, audits and summaries can contain or reveal
private data and should stay local unless you choose to share them.

```sh
python -m unittest discover -s tests -v
```

The rules tests exercise strict configuration failures, blank and Unicode
semantics, exact leading-zero values, trim on/off behavior, multiple reasons,
malformed-row precedence, original review cells, record numbers, deduplication
and output protection. A deterministic generator covers 40 fixed seeds under
four transformation combinations, comparing every ready/review/duplicate record
and checking cleaned-data idempotence. A frozen baseline fixture compares the
four no-rules data files byte-for-byte by SHA-256 over 12 combinations, including
ordinary headers, UTF-8 signatures and literal U+FEFF headers. Separate revised
HTML hashes cover the report-only copy change. Report tests also check singular
and plural counts, warning placement, and saved-example/demo parity. The existing
cleanup, BOM, generated-property and profiler suites still run.

The rules/header preflight tests also cover coverage, missing targets, shared
binding/header behavior, invalid JSON, file failures, safe error output, BOMs,
TSV, Unicode and multiline names, deterministic CLI reports, deliberately
unchecked data, source-change detection, and exact reproduction of the saved
reports. Generated header cases compare preflight acceptance with cleanup under
both trimming settings. SHA-256 snapshots prove source, configuration and
existing result files are unchanged by successful and failed checks.
