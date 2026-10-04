#!/usr/bin/env python3
"""Check local review rules against a CSV header without running cleanup."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import sys

from review_rules import RulesError, load_rules
from tidy_csv import InputError, read_header


NOTES = [
    "Only the rules and CSV header are checked. Data rows are not parsed or evaluated; text decoding may read ahead.",
    "Column names match exactly after optional header trimming. Rule names and allowed values are never trimmed.",
    "No cleanup outputs are created. Passing this check does not mean the data passes its rules or is safe for spreadsheets.",
    "Basenames and column names are included; data cells and configured allowed values are never included.",
]


def check_rules(source: Path | str, rules: Path | str, *, trim: bool = False,
                delimiter: str = ",") -> dict:
    """Return a deterministic rules/header report, including validation failures.

    Invalid API options raise ValueError. Expected file/configuration/header
    failures are returned with valid=False. A successful report says nothing
    about data records, including their quoting, widths or rule compliance.
    """
    if type(trim) is not bool:
        raise ValueError("trim must be a boolean.")
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in "\r\n\0":
        raise ValueError("Delimiter must be one character, other than a line break or NUL.")
    source, rules = Path(source), Path(rules)
    result = {
        "schema_version": 1,
        "scope": "rules_and_header",
        "valid": False,
        "source": source.name,
        "rules": {"source": rules.name, "sha256": None},
        "settings": {"encoding": "utf-8-sig", "delimiter": delimiter, "trim": trim},
        "data_rows_checked": False,
        "columns": None,
        "unmatched_rule_columns": None,
        "errors": [],
        "notes": NOTES.copy(),
    }

    def invalid(code: str, message: str) -> dict:
        result["errors"].append({"code": code, "message": message})
        return result

    try:
        config = load_rules(rules)
    except RulesError as exc:
        return invalid("invalid_rules", str(exc))
    except OSError:
        # OS errors may contain private directory paths; never echo them.
        return invalid("unreadable_rules", "Could not read the rules file.")
    result["rules"]["sha256"] = config.sha256

    try:
        if not source.is_file():
            return invalid("unreadable_source", "CSV source must be a file.")
        with source.open(encoding="utf-8-sig", newline="") as src:
            before = os.fstat(src.fileno())
            _, header = read_header(csv.reader(src, delimiter=delimiter, strict=True), trim=trim)
            after = os.fstat(src.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return invalid("source_changed", "Source changed during inspection; retry with a stable file.")
    except InputError as exc:
        return invalid("invalid_header", str(exc))
    except (csv.Error, UnicodeError) as exc:
        reason = "invalid UTF-8 near the header" if isinstance(exc, UnicodeError) else "invalid CSV quoting or field size in the header"
        return invalid("unreadable_header", f"Could not read the CSV header: {reason}.")
    except OSError:
        return invalid("unreadable_source", "Could not read the CSV source.")

    required = set(config.required_nonblank)
    allowed_counts = {name: len(values) for name, values in config.allowed_values}
    result["columns"] = [
        {"column_index": index, "name": name, "required_nonblank": name in required,
         "allowed_value_count": allowed_counts.get(name, 0)}
        for index, name in enumerate(header, 1)
    ]
    configured_names = dict.fromkeys((*config.required_nonblank, *allowed_counts))
    result["unmatched_rule_columns"] = [name for name in configured_names if name not in header]
    try:
        # The cleanup binder remains the authority on rule/header compatibility.
        config.bind(header)
    except RulesError as exc:
        return invalid("unbound_rules", str(exc))
    result["valid"] = True
    return result


def render_text(result: dict) -> str:
    """JSON-escape all untrusted names, including terminal controls."""
    quote = lambda value: json.dumps(value, ensure_ascii=True)
    lines = [
        f"Review-rules preflight: {quote(result['source'])}",
        "Rules and header only. Data rows have NOT been checked.",
        "No cleanup or output files were created.",
        f"Rules: {quote(result['rules']['source'])}",
    ]
    if result["rules"]["sha256"]:
        lines.append(f"Rules SHA-256 (exact file bytes): {result['rules']['sha256']}")
    lines.append(f"Trim header edges: {'yes' if result['settings']['trim'] else 'no'}; "
                 f"delimiter: {quote(result['settings']['delimiter'])}.")
    lines.append("Result: configuration can bind to this header." if result["valid"]
                 else "Result: invalid configuration or unreadable/invalid header.")
    if result["columns"] is not None:
        lines.append("Configured checks by effective CSV column:")
        for column in result["columns"]:
            checks = []
            if column["required_nonblank"]:
                checks.append("required nonblank")
            if column["allowed_value_count"]:
                checks.append(f"{column['allowed_value_count']} allowed exact values (hidden)")
            lines.append(f"  {column['column_index']}. {quote(column['name'])}: "
                         + ("; ".join(checks) if checks else "no configured checks"))
    if result["unmatched_rule_columns"]:
        lines.append("Rule columns absent from the effective header: "
                     + quote(result["unmatched_rule_columns"]))
    for error in result["errors"]:
        lines.append(f"Error [{error['code']}]: {quote(error['message'])}")
    lines.extend(result["notes"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--rules", type=Path, required=True, help="Local JSON review rules to validate")
    parser.add_argument("--trim", action="store_true", help="Match the effective header used by cleanup --trim")
    parser.add_argument("--delimiter", default=",", help="Input delimiter; use 'tab' for TSV. No guessing.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Report format on stdout")
    args = parser.parse_args(argv)
    try:
        result = check_rules(args.source, args.rules, trim=args.trim,
                             delimiter="\t" if args.delimiter == "tab" else args.delimiter)
    except ValueError as exc:
        print(f"Error: {ascii(str(exc))}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2) if args.format == "json"
          else render_text(result), end="\n" if args.format == "json" else "")
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
