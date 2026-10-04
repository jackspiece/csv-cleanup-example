"""Read-only profiling, privacy defaults, exact accounting and scan boundaries."""
import codecs
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock

from profile_csv import (MAX_PREVIEW_CELL_CHARS, MAX_PREVIEW_COLUMNS,
                         MAX_PREVIEW_ROWS, MAX_WIDTH_EXAMPLES, main,
                         profile, render_text)
from tidy_csv import InputError, clean

ROOT = Path(__file__).resolve().parents[1]


class ProfilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "private-source.csv"

    def write_rows(self, rows, *, delimiter=",", signature=False, quoting=csv.QUOTE_MINIMAL):
        with self.source.open("w", encoding="utf-8-sig" if signature else "utf-8", newline="") as stream:
            csv.writer(stream, delimiter=delimiter, quoting=quoting).writerows(rows)

    def checked_profile(self, **kwargs):
        original = self.source.read_bytes()
        before_hash = hashlib.sha256(original).hexdigest()
        before_files = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        result = profile(self.source, **kwargs)
        after_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(result["source_sha256"], before_hash)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(before_files, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))
        return result

    def test_exact_counts_valid_rows_only_and_source_hash_unchanged(self):
        self.write_rows([
            ["id", "name", "formula", "note"],
            ["0001", "José", "=1", ""],
            ["0002", " \t ", " \t@calc", "a,b\nc"],
            [],
            ["MALFORMED-SECRET"],
            ["MALFORMED-WIDE", "", "\ufeff-1", "ignore", "", ""],
            ["0003", " Ω ", "plain", "\u2003"],
        ])
        result = self.checked_profile()
        self.assertTrue(result["scan"]["complete"])
        self.assertEqual(result["scan"]["rows_scanned"], 6)
        self.assertEqual(result["scan"]["total_rows"], 6)
        self.assertEqual(result["scan"]["column_counts_denominator_rows"], 3)
        self.assertEqual(result["rows"], {
            "matching_width": 3, "short": 2, "wide": 1, "malformed_width": 3,
            "blank_records": 1, "max_observed_width": 6, "missing_cells": 7,
            "extra_cells": 2, "width_examples": [
                {"source_record": 4, "cell_count": 0},
                {"source_record": 5, "cell_count": 1},
                {"source_record": 6, "cell_count": 6}], "omitted_width_examples": 0})
        self.assertEqual(result["cells"], {
            "present_cells": 12, "empty_cells": 1, "whitespace_only_cells": 2,
            "outer_whitespace_cells": 4, "formula_like_cells": 2,
            "maximum_cell_length": 7})
        for column in result["columns"]:
            self.assertEqual(column["present_cells"], 3)
        self.assertEqual([c["maximum_cell_length"] for c in result["columns"]], [4, 4, 7, 5])
        self.assertEqual([c["empty_cells"] for c in result["columns"]], [0, 0, 0, 1])
        self.assertEqual([c["whitespace_only_cells"] for c in result["columns"]], [0, 1, 0, 1])
        self.assertEqual([c["formula_like_cells"] for c in result["columns"]], [0, 0, 2, 0])

    def test_default_preview_never_contains_data_or_absolute_path(self):
        secret = "DO-NOT-EXPOSE-raw-customer-data"
        self.write_rows([["id", "note"], [secret, "0012"], [secret], [secret, "x", secret]])
        result = self.checked_profile()
        for output in (json.dumps(result), render_text(result)):
            self.assertNotIn(secret, output)
            self.assertNotIn("0012", output)
            self.assertNotIn(str(self.root), output)
        self.assertEqual(result["source"], self.source.name)
        self.assertEqual(len(result["preview"]), 3)
        self.assertTrue(all("values" not in row for row in result["preview"]))

    def test_opt_in_preview_has_bounded_rows_columns_and_characters(self):
        long = "秘密" * MAX_PREVIEW_CELL_CHARS
        self.write_rows([[f"c{i}" for i in range(25)], *[[long] * 25 for _ in range(30)]])
        result = self.checked_profile(include_values=True, preview_rows=MAX_PREVIEW_ROWS)
        self.assertEqual(len(result["preview"]), MAX_PREVIEW_ROWS)
        self.assertEqual(result["scan"]["rows_scanned"], 30)
        for row in result["preview"]:
            self.assertEqual(len(row["values"]), MAX_PREVIEW_COLUMNS)
            self.assertTrue(all(len(value) == MAX_PREVIEW_CELL_CHARS for value in row["values"]))
            self.assertEqual(row["omitted_cells"], 5)
            self.assertEqual(row["truncated_value_columns"], list(range(1, 21)))
        result = self.checked_profile(include_values=True, preview_rows=0)
        self.assertEqual(result["preview"], [])

    def test_delimiters_multiline_unicode_and_literal_bom_are_preserved(self):
        for delimiter in (",", ";", "\t"):
            for signature in (False, True):
                for header in (["id", "note"], ["\ufeffid", "id"], ["\ufeff", "名前"],
                               ["\ufeff\ufeffid", "空山"], [" \ufeffid ", "value"]):
                    with self.subTest(delimiter=delimiter, signature=signature, header=header):
                        row = ["00017", f"José{delimiter}\"\r\nsecond line"]
                        self.write_rows([header, row, ["short"]], delimiter=delimiter,
                                        signature=signature, quoting=csv.QUOTE_ALL)
                        result = self.checked_profile(delimiter=delimiter, include_values=True)
                        self.assertEqual([c["name"] for c in result["columns"]], header)
                        self.assertEqual(result["preview"][0]["values"], row)
                        self.assertEqual(result["rows"]["width_examples"], [{"source_record": 3, "cell_count": 1}])
                        self.assertEqual(result["scan"]["rows_scanned"], 2)
                        self.assertEqual(result["columns"][1]["maximum_cell_length"], len(row[1]))

    def test_unquoted_file_signature_is_not_part_of_header(self):
        self.source.write_bytes(codecs.BOM_UTF8 + b"id,name\r\n0001,Ada\r\n")
        result = self.checked_profile()
        self.assertEqual([c["name"] for c in result["columns"]], ["id", "name"])

    def test_duplicate_blank_and_trim_collision_headers_are_diagnosed_not_fixed(self):
        header = ["id", " id ", "id", "", " \t", "\ufeff"]
        self.write_rows([header, ["value"] * len(header)])
        result = self.checked_profile()
        self.assertEqual([c["name"] for c in result["columns"]], header)
        self.assertEqual([c["index"] for c in result["columns"]], list(range(1, 7)))
        self.assertEqual(result["header"]["empty_name_columns"], [4])
        self.assertEqual(result["header"]["whitespace_only_name_columns"], [5])
        self.assertEqual(result["header"]["outer_whitespace_columns"], [2, 5])
        self.assertEqual(result["header"]["duplicate_names"], [{"name": "id", "columns": [1, 3]}])
        self.assertEqual(result["header"]["duplicate_names_after_trim"], [
            {"name": "id", "columns": [1, 2, 3]}, {"name": "", "columns": [4, 5]}])
        self.assertIn("Duplicate name if trimmed", render_text(result))

    def test_header_only_and_blank_header(self):
        self.write_rows([["id", "name"]])
        result = self.checked_profile()
        self.assertEqual(result["scan"]["total_rows"], 0)
        self.assertEqual(result["cells"]["maximum_cell_length"], 0)
        self.assertEqual(result["preview"], [])
        self.source.write_bytes(b"\n\nx\n")
        result = self.checked_profile()
        self.assertTrue(result["header"]["no_columns"])
        self.assertEqual(result["columns"], [])
        self.assertEqual((result["rows"]["matching_width"], result["rows"]["wide"]), (1, 1))
        self.assertIn("header record is blank", render_text(result))

    def test_formula_heuristic_covers_whitespace_controls_and_bom_without_evaluation(self):
        suspicious = ["=1+1", "+SUM(A1:A2)", "-12", "@text", " \t=1", "\x00\x1f+1",
                      "\x7f-1", "\ufeff@text", "\u2003\r\n=1"]
        ordinary = ["", "\t", "0012", "café", "x=1", "'=1", "\u200b=1"]
        self.write_rows([["value"], *[[value] for value in suspicious + ordinary]])
        result = self.checked_profile(include_values=True, preview_rows=20)
        self.assertEqual(result["cells"]["formula_like_cells"], len(suspicious))
        self.assertEqual([item["values"][0] for item in result["preview"]], suspicious + ordinary)
        self.assertIn("never certify spreadsheet safety, even when zero", " ".join(result["notes"]))
        self.assertIn("trimming can expose prefixes", " ".join(result["notes"]))

    def test_prefix_limit_is_exact_observed_count_not_estimate_or_full_scan(self):
        self.write_rows([["id"], ["0001"], ["0002"], ["0003", "wide"]])
        limited = self.checked_profile(max_rows=2)
        self.assertEqual(limited["scan"], {"complete": False, "stop_reason": "row_limit",
                         "rows_scanned": 2, "total_rows": None, "counts_scope": "scanned_prefix",
                         "counts_are_estimates": False, "column_counts_denominator_rows": 2})
        self.assertEqual(limited["rows"]["malformed_width"], 0)
        self.assertIn("Total rows unknown", render_text(limited))
        self.assertIn("first 2 data records only", render_text(limited))
        exact_limit = self.checked_profile(max_rows=3)
        self.assertFalse(exact_limit["scan"]["complete"])
        self.assertEqual(exact_limit["rows"]["malformed_width"], 1)
        larger_limit = self.checked_profile(max_rows=4)
        self.assertTrue(larger_limit["scan"]["complete"])
        self.assertEqual(larger_limit["scan"]["total_rows"], 3)
        self.assertEqual(limited["source_sha256"], larger_limit["source_sha256"])

    def test_late_invalid_quoting_is_unchecked_past_prefix_limit_but_full_scan_fails(self):
        self.source.write_bytes(b'id\n0001\n"unterminated\n')
        limited = self.checked_profile(max_rows=1)
        self.assertFalse(limited["scan"]["complete"])
        with self.assertRaises(InputError):
            profile(self.source)

    def test_width_examples_are_bounded_and_counts_reconcile(self):
        self.write_rows([["id", "note"], *[["short"] for _ in range(35)]])
        result = self.checked_profile(preview_rows=0)
        self.assertEqual(len(result["rows"]["width_examples"]), MAX_WIDTH_EXAMPLES)
        self.assertEqual(result["rows"]["omitted_width_examples"], 30)
        self.assertEqual(result["rows"]["malformed_width"], 35)
        self.assertEqual(result["cells"]["present_cells"], 0)
        self.assertEqual(result["scan"]["column_counts_denominator_rows"], 0)

    def test_invalid_inputs_fail_without_partial_stdout_or_source_changes(self):
        for raw in (b"", b'id,name\n1,ok\n2,"unterminated\n',
                    b'id,name\n1,ok\n2,\xffSECRET\n', b'a\n' + b'x' * 140_000 + b'\n'):
            for fmt in ("text", "json"):
                with self.subTest(raw_length=len(raw), fmt=fmt):
                    self.source.write_bytes(raw)
                    output, error = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                        code = main([str(self.source), "--format", fmt])
                    self.assertEqual(code, 2)
                    self.assertEqual(output.getvalue(), "")
                    self.assertTrue(error.getvalue().startswith("Error:"))
                    self.assertNotIn("SECRET", error.getvalue())
                    self.assertEqual(self.source.read_bytes(), raw)
                    self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_invalid_options_and_missing_files_do_not_create_anything(self):
        self.write_rows([["id"], ["0001"]])
        for options in ({"delimiter": ""}, {"delimiter": "||"}, {"delimiter": "\n"},
                        {"delimiter": "\r"}, {"delimiter": "\0"}, {"max_rows": -1},
                        {"max_rows": True}, {"max_rows": 2.5}, {"preview_rows": -1},
                        {"preview_rows": 21}, {"preview_rows": False},
                        {"include_values": "false"}, {"include_values": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                profile(self.source, **options)
        with self.assertRaises(InputError):
            profile(self.root / "missing.csv")
        with self.assertRaises(InputError):
            profile(self.root)
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_cli_errors_do_not_disclose_absolute_paths(self):
        for source in (self.root / "missing.csv", self.root):
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                code = main([str(source)])
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(str(self.root), error.getvalue())
        self.write_rows([["id"]])
        output, error = io.StringIO(), io.StringIO()
        with mock.patch("pathlib.Path.open", side_effect=PermissionError(13, "denied", str(self.source))):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                code = main([str(self.source)])
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.root), error.getvalue())
        self.assertIn("Permission denied", error.getvalue())

    def test_human_output_escapes_terminal_controls_in_names_and_opt_in_values(self):
        self.source = self.root / "bad\x1b[31m\nname.csv"
        self.write_rows([["header\n\x1b[31m\u202efake"], ["raw\r\n\x1b[2J\u202e"]])
        result = self.checked_profile(include_values=True)
        text = render_text(result)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\u202e", text)
        self.assertIn(r"\u001b", text)
        self.assertIn(r"\n", text)
        self.assertIn(r"\u202e", text)

    def test_cli_subprocess_json_tsv_and_argument_failures(self):
        self.write_rows([["id", "note"], ["0001", "value\tquoted"]], delimiter="\t")
        command = [sys.executable, str(ROOT / "profile_csv.py"), str(self.source)]
        result = subprocess.run([*command, "--delimiter", "tab", "--format", "json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["rows"]["matching_width"], 1)
        self.assertEqual(report["settings"]["delimiter"], "\t")
        self.assertNotIn("values", report["preview"][0])
        for options in (["--max-rows", "-1"], ["--preview-rows", "21"],
                        ["--format", "html"], ["--max-rows", "abc"], ["--delimiter", "||"]):
            with self.subTest(options=options):
                failed = subprocess.run([*command, *options], capture_output=True, text=True)
                self.assertEqual(failed.returncode, 2)
                self.assertEqual(failed.stdout, "")

    def test_source_change_during_hash_is_rejected(self):
        self.write_rows([["id"], ["0001"]])
        real_digest = hashlib.file_digest

        def mutate_after_hash(stream, algorithm):
            result = real_digest(stream, algorithm)
            with self.source.open("ab") as output:
                output.write(b"0002\n")
            return result

        with mock.patch("profile_csv.hashlib.file_digest", side_effect=mutate_after_hash):
            with self.assertRaisesRegex(InputError, "changed during inspection"):
                profile(self.source)

    def test_existing_five_cleanup_outputs_are_byte_identical_before_and_after_profile(self):
        self.write_rows([[" id ", "name", "note"], ["0001", " Ada ", "one\ntwo"],
                         ["0001", "Ada", "one\ntwo"], ["0002", "José", ""], ["short"]],
                        signature=True)
        before = self.root / "before"
        clean(self.source, before, trim=True, deduplicate=True)
        self.checked_profile(include_values=True)
        after = self.root / "after"
        clean(self.source, after, trim=True, deduplicate=True)
        expected = ["cleaned.csv", "review.csv", "audit.jsonl", "summary.json", "report.html"]
        self.assertEqual(sorted(path.name for path in before.iterdir()), sorted(expected))
        for filename in expected:
            self.assertEqual((before / filename).read_bytes(), (after / filename).read_bytes(), filename)

    def test_large_synthetic_file_keeps_bounded_working_memory(self):
        # Distinct values expose accidental row/distinct-value retention. This is
        # a small regression guard, not a production performance guarantee.
        with self.source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["id", "note"])
            for index in range(40_000):
                writer.writerow([f"{index:08}", f"unique-{index}-" + "x" * 90])
        tracemalloc.start()
        try:
            result = profile(self.source)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["scan"]["total_rows"], 40_000)
        self.assertEqual(result["cells"]["present_cells"], 80_000)
        self.assertEqual(len(result["preview"]), 5)
        self.assertLess(peak, 2_000_000, f"Peak traced allocation grew unexpectedly: {peak}")

    def test_generated_accounting_with_independent_reference(self):
        values = ["", " ", "\u2003", "00017", "José", "空山", "a,b\nc", "=1", " -1", "x=1", "x "]
        for seed in range(16):
            rng = random.Random(seed)
            width = rng.randint(1, 6)
            header = [f"col{i}" for i in range(width)]
            rows = [[rng.choice(values) for _ in range(rng.randint(0, width + 2))] for _ in range(45)]
            delimiter = [",", ";", "\t"][seed % 3]
            self.write_rows([header, *rows], delimiter=delimiter, signature=bool(seed % 2))
            with self.subTest(seed=seed):
                result = self.checked_profile(delimiter=delimiter)
                valid = [row for row in rows if len(row) == width]
                self.assertEqual(result["scan"]["total_rows"], len(rows))
                self.assertEqual(result["rows"]["matching_width"], len(valid))
                self.assertEqual(result["rows"]["malformed_width"], len(rows) - len(valid))
                self.assertEqual(result["cells"]["present_cells"], len(valid) * width)
                self.assertEqual(result["rows"]["missing_cells"], sum(max(0, width - len(row)) for row in rows))
                self.assertEqual(result["rows"]["extra_cells"], sum(max(0, len(row) - width) for row in rows))
                for index, column in enumerate(result["columns"]):
                    expected = [row[index] for row in valid]
                    self.assertEqual(column["present_cells"], len(valid))
                    self.assertEqual(column["empty_cells"], expected.count(""))
                    self.assertEqual(column["whitespace_only_cells"], sum(v != "" and v.isspace() for v in expected))
                    self.assertEqual(column["outer_whitespace_cells"], sum(bool(v) and (v[0].isspace() or v[-1].isspace()) for v in expected))
                    self.assertEqual(column["maximum_cell_length"], max(map(len, expected), default=0))


if __name__ == "__main__":
    unittest.main()
