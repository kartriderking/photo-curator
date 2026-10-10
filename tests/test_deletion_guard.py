"""Verify keeper protection using temporary fixtures and a mocked Recycle Bin."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import webapp
from cache_db import item_for
from feature_config import normalize_options
from task_store import TaskStore


class DeletionGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.db_path = self.root / 'curator.sqlite3'
        self.items = []
        for name in ('a.jpg', 'b.jpg', 'c.jpg', 'keep-one.jpg', 'keep-two.jpg'):
            p = self.library / name
            p.write_bytes(b'media fixture')
            self.items.append(item_for(p, self.library))
        self.frame = pd.DataFrame([
            {'相对路径': 'a.jpg', '建议标签': '可删-重复', '同组最优': 'keep-one.jpg', '类型': 'photo'},
            {'相对路径': 'b.jpg', '建议标签': '待审-相似', '同组最优': 'keep-one.jpg', '类型': 'photo'},
            {'相对路径': 'c.jpg', '建议标签': '可删-重复', '同组最优': 'keep-two.jpg', '类型': 'photo'},
            {'相对路径': 'keep-one.jpg', '建议标签': '可删-模糊', '同组最优': '', '类型': 'photo'},
            {'相对路径': 'keep-two.jpg', '建议标签': '保留候选', '同组最优': '', '类型': 'photo'},
        ])
        self.snapshot = {'input_dir': str(self.library), 'items': self.items, 'options': normalize_options({'features': ['phash', 'quality']})}
        with TaskStore(path=self.db_path) as store:
            store.create_job({'id': 'sample', 'input_dir': str(self.library), 'status': 'done'}, self.root / 'job')
            store.save_results(self.root / 'job', self.frame, self.snapshot)
            self.revision = store.results('sample', include_revision=True)[1]
        self.addCleanup(patch.stopall)
        patch.object(webapp, 'DB_PATH', self.db_path).start()
        self.trash = patch('send2trash.send2trash').start()

    def delete(self, names, **body):
        return webapp.delete_files({'job_id': 'sample', 'paths': [str(self.library / n) for n in names],
                                    'report_revision': self.revision, **body})

    def test_selecting_all_candidates_only_sends_candidates_to_recycle_bin(self):
        result = self.delete(['a.jpg', 'b.jpg', 'c.jpg'])
        self.assertEqual(result['deleted'], 3)
        sent = {Path(call.args[0]).name for call in self.trash.call_args_list}
        self.assertEqual(sent, {'a.jpg', 'b.jpg', 'c.jpg'})
        self.assertTrue((self.library / 'keep-one.jpg').exists())
        self.assertTrue((self.library / 'keep-two.jpg').exists())

    def test_accidental_all_photo_selection_cannot_recycle_keepers(self):
        result = self.delete([i['rel'] for i in self.items])
        self.assertEqual(result['deleted'], 3)
        protected = {Path(r['path']).name for r in result['results'] if r.get('protected')}
        self.assertEqual(protected, {'keep-one.jpg', 'keep-two.jpg'})
        self.assertFalse(any('keep-' in Path(call.args[0]).name for call in self.trash.call_args_list))

    def test_keeper_is_protected_even_if_selected_alone(self):
        result = self.delete(['keep-one.jpg'])
        self.assertEqual(result['deleted'], 0)
        self.assertTrue(result['results'][0]['protected'])
        self.trash.assert_not_called()

    def test_missing_keeper_blocks_deletion_of_its_candidate(self):
        (self.library / 'keep-one.jpg').rename(self.library / 'moved-keeper.jpg')
        result = self.delete(['a.jpg'])
        self.assertEqual(result['deleted'], 0)
        self.assertIn('建议保留', result['results'][0]['msg'])
        self.trash.assert_not_called()

    def test_modified_keeper_blocks_deletion_of_its_candidate(self):
        (self.library / 'keep-one.jpg').write_bytes(b'changed keeper fixture')
        self.assertEqual(self.delete(['a.jpg'])['deleted'], 0)
        self.trash.assert_not_called()

    def test_modified_candidate_is_not_deleted_based_on_old_analysis(self):
        (self.library / 'a.jpg').write_bytes(b'changed candidate fixture')
        self.assertEqual(self.delete(['a.jpg'])['deleted'], 0)
        self.trash.assert_not_called()

    def test_stale_result_revision_is_rejected_before_recycling(self):
        with TaskStore(path=self.db_path) as store, patch('task_store.time.time', return_value=self.revision + 1):
            store.save_results(self.root / 'job', self.frame, self.snapshot)
        with self.assertRaises(HTTPException) as error:
            self.delete(['a.jpg'])
        self.assertEqual(error.exception.status_code, 409)
        self.trash.assert_not_called()

    def test_running_analysis_does_not_allow_destructive_actions(self):
        with TaskStore(path=self.db_path) as store:
            store.update_job('sample', {'status': 'running'})
        with self.assertRaises(HTTPException) as error:
            self.delete(['a.jpg'])
        self.assertEqual(error.exception.status_code, 409)
        self.trash.assert_not_called()

    def test_hidden_references_still_protect_keepers_in_filtered_results(self):
        data = json.loads(webapp.job_results('sample', label='可删-模糊').body)
        self.assertEqual(data['total'], 1)
        self.assertTrue(data['rows'][0]['__protected_keep'])
        self.assertEqual(data['report_revision'], self.revision)

    def test_candidate_that_is_also_another_candidates_keeper_is_protected(self):
        frame = self.frame.copy()
        frame.loc[0, '同组最优'] = 'b.jpg'
        with TaskStore(path=self.db_path) as store:
            store.save_results(self.root / 'job', frame, self.snapshot)
            self.revision = store.results('sample', include_revision=True)[1]
        result = self.delete(['a.jpg', 'b.jpg'])
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(result['results'][1]['protected'])
        self.trash.assert_called_once_with(str((self.library / 'a.jpg').resolve()))


if __name__ == '__main__':
    unittest.main()
