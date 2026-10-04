# Behavior and limits

[← Back to the project](../README.md)

## Transformations

Both transformations are opt-in:

- `--trim` removes surrounding whitespace from fields.
- `--deduplicate` compares the entire row after optional trimming and keeps the first match.

Without these flags, whitespace and repeated records remain. The tool does not infer that two different names or email addresses represent the same person.

All fields stay text. `00017` stays `00017`, and `1,204.50` stays `1,204.50`. Dates, currencies, missing values and delimiters are not guessed. Spreadsheet applications can still interpret a CSV when opening it; explicitly import identifier columns as text.

## Encoding and delimiters

The input must be UTF-8, with an optional byte-order mark.

```sh
python tidy_csv.py input.csv new-result --delimiter ';'
python tidy_csv.py input.tsv new-result --delimiter tab
```

Output uses UTF-8 and comma-separated fields.

A byte-order mark at the very start of the input file is treated as an encoding signature. A literal U+FEFF inside a quoted header is field data and stays intact. When the first output column begins with that character, the header is quoted so reading the cleaned file again cannot mistake it for a file signature.

## Spreadsheet safety

This tool preserves data; it does **not** neutralize spreadsheet formulas. A value such as `=1+1`, `+SUM(A1:A2)`, `-12` or `@text` is written unchanged (apart from requested trimming). CSV quoting only escapes delimiters and quotes; it is not a formula-injection defense. Leading whitespace or control characters may also affect how a spreadsheet interprets a cell, and `--trim` can expose a formula prefix.

For exports you do not trust:

- Inspect the file in a text editor first, rather than double-clicking it in a spreadsheet.
- Use your spreadsheet's import flow and explicitly set **all untrusted columns to text**, with formula evaluation disabled where offered. Check the resulting cell types before saving or sharing the workbook.
- Do not enable links, macros, or other active content prompted by imported data.
- If you need a spreadsheet-safe delivery format, make that a separate, reviewed export step. Prefixing values or changing their types would alter the original data, so this cleanup sample does not silently do it.

The HTML report escapes the source filename and does not render cell values. It still links to the raw CSV files; opening those files has the risks above. Audit and review files can contain the original data and need the same privacy care as the source.

## Record numbers

Numbers count CSV records, including the header, rather than physical lines. A quoted multiline field is one record.

## Review and failure handling

Rows with too many or too few cells are kept in `review.csv` with their original values. Inspect them before using the cleaned data.

Invalid quoting, invalid UTF-8 and ambiguous headers stop the run without publishing partial output. The output directory must be new. The source file is read, not edited.

`audit.jsonl` records changed, removed and review records; `summary.json` records the settings, counts and a SHA-256 fingerprint of the input.

### Source fingerprint and concurrent changes

Cleanup reads the source once into a private temporary copy in the output's parent filesystem. It hashes the exact bytes copied, then parses only that copy. `source_sha256` therefore describes the bytes actually processed, including the original UTF-8 signature and line endings. The copy is closed and removed before publishing; it is not an extra output file. A stable source produces the same five output files and bytes as before.

Keep the source stable while it is being captured. An observed change to its size or modification/change timestamps during capture stops the run with no published output. The check is a best-effort guard, not a filesystem lock or an atomic point-in-time snapshot: a concurrent writer can cause the captured stream to contain bytes from different versions. Even if that change evades the metadata check, the fingerprint still describes exactly the private copy parsed. An edit or path replacement after capture does not change that copy; the result can describe an earlier version than the file currently at the source path. The source basename is a label, not proof that the path still contains those bytes.

Capture uses a fixed 1 MiB buffer and temporary disk space equal to the source byte length, in addition to the cleanup outputs. It adds one sequential temporary-file write; parsing reads the copy instead of rereading the source. Ordinary cleanup memory still depends on header width and the largest CSV record. With `--deduplicate`, the existing set of distinct rows also grows with the data. A capture/read/write/parse failure removes staging files and publishes no partial result. Sufficient free space on the output filesystem is required.

## Supported environment

Python 3.11 or newer on Linux. Other operating systems have not been validated.

## Example and checks

[The saved example](../examples/checked) contains five ready rows, two review rows and one exact duplicate from eight fictional input records.

The [test suite](../tests) covers leading zeroes, Unicode, embedded commas and newlines, opt-in transformations, row accounting, retained review data, malformed input and an existing output directory.

The generated suite runs 40 fixed seeds under each flag combination (160 cases), without extra packages. It compares the full output to an independent small model and checks that `input = ready + duplicates + review`, each removed duplicate points to its first retained record, and every malformed record remains in review. Cleaning `cleaned.csv` again with the same flags must produce byte-identical cleaned data and no new changes, review records or duplicates. This idempotence promise is for cleaned data only: metadata and the audit describe a new input and a new run.
