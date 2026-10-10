# -*- coding: utf-8 -*-
"""共用配置与工具函数：路径、扫描、缓存续跑、读图。"""
import os
import shutil
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
TOOLS_DIR = PROJECT_ROOT / "tools"
EXIFTOOL = os.environ.get('PHOTO_CURATOR_EXIFTOOL') or shutil.which('exiftool') or str(TOOLS_DIR / 'exiftool.exe')
MODELS_DIR = Path(os.environ.get('PHOTO_CURATOR_MODELS_DIR', PROJECT_ROOT / 'models'))

# 下载源和代理由用户配置，不内置个人环境。
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

CONFIG = {
    "input_dir": "",                       # 待整理图库根目录（必填）
    "ollama_url": os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434'),
    "vlm_model": "qwen3-vl:8b",            # 备选: qwen2.5vl:7b
    "clip_model": "ViT-B-32",
    "clip_pretrained": "laion2b_s34b_b79k",
    "clip_batch": 64,
    "scenic_threshold": 0.35,              # CLIP 风景分 >= 该值才送 VLM 精析
    "sharp_min": 60.0,                     # Laplacian 方差低于此值视为模糊废片候选
    "dup_hamming": 4,                      # pHash 汉明距离 <= 该值视为重复图
    "similar_threshold": 0.95,             # CLIP 向量余弦 >= 该值视为"可删-相似"（近同构图）
    "trip_gap_hours": 36,                  # 旅行时间聚类：间隔超过则断开成新旅程
    "max_frames_per_video": 5,             # 每个视频最多抽多少关键帧
    "frame_interval_sec": 2,               # 视频抽帧间隔（秒）
    "frames_dir": os.environ.get('PHOTO_CURATOR_FRAMES_DIR', str(CACHE_DIR / 'frames')),
    "clip_cache_dir": str(MODELS_DIR / 'open_clip'),
    "clip_checkpoint": os.environ.get('PHOTO_CURATOR_CLIP_CHECKPOINT', ''),
    "vlm_max_side": 1280,                  # 送 VLM 前图片最长边缩放
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".wmv", ".ts"}

# 这些 CLIP 类别不需要送 VLM（注定是可删项）
VLM_EXCLUDE = {"screenshot", "document", "meme"}

# CLIP 零样本类别（英文 prompt 对图像检索更稳；键名会作为 CSV 列名）
CLIP_CATEGORIES = {
    "screenshot": ["a screenshot of a smartphone or computer screen",
                   "a chat app screenshot with text message bubbles"],
    "document":   ["a photo of a paper document, receipt or book page"],
    "food":       ["a photo of food, a meal or a dish"],
    "selfie":     ["a close-up selfie of a person's face"],
    "people":     ["a photo of one or more people posing"],
    "scenic":     ["a scenic landscape or famous tourist attraction",
                   "a beautiful natural scenery at a travel destination"],
    "city":       ["a city street view or building exterior", "urban architecture"],
    "interior":   ["an indoor room or interior space"],
    "animal":     ["a photo of an animal or pet"],
    "plant":      ["a photo of plants, flowers or trees close up"],
    "night":      ["a night sky with stars, fireworks or city night view"],
    "meme":       ["an internet meme image with large caption text"],
    "vehicle":    ["a photo of cars, trains, ships or aircraft"],
    "blurry":     ["an accidental blurry photo of the ground, ceiling or a pocket shot"],
    "daily":      ["an everyday casual photo of daily life"],
}


def setup_logging(name: str) -> logging.Logger:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(CACHE_DIR / "pipeline.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


def file_key(rel_path: str, size: int, mtime: float) -> str:
    """断点续跑键：路径相对+大小+修改时间，任一变化视为新文件。"""
    import hashlib
    raw = f"{rel_path}|{size}|{int(mtime)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def scan_files(input_dir: str):
    """枚举图库下全部图片/视频，返回 DataFrame。"""
    import pandas as pd
    root = Path(input_dir).resolve()
    if not root.is_dir():
        raise SystemExit(f"输入目录不存在: {root}")
    skip = {CACHE_DIR.resolve(), OUTPUT_DIR.resolve()}
    rows = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
            continue
        if p.parent.resolve() in skip or CACHE_DIR.resolve() in p.resolve().parents:
            continue
        st = p.stat()
        ext = p.suffix.lower()
        rows.append({
            "path": str(p),
            "rel": str(p.relative_to(root)),
            "kind": "video" if ext in VIDEO_EXTS else "photo",
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit(f"目录里没有找到图片/视频: {root}")
    df["key"] = [file_key(r, s, m) for r, s, m in zip(df["rel"], df["size"], df["mtime"])]
    return df


def load_image(path: str):
    """读图为 RGB PIL.Image，兼容 HEIC/HEIF，自动按 EXIF 方向摆正。"""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    img = Image.open(path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    return img


def load_cache(name: str):
    """读缓存 parquet，不存在返回空 DataFrame。"""
    import pandas as pd
    f = CACHE_DIR / name
    if f.exists():
        try:
            return pd.read_parquet(f)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def save_cache(df, name: str):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE_DIR / name, index=False)


def merge_progress(base_df, new_df, key_col="key"):
    """按 key 合并：保留旧结果，只补充/替换新算出的行。"""
    import pandas as pd
    if base_df.empty:
        return new_df
    keep = base_df[~base_df[key_col].isin(set(new_df[key_col]))]
    return pd.concat([keep, new_df], ignore_index=True)
