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
import unittest
from unittest.mock import patch

from check_rules import check_rules, main, render_text
from review_rules import MAX_RULE_BYTES, ReviewRules
from tidy_csv import InputError, clean, read_header


ROOT = Path(__file__).resolve().parents[1]


class CheckRulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.csv"
        self.rules = self.root / "rules.json"
        self.write()

    def write(self, header=None, config=None, delimiter=","):
        with self.source.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream, delimiter=delimiter).writerows([
                ["id", "status", "note"] if header is None else header,
                ["private-id", "private-status", "private-cell"],
            ])
        self.rules.write_text(json.dumps(config if config is not None else {
            "required_nonblank": ["id"],
            "allowed_values": {"status": ["private-allowed", "001", "批准"]},
        }), encoding="utf-8")

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), "--rules", str(self.rules), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def snapshot(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_valid_coverage_and_exact_rules_fingerprint(self):
        result = check_rules(self.source, self.rules)
        self.assertTrue(result["valid"])
        self.assertEqual(result["scope"], "rules_and_header")
        self.assertIs(result["data_rows_checked"], False)
        self.assertEqual(result["columns"], [
            {"column_index": 1, "name": "id", "required_nonblank": True, "allowed_value_count": 0},
            {"column_index": 2, "name": "status", "required_nonblank": False, "allowed_value_count": 3},
            {"column_index": 3, "name": "note", "required_nonblank": False, "allowed_value_count": 0},
        ])
        self.assertEqual(result["rules"], {"source": "rules.json",
                                         "sha256": hashlib.sha256(self.rules.read_bytes()).hexdigest()})
        self.assertEqual(result["unmatched_rule_columns"], [])
        self.assertEqual(result["errors"], [])

    def test_reports_never_include_data_values_allowed_values_or_parent_paths(self):
        result = check_rules(self.source, self.rules)
        for output in (json.dumps(result), render_text(result)):
            for hidden in ("private-id", "private-status", "private-cell", "private-allowed", str(self.root)):
                self.assertNotIn(hidden, output)
        self.assertIn('"note": no configured checks', render_text(result))
        self.assertIn("Data rows have NOT been checked", render_text(result))

    def test_files_and_existing_outputs_unchanged_and_no_new_files(self):
        output = self.root / "existing-output"
        clean(self.source, output, rules=self.rules)
        before = self.snapshot()
        for args in ((), ("--format", "json"), ("--trim",)):
            self.assertEqual(self.cli(*args)[0], 0)
            self.assertEqual(self.snapshot(), before)
        self.rules.write_text('{"required_nonblank":["missing"]}', encoding="utf-8")
        before = self.snapshot()
        self.assertEqual(self.cli("--format", "json")[0], 2)
        self.assertEqual(self.snapshot(), before)

    def test_missing_targets_report_each_exact_name_once(self):
        self.write(config={"required_nonblank": ["missing", "status"],
                           "allowed_values": {"missing": ["hidden"], "also absent": [""]}})
        result = check_rules(self.source, self.rules)
        self.assertFalse(result["valid"])
        self.assertEqual(result["unmatched_rule_columns"], ["missing", "also absent"])
        self.assertEqual(result["errors"][0]["code"], "unbound_rules")
        self.assertIn("missing", render_text(result))
        self.assertNotIn("hidden", json.dumps(result))

    def test_shared_binder_is_called_for_valid_and_missing_columns(self):
        original = ReviewRules.bind
        observed = []

        def bind(config, header):
            observed.append(header.copy())
            return original(config, header)

        with patch.object(ReviewRules, "bind", bind):
            self.assertTrue(check_rules(self.source, self.rules)["valid"])
            self.write(config={"required_nonblank": ["missing"]})
            self.assertFalse(check_rules(self.source, self.rules)["valid"])
        self.assertEqual(observed, [["id", "status", "note"]] * 2)

    def test_trim_names_are_exact_and_rule_values_not_trimmed_or_exposed(self):
        self.write(header=[" id ", "status"], config={"required_nonblank": ["id"],
                   "allowed_values": {"status": [" private ", "private"]}})
        self.assertFalse(check_rules(self.source, self.rules)["valid"])
        result = check_rules(self.source, self.rules, trim=True)
        self.assertTrue(result["valid"])
        self.assertEqual(result["columns"][1]["allowed_value_count"], 2)
        self.write(header=[" id ", "status"], config={"required_nonblank": [" id "]})
        self.assertTrue(check_rules(self.source, self.rules)["valid"])
        self.assertFalse(check_rules(self.source, self.rules, trim=True)["valid"])

    def test_both_checks_on_same_column_and_unconfigured_columns(self):
        self.write(config={"required_nonblank": ["id"], "allowed_values": {"id": ["001", "1"]}})
        result = check_rules(self.source, self.rules)
        self.assertTrue(result["valid"])
        self.assertEqual(result["columns"][0]["allowed_value_count"], 2)
        self.assertTrue(result["columns"][0]["required_nonblank"])
        self.assertEqual(render_text(result).count("no configured checks"), 2)

    def test_empty_blank_duplicate_and_trim_collision_headers_match_cleanup(self):
        for raw, trim in ((b"", False), (b"\n", False), (b"id,\n", False),
                          (b"id, \n", False), (b"id,id\n", False), (b" id,id\n", True)):
            with self.subTest(raw=raw, trim=trim):
                self.source.write_bytes(raw)
                result = check_rules(self.source, self.rules, trim=trim)
                self.assertFalse(result["valid"])
                self.assertIsNone(result["columns"])
                self.assertEqual(result["errors"][0]["code"], "invalid_header")
                with self.assertRaises(InputError) as raised:
                    clean(self.source, self.root / "output", trim=trim, rules=self.rules)
                self.assertEqual(str(raised.exception), result["errors"][0]["message"])

    def test_header_only_file_is_valid(self):
        self.source.write_text("id,status,note\n", encoding="utf-8")
        self.assertTrue(check_rules(self.source, self.rules)["valid"])

    def test_delimiters_multiline_names_signatures_and_literal_bom(self):
        for delimiter in (",", ";", "\t"):
            for signature in (False, True):
                for name in ("id", "\ufeffid", "line\r\nname", "批准", "é", "e\u0301"):
                    with self.subTest(delimiter=delimiter, signature=signature, name=name):
                        # Quote all fields so a literal U+FEFF stays field data at byte zero.
                        stream = io.StringIO(newline="")
                        csv.writer(stream, delimiter=delimiter, quoting=csv.QUOTE_ALL).writerow([name, "note"])
                        self.source.write_bytes((b"\xef\xbb\xbf" if signature else b"") + stream.getvalue().encode())
                        self.rules.write_text(json.dumps({"required_nonblank": [name]}), encoding="utf-8-sig")
                        result = check_rules(self.source, self.rules, delimiter=delimiter)
                        self.assertTrue(result["valid"])
                        self.assertEqual(result["columns"][0]["name"], name)

    def test_strict_json_failures_are_machine_readable_without_rule_values(self):
        invalid = [
            b'{"required_nonblank": ["id"], "required_nonblank": ["private-value"]}',
            b'{"required_nonblank": ["id"], "unknown-private-key": "private-value"}',
            b'{"allowed_values": {"id": ["private-value", "private-value"]}}',
            b'{"allowed_values": {"id": [1]}}', b'{"allowed_values": {"id": [true]}}',
            b'{"allowed_values": {"id": [NaN]}}', b'{"required_nonblank": ["id", "id"]}',
            b'{"required_nonblank": ["\\ud800"]}', b'{"allowed_values": {"id": []}}',
            b'{}', b'[]', b'{', b'{} trailing-private-value', b'\xff', b' ' * (MAX_RULE_BYTES + 1),
            b'[' * 2000 + b']' * 2000,
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                self.rules.write_bytes(raw)
                code, stdout, stderr = self.cli("--format", "json")
                result = json.loads(stdout)
                self.assertEqual(code, 2)
                self.assertEqual(stderr, "")
                self.assertFalse(result["valid"])
                self.assertEqual(result["errors"][0]["code"], "invalid_rules")
                self.assertIsNone(result["columns"])
                self.assertIsNone(result["rules"]["sha256"])
                self.assertNotIn("private-value", stdout)
                self.assertNotIn("private-key", stdout)

    def test_invalid_header_encoding_and_quoting_fail_without_raw_values(self):
        for raw in (b'private-value,\xff\n', b'"private-value\n'):
            self.source.write_bytes(raw)
            code, stdout, stderr = self.cli("--format", "json")
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout)["errors"][0]["code"], "unreadable_header")
            self.assertNotIn("private-value", stdout)

    def test_invalid_data_rows_are_deliberately_unchecked(self):
        self.source.write_bytes(b'id,status,note\nshort\n,,\n"unclosed private-data')
        result = check_rules(self.source, self.rules)
        self.assertTrue(result["valid"])
        self.assertIs(result["data_rows_checked"], False)
        self.assertNotIn("private-data", json.dumps(result))
        with self.assertRaises(InputError):
            clean(self.source, self.root / "output", rules=self.rules)
        self.assertFalse((self.root / "output").exists())

    def test_read_ahead_encoding_failure_does_not_claim_data_validation(self):
        self.source.write_bytes(b'id,status,note\nprivate-cell,\xff\n')
        result = check_rules(self.source, self.rules)
        self.assertFalse(result["valid"])
        self.assertIs(result["data_rows_checked"], False)
        self.assertNotIn("private-cell", json.dumps(result))

    def test_missing_or_directory_inputs_and_os_errors_hide_parent_paths(self):
        for source, rules in ((self.root / "absent.csv", self.rules), (self.root, self.rules),
                              (self.source, self.root / "absent.json"), (self.source, self.root)):
            result = check_rules(source, rules)
            self.assertFalse(result["valid"])
            self.assertNotIn(str(self.root), json.dumps(result))
        for target in ("check_rules.load_rules", "check_rules.Path.open"):
            with patch(target, side_effect=PermissionError(13, "Private reason", str(self.root / "private"))):
                result = check_rules(self.source, self.rules)
                self.assertFalse(result["valid"])
                self.assertNotIn(str(self.root), json.dumps(result))
                self.assertNotIn("Private reason", json.dumps(result))

    def test_source_change_while_reading_header_is_rejected(self):
        def change(reader, *, trim):
            header = read_header(reader, trim=trim)
            with self.source.open("a", encoding="utf-8") as stream:
                stream.write("another,private,row\n")
            return header

        with patch("check_rules.read_header", side_effect=change):
            result = check_rules(self.source, self.rules)
        self.assertFalse(result["valid"])
        self.assertEqual(result["errors"][0]["code"], "source_changed")

    def test_human_and_json_output_escape_terminal_controls(self):
        name = "id\x1b[31m\r\n\u202e"
        self.source = self.root / "source\x1b.csv"
        self.rules = self.root / "rules\x1b.json"
        self.write(header=[name], config={"required_nonblank": [name, "absent\x1b"]})
        for args in ((), ("--format", "json")):
            code, stdout, stderr = self.cli(*args)
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            self.assertNotIn("\x1b", stdout)
            self.assertNotIn("\r", stdout)
            self.assertNotIn("\u202e", stdout)
            self.assertIn("\\u001b", stdout)

    def test_api_options_and_cli_invalid_options(self):
        for options in ({"trim": 1}, {"trim": "yes"}, {"delimiter": None},
                        {"delimiter": ""}, {"delimiter": ",,"}, {"delimiter": "\n"},
                        {"delimiter": "\r"}, {"delimiter": "\0"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                check_rules(self.source, self.rules, **options)
        code, stdout, stderr = self.cli("--delimiter", "many")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Delimiter", stderr)

    def test_cli_subprocess_json_tsv_determinism_and_no_outputs(self):
        self.write(delimiter="\t")
        before = self.snapshot()
        args = [sys.executable, "-B", str(ROOT / "check_rules.py"), str(self.source),
                "--rules", str(self.rules), "--delimiter", "tab", "--format", "json"]
        first = subprocess.run(args, cwd=self.root, capture_output=True, text=True)
        second = subprocess.run(args, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        self.assertTrue(json.loads(first.stdout)["valid"])
        self.assertEqual(self.snapshot(), before)
        missing = subprocess.run([sys.executable, "-B", str(ROOT / "check_rules.py"), str(self.source)],
                                 cwd=self.root, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(missing.stdout, "")

    def test_generated_header_compatibility_matches_cleanup(self):
        choices = ["id", " id ", "", " ", "status", "批准", "\ufeffid", "line\nname"]
        for seed in range(40):
            rng = random.Random(seed)
            header = [rng.choice(choices) for _ in range(rng.randrange(1, 5))]
            selected = rng.choice(["id", " id ", "status", "批准", "\ufeffid", "line\nname"])
            for trim in (False, True):
                with self.subTest(seed=seed, trim=trim):
                    self.write(header=header, config={"required_nonblank": [selected]})
                    result = check_rules(self.source, self.rules, trim=trim)
                    output = self.root / f"output-{seed}-{trim}"
                    try:
                        clean(self.source, output, trim=trim, rules=self.rules)
                        accepted = True
                    except ValueError:
                        accepted = False
                    self.assertEqual(result["valid"], accepted)

    def test_saved_example_matches_current_report_exactly(self):
        result = check_rules(ROOT / "examples/review-rules/customers.csv",
                             ROOT / "examples/review-rules/rules.json", trim=True)
        demo = ROOT / "examples/review-rules/preflight.json"
        self.assertEqual(demo.read_text(encoding="utf-8"), json.dumps(result, ensure_ascii=True, indent=2) + "\n")
        self.assertEqual((demo.with_suffix(".txt")).read_text(encoding="utf-8"), render_text(result))


if __name__ == "__main__":
    unittest.main()
