"""UUID pairing, independent scans, cache reuse and recycling guards."""
import hashlib
import json
import sys
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cache_db import canonical
from live_photos import LivePhotoStore, identifier_from_tags, extract_identifiers, run_scan

UUID_A = '6bc8dc11-417d-4a49-8f86-325998612531'
UUID_B = '706bd2ec-68f8-49b5-9c8b-afd750527408'


class LivePhotosTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.db_path = self.root / 'curator.sqlite3'
        self.metadata = {}

    def file(self, rel, identifier=None, **extra):
        p = self.library / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'fixture')
        meta = identifier_from_tags({'MakerNotes:Apple:ContentIdentifier': identifier}) if identifier else identifier_from_tags({})
        self.metadata[canonical(p)] = {**meta, **extra}
        return p

    def scan(self, recursive=True):
        with LivePhotoStore(path=self.db_path) as store:
            state = store.create_scan(self.library, recursive)
        with patch('live_photos.extract_identifiers', side_effect=lambda paths: {canonical(p): self.metadata[canonical(p)] for p in paths}):
            run_scan(state['id'], self.db_path)
        with LivePhotoStore(path=self.db_path) as store:
            return store.scan(state['id']), store.rows(state['id'])

    def test_renamed_files_in_different_subfolders_pair_by_uuid(self):
        self.file('photo/renamed.jpg', UUID_A.upper())
        self.file('video/completely-different.mov', '{' + UUID_A + '}')
        state, rows = self.scan()
        self.assertEqual(state['summary']['paired'], 1)
        self.assertEqual(rows, [])

    def test_same_filename_different_uuid_does_not_pair(self):
        self.file('same.jpg', UUID_A)
        self.file('same.mov', UUID_B)
        state, rows = self.scan()
        self.assertEqual(state['summary']['paired'], 0)
        self.assertEqual({r['配对状态'] for r in rows}, {'missing_video', 'missing_photo'})

    def test_ordinary_photos_without_uuid_are_not_recycling_candidates(self):
        self.file('ordinary.jpg')
        self.file('ordinary.mov')
        state, rows = self.scan()
        self.assertEqual(state['summary']['no_uuid'], 2)
        self.assertFalse(any(r['__can_delete'] for r in rows))
        self.assertIsNone(identifier_from_tags({'ImageUniqueID': UUID_A})['uuid'])

    def test_conflicting_or_invalid_identifiers_are_not_matched(self):
        meta = identifier_from_tags({'MakerNotes:Apple:ContentIdentifier': UUID_A, 'QuickTime:Keys:ContentIdentifier': UUID_B})
        self.assertEqual(meta['state'], 'conflict')
        self.assertIsNone(meta['uuid'])
        self.assertEqual(identifier_from_tags({'ContentIdentifier': 'not-a-uuid'})['state'], 'conflict')
        self.assertEqual(identifier_from_tags({'QuickTime:Keys:ContentIdentifier': UUID_A})['uuid'], UUID_A)

    def test_nonrecursive_scope_reports_counterpart_outside_scope_as_missing(self):
        self.file('photo.jpg', UUID_A)
        self.file('nested/video.mov', UUID_A)
        state, rows = self.scan(recursive=False)
        self.assertEqual(state['total'], 1)
        self.assertEqual(rows[0]['配对状态'], 'missing_video')
        self.assertIn('其他文件夹', rows[0]['提示'])

    def test_repeated_scan_reuses_metadata_without_exiftool_or_image_decoding(self):
        self.file('photo.jpg', UUID_A)
        self.scan()
        with LivePhotoStore(path=self.db_path) as store:
            state = store.create_scan(self.library)
        with patch('live_photos.extract_identifiers', side_effect=lambda paths: {} if not paths else (_ for _ in ()).throw(AssertionError('uncached extraction'))):
            run_scan(state['id'], self.db_path)
        with LivePhotoStore(path=self.db_path) as store:
            self.assertEqual(store.scan(state['id'])['cached'], 1)
            self.assertEqual(store.list_jobs(), [])

    def test_changed_file_invalidates_identifier_cache(self):
        p = self.file('photo.jpg', UUID_A)
        self.scan()
        p.write_bytes(b'changed fixture')
        self.metadata[canonical(p)] = identifier_from_tags({'ContentIdentifier': UUID_B})
        state, rows = self.scan()
        self.assertEqual(state['cached'], 0)
        self.assertEqual(rows[0]['实况UUID'], UUID_B)

    def test_read_errors_prevent_recycling_based_on_incomplete_results(self):
        self.file('photo.jpg', UUID_A)
        self.file('unreadable.mov', state='error', issue='unreadable')
        state, rows = self.scan()
        self.assertEqual(state['summary']['error'], 1)
        self.assertFalse(any(r['__can_delete'] for r in rows))

    def test_stop_preserves_cache_and_disables_incomplete_results(self):
        self.file('photo.jpg', UUID_A)
        with LivePhotoStore(path=self.db_path) as store:
            state = store.create_scan(self.library)
        stopped = threading.Event()
        stopped.set()
        with patch('live_photos.extract_identifiers', side_effect=AssertionError('cancelled')):
            run_scan(state['id'], self.db_path, stopped)
        with LivePhotoStore(path=self.db_path) as store:
            self.assertEqual(store.scan(state['id'])['status'], 'cancelled')
            self.assertEqual(store.rows(state['id']), [])

    def test_new_counterpart_after_scan_blocks_recycling(self):
        import webapp
        from fastapi import HTTPException
        photo = self.file('photo.jpg', UUID_A)
        state, _ = self.scan()
        self.file('new-video.mov', UUID_A)
        with patch.object(webapp, 'DB_PATH', self.db_path), patch('send2trash.send2trash') as trash:
            with self.assertRaises(HTTPException) as error:
                webapp.delete_pair_files(state['id'], {'paths': [str(photo)]})
            self.assertEqual(error.exception.status_code, 409)
        trash.assert_not_called()
        self.assertTrue(photo.exists())

    def test_complete_pairs_and_files_outside_folder_cannot_be_recycled(self):
        import webapp
        photo = self.file('photo.jpg', UUID_A)
        self.file('video.mov', UUID_A)
        state, _ = self.scan()
        outside = self.root / 'outside.jpg'
        outside.write_bytes(b'outside fixture')
        with patch.object(webapp, 'DB_PATH', self.db_path), patch('send2trash.send2trash') as trash:
            result = webapp.delete_pair_files(state['id'], {'paths': [str(photo), str(outside)]})
        self.assertEqual(result['deleted'], 0)
        self.assertFalse(any(r['ok'] for r in result['results']))
        trash.assert_not_called()

    def test_eligible_orphan_is_recycled_and_audited_without_a_job(self):
        import webapp
        photo = self.file('photo.jpg', UUID_A)
        state, _ = self.scan()
        with patch.object(webapp, 'DB_PATH', self.db_path), patch('send2trash.send2trash') as trash:
            result = webapp.delete_pair_files(state['id'], {'paths': [str(photo)]})
        trash.assert_called_once_with(str(photo.resolve()))
        self.assertEqual(result['deleted'], 1)
        with LivePhotoStore(path=self.db_path) as store:
            self.assertEqual(len(store.deletion_records()), 1)
            self.assertEqual(store.list_jobs(), [])
            self.assertEqual(store.scan(state['id'])['status'], 'stale')

    def test_exiftool_reads_metadata_without_modifying_a_real_fixture(self):
        from PIL import Image
        p = self.library / 'ordinary.jpg'
        Image.new('RGB', (24, 24), 'red').save(p)
        digest = hashlib.sha256(p.read_bytes()).digest()
        records = extract_identifiers([str(p)])
        self.assertEqual(records[canonical(p)]['state'], 'no_uuid')
        self.assertEqual(hashlib.sha256(p.read_bytes()).digest(), digest)

    def test_real_apple_makernotes_and_quicktime_keys_pair_through_exiftool(self):
        from PIL import Image
        uid = UUID_A.encode() + b'\0'
        maker = (b'Apple iOS\0' + b'\0\x01MM' + struct.pack('>H', 1) +
                 struct.pack('>HHII', 0x11, 2, len(uid), 32) + struct.pack('>I', 0) + uid)
        exif = Image.Exif()
        exif[271] = 'Apple'
        exif[34665] = {37500: maker}
        photo = self.library / 'renamed-photo.jpg'
        Image.new('RGB', (24, 24), 'red').save(photo, exif=exif)
        def atom(name, data):
            return struct.pack('>I', len(data) + 8) + name + data
        key = b'com.apple.quicktime.content.identifier'
        keys = atom(b'keys', b'\0' * 4 + struct.pack('>I', 1) + struct.pack('>I', len(key) + 8) + b'mdta' + key)
        ilst = atom(b'ilst', atom(struct.pack('>I', 1), atom(b'data', struct.pack('>II', 1, 0) + UUID_A.encode())))
        video = self.library / 'different-name.mov'
        video.write_bytes(atom(b'ftyp', b'qt  ' + b'\0' * 4 + b'qt  ') + atom(b'moov', atom(b'meta', keys + ilst)))
        before = {p: hashlib.sha256(p.read_bytes()).digest() for p in (photo, video)}
        with LivePhotoStore(path=self.db_path) as store:
            scan = store.create_scan(self.library)
        run_scan(scan['id'], self.db_path)
        with LivePhotoStore(path=self.db_path) as store:
            self.assertEqual(store.scan(scan['id'])['summary']['paired'], 1)
            self.assertEqual(store.rows(scan['id']), [])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).digest() for p in (photo, video)})

    def test_exiftool_command_is_read_only_and_preserves_unicode_paths(self):
        p = self.file('子目录/照片.jpg', UUID_A)
        result = type('Result', (), {'stdout': json.dumps([{'SourceFile': str(p), 'MakerNotes:Apple:ContentIdentifier': UUID_A}], ensure_ascii=False).encode(), 'stderr': b'', 'returncode': 0})()
        with patch('live_photos.subprocess.run', return_value=result) as run:
            self.assertEqual(extract_identifiers([str(p)])[canonical(p)]['uuid'], UUID_A)
        command = run.call_args.args[0]
        self.assertNotIn('-overwrite_original', command)
        self.assertIn('-ContentIdentifier', command)
        self.assertIn(str(p), run.call_args.kwargs['input'].decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
