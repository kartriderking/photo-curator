"""Incremental SQLite feature cache; legacy parquet/jsonl files are never overwritten."""
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path

from common import CACHE_DIR, file_key
from feature_config import signature


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if hasattr(value, 'item'):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return None  # pandas NaT / NA and other missing scalars


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def version(path):
    st = Path(path).stat()
    return f'{st.st_size}:{st.st_mtime_ns}'


def item_for(path, root, kind='photo'):
    p = Path(path).resolve()
    st = p.stat()
    rel = str(p.relative_to(Path(root).resolve()))
    ver = version(p)
    return {'path': str(p), 'rel': rel, 'kind': kind, 'size': st.st_size,
            'mtime': st.st_mtime, 'version': ver,
            'key': hashlib.sha1(f'{canonical(p)}|{ver}'.encode()).hexdigest()[:24]}


class CacheDB:
    def __init__(self, path=None):
        self.path = Path(path) if path else CACHE_DIR / 'curator.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=30000')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS cache (
                path TEXT, version TEXT, feature TEXT, signature TEXT,
                data TEXT NOT NULL, vector BLOB, source TEXT NOT NULL DEFAULT 'computed',
                PRIMARY KEY(path, version, feature, signature));
            CREATE TABLE IF NOT EXISTS manifest (
                root TEXT, path TEXT, data TEXT NOT NULL, PRIMARY KEY(root,path));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
            CREATE INDEX IF NOT EXISTS cache_feature ON cache(feature,signature);
        ''')

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def get(self, item, feature, sig):
        row = self.conn.execute('SELECT data,vector,source FROM cache WHERE path=? AND version=? AND feature=? AND signature=?',
                                (canonical(item['path']), item['version'], feature, sig)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data['_source'] = row[2]
        if row[1] is not None:
            import numpy as np
            data['_vector'] = np.frombuffer(row[1], dtype='float16').copy()
        return data

    def put_many(self, feature, sig, rows, source='computed', preserve=False):
        verb = 'INSERT OR IGNORE' if preserve else 'INSERT OR REPLACE'
        values = []
        for item, result, vector in rows:
            payload = {k: v for k, v in result.items() if not k.startswith('_')}
            blob = vector.astype('float16').tobytes() if vector is not None else None
            values.append((canonical(item['path']), item['version'], feature, sig,
                           json.dumps(clean(payload), ensure_ascii=False, allow_nan=False), blob, source))
        with self.conn:
            self.conn.executemany(f'{verb} INTO cache VALUES (?,?,?,?,?,?,?)', values)

    def set_manifest(self, root, items):
        root = canonical(root)
        with self.conn:
            self.conn.execute('DELETE FROM manifest WHERE root=?', (root,))
            self.conn.executemany('INSERT INTO manifest VALUES (?,?,?)',
                                 [(root, canonical(i['path']), json.dumps(clean(i), ensure_ascii=False)) for i in items])

    def manifest(self, root):
        return [json.loads(r[0]) for r in self.conn.execute('SELECT data FROM manifest WHERE root=? ORDER BY path', (canonical(root),))]

    def import_legacy(self):
        """One-time import, validating file identity and preserving all existing new records."""
        if self.conn.execute("SELECT 1 FROM settings WHERE key='legacy-v1'").fetchone():
            return self.seed_legacy_places()
        import pandas as pd
        import numpy as np
        counts = {}
        mapping = {}
        for name, feat in [('metadata.parquet', 'metadata'), ('quality.parquet', 'quality'), ('clip_scores.parquet', 'clip')]:
            f = CACHE_DIR / name
            if not f.exists():
                continue
            df = pd.read_parquet(f)
            rows = []
            for raw in df.to_dict('records'):
                p = Path(raw['path'])
                if not p.is_file():
                    continue
                st = p.stat()
                expected = file_key(str(raw['rel']), st.st_size, st.st_mtime)
                if raw.get('kind') == 'frame':
                    expected = hashlib.sha1(f'{raw["path"]}|{st.st_size}|{int(st.st_mtime)}'.encode()).hexdigest()[:16]
                if expected != raw['key']:
                    continue
                item = {'path': str(p.resolve()), 'version': version(p)}
                mapping[(canonical(p), str(raw['key']))] = item
                rows.append((item, raw, None))
            if feat == 'clip':
                vf, kf = CACHE_DIR / 'clip_embeddings_fp16.npy', CACHE_DIR / 'clip_embeddings_keys.json'
                if vf.exists() and kf.exists():
                    keys = json.loads(kf.read_text(encoding='utf-8'))
                    vectors = np.load(vf, mmap_mode='r')
                    bykey = {k: idx for idx, k in enumerate(keys)}
                    key_counts = df['key'].value_counts()
                    rows = [(i, r, vectors[bykey[r['key']]] if r['key'] in bykey and key_counts[r['key']] == 1 else None) for i, r, _ in rows]
            self.put_many(feat, signature(feat), rows, 'legacy', preserve=True)
            if feat == 'quality':
                self.put_many('phash', signature('phash'), rows, 'legacy', preserve=True)
            counts[feat] = len(rows)
        vlm = CACHE_DIR / 'vlm_results.jsonl'
        rows = []
        if vlm.exists():
            for line in vlm.read_text(encoding='utf-8').splitlines():
                try:
                    r = json.loads(line)
                    item = mapping.get((canonical(r['path']), str(r['key'])))
                    if item:
                        rows.append((item, r, None))
                except (ValueError, KeyError):
                    continue
            # Legacy model/prompt provenance is unknown; opt-in reuse only for the default model.
            self.put_many('vlm', 'legacy', rows, 'legacy', preserve=True)
            counts['vlm'] = len(rows)
        gf = CACHE_DIR / 'geonames.parquet'
        if gf.exists():
            rows = [({'path': str(CACHE_DIR / ('geo-' + str(r['key']))), 'version': '1'}, r, None)
                    for r in pd.read_parquet(gf).to_dict('records') if r.get('src') != 'fail']
            self.put_many('geocode', 'online-v1', rows, 'legacy', preserve=True)
            counts['geocode'] = len(rows)
        # Frames are copied as references and verified again before use.
        ff = CACHE_DIR / 'frames_manifest.parquet'
        if ff.exists():
            rows = []
            for path, group in pd.read_parquet(ff).groupby('video_path'):
                first = group.iloc[0]
                item = mapping.get((canonical(path), str(first['video_key'])))
                paths = [p for p in group['frame_path'] if Path(p).is_file()]
                if item and paths:
                    rows.append((item, {'paths': paths}, None))
            self.put_many('frames', signature('frames'), rows, 'legacy', preserve=True)
            counts['frames'] = len(rows)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('legacy-v1',?)", (json.dumps(counts),))
        counts.update(self.seed_legacy_places())
        return counts

    def seed_legacy_places(self):
        """Reuse legacy city names and previously queried coordinates without any networking."""
        if self.conn.execute("SELECT 1 FROM settings WHERE key='legacy-places-v1'").fetchone():
            return {}
        geocodes = {p: json.loads(data) for p, data in self.conn.execute("SELECT path,data FROM cache WHERE feature='geocode' AND signature='online-v1'")}
        offline, online = [], []
        for path, ver, data in self.conn.execute("SELECT path,version,data FROM cache WHERE feature='metadata' AND source='legacy'"):
            raw = json.loads(data)
            item = {'path': path, 'version': ver}
            city = raw.get('city')
            if isinstance(city, str) and city:
                offline.append((item, {'city': city, 'place': '', 'src': 'offline'}, None))
            if raw.get('gps_lat') is not None and raw.get('gps_lon') is not None:
                lat, lon = float(raw['gps_lat']), float(raw['gps_lon'])
                geo_path = canonical(CACHE_DIR / f'geo-{round(lat, 3):.3f},{round(lon, 3):.3f}')
                if geo_path in geocodes:
                    online.append((item, geocodes[geo_path], None))
        self.put_many('places', 'offline-v1', offline, 'legacy', preserve=True)
        self.put_many('places', 'online-v1', online, 'legacy', preserve=True)
        counts = {'places_offline': len(offline), 'places_online': len(online)}
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('legacy-places-v1',?)", (json.dumps(counts),))
        return counts
