"""Feature-driven incremental pipeline; export never runs inference, grouping or networking."""
import hashlib
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cache_db import CacheDB, canonical, clean, item_for, version
from common import CONFIG, IMAGE_EXTS, VIDEO_EXTS, CACHE_DIR, OUTPUT_DIR
from feature_config import FEATURES, dependencies, signature


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(clean(data), ensure_ascii=False, indent=1, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)


def scan(root, include_videos):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'图库不存在: {root}')
    items = []
    excluded = [CACHE_DIR.resolve(), OUTPUT_DIR.resolve()]
    for p in root.rglob('*'):
        if not p.is_file() or any(p.is_relative_to(d) for d in excluded):
            continue
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS or (include_videos and ext in VIDEO_EXTS):
            items.append(item_for(p, root, 'video' if ext in VIDEO_EXTS else 'photo'))
    return sorted(items, key=lambda r: r['rel'].lower())


def legacy_vlm(db, item, opts):
    data = db.get(item, 'vlm', signature('vlm', opts))
    if data is None and opts.get('reuse_legacy_vlm', True) and opts['vlm_model'] == 'qwen3-vl:8b':
        data = db.get(item, 'vlm', 'legacy')
    return data


def measure_image(item, do_phash, do_quality):
    from common import load_image
    import numpy as np
    try:
        img = load_image(item['path'])
        w, h = img.size
        result = {'width': w, 'height': h}
        if do_phash:
            import imagehash
            result['phash'] = str(imagehash.phash(img))
        if do_quality:
            import cv2
            # Same measurement scale as existing cache; portrait images now avoid huge arrays.
            gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
            if w > 1024:
                gray = cv2.resize(gray, (1024, max(1, int(h * 1024 / w))))
            result.update(sharpness=float(cv2.Laplacian(gray, cv2.CV_64F).var()), brightness=float(gray.mean()))
        img.close()
        return item, result, None
    except Exception as ex:
        return item, None, str(ex)


def phash_groups(items, records, distance):
    """Exact Hamming-radius BK-tree lookup; no all-pairs matrix or repeated array copies."""
    from collections import defaultdict
    groups = defaultdict(list)
    tree = None
    for item in items:
        value = records.get(canonical(item['path']), {}).get('phash')
        if not value:
            continue
        h = int(value, 16)
        found = []
        stack = [tree] if tree else []
        while stack:
            node = stack.pop()
            d = (h ^ node[0]).bit_count()
            if d <= distance:
                found.append((d, node[1]))
            stack.extend(child for edge, child in node[2].items() if d - distance <= edge <= d + distance)
        if found:
            group = min(found)[1]
        else:
            group = len(groups)
            node = [h, group, {}]
            if tree is None:
                tree = node
            else:
                current = tree
                while True:
                    edge = (h ^ current[0]).bit_count()
                    if edge not in current[2]:
                        current[2][edge] = node
                        break
                    current = current[2][edge]
        groups[group].append(item['path'])
    return [paths for paths in groups.values() if len(paths) > 1]


def rank_items(items, quality):
    return sorted(items, key=lambda i: (-quality.get(canonical(i['path']), {}).get('sharpness', -1),
                                        -quality.get(canonical(i['path']), {}).get('width', 0) * quality.get(canonical(i['path']), {}).get('height', 0),
                                        -i['size'], i['rel'].lower()))


def similar_groups(items, vectors, threshold):
    """Greedy grouping with a preallocated matrix and bounded comparisons."""
    import numpy as np
    pool = [i for i in items if canonical(i['path']) in vectors]
    if not pool:
        return []
    reps = np.empty((len(pool), len(next(iter(vectors.values())))), dtype='float32')
    groups = []
    for item in pool:
        v = vectors[canonical(item['path'])].astype('float32')
        v /= max(float(np.linalg.norm(v)), 1e-12)
        best, score = None, -1
        # Bounded slices avoid constructing a full N x N distance matrix.
        for start in range(0, len(groups), 2048):
            sims = reps[start:min(start + 2048, len(groups))] @ v
            j = int(sims.argmax())
            if float(sims[j]) > score:
                best, score = start + j, float(sims[j])
        if best is not None and score >= threshold:
            groups[best].append([item['path'], score])
        else:
            reps[len(groups)] = v
            groups.append([[item['path'], 1.0]])
    return [g for g in groups if len(g) > 1]


def execute(root, options, job_dir, db_path=None):
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    active = dependencies(options)
    state = {'input_dir': str(Path(root).resolve()), 'options': options, 'status': 'running',
             'features': {f: {'status': 'pending' if f in active else 'skipped', 'implicit': f not in options['features']} for f in FEATURES},
             'started': time.time()}
    snapshot = {'items': [], 'phash_groups': [], 'similar_groups': [], 'errors': {}, 'options': options}
    errors = snapshot['errors']
    db = CacheDB(db_path)
    last_export = [0.0]

    def persist():
        atomic_json(job_dir / 'progress.json', state)

    def status(features, status_name, done=0, total=0, cached=0):
        for f in features:
            state['features'][f].update(status=status_name, done=done, total=total, cached=cached)
        persist()

    def checkpoint(force_export=False):
        if not force_export and time.monotonic() - last_export[0] < 30:
            return
        atomic_json(job_dir / 'snapshot.json', snapshot)
        export_results(job_dir, db=db)
        last_export[0] = time.monotonic()

    def error(item, feature, ex):
        errors.setdefault(canonical(item['path']), {})[feature] = str(ex)[:180]
        print(f'{FEATURES.get(feature, feature)} 失败: {item["rel"]}: {str(ex)[:120]}', flush=True)

    try:
        persist()
        imported = db.import_legacy()
        if imported:
            print(f'旧缓存导入: {imported}', flush=True)
        items = scan(root, options['include_videos'])
        snapshot['items'] = items
        state.update(total_files=len(items), total_photos=sum(i['kind'] == 'photo' for i in items), total_videos=sum(i['kind'] == 'video' for i in items))
        db.set_manifest(root, items)
        photos = [i for i in items if i['kind'] == 'photo']
        force = options['force']
        checkpoint(True)
        if 'gps' in active:
            from step1_metadata import extract_exif, _parse_dt
            pending = [i for i in items if force or db.get(i, 'metadata', signature('metadata')) is None]
            cached = len(items) - len(pending)
            status(['gps'], 'running', cached, len(items), cached)
            for start in range(0, len(pending), 400):
                batch = pending[start:start + 400]
                raw = extract_exif([i['path'] for i in batch])
                rows = []
                for i in batch:
                    data = raw.get(i['path'].replace('\\', '/'))
                    if data is None or data.get('Error'):
                        error(i, 'gps', (data or {}).get('Error', '无法读取元数据'))
                        continue
                    e = {'dt': _parse_dt(data.get('DateTimeOriginal')) or _parse_dt(data.get('CreateDate')),
                         'gps_lat': data.get('GPSLatitude'), 'gps_lon': data.get('GPSLongitude'),
                         'camera': ' '.join(str(data.get(k) or '') for k in ('Make', 'Model')).strip(),
                         'width': data.get('ImageWidth'), 'height': data.get('ImageHeight'), 'duration': data.get('Duration')}
                    rows.append((i, e, None))
                db.put_many('metadata', signature('metadata'), rows)
                status(['gps'], 'running', cached + min(start + 400, len(pending)), len(items), cached)
                checkpoint()
            status(['gps'], 'partial' if any('gps' in e for e in errors.values()) else 'done', len(items), len(items), cached)
            checkpoint(True)

        # Video pixels are decoded only when video content analysis is explicitly requested.
        frames = []
        if options['include_videos'] and set(active) & {'classify', 'landmarks', 'frames'}:
            from step2_video_frames import _extract
            videos = [i for i in items if i['kind'] == 'video']
            state['video_analysis'] = {'status': 'running', 'done': 0, 'total': len(videos)}
            if 'frames' in active:
                status(['frames'], 'running', 0, len(videos))
            for n, i in enumerate(videos, 1):
                old = None if force else db.get(i, 'frames', signature('frames'))
                paths = old.get('paths', []) if old else []
                if not paths or not all(Path(p).is_file() for p in paths):
                    try:
                        # New identities avoid mixing frames from edited files.
                        paths = _extract(i['path'], i['key'])
                        if not paths:
                            raise ValueError('未提取到视频帧')
                        db.put_many('frames', signature('frames'), [(i, {'paths': paths}, None)])
                    except Exception as ex:
                        error(i, 'video', ex)
                        paths = []
                for p in paths:
                    frame = item_for(p, Path(p).parent, 'frame')
                    frame['video_path'] = i['path']
                    frames.append(frame)
                state['video_analysis']['done'] = n
                if 'frames' in active:
                    state['features']['frames'].update(done=n, total=len(videos))
                persist()
            state['video_analysis']['status'] = 'partial' if any('video' in e for e in errors.values()) else 'done'
            if 'frames' in active:
                status(['frames'], state['video_analysis']['status'], len(videos), len(videos))
            snapshot['frames'] = frames
            checkpoint(True)

        measures = [f for f in ('phash', 'quality') if f in active]
        quality = {}
        if measures:
            pending = []
            for i in photos:
                need = [f for f in measures if force or db.get(i, f, signature(f)) is None]
                if need:
                    pending.append((i, 'phash' in need, 'quality' in need))
            cached = len(photos) - len(pending)
            status(measures, 'running', cached, len(photos), cached)
            buffered = {f: [] for f in measures}
            with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as workers:
                # Small windows bound decoded image memory and allow prompt cancellation.
                for start in range(0, len(pending), 32):
                    batch = pending[start:start + 32]
                    results = workers.map(lambda args: measure_image(*args), batch)
                    for (i, needs_hash, needs_quality), (_, result, ex) in zip(batch, results):
                        if ex:
                            for f, needed in [('phash', needs_hash), ('quality', needs_quality)]:
                                if needed:
                                    error(i, f, ex)
                        else:
                            if needs_hash:
                                buffered['phash'].append((i, {k: result[k] for k in ('phash', 'width', 'height')}, None))
                            if needs_quality:
                                buffered['quality'].append((i, {k: result[k] for k in ('sharpness', 'brightness', 'width', 'height')}, None))
                    for f in measures:
                        db.put_many(f, signature(f), buffered[f])
                        buffered[f].clear()
                    status(measures, 'running', cached + min(start + 32, len(pending)), len(photos), cached)
                    checkpoint()
            for i in photos:
                q = db.get(i, 'quality', signature('quality')) if 'quality' in active else None
                h = db.get(i, 'phash', signature('phash')) if 'phash' in active else None
                hash_fields = {k: v for k, v in (h or {}).items() if k in ('phash', 'width', 'height')}
                quality[canonical(i['path'])] = {**hash_fields, **(q or {})}
            ranked = rank_items(photos, quality)
            if 'phash' in active:
                snapshot['phash_groups'] = phash_groups(ranked, quality, options['dup_hamming'])
            for f in measures:
                status([f], 'partial' if any(f in e for e in errors.values()) else 'done', len(photos), len(photos), cached)
            checkpoint(True)

        clips, vectors = {}, {}
        clip_features = [f for f in ('classify', 'similar') if f in active]
        if clip_features:
            clip_items = photos + (frames if 'classify' in active else [])
            pending = []
            for i in clip_items:
                data = None if force else db.get(i, 'clip', signature('clip'))
                if data is None or ('similar' in active and i['kind'] == 'photo' and '_vector' not in data):
                    pending.append(i)
                else:
                    clips[canonical(i['path'])] = data
                    if '_vector' in data:
                        vectors[canonical(i['path'])] = data['_vector']
            cached = len(clip_items) - len(pending)
            status(clip_features, 'running', cached, len(clip_items), cached)
            if pending:
                from step4_clip import ClipScorer
                scorer = ClipScorer()
                bs = CONFIG['clip_batch']
                for start in range(0, len(pending), bs):
                    batch = pending[start:start + bs]
                    rows = []
                    def process(group):
                        probs, emb = scorer.encode([i['path'] for i in group])
                        for n, i in enumerate(group):
                            import numpy as np
                            top = int(np.argmax(probs[n]))
                            d = {f'clip_{cat}': float(probs[n][ci]) for ci, cat in enumerate(scorer.cat_names)}
                            d.update(clip_top=scorer.cat_names[top], clip_top_score=float(probs[n][top]))
                            clips[canonical(i['path'])] = d
                            vectors[canonical(i['path'])] = emb[n]
                            rows.append((i, d, emb[n]))
                    try:
                        process(batch)
                    except Exception:
                        for i in batch:
                            try:
                                process([i])
                            except Exception as ex:
                                for f in clip_features:
                                    error(i, f, ex)
                    db.put_many('clip', signature('clip'), rows)
                    status(clip_features, 'running', cached + min(start + bs, len(pending)), len(clip_items), cached)
                    checkpoint()
                del scorer
                import gc
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            if 'similar' in active:
                removed = {canonical(p) for group in snapshot['phash_groups'] for p in group[1:]}
                pool = [i for i in rank_items(photos, quality) if canonical(i['path']) not in removed]
                snapshot['similar_groups'] = similar_groups(pool, vectors, options['similar_threshold'])
            for f in clip_features:
                status([f], 'partial' if any(f in e for e in errors.values()) else 'done', len(clip_items), len(clip_items), cached)
            checkpoint(True)

        if 'places' in active:
            geo_items = [i for i in items if (db.get(i, 'metadata', signature('metadata')) or {}).get('gps_lat') is not None
                         and (db.get(i, 'metadata', signature('metadata')) or {}).get('gps_lon') is not None]
            geo_sig = options['geo_mode'] + '-v1'
            pending = [i for i in geo_items if force or db.get(i, 'places', geo_sig) is None]
            cached = len(geo_items) - len(pending)
            status(['places'], 'running', cached, len(geo_items), cached)
            if pending and options['geo_mode'] == 'offline':
                from step1_metadata import _geocoder
                meta = [db.get(i, 'metadata', signature('metadata')) for i in pending]
                results = _geocoder().query([(float(m['gps_lat']), float(m['gps_lon'])) for m in meta])
                db.put_many('places', geo_sig, [(i, {'city': r['name'], 'place': '', 'src': 'offline'}, None) for i, r in zip(pending, results)])
            elif pending:
                import geocode
                for n, i in enumerate(pending, 1):
                    m = db.get(i, 'metadata', signature('metadata'))
                    geo_item = {'path': str(CACHE_DIR / ('geo-' + geocode._cache_key(float(m['gps_lat']), float(m['gps_lon'])))), 'version': '1'}
                    cached_geo = None if force else db.get(geo_item, 'geocode', geo_sig)
                    try:
                        if cached_geo is None:
                            cached_geo = geocode._nominatim_once(float(m['gps_lat']), float(m['gps_lon']), False)
                            if cached_geo is None:
                                raise ValueError('地名查询无结果')
                            db.put_many('geocode', geo_sig, [(geo_item, cached_geo, None)])
                            time.sleep(geocode.LIMIT_SLEEP)
                        db.put_many('places', geo_sig, [(i, cached_geo, None)])
                    except Exception as ex:
                        error(i, 'places', ex)
                        time.sleep(geocode.LIMIT_SLEEP)
                    status(['places'], 'running', cached + n, len(geo_items), cached)
                    if n % 20 == 0:
                        checkpoint()
            status(['places'], 'partial' if any('places' in e for e in errors.values()) else 'done', len(geo_items), len(geo_items), cached)
            checkpoint(True)

        if 'landmarks' in active:
            import requests
            from common import VLM_EXCLUDE
            from step5_vlm import _ask_ollama, _b64_image
            removed = {canonical(p) for group in snapshot['phash_groups'] for p in group[1:]}
            removed.update(canonical(p) for group in snapshot['similar_groups'] for p, _ in group[1:])
            def eligible(i):
                p = canonical(i['path'])
                if p in removed:
                    return False
                if 'quality' in active and quality.get(p, {}).get('sharpness', CONFIG['sharp_min']) < CONFIG['sharp_min']:
                    return False
                scope = options['vlm_scope']
                if scope == 'all':
                    return True
                c = clips.get(p, {})
                if c.get('clip_top') in VLM_EXCLUDE:
                    return False
                if scope == 'no_gps':
                    parent = next((x for x in items if x['path'] == i.get('video_path')), i) if i['kind'] == 'frame' else i
                    m = db.get(parent, 'metadata', signature('metadata')) or {}
                    return m.get('gps_lat') is None
                return c.get('clip_scenic', 0) >= options['scenic_threshold']
            candidates = [i for i in photos if eligible(i)]
            for video in [i for i in items if i['kind'] == 'video']:
                vf = [f for f in frames if f['video_path'] == video['path'] and eligible(f)]
                if vf:
                    candidates.append(max(vf, key=lambda f: clips.get(canonical(f['path']), {}).get('clip_scenic', 0)))
            snapshot['vlm_candidates'] = [canonical(i['path']) for i in candidates]
            pending = [i for i in candidates if force or legacy_vlm(db, i, options) is None]
            cached = len(candidates) - len(pending)
            status(['landmarks'], 'running', cached, len(candidates), cached)
            for n, i in enumerate(pending, 1):
                try:
                    r = _ask_ollama(CONFIG['ollama_url'], options['vlm_model'], _b64_image(i['path']))
                    db.put_many('vlm', signature('vlm', options), [(i, r, None)])
                except Exception as ex:
                    error(i, 'landmarks', ex)
                    if isinstance(ex, requests.exceptions.ConnectionError):
                        state['features']['landmarks']['error'] = '无法连接 Ollama，其余候选未执行；启动服务后续跑'
                        status(['landmarks'], 'partial', cached + n, len(candidates), cached)
                        break
                status(['landmarks'], 'running', cached + n, len(candidates), cached)
                checkpoint()
            attempted = cached + (n if pending else 0)
            status(['landmarks'], 'partial' if any('landmarks' in e for e in errors.values()) else 'done', attempted, len(candidates), cached)
            if pending:
                try:
                    requests.post(f'{CONFIG["ollama_url"]}/api/generate', json={'model': options['vlm_model'], 'keep_alive': 0}, timeout=5)
                except requests.RequestException:
                    pass
            checkpoint(True)
        if 'trips' in active:
            status(['trips'], 'done', len(items), len(items))
        state['status'] = 'partial' if errors else 'done'
        state['finished'] = time.time()
        persist()
        checkpoint(True)
        print(f'完成: {job_dir / "results.csv"}', flush=True)
        return state
    except BaseException as ex:
        state.update(status='failed', error=str(ex), finished=time.time())
        for info in state['features'].values():
            if info['status'] == 'running':
                info['status'] = 'failed'
        persist()
        checkpoint(True)
        raise
    finally:
        db.close()


def export_results(job_dir, db=None):
    """Read the task's snapshot and feature cache only; no scan/inference/network calls."""
    import pandas as pd
    job_dir = Path(job_dir)
    snapshot = json.loads((job_dir / 'snapshot.json').read_text(encoding='utf-8'))
    own_db = db is None
    db = db or CacheDB()
    opts = snapshot['options']
    active = set(dependencies(opts))
    try:
        if 'legacy_rows' in snapshot:
            old_rows = []
            for i, original in zip(snapshot['items'], snapshot['legacy_rows']):
                if not Path(i['path']).is_file():
                    continue
                row = dict(original)
                row['缓存来源'] = '历史报告'
                if version(i['path']) != i['version']:
                    row['建议标签'] = '未评估'
                    row['同组最优'] = ''
                    row['失败环节'] = '文件已变化，请续跑所需功能'
                best = row.get('同组最优')
                if best and not Path(snapshot['input_dir'], best).is_file():
                    row['同组最优'] = ''
                    row['建议标签'] = '待审-原保留项已删除'
                old_rows.append(row)
            out = pd.DataFrame(old_rows, columns=None if old_rows else ['相对路径', '类型', '建议标签'])
            write_report(out, job_dir)
            return out
        items = []
        changed = set()
        for i in snapshot['items']:
            try:
                if Path(i['path']).is_file():
                    items.append(i)
                    if version(i['path']) != i['version']:
                        changed.add(canonical(i['path']))
            except OSError:
                continue
        bypath = {canonical(i['path']): i for i in items}
        duplicate = {}
        for group in snapshot.get('phash_groups', []):
            alive = [p for p in group if canonical(p) in bypath and canonical(p) not in changed]
            for p in alive[1:]:
                duplicate[canonical(p)] = ('可删-重复', alive[0], None)
        for group in snapshot.get('similar_groups', []):
            alive = [(p, score) for p, score in group if canonical(p) in bypath and canonical(p) not in changed]
            for p, score in alive[1:]:
                duplicate[canonical(p)] = ('待审-相似', alive[0][0], score)
        vf_by_video = {}
        for f in snapshot.get('frames', []):
            vf_by_video.setdefault(canonical(f['video_path']), []).append(f)
        rows = []
        for i in items:
            p = canonical(i['path'])
            r = {'相对路径': i['rel'], '同组最优': '', '类型': i['kind'], '建议标签': '未评估', '失败环节': '', '缓存来源': ''}
            if p in changed:
                r['失败环节'] = '文件已变化，请续跑所需功能'
                rows.append(r)
                continue
            sources = set()
            def read(feature, sig=None, item=i):
                feature_name = {'metadata': 'gps', 'clip': 'classify'}.get(feature, feature)
                if opts['force'] and feature_name in snapshot.get('errors', {}).get(canonical(item['path']), {}):
                    return {}
                d = db.get(item, feature, sig or signature(feature, opts))
                if d:
                    sources.add('历史缓存' if d['_source'] == 'legacy' else '新缓存')
                return d or {}
            meta = read('metadata') if 'gps' in active else {}
            if 'gps' in active:
                r.update({'拍摄时间': meta.get('dt'), 'GPS纬度': meta.get('gps_lat'), 'GPS经度': meta.get('gps_lon'),
                          '相机': meta.get('camera'), '宽': meta.get('width'), '高': meta.get('height'), '时长秒': meta.get('duration')})
            if 'places' in active:
                geo = read('places', opts['geo_mode'] + '-v1')
                r.update({'城市(GPS)': geo.get('city'), '地名': geo.get('place')})
            q = read('quality') if 'quality' in active and i['kind'] == 'photo' else {}
            if 'quality' in active:
                r.update({'清晰度': q.get('sharpness'), '亮度': q.get('brightness')})
                if q.get('sharpness') is not None:
                    r['建议标签'] = '可删-模糊' if q['sharpness'] < CONFIG['sharp_min'] else '保留候选'
            if 'phash' in active:
                h = read('phash') if i['kind'] == 'photo' else {}
                r['pHash'] = h.get('phash')
                if h and r['建议标签'] == '未评估':
                    r['建议标签'] = '未判重复'
            c = read('clip') if 'classify' in active and i['kind'] == 'photo' else {}
            frame_list = vf_by_video.get(p, [])
            if frame_list and 'classify' in active:
                best = max(frame_list, key=lambda f: (db.get(f, 'clip', signature('clip')) or {}).get('clip_scenic', 0))
                c = read('clip', item=best)
            if 'classify' in active:
                r.update({'CLIP类别': c.get('clip_top'), 'CLIP置信度': c.get('clip_top_score'), '风景分': c.get('clip_scenic')})
                if r['建议标签'] == '未评估' and c:
                    r['建议标签'] = '旅行素材' if c.get('clip_scenic', 0) >= opts['scenic_threshold'] else '日常'
                if c.get('clip_top') in ('screenshot', 'document', 'meme') and c.get('clip_top_score', 0) >= 0.5 and r['建议标签'] != '可删-模糊':
                    r['建议标签'] = '可删-截图/文档'
            if 'landmarks' in active:
                allowed = set(snapshot.get('vlm_candidates', [])) if 'vlm_candidates' in snapshot else None
                v = legacy_vlm(db, i, opts) if i['kind'] == 'photo' and (allowed is None or p in allowed) else None
                if frame_list:
                    v = next((d for f in frame_list if (allowed is None or canonical(f['path']) in allowed) and (d := legacy_vlm(db, f, opts))), None)
                v = v or {}
                if v:
                    sources.add('历史景点（模型未知）' if v['_source'] == 'legacy' else '新缓存')
                r.update({'VLM景点': v.get('landmark'), 'VLM城市': v.get('city'), 'VLM场景': v.get('scene'),
                          'VLM置信度': v.get('confidence'), '是否旅行': v.get('is_travel')})
                if v.get('landmark') and v.get('confidence') != 'low' and not r['建议标签'].startswith('可删'):
                    r['建议标签'] = '景点'
            if p in duplicate:
                label, best, score = duplicate[p]
                r.update({'建议标签': label, '同组最优': bypath[canonical(best)]['rel']})
                if score is not None:
                    r['相似度'] = round(score, 4)
            r['失败环节'] = '；'.join(f'{FEATURES.get(f, f)}: {err}' for f, err in snapshot.get('errors', {}).get(p, {}).items())
            r['缓存来源'] = ' / '.join(sorted(sources))
            rows.append(r)
        out = pd.DataFrame(rows, columns=None if rows else ['相对路径', '同组最优', '类型', '建议标签', '失败环节', '缓存来源'])
        if 'trips' in active:
            out['旅程编号'] = ''
            dates = pd.to_datetime(out.get('拍摄时间', pd.Series(index=out.index, dtype='object')), errors='coerce')
            previous, number = None, 0
            for idx in dates.dropna().sort_values().index:
                dt = dates[idx]
                if previous is None or dt - previous > pd.Timedelta(hours=opts['trip_gap_hours']):
                    number += 1
                out.at[idx, '旅程编号'] = f'T{number:03d}'
                previous = dt
        if '拍摄时间' in out.columns:
            out = out.sort_values('拍摄时间', na_position='last')
        write_report(out, job_dir)
        return out
    finally:
        if own_db:
            db.close()


def write_report(out, job_dir):
    tmp = Path(job_dir) / ('results.' + uuid.uuid4().hex + '.tmp.csv')
    out.to_csv(tmp, index=False, encoding='utf-8-sig')
    os.replace(tmp, Path(job_dir) / 'results.csv')


def prepare_legacy_snapshot(job, job_dir):
    """Bridge old tasks without recomputing models or changing original cache files."""
    import pandas as pd
    from feature_config import normalize_options
    options = normalize_options(job, legacy=True)
    options['geo_mode'] = 'online'
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {'options': options, 'input_dir': job['input_dir'], 'items': [], 'errors': {}, 'phash_groups': [], 'similar_groups': []}
    with CacheDB() as db:
        db.import_legacy()
        report = job_dir / 'results.csv'
        if report.exists():
            rows = []
            for row in pd.read_csv(report).fillna('').to_dict('records'):
                p = Path(job['input_dir'], row['相对路径'])
                if p.is_file():
                    item = item_for(p, job['input_dir'], row.get('类型', 'photo'))
                    if db.get(item, 'metadata', signature('metadata')) is None:
                        row['建议标签'] = '未评估'
                        row['同组最优'] = ''
                        row['失败环节'] = '旧报告文件信息无法验证，请续跑所需功能'
                    snapshot['items'].append(item)
                    rows.append(row)
            snapshot['legacy_rows'] = rows
        else:
            snapshot['items'] = scan(job['input_dir'], True)
            groups = {}
            for i in snapshot['items']:
                q = db.get(i, 'phash', signature('phash')) or {}
                if q.get('group') is not None:
                    groups.setdefault(q['group'], []).append((q.get('group_rank', 0), i['path']))
            snapshot['phash_groups'] = [[p for _, p in sorted(g)] for g in groups.values() if len(g) > 1]
        atomic_json(job_dir / 'snapshot.json', snapshot)
