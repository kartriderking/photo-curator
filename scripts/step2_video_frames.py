# -*- coding: utf-8 -*-
"""步骤2：视频按固定间隔抽关键帧（ffmpeg），帧交给步骤3/4统一打分。"""
import subprocess
from pathlib import Path

import pandas as pd

from common import CACHE_DIR, CONFIG, setup_logging, load_cache, save_cache

_log = None


def frames_root() -> Path:
    """抽帧目录：优先 CONFIG['frames_dir']（可指向 HDD），否则项目 cache。"""
    d = CONFIG.get("frames_dir")
    return Path(d) if d else CACHE_DIR / "frames"


def _extract(video_path: str, vkey: str) -> list:
    outdir = frames_root() / vkey
    outdir.mkdir(parents=True, exist_ok=True)
    interval = CONFIG["frame_interval_sec"]
    cmd = ["ffmpeg", "-y", "-i", video_path,
           "-vf", f"fps=1/{interval},scale='min(1280,iw)':-2", "-q:v", "4",
           "-frames:v", str(CONFIG["max_frames_per_video"]),
           str(outdir / "f_%05d.jpg")]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            ( _log or setup_logging("frames") ).warning("ffmpeg 失败 %s: %s", video_path,
                         r.stderr.decode("utf-8", errors="replace")[-200:])
    except subprocess.TimeoutExpired:
        ( _log or setup_logging("frames") ).warning("ffmpeg 超时 %s", video_path)
    return sorted(outdir.glob("f_*.jpg"))


def run(input_dir: str = None):
    global _log
    _log = setup_logging("step2")
    meta = load_cache("metadata.parquet")
    videos = meta[meta["kind"] == "video"] if not meta.empty else pd.DataFrame()
    if videos.empty:
        _log.info("没有视频，跳过抽帧")
        save_cache(pd.DataFrame(columns=["video_key", "video_path", "frame_path", "ts_sec"]),
                   "frames_manifest.parquet")
        return

    FRAMES_DIR = frames_root()
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    # 断点续跑：已有清单的视频直接复用，仅新视频逐个抽取并即时落盘
    old = load_cache("frames_manifest.parquet")
    done_keys = set(old["video_key"]) if not old.empty else set()
    rows = old.to_dict("records") if not old.empty else []
    todo_videos = [r for r in videos.itertuples() if r.key not in done_keys]
    for i, r in enumerate(todo_videos, 1):
        outdir = FRAMES_DIR / r.key
        frames = sorted(outdir.glob("f_*.jpg")) if outdir.is_dir() else []
        if not frames:
            frames = _extract(r.path, r.key)
        for idx, f in enumerate(frames):
            rows.append({"video_key": r.key, "video_path": r.path,
                         "frame_path": str(f), "ts_sec": idx * CONFIG["frame_interval_sec"]})
        save_cache(pd.DataFrame(rows), "frames_manifest.parquet")   # 每视频落盘，进度实时
        print(f"\r视频 {i}/{len(todo_videos)}", end="", flush=True)
    print()
    mf = pd.DataFrame(rows)
    save_cache(mf, "frames_manifest.parquet")
    _log.info("步骤2完成：共 %d 个视频（新抽 %d）→ %d 帧 → cache/frames_manifest.parquet",
              len(videos), len(todo_videos), len(mf))
    return mf


if __name__ == "__main__":
    run()
