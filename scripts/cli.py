"""Feature-driven CLI; --steps remains supported and now produces partial reports."""
import argparse
import hashlib
from pathlib import Path

from common import OUTPUT_DIR
from feature_config import PRESETS, normalize_options


def main():
    ap = argparse.ArgumentParser(description='本地图库按需分析（默认快速清理）')
    ap.add_argument('--input-dir', required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--features', help='gps,places,phash,quality,similar,classify,landmarks,trips,frames')
    mode.add_argument('--preset', choices=list(PRESETS), default='quick')
    mode.add_argument('--steps', help='兼容步骤号: 1元数据/2抽帧/3去重清晰度/4CLIP/5景点/6导出')
    ap.add_argument('--job-dir', help='独立任务目录（报告/进度/快照）')
    ap.add_argument('--include-videos', action='store_true')
    ap.add_argument('--geo-mode', choices=['offline', 'online'], default='offline')
    ap.add_argument('--vlm-scope', choices=['scenic', 'no_gps', 'all'], default='scenic')
    ap.add_argument('--vlm-model', default='qwen3-vl:8b')
    ap.add_argument('--scenic-threshold', type=float, default=0.35)
    ap.add_argument('--similar-threshold', type=float, default=0.95)
    ap.add_argument('--dup-hamming', type=int, default=4)
    ap.add_argument('--trip-gap-hours', type=float, default=36)
    ap.add_argument('--force', action='store_true', help='重算所选功能，其他结果保留')
    ap.add_argument('--skip-vlm', action='store_true', help='兼容旧命令，移除景点识别')
    ap.add_argument('--no-legacy-vlm', action='store_true', help='不复用模型来源未知的历史景点结果')
    ap.add_argument('--export-only', action='store_true', help='仅刷新已有报告，无推理、无联网')
    args = ap.parse_args()
    features = args.features if args.features is not None else PRESETS[args.preset]
    export_only = args.export_only
    if args.steps is not None:
        numbers = {n.strip() for n in args.steps.split(',')}
        if not numbers or not numbers <= set('123456'):
            ap.error('步骤只能是 1 到 6')
        mapping = {'1': ['gps'], '2': ['frames'], '3': ['phash', 'quality'],
                   '4': ['classify', 'similar'], '5': ['landmarks'], '6': []}
        features = list(dict.fromkeys(f for n in sorted(numbers) for f in mapping[n]))
        export_only = numbers == {'6'}
    job_dir = Path(args.job_dir) if args.job_dir else OUTPUT_DIR / 'manual' / hashlib.sha1(str(Path(args.input_dir).resolve()).encode()).hexdigest()[:12]
    from pipeline import execute, export_results, prepare_legacy_snapshot
    if export_only:
        if not (job_dir / 'snapshot.json').exists():
            prepare_legacy_snapshot({'input_dir': args.input_dir, 'skip_vlm': args.skip_vlm}, job_dir)
        export_results(job_dir)
        print(f'结果已刷新: {job_dir / "results.csv"}')
        return
    body = vars(args).copy()
    body['features'] = features
    body['reuse_legacy_vlm'] = not args.no_legacy_vlm
    try:
        options = normalize_options(body)
        if args.skip_vlm:
            options['features'] = [f for f in options['features'] if f != 'landmarks']
            if not options['features']:
                ap.error('跳过 VLM 后没有选中的功能')
        execute(args.input_dir, options, job_dir)
    except ValueError as ex:
        ap.error(str(ex))


if __name__ == '__main__':
    main()
