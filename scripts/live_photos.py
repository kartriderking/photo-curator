"""Independent, read-only Apple/vivo pairing scans with SQLite checkpoints."""
import json
import os
import subprocess
import time
import uuid
from collections import defaultdict
from pathlib import Path

from cache_db import canonical, item_for
from common import CACHE_DIR, OUTPUT_DIR, EXIFTOOL, IMAGE_EXTS, VIDEO_EXTS
from task_store import TaskStore, encode

SIGNATURE = 'apple-vivo-pair-identifier-v4'
STATE_LABELS = {'missing_video': '缺少配对视频', 'missing_photo': '缺少配对照片',
                'paired': '配对完整', 'embedded': '完整的单文件实况', 'no_uuid': '未读到实况标识',
                'error': '读取失败', 'conflict': '配对标识无法确认'}


def enumerate_media(root, recursive=True):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('照片文件夹不存在')
    items = []
    excluded = {CACHE_DIR.resolve(), OUTPUT_DIR.resolve()}
    def walk_error(error):
        raise ValueError(f'无法读取部分文件夹，检查未完成：{error}') from error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()
                   and not (Path(folder) / name).is_junction() and (Path(folder) / name).resolve() not in excluded]
        if not recursive:
            dirs.clear()
        for name in files:
            p = Path(folder) / name
            if p.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS or p.is_symlink():
                continue
            try:
                if p.resolve().is_relative_to(root):
                    items.append(item_for(p, root, 'video' if p.suffix.lower() in VIDEO_EXTS else 'photo'))
            except OSError as error:
                raise ValueError(f'无法读取文件信息：{p.name}：{error}') from error
    return sorted(items, key=lambda item: item['rel'].casefold())


def identifier_from_tags(tags):
    found, source = set(), {}
    for key, value in tags.items():
        name = key.split(':')[-1]
        if name not in ('ContentIdentifier', 'MediaGroupUUID'):
            continue
        # ExifTool group 0/1 prefixes identify Apple MakerNotes, QuickTime Keys and XAttrs.
        if ':' in key and not any(g in key.split(':')[:-1] for g in ('MakerNotes', 'Apple', 'QuickTime', 'Keys', 'XAttr', 'System', 'MacOS')):
            continue
        source[key] = str(value)
        try:
            found.add(str(uuid.UUID(str(value).strip().strip('{}'))))
        except (ValueError, TypeError, AttributeError):
            return {'uuid': None, 'issue': 'UUID 格式无法识别', 'tags': source, 'state': 'conflict'}
    if len(found) > 1:
        return {'uuid': None, 'issue': '同一文件含有相互冲突的内容标识', 'tags': source, 'state': 'conflict'}
    return {'uuid': next(iter(found), None), 'issue': '', 'tags': source, 'state': 'no_uuid'}


def extract_identifiers(paths, fallback=None):
    if not paths:
        return {}
    from vivo_live import read_identifier
    out, pending = {}, []
    for path in paths:
        try:
            metadata = read_identifier(path)
        except (ValueError, UnicodeError) as error:
            out[canonical(path)] = {'uuid': None, 'tags': {}, 'state': 'conflict', 'issue': str(error)}
        except OSError as error:
            out[canonical(path)] = {'uuid': None, 'tags': {}, 'state': 'error', 'issue': str(error)}
        else:
            if metadata:
                out[canonical(path)] = metadata
            elif fallback and canonical(path) in fallback:
                # The old extractor already read Apple's unchanged fields. Always probe
                # vivo first so an old "no UUID" result cannot hide a private pairing ID.
                out[canonical(path)] = fallback[canonical(path)]
            else:
                pending.append(path)
    if not pending:
        return out
    command = [EXIFTOOL, '-charset', 'filename=UTF8', '-j', '-G0:1', '-s', '-a',
               '-ContentIdentifier', '-MediaGroupUUID', '-Error', '-@', '-']
    try:
        result = subprocess.run(command, input=('\n'.join(str(Path(p).resolve()) for p in pending) + '\n').encode('utf-8'),
                                capture_output=True, timeout=300)
    except FileNotFoundError as error:
        raise ValueError('未找到 ExifTool，请按安装说明配置 tools/exiftool.exe 或 PHOTO_CURATOR_EXIFTOOL。') from error
    text = result.stdout.decode('utf-8', errors='replace').strip()
    if not text:
        raise ValueError('ExifTool 未返回结果：' + result.stderr.decode('utf-8', errors='replace')[-300:])
    records = json.loads(text)
    if not isinstance(records, list):
        raise ValueError('ExifTool 返回格式不正确')
    for record in records:
        path = record.get('SourceFile') or record.get('ExifTool:SourceFile')
        if not path:
            continue
        error = next((v for k, v in record.items() if k.split(':')[-1] == 'Error'), None)
        out[canonical(path)] = {'uuid': None, 'tags': {}, 'state': 'error', 'issue': str(error)} if error else identifier_from_tags(record)
    return out


class LivePhotoStore(TaskStore):
    def __init__(self, db=None, path=None):
        super().__init__(db=db, path=path)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS live_photo_scans (
                id TEXT PRIMARY KEY, input_dir TEXT NOT NULL, recursive INTEGER NOT NULL,
                status TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS live_photo_files (
                scan_id TEXT NOT NULL, path TEXT NOT NULL, uuid TEXT, kind TEXT NOT NULL,
                state TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(scan_id,path));
            CREATE INDEX IF NOT EXISTS live_photo_uuid ON live_photo_files(scan_id,uuid);
        ''')

    def create_scan(self, root, recursive=True):
        state = {'id': uuid.uuid4().hex[:12], 'input_dir': str(Path(root).resolve()), 'recursive': bool(recursive),
                 'status': 'queued', 'done': 0, 'total': 0, 'cached': 0, 'summary': {}, 'signature': SIGNATURE}
        with self.conn:
            self.conn.execute('INSERT INTO live_photo_scans VALUES (?,?,?,?,?,?)',
                              (state['id'], state['input_dir'], int(recursive), state['status'], encode(state), time.time()))
        return state

    def scan(self, scan_id):
        row = self.conn.execute('SELECT data,status FROM live_photo_scans WHERE id=?', (scan_id,)).fetchone()
        if not row:
            return None
        state = json.loads(row[0])
        state['status'] = row[1]
        if state['status'] == 'done' and state.get('signature') != SIGNATURE:
            state.update(status='stale', error='配对识别规则已更新，请重新检查以识别 vivo 两文件及单文件实况照片。')
        return state

    def save_state(self, state):
        with self.conn:
            self.conn.execute('UPDATE live_photo_scans SET status=?,data=? WHERE id=?', (state['status'], encode(state), state['id']))

    def save_files(self, scan_id, rows):
        with self.conn:
            self.conn.executemany('INSERT OR REPLACE INTO live_photo_files VALUES (?,?,?,?,?,?)',
                                  [(scan_id, canonical(item['path']), meta.get('uuid'), item['kind'], meta.get('state', 'no_uuid'),
                                    encode({'item': item, 'metadata': meta})) for item, meta in rows])

    def files(self, scan_id):
        return [json.loads(r[0]) for r in self.conn.execute('SELECT data FROM live_photo_files WHERE scan_id=? ORDER BY path', (scan_id,))]

    def classify(self, state):
        groups = defaultdict(set)
        files = self.files(state['id'])
        for row in files:
            if row['metadata'].get('uuid'):
                groups[row['metadata']['uuid']].add(row['item']['kind'])
        summary = {'paired': sum(kinds == {'photo', 'video'} for kinds in groups.values()),
                   'missing_video': 0, 'missing_photo': 0, 'no_uuid': 0, 'embedded': 0, 'error': 0, 'conflict': 0}
        updates = []
        for row in files:
            item, meta = row['item'], row['metadata']
            if meta.get('uuid'):
                name = 'paired' if groups[meta['uuid']] == {'photo', 'video'} else 'missing_video' if item['kind'] == 'photo' else 'missing_photo'
            else:
                name = meta.get('state', 'no_uuid')
            if name != 'paired':
                summary[name] += 1
            updates.append((name, state['id'], canonical(item['path'])))
        state.update(summary=summary, status='done', done=len(files), total=len(files))
        with self.conn:
            self.conn.executemany('UPDATE live_photo_files SET state=? WHERE scan_id=? AND path=?', updates)
            self.conn.execute('UPDATE live_photo_scans SET status=?,data=? WHERE id=?', ('done', encode(state), state['id']))

    def rows(self, scan_id, include_complete=False):
        state = self.scan(scan_id)
        if not state or state['status'] not in ('done', 'stale'):
            return []
        result = []
        unsafe = state['status'] == 'stale' or bool(state['summary'].get('error') or state['summary'].get('conflict'))
        query = 'SELECT data,state FROM live_photo_files WHERE scan_id=?'
        if not include_complete:
            query += " AND state NOT IN ('paired','embedded')"
        for data, name in self.conn.execute(query + ' ORDER BY path', (scan_id,)):
            row = json.loads(data)
            item, meta = row['item'], row['metadata']
            result.append({'相对路径': item['rel'], '类型': item['kind'], '建议标签': STATE_LABELS[name], '配对状态': name,
                           '实况UUID': meta.get('uuid') or '', '失败环节': meta.get('issue', ''),
                           '配对标识': meta.get('identifier') or meta.get('uuid') or '',
                           '实况类型': meta.get('format') or ('Apple' if meta.get('uuid') else ''),
                           '提示': '同一标识的照片与视频均在当前范围内。' if name == 'paired' else
                                   '视频已嵌在照片中，无需独立的配对视频文件。' if name == 'embedded' else
                                   '仅表示当前扫描范围内未找到对应文件，对方可能在其他文件夹。' if name.startswith('missing_') else
                                   '无法据此判断是否是实况照片残片。',
                           '__abs': item['path'], '__abs_best': '', '__can_delete': name.startswith('missing_') and not unsafe})
        return result


def run_scan(scan_id, db_path, cancel=None):
    with LivePhotoStore(path=db_path) as store:
        state = store.scan(scan_id)
        state['status'] = 'running'
        store.save_state(state)
        try:
            items = enumerate_media(state['input_dir'], state['recursive'])
            state['total'] = len(items)
            store.save_state(state)
            for offset in range(0, len(items), 200):
                if cancel is not None and cancel.is_set():
                    state['status'] = 'cancelled'
                    store.save_state(state)
                    return
                batch = items[offset:offset + 200]
                rows, pending = [], []
                for item in batch:
                    cached = store.db.get(item, 'live_photo', SIGNATURE)
                    if cached is None:
                        for previous_signature in ('apple-vivo-pair-identifier-v3', 'apple-vivo-pair-identifier-v2'):
                            previous = store.db.get(item, 'live_photo', previous_signature)
                            if previous is not None and previous.get('state') != 'conflict':
                                cached = previous
                                break
                    if cached is not None:
                        rows.append((item, cached))
                        state['cached'] += 1
                    else:
                        pending.append(item)
                old_metadata = {canonical(i['path']): data for i in pending
                                if (data := store.db.get(i, 'live_photo', 'apple-content-identifier-v1')) is not None}
                paths = [i['path'] for i in pending]
                metadata = extract_identifiers(paths, fallback=old_metadata) if old_metadata else extract_identifiers(paths)
                for item in pending:
                    meta = metadata.get(canonical(item['path']), {'uuid': None, 'tags': {}, 'state': 'error', 'issue': 'ExifTool 未返回此文件的元数据'})
                    rows.append((item, meta))
                store.db.put_many('live_photo', SIGNATURE, [(item, meta, None) for item, meta in rows if meta.get('state') != 'error'])
                store.save_files(scan_id, rows)
                state['done'] += len(batch)
                store.save_state(state)
            if cancel is not None and cancel.is_set():
                state['status'] = 'cancelled'
                store.save_state(state)
                return
            store.classify(state)
            latest = enumerate_media(state['input_dir'], state['recursive'])
            if {(canonical(i['path']), i['version']) for i in items} != {(canonical(i['path']), i['version']) for i in latest}:
                state.update(status='stale', error='检查期间文件夹内容发生了变化，请重新检查。')
                store.save_state(state)
        except Exception as error:
            state.update(status='failed', error=str(error))
            store.save_state(state)
