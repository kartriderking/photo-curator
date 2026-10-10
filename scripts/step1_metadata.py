# -*- coding: utf-8 -*-
"""步骤1：exiftool 批量提取拍摄时间/GPS/机型/尺寸，有 GPS 的离线逆地理解析出城市。"""
import json
import re
import subprocess
import time
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from common import CACHE_DIR, EXIFTOOL, setup_logging, scan_files, load_cache, save_cache, merge_progress

TAGS = ["-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSLongitude",
        "-Make", "-Model", "-ImageWidth", "-ImageHeight", "-Duration"]
CHUNK = 400

_log = None
_geo = None


def _parse_dt(s):
    if not isinstance(s, str) or not s.strip():
        return None
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s.strip())  # 去时区后缀
    s = s.split(".")[0]                              # 去毫秒
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _geocoder():
    """离线逆地理（首次初始化较慢，只做一次）。"""
    global _geo
    if _geo is None:
        import reverse_geocoder
        _geo = reverse_geocoder.RGeocoder(mode=2)
    return _geo


def extract_exif(paths):
    """调 exiftool 分批提取，返回 {path: dict}。"""
    out = {}
    for i in range(0, len(paths), CHUNK):
        batch = paths[i:i + CHUNK]
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("\n".join(batch))
            argfile = f.name
        try:
            cmd = [EXIFTOOL, "-charset", "filename=UTF8", "-j", "-n", "-@", argfile] + TAGS
            r = subprocess.run(cmd, capture_output=True, timeout=1800)
            text = r.stdout.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = []
            if isinstance(data, dict):
                data = [data]
            for d in data:
                key = d.pop("SourceFile", "").replace("\\", "/")
                out[key] = d
        finally:
            Path(argfile).unlink(missing_ok=True)
        print(f"\rexiftool {min(i + CHUNK, len(paths))}/{len(paths)}", end="", flush=True)
    print()
    return out


def run(input_dir: str):
    global _log
    _log = setup_logging("step1")
    df = scan_files(input_dir)
    old = load_cache("metadata.parquet")
    known = set(old["key"]) if not old.empty else set()
    todo = df[~df["key"].isin(known)]
    _log.info("共 %d 个文件，缓存命中 %d，待处理 %d", len(df), len(known), len(todo))
    # 全库文件总数供进度卡做分母（增量批次写盘时元数据行数尚不完整）
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"total": int(len(df)), "ts": time.time()}]).to_parquet(
        CACHE_DIR / "scan_totals.parquet", index=False)

    if len(todo):
        # 分批提取并逐批落盘：进度卡实时爬升，中途中止只丢当前批
        for ci in range(0, len(todo), CHUNK):
            batch = todo.iloc[ci:ci + CHUNK]
            exifs = extract_exif(list(batch["path"]))
            print(f"\r元数据 全局进度 {ci + len(batch)}/{len(todo)}", end="", flush=True)
            rows = []
            for _, r in batch.iterrows():
                e = exifs.get(r["path"].replace("\\", "/"), {})
                rows.append({
                    "path": r["path"], "rel": r["rel"], "kind": r["kind"], "key": r["key"],
                    "dt": _parse_dt(e.get("DateTimeOriginal")) or _parse_dt(e.get("CreateDate")),
                    "gps_lat": e.get("GPSLatitude"), "gps_lon": e.get("GPSLongitude"),
                    "camera": " ".join(str(x) for x in (e.get("Make"), e.get("Model")) if x),
                    "width": e.get("ImageWidth"), "height": e.get("ImageHeight"),
                    "duration": e.get("Duration"), "city": None,
                })
            save_cache(merge_progress(load_cache("metadata.parquet"), pd.DataFrame(rows)),
                       "metadata.parquet")

    meta = load_cache("metadata.parquet")
    if "city" not in meta.columns:
        meta["city"] = None
    has_gps = meta["gps_lat"].notna() & meta["gps_lon"].notna()
    _log.info("有 GPS 的文件：%d/%d", int(has_gps.sum()), len(meta))
    if has_gps.any():
        todo_geo = meta[has_gps & (meta["city"].isna() | meta["city"].eq(None))]
        if len(todo_geo):
            try:
                geo = _geocoder()
                coords = list(zip(todo_geo["gps_lat"].astype(float), todo_geo["gps_lon"].astype(float)))
                res = geo.query(coords)
                meta.loc[todo_geo.index, "city"] = [x["name"] for x in res]
                save_cache(meta, "metadata.parquet")
            except Exception as ex:
                _log.warning("逆地理解析失败（不影响主流程，城市列留空）：%s", ex)
    _log.info("步骤1完成 → cache/metadata.parquet")
    return meta


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    a = ap.parse_args()
    run(a.input_dir)
