"""Distinguish a UTF-8 file signature from literal U+FEFF field data."""
import codecs
import csv
import io
from pathlib import Path
import tempfile
import unittest

from tidy_csv import clean


class BomHeaderTests(unittest.TestCase):
    def assert_repeated_cleanup(self, source, root, expected, *, trim, deduplicate,
                                delimiter=","):
        previous_bytes = None
        for run in range(3):
            output = root / f"result-{run}"
            result = clean(source, output, trim=trim, deduplicate=deduplicate,
                           delimiter=delimiter if run == 0 else ",")
            cleaned = output / "cleaned.csv"
            raw = cleaned.read_bytes()
            self.assertEqual(result.columns, expected[0])
            # Check both readers: output has no file signature, and a literal
            # U+FEFF must survive even when the reader supports signatures.
            for encoding in ("utf-8", "utf-8-sig"):
                with cleaned.open(encoding=encoding, newline="") as stream:
                    self.assertEqual(list(csv.reader(stream)), expected)
            self.assertFalse(raw.startswith(codecs.BOM_UTF8))
            if run:
                self.assertEqual(raw, previous_bytes)
                self.assertEqual((result.changed_cells, result.review_rows,
                                  result.duplicate_rows), (0, 0, 0))
                self.assertEqual((output / "audit.jsonl").read_bytes(), b"")
            previous_bytes, source = raw, cleaned
        return previous_bytes

    def test_literal_bom_headers_keep_values_and_idempotence(self):
        headers = [
            ["\ufeffid", "name"],
            ["\ufeffid", "id"],  # Must not become a duplicate column.
            ["\ufeff", "name"],  # Must not become an empty column.
            ["\ufeff\ufeffid", "name"],
            [" \ufeffid ", "name"],  # Trimming exposes a leading U+FEFF.
            ["\ufeffid", "na,me"],
            ['\ufeffi"d', "name"],
            ["\ufeffid\r\nnext", "name"],
        ]
        rows = [["0007", " Ada "], ["0007", " Ada "], ["\ufeff0008", "\ufeffRae"]]
        for header in headers:
            for signature in (False, True):
                for delimiter in (",", ";", "\t"):
                    for trim in (False, True):
                        for deduplicate in (False, True):
                            with self.subTest(header=header, signature=signature,
                                              delimiter=delimiter, trim=trim,
                                              deduplicate=deduplicate):
                                with tempfile.TemporaryDirectory() as directory:
                                    root = Path(directory)
                                    source = root / "source.csv"
                                    encoding = "utf-8-sig" if signature else "utf-8"
                                    with source.open("w", encoding=encoding, newline="") as stream:
                                        csv.writer(stream, delimiter=delimiter,
                                                   quoting=csv.QUOTE_ALL).writerows([header, *rows])
                                    original = source.read_bytes()
                                    expected_header = [cell.strip() for cell in header] if trim else header
                                    expected_rows = [rows[0], rows[2]] if deduplicate else rows
                                    if trim:
                                        expected_rows = [[cell.strip() for cell in row] for row in expected_rows]
                                    self.assert_repeated_cleanup(
                                        source, root, [expected_header, *expected_rows],
                                        trim=trim, deduplicate=deduplicate, delimiter=delimiter)
                                    self.assertEqual(source.read_bytes(), original)

    def test_header_only_literal_bom_is_stable(self):
        for trim in (False, True):
            for deduplicate in (False, True):
                with self.subTest(trim=trim, deduplicate=deduplicate):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        source = root / "source.csv"
                        source.write_bytes(b'"\xef\xbb\xbf"\r\n')
                        self.assert_repeated_cleanup(source, root, [["\ufeff"]],
                                                     trim=trim, deduplicate=deduplicate)

    def test_normal_headers_keep_minimal_quoting_and_file_bom_support(self):
        headers = [["id", "name"], [" id ", " name "], ["i,d", "name"],
                   ['i"d', "name"], ["id\r\nnext", "name"], ["id", "\ufeffname"],
                   ["id\ufeff", "name"], ["José", "空山"]]
        for header in headers:
            for signature in (False, True):
                for trim in (False, True):
                    for deduplicate in (False, True):
                        with self.subTest(header=header, signature=signature,
                                          trim=trim, deduplicate=deduplicate):
                            with tempfile.TemporaryDirectory() as directory:
                                root = Path(directory)
                                source = root / "source.csv"
                                encoding = "utf-8-sig" if signature else "utf-8"
                                with source.open("w", encoding=encoding, newline="") as stream:
                                    csv.writer(stream).writerows([header, ["0007", "Ada"]])
                                expected_header = [cell.strip() for cell in header] if trim else header
                                expected = [expected_header, ["0007", "Ada"]]
                                minimal = io.StringIO(newline="")
                                csv.writer(minimal).writerows(expected)
                                raw = self.assert_repeated_cleanup(
                                    source, root, expected, trim=trim, deduplicate=deduplicate)
                                self.assertEqual(raw, minimal.getvalue().encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
