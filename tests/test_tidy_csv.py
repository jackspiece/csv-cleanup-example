import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tidy_csv import InputError, clean


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.csv"
        self.output = self.root / "result"

    def write_rows(self, rows):
        with self.source.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)

    def output_rows(self):
        with (self.output / "cleaned.csv").open(encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def test_defaults_preserve_spaces_duplicates_and_leading_zeroes(self):
        rows = [["id", "name"], ["007", " space "], ["007", " space "]]
        self.write_rows(rows)
        before = self.source.read_bytes()
        result = clean(self.source, self.output)
        self.assertEqual(self.output_rows(), rows)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(result.source_sha256, hashlib.sha256(before).hexdigest())
        self.assertEqual((result.input_rows, result.output_rows, result.changed_cells), (2, 2, 0))

    def test_opted_in_changes_have_complete_row_accounting(self):
        self.write_rows([
            [" id ", "name", "amount", "note"],
            ["0001", " Alice ", "1,204.50", "first"],
            ["0001", "Alice", "1,204.50", "first"],
            ["0002", "José", "000.00", "line one\nline two"],
            ["0003", "short"],
            ["0004", "extra", "10", "fine", "unexpected"],
            ["0005", "空山", "£39.00", "quoted, note"],
        ])
        result = clean(self.source, self.output, trim=True, deduplicate=True)
        self.assertEqual(self.output_rows(), [
            ["id", "name", "amount", "note"],
            ["0001", "Alice", "1,204.50", "first"],
            ["0002", "José", "000.00", "line one\nline two"],
            ["0005", "空山", "£39.00", "quoted, note"],
        ])
        self.assertEqual((result.input_rows, result.output_rows, result.duplicate_rows, result.review_rows), (6, 3, 1, 2))
        with (self.output / "review.csv").open(encoding="utf-8", newline="") as f:
            reviewed = list(csv.DictReader(f))
        self.assertEqual(json.loads(reviewed[0]["original_cells_json"]), ["0003", "short"])
        self.assertEqual(json.loads(reviewed[1]["original_cells_json"])[-1], "unexpected")
        events = [json.loads(line) for line in (self.output / "audit.jsonl").read_text().splitlines()]
        removed = [event for event in events if event["action"] == "duplicate_removed"]
        self.assertEqual(removed, [{"source_record": 3, "action": "duplicate_removed", "kept_source_record": 2}])
        self.assertEqual([row["source_record"] for row in reviewed], ["5", "6"])
        self.assertEqual(json.loads((self.output / "summary.json").read_text())["input_rows"], 6)

    def test_malformed_late_record_publishes_no_partial_output(self):
        self.source.write_text('id,name\n1,valid\n2,"unterminated\n', encoding="utf-8")
        before = self.source.read_bytes()
        with self.assertRaises(InputError):
            clean(self.source, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source.csv"])

    def test_invalid_encoding_publishes_no_output(self):
        self.source.write_bytes(b"id,name\n1,ok\n2,\xff\n")
        with self.assertRaises(InputError):
            clean(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_header_collision_after_trimming_is_rejected(self):
        self.source.write_text(" id,id \n1,2\n", encoding="utf-8")
        with self.assertRaisesRegex(InputError, "duplicated"):
            clean(self.source, self.output, trim=True)
        self.assertFalse(self.output.exists())

    def test_existing_folder_and_its_contents_are_preserved(self):
        self.write_rows([["id"], ["0001"]])
        self.output.mkdir()
        sentinel = self.output / "cleaned.csv"
        sentinel.write_text("keep this\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            clean(self.source, self.output)
        self.assertEqual(sentinel.read_text(), "keep this\n")

    def test_header_only_file_is_valid(self):
        self.write_rows([["id", "value"]])
        result = clean(self.source, self.output)
        self.assertEqual(result.input_rows, 0)
        self.assertEqual(self.output_rows(), [["id", "value"]])

    def test_semicolon_bom_and_quoted_crlf_are_preserved(self):
        self.source.write_bytes(b'\xef\xbb\xbfitem;note\r\n0004;"a;b\r\nnext"\r\n')
        clean(self.source, self.output, delimiter=";")
        self.assertEqual(self.output_rows(), [["item", "note"], ["0004", "a;b\r\nnext"]])

    def test_blank_record_goes_to_review_without_discarding_it(self):
        self.source.write_text("id,name\n1,one\n\n2,two\n", encoding="utf-8")
        result = clean(self.source, self.output)
        self.assertEqual((result.input_rows, result.output_rows, result.review_rows), (3, 2, 1))

    def test_empty_source_is_rejected(self):
        self.source.write_bytes(b"")
        with self.assertRaises(InputError):
            clean(self.source, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
