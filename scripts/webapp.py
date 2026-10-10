# -*- coding: utf-8 -*-
"""photo-curator 本地 Web 工作台：任务提交/进度/结果/媒体预览。仅监听 127.0.0.1。"""
import hashlib
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from common import CACHE_DIR, OUTPUT_DIR, PROJECT_ROOT, IMAGE_EXTS, VIDEO_EXTS
from feature_config import normalize_options
from task_store import TaskStore

WEB_DIR = Path(__file__).resolve().parent / "web"
JOBS_FILE = PROJECT_ROOT / "jobs.json"
DB_PATH = CACHE_DIR / 'curator.sqlite3'
JOB_DATA_DIR = PROJECT_ROOT / "output" / "jobs"
PREVIEW_DIR = CACHE_DIR / "previews"
PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")

app = FastAPI(title="photo-curator")
_lock = threading.Lock()
_proc = None          # 当前运行中的子进程
_thread = None


# ---------- 任务持久化与执行 ----------

def _load_jobs() -> dict:
    with TaskStore(path=DB_PATH) as store:
        return {'jobs': store.list_jobs()}


def _get_job(job_id):
    with TaskStore(path=DB_PATH) as store:
        job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, '任务不存在')
    return job


def _run_loop():
    """串行执行队列中的任务（GPU 同时只跑一个管线）。"""
    global _proc, _thread
    while True:
        with _lock:
            with TaskStore(path=DB_PATH) as store:
                nxt = store.claim_next()
            if not nxt:
                _thread = None
                return
        task_dir = Path(nxt['task_dir'])
        task_dir.mkdir(parents=True, exist_ok=True)
        log_f = open(task_dir / "run.log", "a", encoding="utf-8", buffering=1)
        cmd = [PYTHON, str(PROJECT_ROOT / "scripts" / "run_all.py"),
               "--input-dir", nxt["input_dir"]]
        options = normalize_options(nxt.get("options") or nxt, legacy="options" not in nxt)
        cmd += ["--features", ",".join(options["features"]), "--job-dir", str(task_dir), '--db-path', str(DB_PATH),
                "--geo-mode", options["geo_mode"], "--vlm-scope", options["vlm_scope"],
                "--vlm-model", options["vlm_model"], "--scenic-threshold", str(options["scenic_threshold"]),
                "--similar-threshold", str(options["similar_threshold"]), "--dup-hamming", str(options["dup_hamming"]),
                "--trip-gap-hours", str(options["trip_gap_hours"])]
        if options["include_videos"]:
            cmd.append("--include-videos")
        if options["force"]:
            cmd.append("--force")
        if not options["reuse_legacy_vlm"]:
            cmd.append("--no-legacy-vlm")
        try:
            _proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
            with _lock:
                with TaskStore(path=DB_PATH) as store:
                    current = store.update_job(nxt['id'], {'pid': _proc.pid})
                # A cancel may arrive while the subprocess is being started.
                if current.get("cancel"):
                    _proc.terminate()
            code = _proc.wait()
        except Exception as ex:
            log_f.write(f"任务启动失败: {ex}\n")
            code = 1
        finally:
            log_f.close()
        with _lock:
            with TaskStore(path=DB_PATH) as store:
                j = store.get_job(nxt['id'])
                state = store.progress(j['id'])
                status = 'cancelled' if j.get('cancel') else (state.get('status', 'done') if code == 0 else 'failed')
                store.update_job(j['id'], {'status': status, 'finished': time.strftime('%F %T')})
        _proc = None


def _ensure_worker():
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run_loop, daemon=True)
            _thread.start()


# ---------- API ----------

@app.on_event("startup")
def startup():
    with _lock:
        with TaskStore(path=DB_PATH) as store:
            store.migrate_legacy(JOBS_FILE, JOB_DATA_DIR, OUTPUT_DIR / 'manual', OUTPUT_DIR / 'deletions.log')
            for j in store.list_jobs():
                if j['status'] == 'running':
                    store.update_job(j['id'], {'status': 'cancelled', 'interrupted': True})
    _ensure_worker()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get('/api/health')
def health():
    return {'app': 'photo-curator', 'storage': 'sqlite'}


@app.get('/assets/{name}')
def frontend_asset(name: str):
    if name not in {'app.js', 'style.css'}:
        raise HTTPException(404, '文件不存在')
    return FileResponse(WEB_DIR / name, headers={'Cache-Control': 'no-store'})


@app.get('/api/folders')
def browse_folders(path: str = ''):
    """Read one folder level for the local folder picker; never scan image contents."""
    if not path:
        roots = [Path(f'{letter}:/') for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ']
        return {'path': '', 'parent': None, 'folders': [{'name': str(p), 'path': str(p)} for p in roots if p.is_dir()]}
    p = Path(path).resolve()
    if not p.is_dir():
        raise HTTPException(400, '文件夹不存在，请检查路径')
    try:
        folders = sorted((f for f in p.iterdir() if f.is_dir() and not f.name.startswith(('.', '$'))), key=lambda f: f.name.casefold())
        return {'path': str(p), 'parent': str(p.parent) if p.parent != p else '',
                'folders': [{'name': f.name, 'path': str(f)} for f in folders[:500]], 'truncated': len(folders) > 500}
    except OSError:
        raise HTTPException(403, '这个文件夹无法访问，请选择其他位置')


@app.post("/api/jobs")
def create_job(body: dict):
    input_dir = str(body.get("input_dir", "")).strip().strip('"')
    p = Path(input_dir)
    if not input_dir or not p.is_dir():
        raise HTTPException(400, f"目录不存在: {input_dir}")
    try:
        options = normalize_options(body)
    except (ValueError, TypeError, OverflowError) as ex:
        raise HTTPException(400, str(ex))
    job = {
        "id": uuid.uuid4().hex[:8],
        "input_dir": str(p.resolve()),
        "skip_vlm": bool(body.get("skip_vlm", False)),
        "scenic_threshold": body.get("scenic_threshold"),
        "options": options,
        "status": "queued",
        "created": time.strftime("%F %T"),
    }
    with _lock:
        with TaskStore(path=DB_PATH) as store:
            job = store.create_job(job, JOB_DATA_DIR / job['id'])
    _ensure_worker()
    return job


@app.get("/api/jobs")
def list_jobs():
    with _lock:
        jobs = _load_jobs()
    running = next((j for j in jobs["jobs"] if j["status"] == "running"), None)
    selected = running or next(iter(jobs['jobs']), None)
    p = {}
    if selected:
        with TaskStore(path=DB_PATH) as store:
            p = store.progress(selected['id'])
        p['job_id'] = selected['id']
    return_progress_log = Path(selected['task_dir']) / 'run.log' if selected else None
    if return_progress_log and return_progress_log.exists():
        with open(return_progress_log, 'rb') as f:
            f.seek(max(0, return_progress_log.stat().st_size - 2500))
            p['log_tail'] = f.read().decode('utf-8', errors='replace').replace('\r', '\n').splitlines()[-12:]
    if not selected:
        p['log_tail'] = ['已有任务和报告已保留。选择功能后提交新任务，自动复用可验证的缓存。']
    return {"jobs": jobs["jobs"], "progress": p,
            "running_pid": _proc.pid if _proc else None}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with _lock:
        with TaskStore(path=DB_PATH) as store:
            j = store.get_job(job_id)
            if not j:
                raise HTTPException(404, "任务不存在")
            if j['status'] == 'queued':
                store.update_job(job_id, {'status': 'cancelled'})
            elif j['status'] == 'running':
                store.update_job(job_id, {'cancel': True})
                if _proc:
                    _proc.terminate()   # 管线全部断点续跑，可安全终止
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    with _lock:
        with TaskStore(path=DB_PATH) as store:
            j = store.get_job(job_id)
            if not j:
                raise HTTPException(404, "任务不存在")
            if j['status'] in ('running', 'queued'):
                raise HTTPException(409, '任务已在运行或排队中')
            store.update_job(job_id, {'status': 'queued', 'options': j.get('options') or normalize_options(j, legacy=True)},
                             remove=('cancel', 'finished', 'interrupted'))
    _ensure_worker()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/results")
def job_results(job_id: str, label: str = "", q: str = "", refresh: int = 0):
    j = _get_job(job_id)
    with TaskStore(path=DB_PATH) as store:
        task_dir = Path(j['task_dir'])
        if not store.has_results(job_id) or refresh:
            from pipeline import refresh_results, prepare_legacy_snapshot
            if store.snapshot(task_dir) is None:
                if j['status'] in ('queued', 'running'):
                    return {'total': 0, 'rows': []}
                prepare_legacy_snapshot(j, task_dir, db=store.db)
            refresh_results(task_dir, db=store.db)
        df = store.results(job_id)
    if label:
        df = df[df["建议标签"].str.startswith(label, na=False)]
    if q:
        mask = df.astype(str).apply(lambda r: r.str.contains(q, case=False, na=False, regex=False).any(), axis=1)
        df = df[mask]
    df = df.fillna("")
    rows = df.head(20000).to_dict("records")   # 本地服务，放开行数上限，分页交给前端
    if j:
        base = j["input_dir"].rstrip("\\/") + "\\"
        for r in rows:
            r["__abs"] = base + str(r.get("相对路径", "")).replace("/", "\\")
            best = str(r.get("同组最优", "") or "")
            r["__abs_best"] = base + best.replace("/", "\\") if best else ""
    return JSONResponse({"total": len(df), "rows": rows})


@app.get('/api/jobs/{job_id}/export')
def download_report(job_id: str):
    _get_job(job_id)
    with TaskStore(path=DB_PATH) as store:
        if not store.has_results(job_id):
            raise HTTPException(404, '还没有结果，完成任一功能后可导出')
        out = store.results(job_id)
    return Response(content='\ufeff' + out.to_csv(index=False), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="photos-{job_id}.csv"'})


@app.post("/api/delete")
def delete_files(body: dict):
    """把文件移入回收站（绝不永久删除）。
    安全校验：路径必须位于该任务 input_dir 之下，且是媒体文件。"""
    from send2trash import send2trash
    job_id = body.get("job_id", "")
    paths = body.get("paths", [])
    with _lock:
        jobs = _load_jobs()
    j = next((x for x in jobs["jobs"] if x["id"] == job_id), None)
    if not j:
        raise HTTPException(404, "任务不存在")
    base = Path(j["input_dir"]).resolve()
    results, deleted = [], []
    for p in paths[:2000]:   # 单次上限，防误操作批量过大
        try:
            f = Path(p)
            rf = f.resolve()
            if not rf.is_file():
                results.append({"path": p, "ok": False, "msg": "文件不存在"})
                continue
            if base not in rf.parents:
                results.append({"path": p, "ok": False, "msg": "不在任务目录内，拒绝删除"})
                continue
            if rf.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
                results.append({"path": p, "ok": False, "msg": "非媒体文件，拒绝删除"})
                continue
            send2trash(str(rf))   # 进回收站
            deleted.append(str(rf))
            results.append({"path": p, "ok": True})
        except Exception as ex:
            results.append({"path": p, "ok": False, "msg": str(ex)[:100]})
    if deleted:
        with TaskStore(path=DB_PATH) as store:
            store.add_deletions(job_id, deleted)
    return {"deleted": len(deleted), "results": results}


@app.get("/api/orphans")
def orphans(job_id: str):
    """从删除日志找 Live Photo/同名配对中"只删了一边"的情况，
    返回幸存的另一半（含数据库中的标签信息），供用户预览后决定是否补删。"""
    with _lock:
        jobs = _load_jobs()
    j = next((x for x in jobs["jobs"] if x["id"] == job_id), None)
    if not j:
        raise HTTPException(404, "任务不存在")
    base = Path(j["input_dir"]).resolve()

    deleted, deleted_ts = [], {}
    with TaskStore(path=DB_PATH) as store:
        for p, ts in store.deletion_records():
            try:
                if Path(p).resolve().parent == base or base in Path(p).resolve().parents:
                    deleted.append(p)
                    deleted_ts[p] = ts
            except OSError:
                continue
    media_exts = IMAGE_EXTS | VIDEO_EXTS
    photo_exts = {".heic", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

    # SQLite 中的标签/景点信息，按绝对路径索引
    info_by_abs = {}
    with TaskStore(path=DB_PATH) as store:
        df = store.results(job_id).fillna('')
        b = j["input_dir"].rstrip("\\/") + "\\"
        for _, r in df.iterrows():
            info_by_abs[(b + str(r.get("相对路径", "")).replace("/", "\\")).lower()] = r.to_dict()

    orphans, seen = [], set()
    checked_dirs = {}
    for p in dict.fromkeys(deleted):   # 去重保序
        f = Path(p)
        if f.suffix.lower() not in media_exts:
            continue
        if f.exists():
            continue   # 已从回收站还原：两边都在，不算孤儿
        d = str(f.parent)
        if d not in checked_dirs:
            checked_dirs[d] = [q for q in f.parent.iterdir()] if f.parent.is_dir() else []
        stem = f.stem.lower()
        my_type = "video" if f.suffix.lower() in VIDEO_EXTS else "photo"
        want_type = "photo" if my_type == "video" else "video"
        partner = next((q for q in checked_dirs[d]
                        if q.stem.lower() == stem and q != f
                        and (q.suffix.lower() in photo_exts) == (want_type == "photo")
                        and q.suffix.lower() in media_exts), None)
        if partner is None:
            continue   # 另一半也不在（两边都删了）→ 无可操作
        pkey = str(partner.resolve())
        if pkey in seen:
            continue
        seen.add(pkey)
        info = info_by_abs.get(pkey.lower(), {})
        row = {"相对路径": partner.name, "类型": want_type,
               "建议标签": info.get("建议标签", ""), "城市(GPS)": info.get("城市(GPS)", ""),
               "VLM景点": info.get("VLM景点", ""), "拍摄时间": info.get("拍摄时间", ""),
               "被删的一半": f.name, "删除时间": deleted_ts.get(p, ""),
               "__abs": pkey, "__abs_best": ""}
        orphans.append(row)
    return {"total": len(orphans), "rows": orphans}


@app.get("/api/labels")
def labels(job_id: str):
    _get_job(job_id)
    with TaskStore(path=DB_PATH) as store:
        return {'labels': store.label_counts(job_id)}


# ---------- 媒体预览 ----------

def _check_media(path: str) -> Path:
    from urllib.parse import unquote
    # 兼容旧前端的双重编码：FastAPI 已解一层，若仍是编码形态再解一层，以"文件真实存在"为准
    if "%" in path:
        once = unquote(path)
        if Path(once).is_file():
            path = once
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
        raise HTTPException(400, "非法媒体路径")
    return p


def _cache_name(path: Path, tag: str) -> Path:
    h = hashlib.sha1(f"{path}|{path.stat().st_mtime}".encode()).hexdigest()[:16]
    return PREVIEW_DIR / f"{tag}_{h}.jpg"


@app.get("/media/photo")
def media_photo(path: str):
    """照片预览：浏览器原生支持的直接给；HEIC/超大图转 ≤1600px JPEG 并缓存。"""
    p = _check_media(path)
    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and p.stat().st_size < 8_000_000:
        return FileResponse(p, headers={"Cache-Control": "max-age=3600"})
    out = _cache_name(p, "img")
    if not out.exists():
        import io
        from PIL import Image
        from common import load_image
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        img = load_image(str(p))
        w, h = img.size
        if max(w, h) > 1600:
            s = 1600 / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        img.save(out, "JPEG", quality=85)
    return FileResponse(out, headers={"Cache-Control": "max-age=86400"})


@app.get("/media/video")
def media_video(path: str, rangeskip: int = Query(0, alias="_")):
    """视频原文件（浏览器装了 HEVC 解码器就能播；Live Photo 的 MOV 常可播）。"""
    p = _check_media(path)
    return FileResponse(p, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})


@app.get("/media/frame")
def media_frame(path: str, ts: float = 0.5):
    """视频抽帧兜底预览（HEVC 播不了时用）。"""
    p = _check_media(path)
    out = _cache_name(p, "frame")
    if not out.exists():
        import subprocess
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ffmpeg", "-y", "-ss", str(ts), "-i", str(p),
                            "-frames:v", "1", "-vf", "scale='min(1600,iw)':-2", "-q:v", "4",
                            str(out)], capture_output=True, timeout=120)
        if r.returncode != 0 or not out.exists():
            raise HTTPException(500, "抽帧失败")
    return FileResponse(out, headers={"Cache-Control": "max-age=86400"})


@app.get("/media/transcode")
def media_transcode(path: str):
    """视频转 mp4（H.264）后播放，按需转换并缓存。长视频首次会等较久。"""
    p = _check_media(path)
    h = hashlib.sha1(f"{p}|{p.stat().st_mtime}".encode()).hexdigest()[:16]
    out = PREVIEW_DIR / f"mp4_{h}.mp4"
    if not out.exists():
        import subprocess
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        r = subprocess.run(["ffmpeg", "-y", "-i", str(p), "-c:v", "libx264", "-preset", "fast",
                            "-crf", "26", "-vf", "scale='min(1280,iw)':-2",
                            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                            str(tmp)], capture_output=True, timeout=1800)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "转码失败")
        tmp.rename(out)
    return FileResponse(out, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8618, log_level="warning")
