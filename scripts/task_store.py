"""Task lifecycle, checkpoints and result rows in the shared SQLite database."""
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from cache_db import CacheDB, canonical, clean


def encode(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class TaskStore:
    def __init__(self, db=None, path=None):
        self.owns_db = db is None
        self.db = db or CacheDB(path)
        self.conn = self.db.conn
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, task_dir TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, created TEXT NOT NULL, updated REAL NOT NULL,
                data TEXT NOT NULL, progress TEXT, snapshot TEXT,
                result_columns TEXT, results_updated REAL, snapshot_revision INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status,created);
            CREATE TABLE IF NOT EXISTS task_results (
                task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                rel TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(task_id,ordinal));
            CREATE INDEX IF NOT EXISTS task_results_label ON task_results(task_id,label);
            CREATE TABLE IF NOT EXISTS deletions (
                id INTEGER PRIMARY KEY, task_id TEXT, path TEXT NOT NULL,
                deleted_at TEXT NOT NULL, UNIQUE(path,deleted_at));
        ''')

    def close(self):
        if self.owns_db:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _job(self, row):
        if row is None:
            return None
        job = json.loads(row[4])
        job.update(id=row[0], task_dir=row[1], status=row[2], created=row[3])
        return job

    def get_job(self, job_id):
        return self._job(self.conn.execute(
            'SELECT id,task_dir,status,created,data FROM tasks WHERE id=?', (job_id,)).fetchone())

    def job_for_dir(self, job_dir):
        return self._job(self.conn.execute(
            'SELECT id,task_dir,status,created,data FROM tasks WHERE task_dir=?', (canonical(job_dir),)).fetchone())

    def list_jobs(self):
        return [self._job(row) for row in self.conn.execute(
            'SELECT id,task_dir,status,created,data FROM tasks ORDER BY created DESC,rowid DESC')]

    def create_job(self, job, job_dir):
        data = dict(job)
        data.setdefault('id', uuid.uuid4().hex[:12])
        data.setdefault('status', 'queued')
        data.setdefault('created', time.strftime('%F %T'))
        with self.conn:
            self.conn.execute('INSERT INTO tasks(id,task_dir,status,created,updated,data) VALUES (?,?,?,?,?,?)',
                              (data['id'], canonical(job_dir), data['status'], data['created'], time.time(), encode(data)))
        return self.get_job(data['id'])

    def ensure_task(self, job_dir, root=None, options=None):
        job = self.job_for_dir(job_dir)
        if job is None:
            key = canonical(job_dir)
            job = self.create_job({'id': 'cli-' + hashlib.sha1(key.encode()).hexdigest()[:16],
                                   'input_dir': str(Path(root).resolve()) if root else '',
                                   'options': options or {}, 'status': 'pending'}, job_dir)
        elif root is not None:
            self.update_job(job['id'], {'input_dir': str(Path(root).resolve()), 'options': options})
        return self.job_for_dir(job_dir)

    def update_job(self, job_id, changes, remove=()):
        # Read/modify/write under the writer lock; another process cannot overwrite newer fields.
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            for key in remove:
                job.pop(key, None)
            job.update(changes)
            self.conn.execute('UPDATE tasks SET status=?,data=?,updated=? WHERE id=?',
                              (job['status'], encode(job), time.time(), job_id))
        return self.get_job(job_id)

    def claim_next(self):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            row = self.conn.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            job = self.get_job(row[0])
            job.update(status='running', started=time.strftime('%F %T'))
            self.conn.execute('UPDATE tasks SET status=?,data=?,updated=? WHERE id=?',
                              ('running', encode(job), time.time(), job['id']))
        return job

    def save_progress(self, job_id, state):
        with self.conn:
            self.conn.execute('UPDATE tasks SET progress=?,status=?,updated=? WHERE id=?',
                              (encode(state), state['status'], time.time(), job_id))

    def progress(self, job_id):
        row = self.conn.execute('SELECT progress,status FROM tasks WHERE id=?', (job_id,)).fetchone()
        if row is None or row[0] is None:
            return {}
        state = json.loads(row[0])
        state['status'] = row[1]
        if row[1] in ('cancelled', 'failed'):
            for feature in state.get('features', {}).values():
                if feature['status'] in ('pending', 'running'):
                    feature['status'] = row[1]
        return state

    def snapshot(self, job_dir):
        return self.snapshot_version(job_dir)[0]

    def snapshot_version(self, job_dir):
        row = self.conn.execute('SELECT snapshot,snapshot_revision FROM tasks WHERE task_dir=?', (canonical(job_dir),)).fetchone()
        return (json.loads(row[0]), row[1]) if row and row[0] else (None, 0)

    def save_snapshot(self, job_dir, snapshot):
        job = self.ensure_task(job_dir, snapshot.get('input_dir'), snapshot['options'])
        with self.conn:
            self.conn.execute('UPDATE tasks SET snapshot=?,updated=?,snapshot_revision=snapshot_revision+1 WHERE id=?',
                              (encode(snapshot), time.time(), job['id']))

    def _replace_results(self, job_id, out):
        rows = [(job_id, n, str(row.get('相对路径', '')), str(row.get('建议标签', '')), encode(row))
                for n, row in enumerate(out.to_dict('records'))]
        self.conn.execute('DELETE FROM task_results WHERE task_id=?', (job_id,))
        self.conn.executemany('INSERT INTO task_results VALUES (?,?,?,?,?)', rows)
        self.conn.execute('UPDATE tasks SET result_columns=?,results_updated=? WHERE id=?',
                          (encode(list(out.columns)), time.time(), job_id))

    def save_results(self, job_dir, out, snapshot=None, expected_revision=None):
        job = self.job_for_dir(job_dir)
        if job is None:
            raise ValueError('任务尚未创建')
        # Snapshot and the complete result set become visible together, including an empty result set.
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            if expected_revision is not None:
                current = self.conn.execute('SELECT snapshot_revision FROM tasks WHERE id=?', (job['id'],)).fetchone()[0]
                if current != expected_revision:
                    return False  # A running pipeline published a newer checkpoint during refresh.
            if snapshot is not None and expected_revision is None:
                self.conn.execute('UPDATE tasks SET snapshot=?,updated=?,snapshot_revision=snapshot_revision+1 WHERE id=?',
                                  (encode(snapshot), time.time(), job['id']))
            self._replace_results(job['id'], out)
        return True

    def has_results(self, job_id):
        row = self.conn.execute('SELECT result_columns FROM tasks WHERE id=?', (job_id,)).fetchone()
        return row is not None and row[0] is not None

    def label_counts(self, job_id):
        return dict(self.conn.execute('SELECT label,count(*) FROM task_results WHERE task_id=? GROUP BY label', (job_id,)))

    def results(self, job_id):
        import pandas as pd
        # One read transaction guarantees columns and rows belong to the same checkpoint.
        with self.conn:
            self.conn.execute('BEGIN')
            row = self.conn.execute('SELECT result_columns FROM tasks WHERE id=?', (job_id,)).fetchone()
            columns = json.loads(row[0]) if row and row[0] else []
            rows = [json.loads(r[0]) for r in self.conn.execute(
                'SELECT data FROM task_results WHERE task_id=? ORDER BY ordinal', (job_id,))]
        return pd.DataFrame(rows, columns=columns)

    def add_deletions(self, job_id, paths):
        with self.conn:
            self.conn.executemany('INSERT OR IGNORE INTO deletions(task_id,path,deleted_at) VALUES (?,?,?)',
                                  [(job_id, str(p), time.strftime('%F %T')) for p in paths])

    def deletion_records(self):
        return self.conn.execute('SELECT path,deleted_at FROM deletions ORDER BY id').fetchall()

    def migrate_legacy(self, jobs_file, job_data_dir, manual_dir=None, deletion_log=None):
        """Idempotent import: never overwrite newer SQLite records or delete source files."""
        import pandas as pd
        marker = 'task-migration-v1:' + canonical(jobs_file)
        if self.conn.execute('SELECT 1 FROM settings WHERE key=?', (marker,)).fetchone():
            return {'already_imported': True}
        jobs_file, job_data_dir = Path(jobs_file), Path(job_data_dir)
        jobs = json.loads(jobs_file.read_text(encoding='utf-8')).get('jobs', []) if jobs_file.exists() else []
        entries = []
        for job in jobs:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', job['id']):
                raise ValueError('旧任务编号不合法')
            entries.append((dict(job), job_data_dir / job['id']))
        known = {canonical(d) for _, d in entries}
        folders = list(job_data_dir.glob('*'))
        if manual_dir is not None:
            folders += list(Path(manual_dir).glob('*'))
        for d in folders:
            if d.is_dir() and canonical(d) not in known and (d / 'snapshot.json').is_file():
                snap = json.loads((d / 'snapshot.json').read_text(encoding='utf-8'))
                state = json.loads((d / 'progress.json').read_text(encoding='utf-8')) if (d / 'progress.json').exists() else {}
                entries.append(({'id': 'cli-' + hashlib.sha1(canonical(d).encode()).hexdigest()[:16],
                                 'input_dir': snap.get('input_dir', state.get('input_dir', '')),
                                 'options': snap['options'], 'status': state.get('status', 'cancelled'),
                                 'created': time.strftime('%F %T', time.localtime(d.stat().st_mtime))}, d))
        imported = 0
        for job, d in entries:
            if self.job_for_dir(d) is not None:
                continue
            progress = json.loads((d / 'progress.json').read_text(encoding='utf-8')) if (d / 'progress.json').exists() else None
            snapshot = json.loads((d / 'snapshot.json').read_text(encoding='utf-8')) if (d / 'snapshot.json').exists() else None
            # Reports without a verifiable snapshot go through the legacy safety bridge on demand.
            out = pd.read_csv(d / 'results.csv').fillna('') if snapshot and (d / 'results.csv').exists() else None
            job.setdefault('created', time.strftime('%F %T'))
            with self.conn:
                self.conn.execute('INSERT INTO tasks(id,task_dir,status,created,updated,data,progress,snapshot) VALUES (?,?,?,?,?,?,?,?)',
                                  (job['id'], canonical(d), job['status'], job['created'], time.time(), encode(job),
                                   encode(progress) if progress is not None else None, encode(snapshot) if snapshot is not None else None))
                if out is not None:
                    self._replace_results(job['id'], out)
            imported += 1
        if deletion_log and Path(deletion_log).exists():
            records = [line.split('\t', 1) for line in Path(deletion_log).read_text(encoding='utf-8').splitlines() if '\t' in line]
            with self.conn:
                self.conn.executemany('INSERT OR IGNORE INTO deletions(path,deleted_at) VALUES (?,?)', [(p, ts) for ts, p in records])
        with self.conn:
            self.conn.execute('INSERT INTO settings(key,value) VALUES (?,?)', (marker, encode({'tasks': imported})))
        return {'tasks': imported}
