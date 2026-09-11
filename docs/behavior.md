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

## Record numbers

Numbers count CSV records, including the header, rather than physical lines. A quoted multiline field is one record.

## Review and failure handling

Rows with too many or too few cells are kept in `review.csv` with their original values. Inspect them before using the cleaned data.

Invalid quoting, invalid UTF-8 and ambiguous headers stop the run without publishing partial output. The output directory must be new. The source file is read, not edited.

`audit.jsonl` records changed, removed and review records; `summary.json` records the settings, counts and a SHA-256 fingerprint of the input.

## Supported environment

Python 3.11 or newer on Linux. Other operating systems have not been validated.

## Example and checks

[The saved example](../examples/checked) contains five ready rows, two review rows and one exact duplicate from eight fictional input records.

The [test suite](../tests) covers leading zeroes, Unicode, embedded commas and newlines, opt-in transformations, row accounting, retained review data, malformed input and an existing output directory.
