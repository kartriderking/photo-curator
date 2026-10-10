"""Synthetic private trailers; no personal media is stored in these fixtures."""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from vivo_live import read_identifier, parse_payload, TRAILER_MAGIC, VIVO_UUID
from live_photos import LivePhotoStore, run_scan, extract_identifiers
from cache_db import canonical, item_for

ID_A = '1700000000000abcdef100000000'
ID_B = '1700000000001abcdef200000000'


def payload(identifier=ID_A):
    data = json.dumps({'com.android.camera.takenmodel': 'vivo test',
                       'com.android.camera.livephoto': identifier}, separators=(',', ':')).encode()
    return b'vivo' + data + struct.pack('>I', len(data)) + b'cameralbum!' + struct.pack('>I', 47) + identifier.encode() + TRAILER_MAGIC


def box(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


class VivoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.db = self.root / 'state.sqlite3'

    def photo(self, name='renamed.jpg', identifier=ID_A):
        p = self.library / name
        p.write_bytes(b'\xff\xd8\xff\xd9' + payload(identifier))
        return p

    def video(self, name='another-name.mp4', identifier=ID_A):
        p = self.library / name
        p.write_bytes(box(b'ftyp', b'isom\0\0\0\0isom') + box(b'uuid', VIVO_UUID + payload(identifier)))
        return p

    def scan(self):
        with LivePhotoStore(path=self.db) as store:
            state = store.create_scan(self.library)
        run_scan(state['id'], self.db)
        with LivePhotoStore(path=self.db) as store:
            return store.scan(state['id']), store.rows(state['id'])

    def test_renamed_jpeg_and_mp4_pair_without_exiftool_and_are_unchanged(self):
        files = [self.photo(), self.video()]
        before = [hashlib.sha256(p.read_bytes()).digest() for p in files]
        with patch('live_photos.subprocess.run', side_effect=AssertionError('vivo should not need ExifTool')):
            state, rows = self.scan()
        self.assertEqual(state['summary']['paired'], 1)
        self.assertEqual(rows, [])
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).digest() for p in files])

    def test_same_filename_different_ids_are_not_a_pair(self):
        self.photo('same.jpg')
        self.video('same.mp4', ID_B)
        state, rows = self.scan()
        self.assertEqual(state['summary']['paired'], 0)
        self.assertEqual({r['配对状态'] for r in rows}, {'missing_video', 'missing_photo'})
        self.assertEqual({r['实况类型'] for r in rows}, {'vivo'})

    def test_missing_counterpart_is_displayed_with_its_private_identifier(self):
        self.photo()
        state, rows = self.scan()
        self.assertEqual(rows[0]['配对标识'], ID_A)
        self.assertTrue(rows[0]['__can_delete'])

    def test_old_apple_cache_is_reused_but_cannot_hide_vivo_metadata(self):
        photo = self.photo()
        video = self.video()
        ordinary = self.library / 'ordinary.jpg'
        ordinary.write_bytes(b'ordinary fixture')
        with LivePhotoStore(path=self.db) as store:
            entries = [(item_for(p, self.library), {'uuid': None, 'state': 'no_uuid', 'tags': {}, 'issue': ''}, None)
                       for p in (photo, video, ordinary)]
            store.db.put_many('live_photo', 'apple-content-identifier-v1', entries)
        with patch('live_photos.subprocess.run', side_effect=AssertionError('unchanged Apple metadata should be reused')):
            state, rows = self.scan()
        self.assertEqual(state['summary']['paired'], 1)
        self.assertEqual(state['summary']['no_uuid'], 1)
        self.assertEqual(rows[0]['相对路径'], 'ordinary.jpg')

    def test_conflicting_payload_fields_and_truncated_trailer_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_payload(payload().replace(ID_A.encode(), ID_B.encode(), 1))
        p = self.photo()
        p.write_bytes(p.read_bytes()[:-5])
        result = extract_identifiers([p])
        self.assertEqual(next(iter(result.values()))['state'], 'conflict')

    def test_unknown_uuid_box_cannot_be_mistaken_for_a_vivo_pair(self):
        p = self.library / 'ordinary.mp4'
        p.write_bytes(box(b'uuid', b'notVivoMetadata!' + payload()))
        self.assertIsNone(read_identifier(p))

    def test_mp4_skips_large_media_payload(self):
        p = self.library / 'large.mp4'
        with p.open('wb') as stream:
            stream.write(struct.pack('>I4s', 20 * 1024 * 1024, b'mdat'))
            stream.seek(20 * 1024 * 1024)
            stream.write(box(b'uuid', VIVO_UUID + payload()))
        with p.open('rb') as stream:
            tracked = MagicMock(wraps=stream)
            tracked.__enter__.return_value = tracked
            tracked.__exit__.return_value = False
            with patch.object(Path, 'open', return_value=tracked):
                self.assertEqual(read_identifier(p)['identifier'], ID_A)
            self.assertLess(sum(c.args[0] for c in tracked.read.call_args_list), 4096)

    def test_ordinary_vivo_video_container_is_not_a_broken_live_photo(self):
        data = json.dumps({'com.android.camera.moduleid': 'video', 'version': 2012}).encode()
        private = b'vivo' + data + struct.pack('>I', len(data)) + b'cameralbum!'
        p = self.library / 'ordinary.mp4'
        p.write_bytes(box(b'uuid', VIVO_UUID + private))
        self.assertIsNone(read_identifier(p))

    def embedded_photo(self, name='motion.jpg', valid=True):
        video = box(b'ftyp', b'isom\0\0\0\0isom') + box(b'mdat', b'video fixture')
        if valid:
            video += box(b'moov', b'')
        video += box(b'uuid', VIVO_UUID + payload('motionphoto00010000000000000'))
        xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:c="http://ns.google.com/photos/1.0/camera/" '
               f'xmlns:i="http://ns.google.com/photos/1.0/container/item/" c:MotionPhoto="1">'
               f'<item i:Mime="video/mp4" i:Semantic="MotionPhoto" i:Length="{len(video)}"/></x:xmpmeta>').encode()
        p = self.library / name
        p.write_bytes(b'\xff\xd8' + xmp + b'\xff\xd9' + video)
        return p

    def test_embedded_motion_photos_do_not_pair_by_the_shared_placeholder(self):
        self.embedded_photo('first.jpg')
        self.embedded_photo('second.jpg')
        state, rows = self.scan()
        self.assertEqual(state['summary']['embedded'], 2)
        self.assertEqual(state['summary']['paired'], 0)
        self.assertEqual(rows, [])
        with LivePhotoStore(path=self.db) as store:
            rows = store.rows(state['id'], include_complete=True)
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(r['__can_delete'] for r in rows))

    def test_incomplete_embedded_video_is_not_marked_complete(self):
        p = self.embedded_photo(valid=False)
        with self.assertRaises(ValueError):
            read_identifier(p)
        state, rows = self.scan()
        self.assertEqual(state['summary']['conflict'], 1)
        self.assertFalse(rows[0]['__can_delete'])

    def test_extended_private_trailer_and_large_json_are_read_correctly(self):
        identifier = 'motionphoto00010000000000000'
        data = json.dumps({'com.android.camera.livephoto': identifier, 'other': 'x' * 6000}).encode()
        private = (b'vivo' + data + struct.pack('>I', len(data)) + b'cameralbum!' + struct.pack('>III', 2, 0, 0)
                   + struct.pack('>I', 47) + identifier.encode() + TRAILER_MAGIC)
        video = box(b'ftyp', b'isom\0\0\0\0isom') + box(b'mdat', b'fixture') + box(b'moov', b'') + box(b'uuid', VIVO_UUID + private)
        xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:c="http://ns.google.com/photos/1.0/camera/" '
               f'xmlns:i="http://ns.google.com/photos/1.0/container/item/" c:MotionPhoto="1">'
               f'<item i:Mime="video/mp4" i:Semantic="MotionPhoto" i:Length="{len(video)}"/></x:xmpmeta>').encode()
        p = self.library / 'extended.jpg'
        p.write_bytes(b'\xff\xd8' + xmp + b'\xff\xd9' + video)
        self.assertEqual(read_identifier(p)['state'], 'embedded')

    def test_complete_pairs_are_browsable_but_never_recycling_candidates(self):
        self.photo()
        self.video()
        state, _ = self.scan()
        with LivePhotoStore(path=self.db) as store:
            rows = store.rows(state['id'], include_complete=True)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['配对状态'] == 'paired' for r in rows))
        self.assertFalse(any(r['__can_delete'] for r in rows))

    def test_old_apple_only_scan_cannot_recycle_vivo_orphans(self):
        self.photo()
        state, _ = self.scan()
        with LivePhotoStore(path=self.db) as store:
            state['signature'] = 'apple-content-identifier-v1'
            store.save_state(state)
            self.assertEqual(store.scan(state['id'])['status'], 'stale')
            self.assertFalse(store.rows(state['id'])[0]['__can_delete'])
        import webapp
        from fastapi import HTTPException
        with patch.object(webapp, 'DB_PATH', self.db), patch('send2trash.send2trash') as trash:
            with self.assertRaises(HTTPException):
                webapp.delete_pair_files(state['id'], {'paths': [str(self.library / 'renamed.jpg')]})
        trash.assert_not_called()


if __name__ == '__main__':
    unittest.main()
