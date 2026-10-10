# -*- coding: utf-8 -*-
"""步骤5：对"疑似风景/旅行"的照片调本地 Ollama Qwen-VL，识别景点/城市/场景，结果写 jsonl 可续跑。"""
import base64
import io
import json
import re
import time

import pandas as pd
import requests

from common import CACHE_DIR, CONFIG, VLM_EXCLUDE, setup_logging, load_cache

_log = None
RESULTS = CACHE_DIR / "vlm_results.jsonl"
FLUSH_EVERY = 10   # 每识别 N 张落盘一次，减少 SSD 小写入

PROMPT = (
    "你是照片整理助手。分析这张照片：如果拍摄的是知名景点或旅游地标，给出它的名称和所在城市；"
    "如果认不出具体景点，landmark 和 city 填空字符串。\n"
    '只输出一个JSON对象，格式：{"landmark": "景点名或空字符串", "city": "城市名或空字符串", '
    '"confidence": "high/medium/low", "scene": "不超过20字的场景描述", "is_travel": true或false}\n'
    "is_travel 表示这张照片是否像旅行途中拍摄的。最多用一句话简短思考，然后立刻输出JSON，"
    "不要长篇推理。除JSON外不要输出任何其他文字。"
)


def _b64_image(path: str) -> str:
    from PIL import Image
    from common import load_image
    img = load_image(path)
    w, h = img.size
    side = CONFIG["vlm_max_side"]
    if max(w, h) > side:
        scale = side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _ask_ollama(url: str, model: str, img_b64: str) -> dict:
    r = requests.post(f"{url}/api/chat", json={
        "model": model,
        "messages": [{"role": "user", "content": PROMPT, "images": [img_b64]}],
        "stream": False,
        # 不用 format=json：qwen3-vl 的思考模板与 JSON 约束冲突会输出空 content
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 2048},
        "keep_alive": "15m",
    }, timeout=CONFIG.get("vlm_timeout", 180))
    r.raise_for_status()
    msg = r.json()["message"]
    # 思考模型可能把 JSON 写进 content 或 thinking，两处都试
    for text in (msg.get("content", ""), msg.get("thinking", "")):
        m = re.search(r"\{[^{}]*\}", text, re.S)
        if m:
            try:
                d = json.loads(m.group())
            except json.JSONDecodeError:
                continue
            return {
                "landmark": str(d.get("landmark", "")).strip(),
                "city": str(d.get("city", "")).strip(),
                "confidence": str(d.get("confidence", "low")).strip().lower(),
                "scene": str(d.get("scene", "")).strip(),
                "is_travel": bool(d.get("is_travel", False)),
            }
    raise ValueError(f"无法解析模型输出: {(msg.get('content') or msg.get('thinking') or '')[:120]}")


def _load_done() -> dict:
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                done[d["path"]] = d
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def select_targets(clip_df: pd.DataFrame, meta_df: pd.DataFrame, threshold: float):
    """返回 [(path, key, kind, reason)]：照片按行选；视频选其最佳帧。"""
    if clip_df.empty:
        return []
    df = clip_df.merge(meta_df[["path", "gps_lat"]], on="path", how="left") \
        if not meta_df.empty else clip_df
    qual = load_cache("quality.parquet")   # 重复组里非最优的照片不必送 VLM
    if not qual.empty and "is_group_best" in qual.columns:
        df = df.merge(qual[["key", "is_group_best"]], on="key", how="left")
    if not qual.empty:
        df = df.merge(qual[["key", "sharpness"]], on="key", how="left")
        if "sharpness" in df.columns:
            df = df[(df["sharpness"].isna()) | (df["sharpness"] >= CONFIG["sharp_min"])]
    has_gps = "gps_lat" in df.columns
    targets, seen = [], set()

    def ok(r, is_photo):
        if is_photo and getattr(r, "is_group_best", True) is False:
            return False
        return r.clip_top not in VLM_EXCLUDE and (
            getattr(r, "clip_scenic", 0) >= threshold or (has_gps and pd.notna(r.gps_lat)))

    for r in df[df["kind"] == "photo"].itertuples():
        if ok(r, True):
            targets.append((r.path, r.key, "photo", "photo"))
    fdf = df[df["kind"] == "frame"]
    if not fdf.empty:
        mf = load_cache("frames_manifest.parquet")
        if not mf.empty:
            fdf = fdf.merge(mf[["frame_path", "video_path"]], left_on="path",
                            right_on="frame_path", how="left")
            best = fdf.loc[fdf.groupby("video_path")["clip_scenic"].idxmax()]
            for r in best.itertuples():
                if ok(r, False):
                    targets.append((r.path, r.key, "video", r.video_path))
    return targets


def run(limit: int = 0):
    global _log
    _log = setup_logging("step5")
    url, model = CONFIG["ollama_url"], CONFIG["vlm_model"]
    try:
        requests.get(f"{url}/api/tags", timeout=5).raise_for_status()
    except Exception:
        raise SystemExit(f"连不上 Ollama（{url}）。请先启动 Ollama 并确认已 pull 模型 {model}。")

    clip_df = load_cache("clip_scores.parquet")
    meta_df = load_cache("metadata.parquet")
    targets = select_targets(clip_df, meta_df, CONFIG["scenic_threshold"])
    done = _load_done()
    todo = [t for t in targets if t[0] not in done]
    if limit:
        todo = todo[:limit]
    if not todo:
        _log.info("步骤5完成（无新增）")
        return done
    # 无 GPS 的优先（VLM 是它们唯一的定位来源）
    no_gps = {p for p in meta_df[meta_df["gps_lat"].isna()]["path"]} if not meta_df.empty else set()
    todo.sort(key=lambda t: (0 if t[0] in no_gps else 1, t[0]))
    _log.info("送 VLM 精析：%d 项（候选 %d，已完成 %d；无GPS优先 %d 项）",
              len(todo), len(targets), len(done), sum(1 for t in todo if t[0] in no_gps))

    t0, ok_n, buf = time.time(), 0, []
    with RESULTS.open("a", encoding="utf-8") as f:
        def flush_buf():
            for res in buf:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.flush()
            buf.clear()
        for i, (path, key, kind, reason) in enumerate(todo, 1):
            try:
                img = _b64_image(path)
                res = _ask_ollama(url, model, img)
                res.update({"path": path, "key": key, "kind": kind, "video": reason})
                buf.append(res)
                ok_n += 1
                if len(buf) >= FLUSH_EVERY:
                    flush_buf()
                speed = (time.time() - t0) / i
                eta = speed * (len(todo) - i) / 60
                mark = res["landmark"] or res["scene"][:20]
                print(f"\r[{i}/{len(todo)}] {speed:.1f}s/张 | 剩余约{eta:.0f}分 | {mark[:30]}"
                      .replace("\n", " "), end="", flush=True)
            except Exception as ex:
                _log.warning("VLM 失败 %s: %s", path, ex)
                time.sleep(2)
        flush_buf()   # 收尾不足 N 张的余量
    print()
    _log.info("步骤5完成：成功 %d/%d → %s", ok_n, len(todo), RESULTS.name)

    # 用完卸载模型，给其他程序腾显存
    try:
        requests.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass
    return _load_done()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 项（测试用）")
    a = ap.parse_args()
    run(a.limit)
