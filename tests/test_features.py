"""Regression tests for selective execution, durable caches and pure export."""
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cache_db import CacheDB, canonical, item_for, version
from feature_config import dependencies, normalize_options, signature
from pipeline import execute, export_results, phash_groups, similar_groups
from task_store import TaskStore


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.db_path = self.root / 'test.sqlite3'
        with CacheDB(self.db_path) as db:
            db.conn.execute("INSERT INTO settings VALUES ('legacy-v1','{}')")
            db.conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def photo(self, name, color='red'):
        from PIL import Image
        p = self.library / name
        Image.new('RGB', (100, 80), color).save(p)
        return item_for(p, self.library)

    def run_features(self, features, **options):
        jd = self.root / ('job-' + str(len(list(self.root.glob('job-*')))))
        result = execute(self.library, normalize_options({'features': features, **options}), jd, self.db_path)
        return jd, result

    def report(self, job_dir):
        with TaskStore(path=self.db_path) as store:
            return store.results(store.job_for_dir(job_dir)['id'])

    def test_only_gps_never_loads_ai_or_reads_pixels(self):
        item = self.photo('a.jpg')
        with patch.dict(sys.modules, {'torch': None, 'open_clip': None}), patch('pipeline.measure_image', side_effect=AssertionError('pixel decode')):
            jd, state = self.run_features(['gps'])
        import pandas as pd
        report = self.report(jd)
        self.assertEqual(len(report), 1)
        self.assertIn('GPS纬度', report.columns)
        self.assertNotIn('pHash', report.columns)
        self.assertEqual(state['features']['landmarks']['status'], 'skipped')

    def test_phash_alone_without_metadata_and_duplicate_group(self):
        self.photo('a.jpg')
        self.photo('b.jpg')
        with patch.dict(sys.modules, {'torch': None, 'open_clip': None}):
            jd, state = self.run_features(['phash'])
        import pandas as pd
        report = self.report(jd)
        self.assertEqual(state['status'], 'done')
        self.assertEqual((report['建议标签'] == '可删-重复').sum(), 1)
        self.assertNotIn('清晰度', report.columns)
        with CacheDB(self.db_path) as db:
            self.assertEqual(db.conn.execute("SELECT count(*) FROM cache WHERE feature='metadata'").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT count(*) FROM cache WHERE feature='quality'").fetchone()[0], 0)

    def test_quality_alone_and_cached_rerun(self):
        self.photo('a.jpg')
        self.run_features(['quality'])
        with patch('pipeline.measure_image', side_effect=AssertionError('cached image decoded')):
            jd, state = self.run_features(['quality'])
        self.assertEqual(state['features']['quality']['cached'], 1)

    def test_joint_measurements_decode_once(self):
        self.photo('a.jpg')
        from common import load_image
        with patch('common.load_image', wraps=load_image) as load:
            self.run_features(['phash', 'quality'])
        self.assertEqual(load.call_count, 1)

    def test_modified_file_invalidates_cache(self):
        item = self.photo('a.jpg')
        self.run_features(['phash'])
        self.photo('a.jpg', 'blue')
        os.utime(item['path'], ns=(1_000_000_001, 1_000_000_001))
        jd, state = self.run_features(['phash'])
        self.assertEqual(state['features']['phash']['cached'], 0)

    def test_same_relative_path_in_two_libraries_isolated(self):
        a = self.photo('same.jpg')
        other = self.root / 'other'
        other.mkdir()
        p = other / 'same.jpg'
        p.write_bytes(Path(a['path']).read_bytes())
        st = Path(a['path']).stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        b = item_for(p, other)
        with CacheDB(self.db_path) as db:
            db.put_many('phash', signature('phash'), [(a, {'phash': '1234'}, None)])
            self.assertIsNone(db.get(b, 'phash', signature('phash')))

    def test_export_does_no_grouping_or_network_and_handles_deleted_keeper(self):
        a, b = self.photo('a.jpg'), self.photo('b.jpg')
        jd, _ = self.run_features(['phash'])
        Path(a['path']).unlink()
        with CacheDB(self.db_path) as db, patch('pipeline.scan', side_effect=AssertionError('scan')), patch('pipeline.phash_groups', side_effect=AssertionError('grouping')):
            report = export_results(jd, db)
        self.assertEqual(len(report), 1)
        self.assertNotEqual(report.iloc[0]['建议标签'], '可删-重复')
        self.assertEqual(report.iloc[0]['同组最优'], '')

    def test_bad_photo_gives_partial_report(self):
        self.photo('a.jpg')
        (self.library / 'bad.jpg').write_text('not an image')
        jd, state = self.run_features(['quality'])
        self.assertEqual(state['status'], 'partial')
        self.assertFalse((jd / 'results.csv').exists())
        self.assertEqual(len(self.report(jd)), 2)
        self.assertEqual(state['features']['quality']['status'], 'partial')

    def test_dependencies_do_not_add_models_to_light_tasks(self):
        self.assertEqual(dependencies(normalize_options({'features': ['phash']})), ['phash'])
        self.assertEqual(dependencies(normalize_options({'features': ['places']})), ['gps', 'places'])
        self.assertEqual(dependencies(normalize_options({'features': ['landmarks'], 'vlm_scope': 'all'})), ['landmarks'])

    def test_signature_changes_with_model(self):
        a = normalize_options({'features': ['landmarks'], 'vlm_model': 'qwen3-vl:8b'})
        b = normalize_options({'features': ['landmarks'], 'vlm_model': 'different-model'})
        self.assertNotEqual(signature('vlm', a), signature('vlm', b))

    def test_bktree_matches_brute_force_greedy(self):
        rng = random.Random(12)
        hashes = [rng.getrandbits(64) for _ in range(80)]
        hashes += [h ^ 7 for h in hashes[:30]]
        items = [{'path': str(self.library / str(n))} for n in range(len(hashes))]
        records = {canonical(i['path']): {'phash': f'{h:016x}'} for i, h in zip(items, hashes)}
        reps, expected = [], []
        for i, h in zip(items, hashes):
            found = sorted(((h ^ rep).bit_count(), n) for n, rep in enumerate(reps) if (h ^ rep).bit_count() <= 4)
            if found:
                expected[found[0][1]].append(i['path'])
            else:
                reps.append(h)
                expected.append([i['path']])
        self.assertEqual(phash_groups(items, records, 4), [g for g in expected if len(g) > 1])

    def test_vector_groups_and_sqlite_roundtrip(self):
        import numpy as np
        a, b, c = [self.photo(n + '.jpg') for n in ('a', 'b', 'c')]
        vectors = {canonical(a['path']): np.array([1, 0], dtype='float16'), canonical(b['path']): np.array([1, 0.01], dtype='float16'), canonical(c['path']): np.array([0, 1], dtype='float16')}
        groups = similar_groups([a, b, c], vectors, 0.95)
        self.assertEqual(len(groups), 1)
        with CacheDB(self.db_path) as db:
            db.put_many('clip', signature('clip'), [(a, {'clip_top': 'scenic'}, vectors[canonical(a['path'])])])
            np.testing.assert_array_equal(db.get(a, 'clip', signature('clip'))['_vector'], vectors[canonical(a['path'])])

    def test_cached_clip_does_not_import_model(self):
        import numpy as np
        a = self.photo('a.jpg')
        b = self.photo('b.jpg')
        with CacheDB(self.db_path) as db:
            db.put_many('clip', signature('clip'), [(i, {'clip_top': 'scenic', 'clip_top_score': 0.9, 'clip_scenic': 0.9}, np.array([1, 0], dtype='float16')) for i in (a, b)])
        with patch.dict(sys.modules, {'torch': None, 'open_clip': None}):
            jd, state = self.run_features(['similar'])
        self.assertEqual(state['features']['similar']['cached'], 2)
        import pandas as pd
        self.assertEqual((self.report(jd)['建议标签'] == '待审-相似').sum(), 1)

    def test_only_vlm_all_no_clip_and_connection_failure_stops(self):
        import requests
        self.photo('a.jpg')
        self.photo('b.jpg')
        with patch.dict(sys.modules, {'torch': None, 'open_clip': None}), patch('step5_vlm._ask_ollama', side_effect=requests.exceptions.ConnectionError('offline')) as ask, patch('requests.post'):
            jd, state = self.run_features(['landmarks'], vlm_scope='all')
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['features']['classify']['status'], 'skipped')
        self.assertEqual(state['features']['landmarks']['done'], 1)

    def test_vlm_success_and_cached_repeat(self):
        self.photo('a.jpg')
        result = {'landmark': '测试景点', 'city': '测试市', 'scene': '景点', 'confidence': 'high', 'is_travel': True}
        with patch('step5_vlm._ask_ollama', return_value=result), patch('requests.post'):
            self.run_features(['landmarks'], vlm_scope='all')
        with patch('step5_vlm._ask_ollama', side_effect=AssertionError('cached VLM called')):
            jd, state = self.run_features(['landmarks'], vlm_scope='all')
        self.assertEqual(state['features']['landmarks']['cached'], 1)

    def test_video_gps_never_extracts_frames(self):
        (self.library / 'a.mp4').write_bytes(b'test')
        with patch('step1_metadata.extract_exif', return_value={str(self.library / 'a.mp4').replace('\\', '/'): {'GPSLatitude': 30, 'GPSLongitude': 120}}), patch('step2_video_frames._extract', side_effect=AssertionError('unrequested frames')):
            jd, state = self.run_features(['gps'], include_videos=True)
        self.assertEqual(state['total_videos'], 1)
        self.assertNotIn('video_analysis', state)

    def test_offline_places_use_metadata_and_cache(self):
        i = self.photo('a.jpg')
        with CacheDB(self.db_path) as db:
            db.put_many('metadata', signature('metadata'), [(i, {'gps_lat': 30, 'gps_lon': 120, 'dt': '2026-01-01T10:00:00'}, None)])
        with patch('step1_metadata._geocoder') as geo:
            geo.return_value.query.return_value = [{'name': 'Hangzhou'}]
            jd, state = self.run_features(['places', 'trips'])
        import pandas as pd
        report = self.report(jd)
        self.assertEqual(report.iloc[0]['城市(GPS)'], 'Hangzhou')
        self.assertEqual(report.iloc[0]['旅程编号'], 'T001')
        self.assertTrue(state['features']['gps']['implicit'])

    def test_legacy_import_validates_files_and_preserves_new_records(self):
        import pandas as pd
        from common import file_key
        i = self.photo('a.jpg')
        legacy = self.root / 'legacy'
        legacy.mkdir()
        st = Path(i['path']).stat()
        row = {'path': i['path'], 'rel': 'a.jpg', 'kind': 'photo', 'key': file_key('a.jpg', st.st_size, st.st_mtime), 'gps_lat': 1, 'gps_lon': 2}
        pd.DataFrame([row]).to_parquet(legacy / 'metadata.parquet')
        db_path = self.root / 'migration.sqlite3'
        with CacheDB(db_path) as db:
            db.put_many('metadata', signature('metadata'), [(i, {'gps_lat': 9}, None)])
            with patch('cache_db.CACHE_DIR', legacy):
                self.assertEqual(db.import_legacy()['metadata'], 1)
                self.assertEqual(db.import_legacy(), {})
            self.assertEqual(db.get(i, 'metadata', signature('metadata'))['gps_lat'], 9)

    def test_legacy_report_without_verified_identity_is_not_deletion_advice(self):
        import pandas as pd
        from pipeline import prepare_legacy_snapshot
        self.photo('a.jpg')
        jd = self.root / 'old-job'
        jd.mkdir()
        pd.DataFrame([{'相对路径': 'a.jpg', '类型': 'photo', '建议标签': '可删-重复', '同组最优': 'b.jpg'}]).to_csv(jd / 'results.csv', index=False)
        with patch('pipeline.CacheDB', side_effect=lambda *args: CacheDB(self.db_path)):
            prepare_legacy_snapshot({'input_dir': str(self.library), 'skip_vlm': True}, jd)
            result = export_results(jd)
        self.assertEqual(result.iloc[0]['建议标签'], '未评估')
        self.assertIn('无法验证', result.iloc[0]['失败环节'])

    def test_product_modes_select_only_their_cost_tier(self):
        self.assertEqual(normalize_options({'mode': 'quick'})['features'], ['phash', 'quality'])
        self.assertEqual(set(normalize_options({'mode': 'quick', 'cleanup_level': 'ai'})['features']),
                         {'phash', 'quality', 'similar', 'classify'})
        self.assertEqual(set(normalize_options({'mode': 'travel'})['features']), {'gps', 'places', 'trips'})
        ordinary = normalize_options({'mode': 'content'})
        self.assertEqual(set(ordinary['features']), {'classify', 'similar'})
        deep = normalize_options({'mode': 'content', 'content_level': 'deep'})
        self.assertEqual(set(deep['features']), {'classify', 'similar', 'landmarks'})
        self.assertEqual(deep['vlm_scope'], 'all')
        self.assertEqual(deep['vlm_task'], 'content')
        for body in ({'mode': 'unknown'}, {'mode': 'quick', 'cleanup_level': 'unknown'}):
            with self.assertRaises(ValueError):
                normalize_options(body)

    def test_ordinary_content_encodes_once_and_cleanup_reuses_both_outputs(self):
        import numpy as np
        from types import SimpleNamespace
        self.photo('a.jpg')
        self.photo('b.jpg', 'blue')
        batches = []
        class Scorer:
            cat_names = ['food', 'scenic']
            def encode(self, paths):
                batches.append(paths)
                return np.array([[.9, .1] for _ in paths]), np.array([[1., 0.] for _ in paths], dtype='float16')
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict(sys.modules, {'step4_clip': SimpleNamespace(ClipScorer=Scorer), 'torch': fake_torch}), \
             patch('step5_vlm._ask_ollama', side_effect=AssertionError('ordinary mode must not use Qwen')):
            jd, state = self.run_features([], mode='content')
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 2)
        report = self.report(jd)
        self.assertTrue(report['CLIP类别'].eq('food').all())
        self.assertEqual(report['同组最优'].fillna('').ne('').sum(), 1)
        with patch.dict(sys.modules, {'step4_clip': None, 'torch': None, 'open_clip': None}):
            jd, state = self.run_features([], mode='quick', cleanup_level='ai')
        self.assertEqual(state['features']['classify']['cached'], 2)
        self.assertEqual(state['features']['similar']['cached'], 2)
        self.assertIn('CLIP类别', self.report(jd).columns)

    def test_deep_content_uses_general_prompt_and_preserves_objects(self):
        import numpy as np
        item = self.photo('a.jpg')
        with CacheDB(self.db_path) as db:
            db.put_many('clip', signature('clip'), [(item, {'clip_top': 'food', 'clip_top_score': .9}, np.array([1., 0.]))])
            db.put_many('vlm', 'legacy', [(item, {'scene': 'old travel description'}, None)])
        result = {'landmark': '', 'city': '', 'scene': '桌上的饭菜', 'objects': '碗,筷子', 'confidence': 'high', 'is_travel': False}
        with patch('step5_vlm._ask_ollama', return_value=result) as ask, patch('requests.post'):
            jd, state = self.run_features([], mode='content', content_level='deep')
        self.assertEqual(ask.call_args.kwargs['task'], 'content')
        self.assertEqual(self.report(jd).iloc[0]['主要物体'], '碗,筷子')
        self.assertNotEqual(signature('vlm', normalize_options({'features': ['landmarks']})),
                            signature('vlm', normalize_options({'mode': 'content', 'content_level': 'deep'})))

    def test_cli_keeps_deep_product_options_for_worker_execution(self):
        import cli
        args = ['cli.py', '--input-dir', str(self.library), '--mode', 'content', '--content-level', 'deep',
                '--vlm-scope', 'all', '--job-dir', str(self.root / 'cli-job')]
        with patch.object(sys, 'argv', args), patch('pipeline.execute') as execute_mock:
            cli.main()
        options = execute_mock.call_args.args[1]
        self.assertEqual(options['vlm_task'], 'content')
        self.assertEqual(options['content_level'], 'deep')

    def test_web_submission_persists_the_requested_product_tier(self):
        import webapp
        bodies = [{'mode': 'quick'}, {'mode': 'quick', 'cleanup_level': 'ai'}, {'mode': 'travel'},
                  {'mode': 'content'}, {'mode': 'content', 'content_level': 'deep'}]
        with patch.object(webapp, 'DB_PATH', self.db_path), patch.object(webapp, 'JOB_DATA_DIR', self.root / 'web-jobs'), \
             patch('webapp._ensure_worker'):
            submitted = [webapp.create_job({'input_dir': str(self.library), **body}) for body in bodies]
        with TaskStore(path=self.db_path) as store:
            for job, body in zip(submitted, bodies):
                saved = store.get_job(job['id'])['options']
                self.assertEqual(saved, normalize_options(body))
        self.assertNotIn('landmarks', submitted[3]['options']['features'])
        self.assertEqual(submitted[4]['options']['vlm_task'], 'content')

    def test_general_vlm_request_and_json_parser_include_objects(self):
        from step5_vlm import _ask_ollama, CONTENT_PROMPT
        response = {'message': {'content': json.dumps({'scene': '桌上的饭菜', 'objects': '碗,筷子', 'is_travel': False})}}
        with patch('step5_vlm.requests.post') as post:
            post.return_value.json.return_value = response
            result = _ask_ollama('http://localhost:11434', 'test-model', 'test-image', task='content')
        self.assertEqual(post.call_args.kwargs['json']['messages'][0]['content'], CONTENT_PROMPT)
        self.assertEqual(result['objects'], '碗,筷子')
        self.assertEqual(result['scene'], '桌上的饭菜')


if __name__ == '__main__':
    unittest.main()
