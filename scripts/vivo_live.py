"""Read vivo pairing IDs from JPEG trailers and MP4 UUID boxes without decoding media."""
import json
import re
import struct
from pathlib import Path

VIVO_UUID = b'vivoMediaExtInfo'
TRAILER_MAGIC = b'\xff' * 4 + bytes.fromhex('1b2a39485766758493a2b3')
LIMIT = 256 * 1024


def parse_payload(data):
    album_pos = data.rfind(b'cameralbum!')
    if album_pos < 4:
        raise ValueError('vivo 私有内容尾部不完整')
    json_size_pos = album_pos - 4
    json_size = struct.unpack('>I', data[json_size_pos:album_pos])[0]
    json_pos = json_size_pos - json_size
    if json_pos < 4 or data[json_pos - 4:json_pos] != b'vivo':
        raise ValueError('vivo 私有内容长度不一致')
    def unique_fields(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('vivo 实况内容含有重复字段')
            out[key] = value
        return out
    metadata = json.loads(data[json_pos:json_size_pos].decode('utf-8'), object_pairs_hook=unique_fields)
    if not isinstance(metadata, dict):
        raise ValueError('vivo 私有内容格式无法确认')
    identifier = metadata.get('com.android.camera.livephoto')
    if identifier in (None, '', 0, '0'):
        return None  # Ordinary vivo videos have this metadata container too.
    if not isinstance(identifier, str):
        raise ValueError('vivo 实况标识不是文本')
    if not data.endswith(TRAILER_MAGIC):
        raise ValueError('vivo 实况尾部不完整')
    end = len(data) - len(TRAILER_MAGIC)
    trailer_id = data[end - 28:end].decode('ascii', errors='strict')
    size_pos = end - 32
    extension = data[album_pos + len(b'cameralbum!'):size_pos]
    if extension not in (b'', struct.pack('>III', 2, 0, 0)) or struct.unpack('>I', data[size_pos:size_pos + 4])[0] != 47:
        raise ValueError('vivo 实况尾部长度不一致')
    if identifier != trailer_id:
        raise ValueError('vivo 实况内容与尾部的标识不一致')
    base = {'tags': {'vivo:com.android.camera.livephoto': identifier}, 'format': 'vivo', 'issue': ''}
    if re.fullmatch(r'motionphoto\d{17}', identifier):
        return {**base, 'uuid': None, 'state': 'embedded_candidate'}
    if not re.fullmatch(r'\d{13}[0-9a-fA-F]{15}', identifier):
        raise ValueError('vivo 实况标识格式无法确认')
    return {**base, 'uuid': 'vivo:' + identifier.lower(), 'identifier': identifier.lower(), 'state': 'no_uuid'}


def embedded_video(stream, size):
    import xml.etree.ElementTree as ET
    stream.seek(0)
    head = stream.read(min(size, 128 * 1024))
    start, end = head.find(b'<x:xmpmeta'), head.find(b'</x:xmpmeta>')
    if start < 0 or end < start:
        return False
    try:
        tree = ET.fromstring(head[start:end + len(b'</x:xmpmeta>')])
    except ET.ParseError:
        return False
    camera = '{http://ns.google.com/photos/1.0/camera/}MotionPhoto'
    item = '{http://ns.google.com/photos/1.0/container/item/}'
    if not any(element.attrib.get(camera) == '1' for element in tree.iter()):
        return False
    for element in tree.iter():
        if element.attrib.get(item + 'Mime') != 'video/mp4' or element.attrib.get(item + 'Semantic') != 'MotionPhoto':
            continue
        length = element.attrib.get(item + 'Length', '')
        if not length.isdigit() or not 0 < int(length) < size:
            raise ValueError('单文件实况的视频长度无法确认')
        pos, kinds = size - int(length), set()
        for _ in range(10000):
            if pos == size:
                if {b'ftyp', b'mdat', b'moov'} <= kinds:
                    return True
                break
            if pos + 8 > size:
                break
            stream.seek(pos)
            raw = stream.read(8)
            length, kind = struct.unpack('>I4s', raw)
            header_size = 8
            if length == 1:
                raw = stream.read(8)
                if len(raw) != 8:
                    break
                length = struct.unpack('>Q', raw)[0]
                header_size = 16
            elif length == 0:
                length = size - pos
            if length < header_size or pos + length > size or not kinds and kind != b'ftyp':
                break
            kinds.add(kind)
            pos += length
        raise ValueError('单文件实况的内嵌视频不完整')
    return False


def read_identifier(path):
    path = Path(path)
    with path.open('rb') as stream:
        size = path.stat().st_size
        if path.suffix.lower() in ('.jpg', '.jpeg'):
            stream.seek(max(0, size - 4096))
            tail = stream.read(4096)
            if not tail.endswith(TRAILER_MAGIC):
                if b'vivo{"' in tail and b'"com.android.camera.livephoto"' in tail:
                    raise ValueError('vivo 实况尾部无法完整读取')
                return None
            album_pos = tail.rfind(b'cameralbum!')
            if album_pos < 4:
                raise ValueError('vivo 实况尾部无法完整读取')
            json_size = struct.unpack('>I', tail[album_pos - 4:album_pos])[0]
            needed = len(tail) - album_pos + json_size + 8
            if needed > LIMIT:
                raise ValueError('vivo 实况元数据过大，无法确认')
            if needed > len(tail):
                stream.seek(max(0, size - needed))
                tail = stream.read(needed)
            metadata = parse_payload(tail)
            if metadata and metadata['state'] == 'embedded_candidate':
                metadata['state'] = 'embedded' if embedded_video(stream, size) else 'no_uuid'
                metadata['issue'] = '' if metadata['state'] == 'embedded' else '单文件实况标记不能用于两文件配对'
            return metadata
        if path.suffix.lower() not in ('.mp4', '.mov', '.m4v'):
            return None
        found = []
        budget = [10000]
        def boxes(start, end, depth=0):
            if depth > 6:
                return
            pos = start
            while pos + 8 <= end and budget[0] > 0:
                budget[0] -= 1
                stream.seek(pos)
                head = stream.read(8)
                length, kind = struct.unpack('>I4s', head)
                header_size = 8
                if length == 1:
                    raw = stream.read(8)
                    if len(raw) != 8:
                        break
                    length = struct.unpack('>Q', raw)[0]
                    header_size = 16
                elif length == 0:
                    length = end - pos
                if length < header_size or pos + length > end:
                    break
                if kind == b'uuid':
                    if length < header_size + 16:
                        pos += length
                        continue
                    marker = stream.read(16)
                    if marker == VIVO_UUID:
                        if length - header_size > LIMIT:
                            raise ValueError('vivo 实况元数据过大，无法确认')
                        payload = stream.read(length - header_size - 16)
                        metadata = parse_payload(payload)
                        if metadata and metadata['state'] != 'embedded_candidate':
                            found.append(metadata)
                elif kind in (b'moov', b'udta', b'meta'):
                    boxes(pos + header_size + (4 if kind == b'meta' else 0), pos + length, depth + 1)
                pos += length
        boxes(0, size)
        if len({m['uuid'] for m in found}) > 1:
            raise ValueError('视频中含有相互冲突的 vivo 实况标识')
        return found[0] if found else None
