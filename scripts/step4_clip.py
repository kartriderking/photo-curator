# -*- coding: utf-8 -*-
"""步骤4：OpenCLIP 零样本分类（截图/美食/风景/自拍…），附向量缓存供以后做"找相似"。"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from PIL import Image

from common import CACHE_DIR, CLIP_CATEGORIES, CONFIG, setup_logging, scan_files, \
    load_cache, save_cache, merge_progress, load_image

_log = None
EMB_FILE = CACHE_DIR / "clip_embeddings_fp16.npy"
EMB_KEYS = CACHE_DIR / "clip_embeddings_keys.json"


def _gather_files(input_dir: str):
    """图片 + 视频抽出的帧，统一进入打分队列。"""
    import hashlib
    files = scan_files(input_dir)
    files = files[files["kind"] == "photo"]
    frames = load_cache("frames_manifest.parquet")
    if not frames.empty:
        fkeys = [hashlib.sha1(f"{p}|{Path(p).stat().st_size}|{int(Path(p).stat().st_mtime)}".encode()).hexdigest()[:16]
                 for p in frames["frame_path"]]
        frames = frames.assign(path=frames["frame_path"], rel=frames["frame_path"], kind="frame", key=fkeys,
                               size=0, mtime=0)
        files = pd.concat([files, frames[["path", "rel", "kind", "size", "mtime", "key"]]], ignore_index=True)
    return files


class ClipScorer:
    def __init__(self):
        import open_clip
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        checkpoint = CONFIG.get('clip_checkpoint')
        if checkpoint and not Path(checkpoint).is_file():
            raise FileNotFoundError('指定的 CLIP 权重文件不存在')
        cache_dir = Path(CONFIG['clip_cache_dir'])
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            CONFIG["clip_model"], pretrained=checkpoint or CONFIG["clip_pretrained"], cache_dir=str(cache_dir))
        self.model = self.model.to(self.device).eval()
        if self.device == "cuda":
            self.model = self.model.half()
        self.tokenize = open_clip.get_tokenizer(CONFIG["clip_model"])
        with torch.no_grad():
            prompts = [p for ps in CLIP_CATEGORIES.values() for p in ps]
            tf = self.model.encode_text(self.tokenize(prompts).to(self.device)).float()
            tf = tf / tf.norm(dim=-1, keepdim=True)
        cats = [c for c, ps in CLIP_CATEGORIES.items() for _ in ps]
        emb = torch.stack([tf[[i for i, c in enumerate(cats) if c == cat]].mean(0)
                           for cat in CLIP_CATEGORIES])
        self.cat_emb = (emb / emb.norm(dim=-1, keepdim=True)).to(self.device)
        self.cat_names = list(CLIP_CATEGORIES.keys())

    @torch.no_grad()
    def encode(self, paths):
        from concurrent.futures import ThreadPoolExecutor
        def prepare(path):
            img = load_image(path)
            try:
                return self.preprocess(img)
            finally:
                img.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            x = torch.stack(list(pool.map(prepare, paths))).to(self.device)
        if self.device == "cuda":
            x = x.half()
        ef = self.model.encode_image(x).float()
        ef = ef / ef.norm(dim=-1, keepdim=True)
        probs = (ef @ self.cat_emb.T * 100.0).softmax(dim=-1)
        return probs.cpu().numpy(), ef.cpu().numpy().astype(np.float16)


def _load_emb_store():
    if EMB_FILE.exists() and EMB_KEYS.exists():
        return json.loads(EMB_KEYS.read_text(encoding="utf-8")), np.load(EMB_FILE)
    return [], np.zeros((0, 512), np.float16)


def _save_emb_store(keys, mat):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(EMB_FILE, mat)
    EMB_KEYS.write_text(json.dumps(keys), encoding="utf-8")


def run(input_dir: str):
    global _log
    _log = setup_logging("step4")
    files = _gather_files(input_dir)
    old = load_cache("clip_scores.parquet")
    known = set(old["key"]) if not old.empty else set()
    todo = files[~files["key"].isin(known)]
    _log.info("待 CLIP 打分：%d/%d（设备：%s）", len(todo), len(files),
              "CUDA" if torch.cuda.is_available() else "CPU（未检测到显卡！）")
    if not len(todo):
        _log.info("步骤4完成（无新增）")
        return old

    scorer = ClipScorer()
    bs = CONFIG["clip_batch"]
    rows, embs, failed = [], [], 0

    def flush_pending():
        """增量落盘（分数 + 向量），中止后从断点续跑。"""
        if not rows:
            return
        save_cache(merge_progress(load_cache("clip_scores.parquet"), pd.DataFrame(rows)),
                   "clip_scores.parquet")
        keys, mat = _load_emb_store()
        have = set(keys)
        add = [(k, e) for k, e in zip([r["key"] for r in rows], embs) if k not in have]
        if add:
            keys = keys + [k for k, _ in add]
            mat = np.vstack([mat, np.stack([e for _, e in add])])
            _save_emb_store(keys, mat)
        rows.clear()
        embs.clear()

    def process(paths, metas, probs, emb):
        for i, p in enumerate(paths):
            row = {"path": p, "rel": metas[i]["rel"], "kind": metas[i]["kind"], "key": metas[i]["key"]}
            for ci, cat in enumerate(scorer.cat_names):
                row[f"clip_{cat}"] = float(probs[i][ci])
            top = int(probs[i].argmax())
            row["clip_top"] = scorer.cat_names[top]
            row["clip_top_score"] = float(probs[i][top])
            rows.append(row)
            embs.append(emb[i])

    todo_list = list(todo.itertuples())
    for i in range(0, len(todo_list), bs):
        batch = todo_list[i:i + bs]
        paths = [r.path for r in batch]
        metas = [{"rel": r.rel, "kind": r.kind, "key": r.key} for r in batch]
        try:
            probs, emb = scorer.encode(paths)
            process(paths, metas, probs, emb)
        except Exception:
            # 整批失败时逐张隔离坏图
            for p, m in zip(paths, metas):
                try:
                    probs, emb = scorer.encode([p])
                    process([p], [m], probs, emb)
                except Exception as ex:
                    failed += 1
                    _log.warning("坏图 %s: %s", p, ex)
        if (i // bs) % 8 == 7:
            flush_pending()
        print(f"\rCLIP {min(i + bs, len(todo_list))}/{len(todo_list)}", end="", flush=True)
    print()
    flush_pending()
    if failed:
        _log.warning("跳过坏图 %d 张", failed)

    _log.info("步骤4完成 → cache/clip_scores.parquet（向量 %d 条）",
              len(_load_emb_store()[0]))
    return load_cache("clip_scores.parquet")


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    a = ap.parse_args()
    run(a.input_dir)
