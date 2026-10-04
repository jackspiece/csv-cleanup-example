"""The published source digest must describe exactly the bytes parsed."""
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest import mock

import tidy_csv
from tidy_csv import InputError, clean


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.csv"
        self.output = self.root / "result"
        self.before = b"id,name\n0001,Ada\n"
        self.after = b"id,name\n0002,Bea\n"
        self.source.write_bytes(self.before)

    def assert_fingerprint(self, result, captured):
        digest = hashlib.sha256(captured).hexdigest()
        self.assertEqual(result.source_sha256, digest)
        saved = json.loads((self.output / "summary.json").read_text())
        self.assertEqual(saved["source_sha256"], digest)
        self.assertIn(digest, (self.output / "report.html").read_text())
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["audit.jsonl", "cleaned.csv", "report.html", "review.csv", "summary.json"])
        self.assertFalse(list(self.root.glob(".csv-tidy-*")))

    def assert_no_output(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".csv-tidy-*")))

    def at_parse_boundary(self, callback):
        original_reader = csv.reader

        def reader(*args, **kwargs):
            callback()
            return original_reader(*args, **kwargs)

        return mock.patch.object(tidy_csv.csv, "reader", side_effect=reader)

    def test_rewrite_between_fingerprinting_and_parsing_cannot_mislabel_rows(self):
        # On the former two-pass implementation this returns Bea's rows with
        # Ada's digest. Hook a real public parser boundary, without sleeps.
        with self.at_parse_boundary(lambda: self.source.write_bytes(self.after)):
            result = clean(self.source, self.output)
        cleaned = (self.output / "cleaned.csv").read_bytes()
        observed = self.before if b"Ada" in cleaned else self.after
        self.assert_fingerprint(result, observed)
        self.assertEqual(cleaned, b"id,name\r\n0001,Ada\r\n")
        self.assertEqual(self.source.read_bytes(), self.after)

    def test_path_replacement_after_capture_uses_the_captured_version(self):
        replacement = self.root / "replacement.csv"
        replacement.write_bytes(self.after)
        with self.at_parse_boundary(lambda: replacement.replace(self.source)):
            result = clean(self.source, self.output)
        self.assert_fingerprint(result, self.before)
        self.assertEqual((self.output / "cleaned.csv").read_bytes(),
                         b"id,name\r\n0001,Ada\r\n")
        self.assertEqual(self.source.read_bytes(), self.after)

    def test_source_is_opened_once_in_binary_read_mode(self):
        original_open = Path.open
        modes = []

        def opened(path, *args, **kwargs):
            if path == self.source:
                modes.append(args[0] if args else kwargs.get("mode", "r"))
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=opened):
            result = clean(self.source, self.output)
        self.assertEqual(modes, ["rb"])
        self.assert_fingerprint(result, self.before)

    def source_read_hook(self, callback):
        original_open = Path.open

        class Reader:
            def __init__(self, raw):
                self.raw = raw
                self.triggered = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.raw.__exit__(*args)

            def fileno(self):
                return self.raw.fileno()

            def readinto(self, buffer):
                size = self.raw.readinto(buffer)
                if not self.triggered and size:
                    self.triggered = True
                    callback(size)
                return size

        def opened(path, *args, **kwargs):
            raw = original_open(path, *args, **kwargs)
            if path == self.source and args == ("rb",):
                return Reader(raw)
            return raw

        return mock.patch.object(Path, "open", new=opened)

    def test_same_length_mid_capture_rewrite_is_rejected_without_output(self):
        def change(_):
            self.source.write_bytes(self.after)
            # Ensure a deterministic metadata difference even on coarse clocks.
            stat = self.source.stat()
            os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        with self.source_read_hook(change):
            with self.assertRaisesRegex(InputError, "Source changed during capture"):
                clean(self.source, self.output)
        self.assert_no_output()
        self.assertEqual(self.source.read_bytes(), self.after)

    def test_mid_capture_path_replacement_rejects_or_keeps_opened_version(self):
        replacement = self.root / "replacement.csv"
        replacement.write_bytes(self.after)
        with self.source_read_hook(lambda _: replacement.replace(self.source)):
            try:
                result = clean(self.source, self.output)
            except InputError as exc:
                self.assertIn("Source changed during capture", str(exc))
                self.assert_no_output()
            else:
                # File unlink timestamps vary by filesystem. Either outcome
                # is valid; reopening and mislabeling the replacement is not.
                self.assert_fingerprint(result, self.before)
                self.assertIn(b"Ada", (self.output / "cleaned.csv").read_bytes())
        self.assertEqual(self.source.read_bytes(), self.after)

    def test_hidden_mid_capture_change_still_hashes_exact_captured_stream(self):
        # Simulate a writer escaping the advisory metadata check. A copy is
        # not an atomic filesystem snapshot, but its digest must remain exact.
        before = b"id,name\n" + b"0001,AAA\n" * 240_000
        after = before.replace(b"AAA", b"BBB")
        self.source.write_bytes(before)
        unchanged_stat = self.source.stat()
        boundary = []

        def change(size):
            boundary.append(size)
            self.source.write_bytes(after)

        with self.source_read_hook(change), \
             mock.patch.object(tidy_csv.os, "fstat", return_value=unchanged_stat):
            result = clean(self.source, self.output)
        captured = before[:boundary[0]] + after[boundary[0]:]
        self.assertGreater(boundary[0], 0)
        self.assertLess(boundary[0], len(before))
        self.assertNotEqual(captured, before)
        self.assertNotEqual(captured, after)
        self.assert_fingerprint(result, captured)
        # No quotes or embedded newlines: normalized output is an independent
        # complete-byte check of the captured stream, not just its row counts.
        self.assertEqual((self.output / "cleaned.csv").read_bytes(),
                         captured.replace(b"\n", b"\r\n"))
        self.assertEqual(self.source.read_bytes(), after)

    def test_raw_bom_crlf_delimiter_and_trim_keep_exact_source_fingerprint(self):
        raw = b'\xef\xbb\xbf id ;note\r\n0001;" a;b\r\nnext "\r\n'
        self.source.write_bytes(raw)
        result = clean(self.source, self.output, delimiter=";", trim=True)
        self.assert_fingerprint(result, raw)
        self.assertEqual((self.output / "cleaned.csv").read_bytes(),
                         b'id,note\r\n0001,"a;b\r\nnext"\r\n')
        self.assertEqual(self.source.read_bytes(), raw)

    def test_capture_read_failure_cleans_staging(self):
        def fail(_):
            raise OSError("synthetic read failure")

        with self.source_read_hook(fail):
            with self.assertRaisesRegex(OSError, "synthetic read failure"):
                clean(self.source, self.output)
        self.assert_no_output()
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_capture_write_failure_closes_snapshot_and_cleans_staging(self):
        original_temporary_file = tidy_csv.tempfile.TemporaryFile
        opened = []

        def temporary_file(*args, **kwargs):
            raw = original_temporary_file(*args, **kwargs)
            opened.append(raw)
            wrapper = mock.MagicMock(wraps=raw)
            wrapper.__enter__.return_value = wrapper
            wrapper.__exit__.side_effect = raw.__exit__
            wrapper.write.side_effect = OSError("synthetic disk full")
            return wrapper

        with mock.patch.object(tidy_csv.tempfile, "TemporaryFile", side_effect=temporary_file):
            with self.assertRaisesRegex(OSError, "synthetic disk full"):
                clean(self.source, self.output)
        self.assertTrue(opened[0].closed)
        self.assert_no_output()
        self.assertEqual(self.source.read_bytes(), self.before)

    def test_parse_failure_closes_snapshot_and_cleans_staging(self):
        self.source.write_bytes(b'id,name\n1,"unterminated\n')
        original_temporary_file = tidy_csv.tempfile.TemporaryFile
        opened = []

        def temporary_file(*args, **kwargs):
            raw = original_temporary_file(*args, **kwargs)
            opened.append(raw)
            return raw

        with mock.patch.object(tidy_csv.tempfile, "TemporaryFile", side_effect=temporary_file):
            with self.assertRaises(InputError):
                clean(self.source, self.output)
        self.assertTrue(opened[0].closed)
        self.assert_no_output()

    def test_existing_output_is_rejected_before_snapshot_creation(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_bytes(b"keep")
        with mock.patch.object(tidy_csv.tempfile, "TemporaryFile") as temporary_file:
            with self.assertRaises(FileExistsError):
                clean(self.source, self.output)
        temporary_file.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertFalse(list(self.root.glob(".csv-tidy-*")))

    def test_capture_memory_is_bounded_for_larger_sources(self):
        # Exercise capture alone so row parsing and optional deduplication
        # memory cannot mask whether the copy itself retains the input.
        peaks = []
        for mebibytes in (2, 16):
            with self.source.open("wb") as target:
                for _ in range(mebibytes):
                    target.write(b"x" * (1024 * 1024))
            tracemalloc.start()
            try:
                with tidy_csv._source_snapshot(self.source, self.root) as (stream, digest):
                    self.assertEqual(stream.read(1), "x")
                    self.assertEqual(len(digest), 64)
                    peaks.append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
        self.assertLess(max(peaks), 3 * 1024 * 1024)
        self.assertLess(abs(peaks[1] - peaks[0]), 512 * 1024)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["source.csv"])


if __name__ == "__main__":
    unittest.main()
