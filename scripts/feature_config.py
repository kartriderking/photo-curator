"""Shared, validated task options. Importing this module never loads an AI model."""
import hashlib
import json

FEATURES = {
    'gps': 'GPS / 拍摄时间', 'places': '中文城市 / 地名',
    'phash': 'pHash 去重', 'quality': '模糊筛选',
    'similar': 'CLIP 相似照片', 'classify': 'AI 内容分类',
    'landmarks': '深度内容 / 景点识别', 'trips': '时间 / 旅程分组',
    'frames': '仅视频抽帧',
}
DEFAULT_FEATURES = ['phash', 'quality']
PRESETS = {
    'quick': DEFAULT_FEATURES,
    'travel': ['gps', 'trips'],
    'deep': [f for f in FEATURES if f != 'frames'],
}


def normalize_options(body=None, legacy=False):
    body = body or {}
    features = body.get('features')
    mode = body.get('mode')
    cleanup_level = body.get('cleanup_level', 'fast')
    content_level = body.get('content_level', 'ordinary')
    if mode is not None:
        if mode not in ('quick', 'travel', 'content'):
            raise ValueError('整理功能无效')
        if cleanup_level not in ('fast', 'ai') or content_level not in ('ordinary', 'deep'):
            raise ValueError('识别档位无效')
        features = {'quick': ['phash', 'quality'], 'travel': ['gps', 'places', 'trips'],
                    'content': ['classify', 'similar']}[mode].copy()
        if mode == 'quick' and cleanup_level == 'ai':
            features += ['similar', 'classify']
        if mode == 'content' and content_level == 'deep':
            features += ['landmarks']
    if features is None:
        features = list(FEATURES) if legacy else list(DEFAULT_FEATURES)
        if body.get('skip_vlm') and 'landmarks' in features:
            features.remove('landmarks')
    if isinstance(features, str):
        features = [x.strip() for x in features.split(',') if x.strip()]
    if not isinstance(features, list) or any(x not in FEATURES for x in features):
        raise ValueError('功能名称无效')
    features = list(dict.fromkeys(features))
    if not features:
        raise ValueError('请至少选择一项功能')
    scope = body.get('vlm_scope', 'all' if mode == 'content' else 'scenic')
    if scope not in ('scenic', 'no_gps', 'all'):
        raise ValueError('景点识别范围无效')
    geo_mode = body.get('geo_mode', 'offline')
    if geo_mode not in ('offline', 'online'):
        raise ValueError('地名查询方式无效')
    threshold = float(body.get('scenic_threshold') if body.get('scenic_threshold') is not None else 0.35)
    similarity = float(body.get('similar_threshold', 0.95))
    hamming = int(body.get('dup_hamming', 4))
    gap = float(body.get('trip_gap_hours', 36))
    if not 0 <= threshold <= 1 or not 0 <= similarity <= 1:
        raise ValueError('相似度和风景阈值必须在 0 到 1 之间')
    if not 0 <= hamming <= 64 or gap <= 0:
        raise ValueError('pHash 距离需在 0 到 64 之间，时间间隔必须大于 0')
    vlm_task = 'content' if mode == 'content' else body.get('vlm_task', 'landmarks')
    if vlm_task not in ('content', 'landmarks'):
        raise ValueError('深度识别任务无效')
    return {
        'features': features, 'include_videos': bool(body.get('include_videos', legacy)) or 'frames' in features,
        'vlm_scope': scope, 'geo_mode': geo_mode, 'scenic_threshold': threshold,
        'similar_threshold': similarity, 'dup_hamming': hamming, 'trip_gap_hours': gap,
        'vlm_model': str(body.get('vlm_model') or 'qwen3-vl:8b'),
        'force': bool(body.get('force', False)),
        'reuse_legacy_vlm': bool(body.get('reuse_legacy_vlm', True)),
        'vlm_task': vlm_task,
        **({'mode': mode, 'cleanup_level': cleanup_level, 'content_level': content_level} if mode else {}),
    }


def dependencies(options):
    f = set(options['features'])
    if f & {'places', 'trips'} or ('landmarks' in f and options['vlm_scope'] == 'no_gps'):
        f.add('gps')
    if 'landmarks' in f and options['vlm_scope'] in ('scenic', 'no_gps'):
        f.add('classify')
    return [x for x in FEATURES if x in f]


def signature(feature, options=None):
    from common import CONFIG, CLIP_CATEGORIES
    opts = options or normalize_options()
    spec = {'feature': feature, 'schema': 1}
    if feature == 'clip':
        spec.update(model=CONFIG['clip_model'], weights=CONFIG['clip_pretrained'], categories=CLIP_CATEGORIES)
        checkpoint = CONFIG.get('clip_checkpoint')
        if checkpoint:
            from pathlib import Path
            p = Path(checkpoint).resolve()
            spec['checkpoint'] = str(p)
            if p.is_file():
                st = p.stat()
                spec['checkpoint_version'] = [st.st_size, st.st_mtime_ns]
    elif feature == 'vlm':
        from step5_vlm import PROMPT, CONTENT_PROMPT
        spec.update(model=opts['vlm_model'], prompt=CONTENT_PROMPT if opts.get('vlm_task') == 'content' else PROMPT,
                    side=CONFIG['vlm_max_side'])
    elif feature == 'frames':
        spec.update(interval=CONFIG['frame_interval_sec'], limit=CONFIG['max_frames_per_video'])
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
