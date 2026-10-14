"""Durable batch queue and bounded file storage, independent of browser sessions."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MODELS = {'ocr': 'glm-ocr-0.9b', 'asr': 'sensevoice'}
TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled', 'interrupted'}
ITEM_TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}
MAX_FILES = 50
MAX_JOB_BYTES = 250 * 1024 * 1024
MAX_STORAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ITEM_RESULT_BYTES = 1024 * 1024
MAX_JOB_RESULT_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 128 * 1024 * 1024
EXTENSIONS = {'ocr': {'.pdf', '.png', '.jpg', '.jpeg', '.webp'},
              'asr': {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac'}}


class BatchError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def identifier(value: str) -> str:
    try: return uuid.UUID(value).hex
    except (ValueError, AttributeError): raise BatchError(404, '任务或文件不存在') from None


class BatchStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / 'batches'
        self.path = data_dir / 'batch-jobs.db'

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db: yield db
        finally: db.close()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL, language TEXT NOT NULL,
                    status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, submitted_at REAL);
                CREATE TABLE IF NOT EXISTS batch_items (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    job_id TEXT NOT NULL REFERENCES batch_jobs(id) ON DELETE CASCADE,
                    filename TEXT NOT NULL, extension TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    pages INTEGER NOT NULL DEFAULT 0, completed_pages INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    warning TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, started_at REAL, finished_at REAL);
                CREATE INDEX IF NOT EXISTS batch_items_queue ON batch_items(status, next_attempt, seq);
                CREATE INDEX IF NOT EXISTS batch_items_job ON batch_items(job_id, seq);
            ''')

    def directory(self, job_id: str) -> Path:
        path = self.root / identifier(job_id)
        if path.resolve().parent != self.root.resolve() or path.is_symlink():
            raise BatchError(400, '任务路径无效')
        return path

    def file_path(self, job_id: str, file_id: str) -> Path:
        return self.directory(job_id) / (identifier(file_id) + '.source')

    def create(self, job_id: str, name: str, kind: str, language: str):
        job_id = identifier(job_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM batch_jobs WHERE id=?', (job_id,)).fetchone()
            if old:
                if (old['name'], old['kind'], old['language']) != (name, kind, language):
                    raise BatchError(409, '相同提交编号已用于其他任务')
            else:
                if db.execute('SELECT count(*) FROM batch_jobs').fetchone()[0] >= 200:
                    raise BatchError(409, '任务历史已达 200 条，请下载结果并删除不再需要的任务')
                stamp = time.time()
                db.execute('INSERT INTO batch_jobs(id,name,kind,language,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                           (job_id, name, kind, language, 'draft', stamp, stamp))
        self.directory(job_id).mkdir(exist_ok=True)
        return self.get(job_id)

    def _job(self, db, job_id):
        row = db.execute('SELECT * FROM batch_jobs WHERE id=?', (identifier(job_id),)).fetchone()
        if row is None: raise BatchError(404, '任务不存在')
        return dict(row)

    def _summary(self, db, job):
        counts = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM batch_items WHERE job_id=? GROUP BY status', (job['id'],))}
        total = sum(counts.values())
        done = sum(counts.get(s, 0) for s in ITEM_TERMINAL)
        position = 0
        if job['status'] in {'queued', 'running', 'waiting'}:
            position = db.execute("SELECT count(*) FROM batch_jobs WHERE kind=? AND (submitted_at<? OR (submitted_at=? AND seq<=?)) AND status IN ('queued','running','waiting','cancelling')", (job['kind'], job['submitted_at'], job['submitted_at'], job['seq'])).fetchone()[0]
        return {**job, 'model': MODELS[job['kind']], 'counts': counts, 'total': total, 'completed': done,
                'progress': round(done * 100 / total) if total else 0, 'queue_position': position}

    def get(self, job_id, *, results=False):
        with self.db() as db:
            job = self._summary(db, self._job(db, job_id))
            columns = '*' if results else "seq,id,job_id,filename,extension,size,status,pages,completed_pages,stage,error,warning,request_id,attempts,started_at,finished_at,(result!='{}') AS has_result"
            job['items'] = [dict(r) for r in db.execute(f'SELECT {columns} FROM batch_items WHERE job_id=? ORDER BY seq', (job['id'],))]
        if results:
            for item in job['items']: item['result'] = json.loads(item['result'])
        return job

    def listing(self):
        with self.db() as db:
            jobs = [self._summary(db, dict(j)) for j in db.execute('SELECT * FROM batch_jobs ORDER BY seq DESC LIMIT 200').fetchall()]
            storage = db.execute('SELECT COALESCE(SUM(size),0) FROM batch_items').fetchone()[0]
        return {'data': jobs, 'storage_bytes': storage, 'storage_limit_bytes': MAX_STORAGE_BYTES,
                'limits': {'files': MAX_FILES, 'job_bytes': MAX_JOB_BYTES, 'pdf_pages': 50, 'audio_seconds': 1800},
                'workers': {'ocr': 1, 'asr': 1}}

    def reserve_file(self, job_id, file_id, filename, size):
        job_id, file_id = identifier(job_id), identifier(file_id)
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        filename = re.sub(r'[\x00-\x1f\x7f]', '', filename).strip()[:180]
        extension = Path(filename).suffix.lower()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, job_id)
            if job['status'] != 'draft': raise BatchError(409, '任务已提交或取消，不能再上传文件')
            if extension not in EXTENSIONS[job['kind']]: raise BatchError(400, '该任务不支持此文件格式')
            limit = (50 if job['kind'] == 'asr' else 40 if extension == '.pdf' else 10) * 1024 * 1024
            if not 0 < size <= limit: raise BatchError(413, f'该文件须在 1 字节至 {limit//1024//1024} MB 之间')
            old = db.execute('SELECT * FROM batch_items WHERE id=?', (file_id,)).fetchone()
            if old:
                if (old['job_id'], old['filename'], old['size']) == (job_id, filename, size) and old['status'] == 'ready':
                    return dict(old), False
                raise BatchError(409, '文件编号已使用，或该文件仍在上传')
            count, used = db.execute('SELECT count(*),COALESCE(SUM(size),0) FROM batch_items WHERE job_id=?', (job_id,)).fetchone()
            if count >= MAX_FILES or used + size > MAX_JOB_BYTES: raise BatchError(413, '每批最多 50 个文件、合计 250 MB')
            stored = db.execute('SELECT COALESCE(SUM(size),0) FROM batch_items').fetchone()[0]
            if stored + size > MAX_STORAGE_BYTES or shutil.disk_usage(self.root).free < size + 150*1024*1024:
                raise BatchError(507, '批量任务存储空间不足，请删除不再需要的任务')
            db.execute('INSERT INTO batch_items(id,job_id,filename,extension,size,status) VALUES(?,?,?,?,?,?)',
                       (file_id, job_id, filename, extension, size, 'uploading'))
            return dict(db.execute('SELECT * FROM batch_items WHERE id=?', (file_id,)).fetchone()), True

    def finish_upload(self, job_id, file_id, digest):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._job(db, job_id)['status'] != 'draft': raise BatchError(409, '任务已取消')
            db.execute("UPDATE batch_items SET status='ready',sha256=? WHERE job_id=? AND id=? AND status='uploading'", (digest, identifier(job_id), identifier(file_id)))

    def abandon_upload(self, job_id, file_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            removed = db.execute("DELETE FROM batch_items WHERE id=? AND job_id=? AND status='uploading'", (identifier(file_id), identifier(job_id))).rowcount
            # An upload may have committed before its HTTP handler was cancelled.
            # Do not remove a ready file, or race a retry using the same UUID.
            if removed: self.file_path(job_id, file_id).unlink(missing_ok=True)

    def submit(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, job_id)
            if job['submitted_at'] is not None: return self._summary(db, job)
            if job['status'] != 'draft': raise BatchError(409, '任务已取消')
            counts = dict(db.execute('SELECT status,count(*) FROM batch_items WHERE job_id=? GROUP BY status', (job['id'],)))
            if not counts.get('ready') or counts.get('uploading'): raise BatchError(409, '请先完成至少一个文件的上传')
            if db.execute("SELECT count(*) FROM batch_jobs WHERE status IN ('queued','running','waiting','cancelling')").fetchone()[0] >= 20:
                raise BatchError(429, '最多同时保留 20 个待处理批次，请稍后提交')
            db.execute("UPDATE batch_items SET status='queued' WHERE job_id=? AND status='ready'", (job['id'],))
            db.execute("UPDATE batch_jobs SET status='queued',submitted_at=?,updated_at=? WHERE id=?", (time.time(), time.time(), job['id']))
        return self.get(job_id)

    def _refresh(self, db, job_id):
        job = self._job(db, job_id)
        states = [r[0] for r in db.execute('SELECT status FROM batch_items WHERE job_id=?', (job_id,))]
        if job['cancel_requested']:
            status = 'cancelling' if 'running' in states else 'interrupted' if 'interrupted' in states else 'cancelled'
        elif 'running' in states: status = 'running'
        elif 'queued' in states: status = 'queued'
        elif 'waiting' in states: status = 'waiting'
        elif states and all(s == 'succeeded' for s in states): status = 'succeeded'
        elif any(s == 'succeeded' for s in states): status = 'partial'
        elif 'interrupted' in states: status = 'interrupted'
        else: status = 'failed'
        db.execute('UPDATE batch_jobs SET status=?,updated_at=? WHERE id=?', (status, time.time(), job_id))

    def cancel(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, job_id)
            if job['status'] in TERMINAL: return self._summary(db, job)
            db.execute('UPDATE batch_jobs SET cancel_requested=1 WHERE id=?', (job['id'],))
            db.execute("UPDATE batch_items SET status='cancelled',stage='已取消',finished_at=? WHERE job_id=? AND status IN ('ready','queued','waiting')", (time.time(), job['id']))
            self._refresh(db, job['id'])
        return self.get(job_id)

    def cancelled(self, job_id):
        with self.db() as db: return bool(self._job(db, job_id)['cancel_requested'])

    def claim(self, kind):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Only one worker per kind, even if callers race to claim.
            if db.execute("SELECT 1 FROM batch_items i JOIN batch_jobs j ON j.id=i.job_id WHERE j.kind=? AND i.status='running'", (kind,)).fetchone(): return None
            row = db.execute("SELECT i.* FROM batch_items i JOIN batch_jobs j ON j.id=i.job_id WHERE j.kind=? AND j.cancel_requested=0 AND j.submitted_at IS NOT NULL AND i.status IN ('queued','waiting') AND i.next_attempt<=? ORDER BY j.submitted_at,j.seq,i.seq LIMIT 1", (kind, time.time())).fetchone()
            if row is None: return None
            item = dict(row)
            db.execute("UPDATE batch_items SET status='running',stage='准备处理',started_at=COALESCE(started_at,?),attempts=attempts+1 WHERE id=?", (time.time(), item['id']))
            self._refresh(db, item['job_id'])
            return {**item, 'job': self._job(db, item['job_id'])}

    def progress(self, item, *, pages=None, result=None, stage=None, warning=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            updates, values = [], []
            if pages is not None:
                other = db.execute('SELECT COALESCE(SUM(pages),0) FROM batch_items WHERE job_id=? AND id!=?', (item['job_id'], item['id'])).fetchone()[0]
                if other + pages > 200: raise BatchError(400, '每批 OCR 合计最多 200 页，请拆分批次')
                updates.append('pages=?'); values.append(pages)
            if result is not None:
                text = json.dumps(result, ensure_ascii=False)
                size = len(text.encode())
                if size > MAX_ITEM_RESULT_BYTES: raise BatchError(400, '单文件输出超过 1 MB，请拆分文件')
                job_size, total_size = db.execute('SELECT COALESCE(SUM(CASE WHEN job_id=? THEN length(CAST(result AS BLOB)) ELSE 0 END),0),COALESCE(SUM(length(CAST(result AS BLOB))),0) FROM batch_items WHERE id!=?', (item['job_id'], item['id'])).fetchone()
                if job_size + size > MAX_JOB_RESULT_BYTES: raise BatchError(400, '该批结果超过 8 MB，请拆分批次；已有结果可下载')
                if total_size + size > MAX_RESULT_BYTES: raise BatchError(507, '结果存储已满，请下载并删除历史任务后重新提交')
                updates += ['result=?', 'completed_pages=?']; values += [text, len(result.get('pages', []))]
            if stage is not None: updates.append('stage=?'); values.append(stage)
            if warning is not None: updates.append('warning=?'); values.append(warning)
            if updates: db.execute(f"UPDATE batch_items SET {','.join(updates)} WHERE id=? AND status='running'", (*values, item['id']))

    def request_id(self, item, request_id):
        with self.db() as db: db.execute('UPDATE batch_items SET request_id=? WHERE id=?', (request_id, item['id']))

    def finish(self, item, status, error=''):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, item['job_id'])
            # A cancellation request cannot establish the outcome of an already
            # submitted inference. Preserve unknown/failure evidence.
            if job['cancel_requested'] and status not in {'interrupted','failed'}: status = 'cancelled'
            db.execute("UPDATE batch_items SET status=?,stage=?,error=?,finished_at=? WHERE id=? AND status='running'",
                       (status, '已完成' if status == 'succeeded' else '已结束', error[:500], time.time(), item['id']))
            self._refresh(db, item['job_id'])

    def defer(self, item, reason, delay):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, item['job_id'])
            state = 'cancelled' if job['cancel_requested'] else 'waiting'
            db.execute("UPDATE batch_items SET status=?,stage=?,next_attempt=? WHERE id=? AND status='running'", (state, reason[:300], time.time()+delay, item['id']))
            self._refresh(db, item['job_id'])

    def recover(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            interrupted = db.execute("SELECT DISTINCT job_id FROM batch_items WHERE status='running'").fetchall()
            db.execute("UPDATE batch_items SET status='interrupted',error='管理服务重启，正在处理的文件结果未知，未自动重试；请核查后重新提交',stage='处理被中断',finished_at=? WHERE status='running'", (time.time(),))
            uploads = db.execute("SELECT id,job_id FROM batch_items WHERE status='uploading'").fetchall()
            db.execute("DELETE FROM batch_items WHERE status='uploading'")
            for row in interrupted: self._refresh(db, row['job_id'])
        for row in uploads: self.file_path(row['job_id'], row['id']).unlink(missing_ok=True)
        for path in self.root.glob('*/*.tmp'): path.unlink(missing_ok=True)

    def delete(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db, job_id)
            if job['status'] not in TERMINAL | {'draft'} or db.execute("SELECT 1 FROM batch_items WHERE job_id=? AND status IN ('uploading','running')", (job['id'],)).fetchone():
                raise BatchError(409, '请先取消任务，并等待上传或当前文件结束后再删除')
            directory = self.directory(job['id'])  # Resolves and confines deletion to the UUID job directory.
            if directory.exists(): shutil.rmtree(directory)
            db.execute('DELETE FROM batch_jobs WHERE id=?', (job['id'],))
        return {'deleted': True, 'id': job['id']}
