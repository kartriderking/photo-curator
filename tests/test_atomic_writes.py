"""Regression coverage for Windows readers blocking task progress/report writes."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pipeline import write_report


def sharing_error():
    error = PermissionError('file is being read')
    error.winerror = 5
    return error


class AtomicWritesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'results.csv'
        write_report(pd.DataFrame([{'done': 1}]), self.root)

    def write_next(self):
        write_report(pd.DataFrame([{'done': 2}]), self.root)

    def read_value(self):
        return pd.read_csv(self.path)['done'].tolist()

    def test_transient_reader_preserves_old_export_until_replaced(self):
        real_replace = os.replace
        calls = []

        def replace(src, dst):
            calls.append(1)
            if len(calls) < 3:
                self.assertEqual(self.read_value(), [1])
                raise sharing_error()
            return real_replace(src, dst)

        with patch('pipeline.os.replace', side_effect=replace), patch('pipeline.time.sleep'):
            self.write_next()
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.read_value(), [2])
        self.assertEqual(list(self.root.glob('*.tmp.csv')), [])

    def test_persistent_lock_is_bounded_and_keeps_previous_export(self):
        with patch('pipeline.os.replace', side_effect=sharing_error()) as replace, patch('pipeline.time.sleep'):
            with self.assertRaises(PermissionError):
                self.write_next()
        self.assertEqual(replace.call_count, 9)
        self.assertEqual(self.read_value(), [1])
        self.assertEqual(list(self.root.glob('*.tmp.csv')), [])

    def test_unrelated_io_error_is_not_retried(self):
        with patch('pipeline.os.replace', side_effect=OSError('disk error')) as replace, patch('pipeline.time.sleep') as sleep:
            with self.assertRaises(OSError):
                self.write_next()
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.read_value(), [1])

    def test_csv_report_retries_reader_lock(self):
        import pandas as pd
        real_replace = os.replace
        def attempt(src, dst):
            if attempt.first:
                attempt.first = False
                raise sharing_error()
            return real_replace(src, dst)
        attempt.first = True
        with patch('pipeline.os.replace', side_effect=attempt), patch('pipeline.time.sleep'):
            write_report(pd.DataFrame([{'done': 2}]), self.root)
        self.assertEqual(pd.read_csv(self.root / 'results.csv')['done'].tolist(), [2])
        self.assertEqual(list(self.root.glob('*.tmp.csv')), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing behavior')
    def test_real_windows_reader_lock(self):
        ready = threading.Event()
        def reader():
            with self.path.open('rb'):
                ready.set()
                threading.Event().wait(0.15)
        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(ready.wait(2))
        try:
            self.write_next()
        finally:
            thread.join()
        self.assertEqual(self.read_value(), [2])


if __name__ == '__main__':
    unittest.main()
