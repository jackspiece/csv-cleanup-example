# CSV Cleanup

Clean a CSV export and keep a record of what changed. IDs stay text, rows with missing or extra cells go to review, and the result comes with a readable report.

<picture>
  <source media="(max-width: 600px)" srcset="docs/assets/overview-mobile.png">
  <img src="docs/assets/overview.png" alt="CSV Cleanup example: 8 input rows, 5 ready, 2 for review and 1 duplicate, alongside the actual HTML report." width="1280">
</picture>

**[See the example report](https://jackspiece.github.io/csv-cleanup-example/)** · **[Quick start](#quick-start)** · [Preflight profiling](docs/profiling.md) · [Explicit review rules](docs/review-rules.md) · [Behavior and limits](docs/behavior.md)

[![Check CSV cleanup](https://github.com/jackspiece/csv-cleanup-example/actions/workflows/check.yml/badge.svg)](https://github.com/jackspiece/csv-cleanup-example/actions/workflows/check.yml)

Python 3.11+ · Standard library only · Verified on Linux

## Quick start

```sh
git clone https://github.com/jackspiece/csv-cleanup-example.git
cd csv-cleanup-example
python tidy_csv.py examples/messy_customers.csv result --trim --deduplicate
```

Open **`result/report.html`** in your browser. Use a new output directory for each run.

To inspect an export first, run `python profile_csv.py examples/messy_customers.csv`. The [read-only preflight guide](docs/profiling.md) explains structural counts, explicit prefix limits, and opt-in value previews. Profiling does not choose cleanup flags or certify spreadsheet safety.

Before applying review rules, run `python check_rules.py examples/review-rules/customers.csv --rules examples/review-rules/rules.json --trim`. The [rules/header preflight](docs/review-rules.md#check-the-rules-before-cleanup) shows which columns have configured checks and flags invalid configuration or missing targets. It creates no cleanup outputs and does not check data rows.

The included data is fictional. With the two flags above, the example produces:

| Input | Ready | Review | Duplicate |
| ---: | ---: | ---: | ---: |
| 8 | 5 | 2 | 1 |

**[Browse the saved output files →](examples/checked)**

## What you get

| File | Purpose |
| --- | --- |
| `cleaned.csv` | Rows with the expected columns and the transformations you requested. |
| `review.csv` | Malformed or rule-failing rows, original values and source record numbers. |
| `audit.jsonl` | A trace of changes, duplicate removal and review decisions. |
| `summary.json` | Counts, settings and a fingerprint of the source file. |
| `report.html` | A local page linking the results. |

## The rules are deliberately small

Trimming and exact deduplication are optional. Optional [review rules](docs/review-rules.md) can require nonblank fields or exact allowed values; failing rows retain their original cells and explicit reasons for review. Without `--rules`, the existing data-file formats and bytes are unchanged.

Duplicate comparison uses the whole row after any requested trimming. It does not guess whether different names belong to the same person.

Values stay text: `00017` remains `00017`. Dates, currencies and missing values are not inferred. Spreadsheet applications may still guess types when opening a CSV, so import identifier columns as text.

CSV output is **not sanitized for spreadsheet formulas**. Untrusted cells beginning with `=`, `+`, `-` or `@` can be interpreted as formulas by spreadsheet software. Import all untrusted columns explicitly as text; see the [spreadsheet safety guidance](docs/behavior.md#spreadsheet-safety) before opening them.

Invalid quoting, invalid UTF-8 and ambiguous headers stop the run without publishing partial output. Rows with the wrong number of cells are retained for review.

See **[the complete behavior guide](docs/behavior.md)** for delimiters, encoding, record numbers and output protection.

## Run the checks

```sh
python -m unittest discover -s tests -v
```

The original ten regression tests cover text preservation, quoted and multiline fields, optional transformations, record accounting and malformed input. A deterministic generated test adds 160 cases (40 fixed seeds × four flag combinations), checking exact values, row conservation, duplicate references, retained review records and cleaned-data idempotence across comma, semicolon and tab inputs.

Dedicated header regressions distinguish UTF-8 file signatures from literal U+FEFF characters, including repeated cleanup, optional trimming, and ordinary-header output compatibility.

---

Built by [jackspiece](https://github.com/jackspiece). For a small CSV or Python job, [open a project enquiry](https://github.com/jackspiece/csv-cleanup-example/issues/new?template=work-request.md) with a redacted example. Scope, price and funding are agreed before work starts. Keep private customer and payment details out of public issues.
