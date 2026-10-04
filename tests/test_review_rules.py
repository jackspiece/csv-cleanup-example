import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import unittest

from review_rules import MAX_RULE_BYTES, RulesError, load_rules
from tidy_csv import InputError, clean, main


FIXTURE_SOURCE = {
    'ordinary': b'id,note\r\n0001," x, y "\r\n0001,"x, y"\r\n0002,"a\r\nb"\r\nshort\r\n\r\n',
    'signature': b'\xef\xbb\xbf id ;note\n0001; ready \n0001;ready\nshort\n',
    'literal': '"\ufeff id ",note\n0001, x \n0001,x\nshort\n'.encode('utf-8'),
}


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.csv'
        self.output = self.root / 'result'
        self.rules = self.root / 'rules.json'

    def write(self, rows, config=None, delimiter=','):
        with self.source.open('w', encoding='utf-8', newline='') as source:
            csv.writer(source, delimiter=delimiter).writerows(rows)
        self.rules.write_text(json.dumps(config or {
            'required_nonblank': ['id'], 'allowed_values': {'status': ['ready', '批准', '001']}
        }, ensure_ascii=False), encoding='utf-8')

    def read_csv(self, name):
        with (self.output / name).open(encoding='utf-8', newline='') as source:
            return list(csv.reader(source))

    def audit(self):
        return [json.loads(line) for line in (self.output / 'audit.jsonl').read_text().splitlines()]

    def test_rules_apply_before_deduplication_and_preserve_originals(self):
        self.write([
            [' id ', 'status', 'note'],
            ['0001', ' ready ', ' first '],
            ['0001', 'ready', 'first'],
            [' \u2003 ', 'missing', ' original '],
            [' \u2003 ', 'missing', ' original '],
            ['0002', '批准', 'line one\nline two'],
            ['short'],
            ['0003', '001', '01'],
            ['0004', '1', 'bad'],
        ])
        source_before, rules_before = self.source.read_bytes(), self.rules.read_bytes()
        summary = clean(self.source, self.output, trim=True, deduplicate=True, rules=self.rules)
        self.assertEqual((summary.input_rows, summary.output_rows, summary.review_rows,
                          summary.duplicate_rows, summary.changed_cells), (8, 3, 4, 1, 6))
        self.assertEqual(self.read_csv('cleaned.csv'), [
            ['id', 'status', 'note'], ['0001', 'ready', 'first'],
            ['0002', '批准', 'line one\nline two'], ['0003', '001', '01']])
        reviewed = self.read_csv('review.csv')
        self.assertEqual(reviewed[0], ['source_record', 'reason', 'original_cells_json', 'rules_sha256'])
        self.assertEqual([row[0] for row in reviewed[1:]], ['4', '5', '7', '9'])
        self.assertEqual(json.loads(reviewed[1][2]), [' \u2003 ', 'missing', ' original '])
        self.assertIn('Required nonblank column "id" is blank', reviewed[1][1])
        self.assertIn('Column "status" is not an allowed exact value', reviewed[1][1])
        self.assertNotIn('missing', reviewed[1][1])
        fingerprint = hashlib.sha256(rules_before).hexdigest()
        self.assertTrue(all(row[3] == fingerprint for row in reviewed[1:]))
        events = self.audit()
        self.assertTrue(all(event['rules_sha256'] == fingerprint for event in events))
        self.assertEqual(events[0]['action'], 'review_rules_applied')
        failures = [event for event in events if event['action'] == 'quarantined']
        self.assertEqual(failures[0]['rule_failures'], [
            {'code': 'required_nonblank', 'column': 'id', 'column_index': 1},
            {'code': 'not_allowed_value', 'column': 'status', 'column_index': 2}])
        self.assertEqual(failures[2]['reason'], 'Expected 3 cells, found 1')
        self.assertNotIn('rule_failures', failures[2])
        duplicates = [event for event in events if event['action'] == 'duplicate_removed']
        self.assertEqual([(event['source_record'], event['kept_source_record']) for event in duplicates], [(3, 2)])
        saved = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(saved['review_rules'], {
            'source': 'rules.json', 'sha256': fingerprint, 'required_nonblank': ['id'],
            'allowed_value_columns': ['status']})
        self.assertIn(fingerprint, (self.output / 'report.html').read_text())
        self.assertEqual((self.source.read_bytes(), self.rules.read_bytes()), (source_before, rules_before))

    def test_blank_semantics_include_unicode_whitespace_without_trimming(self):
        rows = [['id']] + [[value] for value in ['', ' ', '\t', '\u00a0\u2003', '\u200b', '\ufeff', '000']]
        for trim in (False, True):
            with self.subTest(trim=trim):
                self.write(rows, {'required_nonblank': ['id']})
                self.output = self.root / f'result-{trim}'
                summary = clean(self.source, self.output, trim=trim, rules=self.rules)
                self.assertEqual((summary.output_rows, summary.review_rows), (3, 4))
                self.assertEqual(self.read_csv('cleaned.csv'), [['id'], ['\u200b'], ['\ufeff'], ['000']])
                self.assertEqual([json.loads(row[2]) for row in self.read_csv('review.csv')[1:]], rows[1:5])

    def test_exact_strings_keep_leading_zeroes_case_and_unicode_forms_distinct(self):
        self.write([['status']] + [[value] for value in ['001', '1', 'é', 'e\u0301', '批准', 'Ready', 'ready', '']],
                   {'allowed_values': {'status': ['001', 'é', '批准', 'ready', '']}})
        summary = clean(self.source, self.output, rules=self.rules)
        self.assertEqual((summary.output_rows, summary.review_rows), (5, 3))
        self.assertEqual(self.read_csv('cleaned.csv'), [['status'], ['001'], ['é'], ['批准'], ['ready'], ['']])

    def test_trim_on_off_applies_before_exact_matching_but_never_trims_rule_values(self):
        for trim, allowed, expected in [(False, ['ready'], 0), (True, ['ready'], 1),
                                        (False, [' ready '], 1), (True, [' ready '], 0)]:
            with self.subTest(trim=trim, allowed=allowed):
                self.output = self.root / f'result-{trim}-{expected}'
                self.write([['status'], [' ready ']], {'allowed_values': {'status': allowed}})
                summary = clean(self.source, self.output, trim=trim, rules=self.rules)
                self.assertEqual(summary.output_rows, expected)
                self.assertEqual(summary.changed_cells, int(trim))
                if not expected:
                    self.assertEqual(json.loads(self.read_csv('review.csv')[1][2]), [' ready '])

    def test_same_column_may_have_both_checks_and_two_failures(self):
        self.write([['id'], ['']], {'required_nonblank': ['id'], 'allowed_values': {'id': ['001']}})
        summary = clean(self.source, self.output, rules=self.rules)
        self.assertEqual(summary.review_rows, 1)
        self.assertEqual(len(self.audit()[-1]['rule_failures']), 2)

    def test_header_names_are_exact_after_requested_trimming(self):
        for trim, column, succeeds in [(False, ' id ', True), (False, 'id', False),
                                       (True, 'id', True), (True, ' id ', False)]:
            with self.subTest(trim=trim, column=column):
                self.output = self.root / f'result-{trim}-{succeeds}'
                self.write([[' id '], ['001']], {'required_nonblank': [column]})
                if succeeds:
                    self.assertEqual(clean(self.source, self.output, trim=trim, rules=self.rules).output_rows, 1)
                else:
                    with self.assertRaisesRegex(RulesError, 'missing'):
                        clean(self.source, self.output, trim=trim, rules=self.rules)
                    self.assertFalse(self.output.exists())
                    self.assertFalse(list(self.root.glob('.csv-tidy-*')))

    def test_malformed_records_precede_rules_and_use_record_not_line_numbers(self):
        self.write([['id', 'status'], ['0001', 'line\none'], [''], [], ['', 'bad', 'extra']],
                   {'required_nonblank': ['id'], 'allowed_values': {'status': ['line\none']}})
        summary = clean(self.source, self.output, trim=True, rules=self.rules)
        self.assertEqual((summary.input_rows, summary.output_rows, summary.review_rows), (4, 1, 3))
        reviewed = self.read_csv('review.csv')[1:]
        self.assertEqual([row[0] for row in reviewed], ['3', '4', '5'])
        self.assertTrue(all('Expected 2 cells' in row[1] for row in reviewed))
        self.assertTrue(all('rule_failures' not in event for event in self.audit()))
        self.assertEqual(summary.changed_cells, 0)

    def test_invalid_configuration_fails_before_publishing(self):
        bad = ['[]', 'null', 'true', '{}', '{"unknown": []}',
               '{"required_nonblank": null}', '{"required_nonblank": "id"}',
               '{"required_nonblank": [1]}', '{"required_nonblank": [""]}',
               '{"required_nonblank": [" "]}', '{"required_nonblank": ["id", "id"]}',
               '{"required_nonblank": ["missing"]}', '{"allowed_values": []}',
               '{"allowed_values": {"status": []}}', '{"allowed_values": {"status": "ready"}}',
               '{"allowed_values": {"status": [1]}}', '{"allowed_values": {"status": [true]}}',
               '{"allowed_values": {"status": [1.0]}}', '{"allowed_values": {"status": [1e99999]}}',
               '{"allowed_values": {"status": [' + '9' * 5000 + ']}}',
               '{"allowed_values": {"status": [null]}}', '{"allowed_values": {"status": [NaN]}}',
               '{"allowed_values": {"status": [Infinity]}}', '{"allowed_values": {"status": [[]]}}',
               '{"allowed_values": {" ": ["a"]}}', '{"allowed_values": {"missing": ["a"]}}',
               '{"allowed_values": {"status": ["a", "a"]}}',
               '{"required_nonblank":["id"],"required_nonblank":["status"]}',
               '{"allowed_values":{"status":["a"],"status":["b"]}}',
               '{"required_nonblank":["id"],}', '{"required_nonblank":["id"]} true',
               '{"required_nonblank":["\\ud800"]}', '{"allowed_values":{"status":["\\udfff"]}}',
               '{"required_nonblank":["id"],"allowed_values":{"status":[{}]}}']
        self.write([['id', 'status'], ['001', 'ready']])
        for config in bad:
            with self.subTest(config=config):
                self.rules.write_text(config, encoding='utf-8')
                with self.assertRaises(RulesError):
                    clean(self.source, self.output, rules=self.rules)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.root.glob('.csv-tidy-*')))

    def test_invalid_encoding_oversize_and_deeply_nested_config(self):
        self.write([['id'], ['1']], {'required_nonblank': ['id']})
        for raw in [b'\xff', b' ' * (MAX_RULE_BYTES + 1), b'[' * 2000 + b']' * 2000]:
            with self.subTest(length=len(raw)):
                self.rules.write_bytes(raw)
                with self.assertRaises(RulesError):
                    clean(self.source, self.output, rules=self.rules)
                self.assertFalse(self.output.exists())

    def test_signature_and_literal_bom_rule_columns(self):
        for signature in (False, True):
            self.output = self.root / str(signature)
            name = 'id' if signature else '\ufeffid'
            self.write([[name], ['0001']], {'required_nonblank': [name]})
            # csv.writer does not quote the literal marker. It must be quoted
            # here to distinguish cell data from the optional file signature.
            if not signature:
                self.source.write_text('"\ufeffid"\n0001\n', encoding='utf-8')
            else:
                self.source.write_bytes(b'\xef\xbb\xbf' + self.source.read_bytes())
            self.rules.write_bytes(b'\xef\xbb\xbf' + self.rules.read_bytes())
            summary = clean(self.source, self.output, rules=self.rules)
            self.assertEqual(summary.output_rows, 1)
            self.assertEqual(self.read_csv('cleaned.csv'), [[name], ['0001']])
            second = self.root / f'second-{signature}'
            clean(self.output / 'cleaned.csv', second, rules=self.rules)
            self.assertEqual((self.output / 'cleaned.csv').read_bytes(), (second / 'cleaned.csv').read_bytes())

    def test_duplicate_headers_fail_with_rules(self):
        for header, trim in [(['id', 'id'], False), ([' id', 'id '], True)]:
            self.write([header, ['1', '2']], {'required_nonblank': ['id']})
            with self.assertRaisesRegex(InputError, 'duplicated'):
                clean(self.source, self.output, trim=trim, rules=self.rules)
            self.assertFalse(self.output.exists())

    def test_late_parse_failure_does_not_publish_prior_rule_results(self):
        for suffix in [b'2,"unterminated\n', b'2,\xff\n']:
            self.write([['id', 'status'], ['', 'bad'], ['001', 'ready']])
            self.source.write_bytes(self.source.read_bytes() + suffix)
            with self.assertRaises(InputError):
                clean(self.source, self.output, rules=self.rules)
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob('.csv-tidy-*')))

    def test_header_only_has_rules_fingerprint_in_summary_and_audit(self):
        self.write([['id', 'status']])
        summary = clean(self.source, self.output, rules=self.rules)
        self.assertEqual(summary.input_rows, 0)
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(self.audit()[0]['rules_sha256'], summary.review_rules['sha256'])

    def test_fingerprint_identifies_exact_rule_bytes(self):
        self.rules.write_text('{"required_nonblank":["id"]}', encoding='utf-8')
        first = load_rules(self.rules)
        self.rules.write_text('{ "required_nonblank": ["id"] }\n', encoding='utf-8')
        second = load_rules(self.rules)
        self.assertEqual(first.required_nonblank, second.required_nonblank)
        self.assertNotEqual(first.sha256, second.sha256)

    def test_existing_output_rules_and_source_stay_unchanged(self):
        self.write([['id', 'status'], ['001', 'ready']])
        self.output.mkdir()
        (self.output / 'sentinel').write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            clean(self.source, self.output, rules=self.rules)
        self.assertEqual((self.output / 'sentinel').read_bytes(), b'keep')

    def test_missing_rules_file_and_directory_are_rejected(self):
        self.write([['id', 'status']])
        for path in [self.root / 'absent.json', self.root]:
            with self.assertRaisesRegex(RulesError, 'file'):
                clean(self.source, self.output, rules=path)
            self.assertFalse(self.output.exists())

    def test_rule_filename_is_escaped_in_report(self):
        self.write([['id']], {'required_nonblank': ['id']})
        hostile = self.root / '<img src=x onerror=alert(1)>.json'
        self.rules.rename(hostile)
        clean(self.source, self.output, rules=hostile)
        report = (self.output / 'report.html').read_text()
        self.assertNotIn('<img src=x', report)
        self.assertIn('&lt;img src=x', report)

    def test_cli_rules_and_invalid_configuration_exit(self):
        self.write([['id', 'status'], ['0001', 'ready'], ['', 'bad']])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), str(self.output), '--rules', str(self.rules), '--trim', '--deduplicate'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['review_rows'], 1)
        self.rules.write_text('{"unknown": []}', encoding='utf-8')
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), str(self.root / 'bad-output'), '--rules', str(self.rules)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('unknown key', stderr.getvalue())
        self.assertFalse((self.root / 'bad-output').exists())

    def test_no_rules_matches_frozen_baseline_bytes_for_all_five_outputs(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'no_rules_sha256.json').read_text())
        for name, raw in FIXTURE_SOURCE.items():
            self.source.write_bytes(raw)
            for trim in (False, True):
                for deduplicate in (False, True):
                    key = f'{name}-{int(trim)}-{int(deduplicate)}'
                    self.output = self.root / key
                    clean(self.source, self.output, trim=trim, deduplicate=deduplicate,
                          delimiter=';' if name == 'signature' else ',', rules=None)
                    actual = {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
                              for file in self.output.iterdir()}
                    self.assertEqual(actual, fixture['outputs'][key], key)
                    self.assertEqual(self.source.read_bytes(), raw)

    def test_fictional_saved_example_reproduces_all_outputs(self):
        fixture = Path(__file__).resolve().parents[1] / 'examples' / 'review-rules'
        summary = clean(fixture / 'customers.csv', self.output, trim=True,
                        deduplicate=True, rules=fixture / 'rules.json')
        self.assertEqual((summary.input_rows, summary.output_rows, summary.review_rows,
                          summary.duplicate_rows), (8, 3, 4, 1))
        expected = {file.name: file.read_bytes() for file in (fixture / 'checked').iterdir()}
        self.assertEqual({file.name: file.read_bytes() for file in self.output.iterdir()}, expected)

    def test_generated_conservation_precedence_duplicates_and_idempotence(self):
        for seed in range(40):
            rng = random.Random(seed)
            delimiter = [',', ';', '\t'][seed % 3]
            rows = []
            for _ in range(35):
                if rows and rng.random() < .2:
                    rows.append(list(rng.choice(rows)))
                elif rng.random() < .15:
                    rows.append(['malformed'] * rng.choice([0, 1, 3]))
                else:
                    rows.append([rng.choice(['', ' ', '\u2003', '001', 'é', ' 002 ']),
                                 rng.choice(['ready', ' ready ', '批准', 'bad', '', '01'])])
            for trim in (False, True):
                for deduplicate in (False, True):
                    with self.subTest(seed=seed, trim=trim, deduplicate=deduplicate):
                        self.output = self.root / f'generated-{seed}-{trim}-{deduplicate}'
                        self.write([['id', 'status']] + rows, delimiter=delimiter)
                        expected_ready, expected_review, expected_duplicates, first = [], [], [], {}
                        for record, row in enumerate(rows, 2):
                            if len(row) != 2:
                                expected_review.append((record, row))
                                continue
                            changed = [value.strip() for value in row] if trim else row[:]
                            if not changed[0].strip() or changed[1] not in {'ready', '批准', '001'}:
                                expected_review.append((record, row))
                                continue
                            key = tuple(changed)
                            if deduplicate and key in first:
                                expected_duplicates.append((record, first[key]))
                            else:
                                first[key] = record
                                expected_ready.append(changed)
                        summary = clean(self.source, self.output, trim=trim, deduplicate=deduplicate,
                                        delimiter=delimiter, rules=self.rules)
                        self.assertEqual(self.read_csv('cleaned.csv'), [['id', 'status']] + expected_ready)
                        self.assertEqual([(int(row[0]), json.loads(row[2])) for row in self.read_csv('review.csv')[1:]], expected_review)
                        self.assertEqual([(event['source_record'], event['kept_source_record'])
                                          for event in self.audit() if event['action'] == 'duplicate_removed'], expected_duplicates)
                        self.assertEqual(summary.input_rows, len(expected_ready) + len(expected_review) + len(expected_duplicates))
                        self.assertEqual(summary.input_rows, summary.output_rows + summary.review_rows + summary.duplicate_rows)
                        next_output = self.root / f'reclean-{seed}-{trim}-{deduplicate}'
                        reclean = clean(self.output / 'cleaned.csv', next_output, trim=trim,
                                        deduplicate=deduplicate, rules=self.rules)
                        self.assertEqual((reclean.review_rows, reclean.duplicate_rows, reclean.changed_cells), (0, 0, 0))
                        self.assertEqual((self.output / 'cleaned.csv').read_bytes(), (next_output / 'cleaned.csv').read_bytes())


if __name__ == '__main__':
    unittest.main()
