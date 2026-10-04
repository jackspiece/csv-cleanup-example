"""Deterministic generated cases: no third-party test dependency required."""
import csv
import json
from pathlib import Path
import random
import tempfile
import unittest

from tidy_csv import clean


class GeneratedProperties(unittest.TestCase):
    def test_accounting_exact_values_and_idempotence(self):
        # A fixed seed makes every CI failure locally reproducible. Each seed
        # exercises all four combinations, including deduplication without trim.
        values = ["", "00017", "  padded \t", "José", "空山", "a,b", 'a"b',
                  "one\ntwo", "one\r\ntwo", "=1+1", "+SUM(A1:A2)", "@text", "-12"]
        for seed in range(40):
            rng = random.Random(seed)
            width = rng.randint(1, 5)
            header = [f" column_{i} " for i in range(width)]
            rows = [[rng.choice(values) for _ in range(width)] for _ in range(18)]
            # Always include duplicates, malformed widths and a blank record.
            rows += [rows[0][:], rows[1][:], [], ["short"] * (width - 1),
                     ["extra"] * (width + 1)]
            rng.shuffle(rows)
            delimiter = rng.choice([",", ";", "\t"])
            for trim in (False, True):
                for deduplicate in (False, True):
                    with self.subTest(seed=seed, trim=trim, deduplicate=deduplicate):
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            source, first, second = root / "in.csv", root / "first", root / "second"
                            with source.open("w", encoding="utf-8", newline="") as stream:
                                csv.writer(stream, delimiter=delimiter).writerows([header, *rows])
                            original = source.read_bytes()
                            result = clean(source, first, trim=trim, deduplicate=deduplicate,
                                           delimiter=delimiter)
                            expected = []
                            seen = {}
                            reviewed = []
                            removed = []
                            for record, row in enumerate(rows, 2):
                                if len(row) != width:
                                    reviewed.append((record, row))
                                    continue
                                transformed = [cell.strip() for cell in row] if trim else row
                                key = tuple(transformed)
                                if deduplicate and key in seen:
                                    removed.append((record, seen[key]))
                                else:
                                    seen.setdefault(key, record)
                                    expected.append(transformed)
                            expected_header = [cell.strip() for cell in header] if trim else header
                            with (first / "cleaned.csv").open(encoding="utf-8", newline="") as stream:
                                self.assertEqual(list(csv.reader(stream)), [expected_header, *expected])
                            with (first / "review.csv").open(encoding="utf-8", newline="") as stream:
                                actual_review = [(int(row["source_record"]), json.loads(row["original_cells_json"]))
                                                 for row in csv.DictReader(stream)]
                            self.assertEqual(actual_review, reviewed)
                            events = [json.loads(line) for line in (first / "audit.jsonl").read_text().splitlines()]
                            actual_removed = [(event["source_record"], event["kept_source_record"])
                                              for event in events if event["action"] == "duplicate_removed"]
                            self.assertEqual(actual_removed, removed)
                            self.assertEqual(result.input_rows, len(rows))
                            self.assertEqual(result.output_rows, len(expected))
                            self.assertEqual(result.review_rows, len(reviewed))
                            self.assertEqual(result.duplicate_rows, len(removed))
                            self.assertEqual(result.input_rows, result.output_rows + result.review_rows + result.duplicate_rows)
                            self.assertEqual(source.read_bytes(), original)

                            again = clean(first / "cleaned.csv", second, trim=trim, deduplicate=deduplicate)
                            # Idempotence applies to the cleaned data, not to the
                            # audit/report whose source and counts intentionally change.
                            self.assertEqual((first / "cleaned.csv").read_bytes(), (second / "cleaned.csv").read_bytes())
                            self.assertEqual((again.changed_cells, again.review_rows, again.duplicate_rows), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
