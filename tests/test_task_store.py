"""Durability, migration, transaction isolation and web/CLI storage regressions."""
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from task_store import TaskStore
from feature_config import normalize_options
from pipeline import execute, refresh_results


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / 'curator.sqlite3'
        self.task_dir = self.root / 'jobs' / 'sample'
        self.options = normalize_options({'features': ['phash', 'quality']})
        self.snapshot = {'input_dir': str(self.root), 'options': self.options, 'items': [], 'phash_groups': []}
        with TaskStore(path=self.db_path) as store:
            self.job = store.create_job({'id': 'sample', 'status': 'queued', 'input_dir': str(self.root),
                                         'options': self.options}, self.task_dir)

    def frame(self, rel='a.jpg', label='保留候选'):
        return pd.DataFrame([{'相对路径': rel, '建议标签': label, '同组最优': '', '清晰度': 1.5}])

    def test_reopen_restores_task_progress_snapshot_and_results(self):
        with TaskStore(path=self.db_path) as store:
            store.save_progress('sample', {'status': 'partial', 'features': {'quality': {'status': 'partial', 'done': 1}}})
            store.save_results(self.task_dir, self.frame(), self.snapshot)
        with TaskStore(path=self.db_path) as store:
            self.assertEqual(store.get_job('sample')['status'], 'partial')
            self.assertEqual(store.progress('sample')['features']['quality']['done'], 1)
            self.assertEqual(store.snapshot(self.task_dir), self.snapshot)
            self.assertEqual(store.results('sample').to_dict('records'), self.frame().to_dict('records'))
        self.assertFalse(self.task_dir.exists())

    def test_failure_rolls_back_snapshot_and_all_result_rows(self):
        with TaskStore(path=self.db_path) as store:
            store.save_results(self.task_dir, self.frame(), self.snapshot)
            original_revision = store.snapshot_version(self.task_dir)[1]
            def interrupted(job_id, out):
                store.conn.execute('DELETE FROM task_results WHERE task_id=?', (job_id,))
                raise RuntimeError('interrupted write')
            with patch.object(store, '_replace_results', side_effect=interrupted), self.assertRaises(RuntimeError):
                store.save_results(self.task_dir, self.frame('b.jpg'), {**self.snapshot, 'items': ['new']})
            self.assertEqual(store.results('sample').iloc[0]['相对路径'], 'a.jpg')
            self.assertEqual(store.snapshot_version(self.task_dir), (self.snapshot, original_revision))

    def test_refresh_cannot_overwrite_a_newer_pipeline_checkpoint(self):
        with TaskStore(path=self.db_path) as store:
            store.save_results(self.task_dir, self.frame(), self.snapshot)
            old_snapshot, revision = store.snapshot_version(self.task_dir)
            store.save_results(self.task_dir, self.frame('b.jpg', '可删-重复'), {**self.snapshot, 'phash_groups': [['a', 'b']]})
            self.assertFalse(store.save_results(self.task_dir, self.frame(), old_snapshot, expected_revision=revision))
            self.assertEqual(store.results('sample').iloc[0]['建议标签'], '可删-重复')

    def test_empty_checkpoint_replaces_previous_results(self):
        with TaskStore(path=self.db_path) as store:
            store.save_results(self.task_dir, self.frame(), self.snapshot)
            store.save_results(self.task_dir, self.frame().iloc[:0], self.snapshot)
            self.assertTrue(store.has_results('sample'))
            self.assertEqual(len(store.results('sample')), 0)
            self.assertEqual(list(store.results('sample').columns), list(self.frame().columns))

    def test_reader_sees_complete_old_checkpoint_while_writer_is_uncommitted(self):
        with TaskStore(path=self.db_path) as writer, TaskStore(path=self.db_path) as reader:
            writer.save_results(self.task_dir, self.frame(), self.snapshot)
            writer.conn.execute('BEGIN IMMEDIATE')
            writer.conn.execute("DELETE FROM task_results WHERE task_id='sample'")
            self.assertEqual(reader.results('sample').iloc[0]['相对路径'], 'a.jpg')
            writer.conn.rollback()

    def test_two_workers_cannot_claim_the_same_queued_task(self):
        barrier = threading.Barrier(2)
        def claim():
            with TaskStore(path=self.db_path) as store:
                barrier.wait(timeout=5)
                return store.claim_next()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_updates_from_separate_connections_preserve_other_fields(self):
        with TaskStore(path=self.db_path) as first, TaskStore(path=self.db_path) as second:
            first.update_job('sample', {'cancel': True})
            second.update_job('sample', {'pid': 123})
            self.assertTrue(first.get_job('sample')['cancel'])
            self.assertEqual(first.get_job('sample')['pid'], 123)

    def test_legacy_import_is_idempotent_and_preserves_newer_sqlite_records(self):
        d = self.root / 'legacy' / 'old'
        d.mkdir(parents=True)
        jobs_file = self.root / 'jobs.json'
        jobs_file.write_text(json.dumps({'jobs': [{'id': 'old', 'input_dir': str(self.root), 'status': 'failed',
                                                   'options': self.options, 'created': '2026-01-01 10:00:00'}]}), encoding='utf-8')
        (d / 'snapshot.json').write_text(json.dumps(self.snapshot), encoding='utf-8')
        (d / 'progress.json').write_text(json.dumps({'status': 'running', 'features': {'phash': {'status': 'running'}}}), encoding='utf-8')
        self.frame().to_csv(d / 'results.csv', index=False)
        with TaskStore(path=self.db_path) as store:
            self.assertEqual(store.migrate_legacy(jobs_file, d.parent), {'tasks': 1})
            self.assertEqual(store.progress('old')['status'], 'failed')
            self.assertEqual(len(store.results('old')), 1)
            store.update_job('old', {'status': 'done'})
            store.save_results(d, self.frame('new.jpg'), self.snapshot)
            self.assertEqual(store.migrate_legacy(jobs_file, d.parent), {'already_imported': True})
            self.assertEqual(store.get_job('old')['status'], 'done')
            self.assertEqual(store.results('old').iloc[0]['相对路径'], 'new.jpg')
        self.assertTrue((d / 'results.csv').exists())

    def test_invalid_legacy_record_can_be_repaired_and_retried_without_half_import(self):
        d = self.root / 'legacy' / 'old'
        d.mkdir(parents=True)
        jobs_file = self.root / 'jobs.json'
        jobs_file.write_text(json.dumps({'jobs': [{'id': 'old', 'status': 'done'}]}), encoding='utf-8')
        (d / 'snapshot.json').write_text('{invalid', encoding='utf-8')
        with TaskStore(path=self.db_path) as store:
            with self.assertRaises(json.JSONDecodeError):
                store.migrate_legacy(jobs_file, d.parent)
            self.assertIsNone(store.get_job('old'))
            (d / 'snapshot.json').write_text(json.dumps(self.snapshot), encoding='utf-8')
            self.assertEqual(store.migrate_legacy(jobs_file, d.parent), {'tasks': 1})

    def test_analysis_refresh_and_web_csv_export_do_not_create_json_or_csv_files(self):
        from PIL import Image
        import webapp
        library = self.root / 'library'
        library.mkdir()
        Image.new('RGB', (80, 60), 'red').save(library / 'a.jpg')
        with TaskStore(path=self.db_path) as store:
            store.update_job('sample', {'input_dir': str(library)})
        execute(library, self.options, self.task_dir, db_path=self.db_path)
        with (patch.object(webapp, 'DB_PATH', self.db_path), patch('pipeline.measure_image', side_effect=AssertionError('decode')),
              patch('pipeline.scan', side_effect=AssertionError('scan')), patch('pipeline.phash_groups', side_effect=AssertionError('group'))):
            response = webapp.job_results('sample', refresh=1)
            self.assertEqual(json.loads(response.body)['total'], 1)
            with patch('pipeline.refresh_results', side_effect=AssertionError('export should read stored rows')):
                csv = webapp.download_report('sample')
            self.assertTrue(csv.body.startswith(b'\xef\xbb\xbf'))
            self.assertIn('a.jpg', csv.body.decode('utf-8-sig'))
            self.assertIn('attachment', csv.headers['content-disposition'])
            self.assertEqual(sum(webapp.labels('sample')['labels'].values()), 1)
            self.assertEqual(webapp.list_jobs()['jobs'][0]['status'], 'done')
        self.assertEqual(list(self.task_dir.glob('*.json')), [])
        self.assertEqual(list(self.task_dir.glob('*.csv')), [])

    def test_web_worker_subprocess_and_cached_retry_use_the_same_database(self):
        from PIL import Image
        import webapp
        library = self.root / 'library'
        library.mkdir()
        Image.new('RGB', (80, 60), 'red').save(library / 'a.jpg')
        with TaskStore(path=self.db_path) as store:
            store.update_job('sample', {'status': 'cancelled'})
        with (patch.object(webapp, 'DB_PATH', self.db_path), patch.object(webapp, 'JOB_DATA_DIR', self.root / 'jobs'),
              patch('webapp._ensure_worker')):
            job = webapp.create_job({'input_dir': str(library), 'features': ['phash', 'quality']})
            webapp._run_loop()
            with TaskStore(path=self.db_path) as store:
                self.assertEqual(store.get_job(job['id'])['status'], 'done')
                self.assertEqual(len(store.results(job['id'])), 1)
            webapp.retry_job(job['id'])
            webapp._run_loop()
            with TaskStore(path=self.db_path) as store:
                self.assertEqual(store.get_job(job['id'])['status'], 'done')
                self.assertEqual(store.progress(job['id'])['features']['phash']['cached'], 1)
            paused = webapp.create_job({'input_dir': str(library), 'features': ['phash']})
            webapp.cancel_job(paused['id'])
            self.assertEqual(webapp._get_job(paused['id'])['status'], 'cancelled')
        self.assertEqual(list((self.root / 'jobs').rglob('*.json')), [])
        self.assertEqual(list((self.root / 'jobs').rglob('*.csv')), [])


if __name__ == '__main__':
    unittest.main()
