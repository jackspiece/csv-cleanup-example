"""Keep report copy correct and warnings ahead of raw CSV download links."""
from dataclasses import asdict
import html
import json
from pathlib import Path
import tempfile
import unittest

from tidy_csv import RulesSummary, Summary, _report, clean


ROOT = Path(__file__).resolve().parents[1]
WARNING = ('Spreadsheet safety: CSV values are not sanitized for formulas. '
           'Import untrusted columns explicitly as text before opening these CSV files in spreadsheet software.')
SOURCE_LINK = ('<p><a href="https://github.com/jackspiece/csv-cleanup-example">'
               'View the source and quick start on GitHub</a></p>\n')


class ReportGuidanceTests(unittest.TestCase):
    def test_row_accounting_uses_singular_only_for_one(self):
        for inputs, duplicates, expected in (
                (0, 0, '0 input rows = 0 ready + 0 duplicates + 0 for review.'),
                (1, 0, '1 input row = 1 ready + 0 duplicates + 0 for review.'),
                (2, 1, '2 input rows = 1 ready + 1 duplicate + 0 for review.'),
                (3, 2, '3 input rows = 1 ready + 2 duplicates + 0 for review.'),
                (1001, 1000, '1,001 input rows = 1 ready + 1,000 duplicates + 0 for review.')):
            with self.subTest(inputs=inputs, duplicates=duplicates):
                summary = Summary('source.csv', 'hash', ['id'], input_rows=inputs,
                                  output_rows=inputs - duplicates, duplicate_rows=duplicates)
                self.assertIn(f'<p class="note">{expected}</p>', _report(summary))

    def test_warning_precedes_downloads_with_and_without_review_rules(self):
        for rules in (False, True):
            with self.subTest(rules=rules):
                summary = Summary('<source>.csv', 'hash', ['id'])
                if rules:
                    summary = RulesSummary(**asdict(summary), review_rules={
                        'source': '<rules>.json', 'sha256': 'rules-hash'})
                report = _report(summary)
                self.assertEqual(report.count(WARNING), 1)
                self.assertIn(html.escape(summary.source), report)
                warning_position = report.index(WARNING)
                self.assertLess(warning_position, report.index('<h2>Files to use</h2>'))
                for filename in ('cleaned.csv', 'review.csv', 'audit.jsonl', 'summary.json'):
                    self.assertLess(warning_position, report.index(f'href="{filename}"'))
                if rules:
                    self.assertIn('not a general validity or safety guarantee', report)
                    self.assertIn('&lt;rules&gt;.json', report)

    def test_saved_example_and_public_demo_reproduce_generated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result'
            clean(ROOT / 'examples/messy_customers.csv', output, trim=True, deduplicate=True)
            generated = {file.name: file.read_bytes() for file in output.iterdir()}
            saved = {file.name: file.read_bytes() for file in (ROOT / 'examples/checked').iterdir()}
            self.assertEqual(generated, saved)
            report = generated['report.html'].decode('utf-8')
            demo = (ROOT / 'docs/index.html').read_text(encoding='utf-8')
            self.assertEqual(demo, report.replace('<footer>', SOURCE_LINK + '<footer>'))
            self.assertEqual(json.loads(generated['summary.json'])['duplicate_rows'], 1)
            for filename, raw in generated.items():
                if filename != 'report.html':
                    self.assertEqual((ROOT / 'docs' / filename).read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
