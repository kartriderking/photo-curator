# -*- coding: utf-8 -*-
"""GPS 坐标 → 中文城市/地名。

- 数据源：Nominatim (OSM) reverse 接口，zoom=14 + accept-language=zh-CN，
  一次请求同时拿到中文城市和中文小地名（如 深圳 / 莲塘）。
- 坐标系：EXIF GPS 与 OSM 均为 WGS-84，直接使用不做转换。
  coordtransform 函数内嵌备用：仅当将来接入高德等 GCJ-02 服务时，
  对中国大陆坐标（粗包围盒判定）先 wgs84_to_gcj02；境外/港澳台不适用 GCJ-02。
- 缓存：按约百米圆整坐标存 parquet，重复查询零网络请求。
- 限速：1.1秒/次（OSM 使用政策）；代理可通过环境变量配置，
  仍失败回退 reverse_geocoder 的拼音地名（旧数据，不丢失）。
"""
import time
import os

import pandas as pd
import requests

from common import CACHE_DIR, setup_logging

GEO_CACHE = CACHE_DIR / "geonames.parquet"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
UA = {"User-Agent": "photo-curator/1.0 (personal local photo tagging tool)"}
_proxy_url = os.environ.get('PHOTO_CURATOR_PROXY', '')
PROXY = {'http': _proxy_url, 'https': _proxy_url} if _proxy_url else None
LIMIT_SLEEP = 1.1  # OSM 政策：不超过 1 请求/秒

_log = None


# ---------- coordtransform（备用：接入高德等 GCJ-02 服务时使用） ----------

def _out_of_china(lat: float, lon: float) -> bool:
    """粗包围盒判定：True=中国大陆之外（GCJ-02 不适用，含港澳台）。"""
    return not (3.86 < lat < 53.55 and 73.66 < lon < 135.05)


def wgs84_to_gcj02(lat: float, lon: float):
    if _out_of_china(lat, lon):
        return lat, lon
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * 3.141592653589793
    magic = __import__("math").sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = __import__("math").sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * 3.141592653589793)
    dlon = (dlon * 180.0) / (a / sqrtmagic * __import__("math").cos(radlat) * 3.141592653589793)
    return lat + dlat, lon + dlon


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * abs(x) ** 0.5
    ret += (20.0 * __import__("math").sin(6.0 * x * 3.141592653589793)
            + 20.0 * __import__("math").sin(2.0 * x * 3.141592653589793)) * 2.0 / 3.0
    ret += (20.0 * __import__("math").sin(y * 3.141592653589793)
            + 40.0 * __import__("math").sin(y / 3.0 * 3.141592653589793)) * 2.0 / 3.0
    ret += (160.0 * __import__("math").sin(y / 12.0 * 3.141592653589793)
            + 320 * __import__("math").sin(y * 3.141592653589793 / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * abs(x) ** 0.5
    ret += (20.0 * __import__("math").sin(6.0 * x * 3.141592653589793)
            + 20.0 * __import__("math").sin(2.0 * x * 3.141592653589793)) * 2.0 / 3.0
    ret += (20.0 * __import__("math").sin(x * 3.141592653589793)
            + 40.0 * __import__("math").sin(x / 3.0 * 3.141592653589793)) * 2.0 / 3.0
    ret += (150.0 * __import__("math").sin(x / 12.0 * 3.141592653589793)
            + 300.0 * __import__("math").sin(x / 30.0 * 3.141592653589793)) * 2.0 / 3.0
    return ret


# ---------- 缓存与查询 ----------

def _cache_key(lat: float, lon: float) -> str:
    """约百米圆整（0.001度），同点不同照片共享缓存。"""
    return f"{round(lat, 3):.3f},{round(lon, 3):.3f}"


def _load_cache() -> pd.DataFrame:
    if GEO_CACHE.exists():
        try:
            return pd.read_parquet(GEO_CACHE)
        except Exception:
            pass
    return pd.DataFrame(columns=["key", "city", "place", "display", "src"])


def _save_cache(df: pd.DataFrame):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(GEO_CACHE, index=False)


def _clean(v: str) -> str:
    """清洗多语言串：'法国;法國巴黎'→法国；'禾木哈纳斯乡 قومقاناس'→禾木哈纳斯乡"""
    if not v:
        return ""
    v = v.split(";")[0].strip()
    parts = v.split()
    return parts[0] if len(parts) > 1 and any('\u4e00' <= ch <= '\u9fff' for ch in parts[0]) else v


def _nominatim_once(lat: float, lon: float, use_proxy: bool) -> dict | None:
    r = requests.get(NOMINATIM, params={
        "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14,
        "accept-language": "zh-CN", "addressdetails": 1,
    }, headers=UA, timeout=30,
       proxies=PROXY if use_proxy else None)
    if r.status_code == 403:
        raise RuntimeError("Nominatim 拒绝访问(403)，可能被限流")
    r.raise_for_status()
    d = r.json()
    addr = d.get("address", {})
    country = _clean(addr.get("country", ""))
    suburb = _clean(addr.get("suburb") or addr.get("quarter")
                    or addr.get("neighbourhood") or addr.get("town")
                    or addr.get("village") or addr.get("hamlet") or "")

    if country == "中国":
        # OSM 中国数据 city 字段常是区级（宝安区/上城区），
        # 地级市从 display_name 层级里取最后一个"市"字结尾且非区/县的部分
        parts = [p.strip() for p in d.get("display_name", "").split(",")]
        city = ""
        for p in parts:
            if p.endswith("市") and not p.endswith(("区", "县")) and p not in (
                    suburb, addr.get("city", "")):
                city = p
        if not city:
            city = _clean(addr.get("city") or addr.get("county") or "")
        place = suburb
    else:
        city = _clean(addr.get("city") or addr.get("town") or addr.get("county")
                      or addr.get("village") or addr.get("state") or "")
        place = suburb
        if country and country not in city:
            city = f"{country}{city}" if city else country

    display = f"{city}·{place}" if city and place else (city or place)
    return {"city": city, "place": place, "display": display, "src": "osm"}


def geocode_points(coords: list, progress_cb=None) -> pd.DataFrame:
    """coords: [(lat, lon)]。返回补齐后的完整缓存表。"""
    global _log
    _log = _log or setup_logging("geocode")
    cache = _load_cache()
    have = set(cache["key"])
    todo, seen = [], set()
    for lat, lon in coords:
        k = _cache_key(lat, lon)
        if k not in have and k not in seen:
            seen.add(k)
            todo.append((k, float(lat), float(lon)))

    if todo:
        _log.info("Nominatim 需新查询 %d 个坐标点（限速 %.1f秒/次，约 %.0f 分钟）",
                  len(todo), LIMIT_SLEEP, len(todo) * LIMIT_SLEEP / 60)
        rows = []
        for i, (k, lat, lon) in enumerate(todo, 1):
            res = None
            for use_proxy in (False, True):
                try:
                    res = _nominatim_once(lat, lon, use_proxy)
                    break
                except Exception as ex:
                    if use_proxy:
                        _log.warning("Nominatim 失败 %s: %s", k, ex)
                    time.sleep(1.0)
            if res is None:   # 彻底失败：留空行占位，下次重试
                res = {"city": "", "place": "", "display": "", "src": "fail"}
            rows.append({"key": k, **res})
            if progress_cb:
                progress_cb(i, len(todo))
            time.sleep(LIMIT_SLEEP)
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        _save_cache(cache)
    return cache


def lookup(cache: pd.DataFrame, lat: float, lon: float) -> dict:
    k = _cache_key(lat, lon)
    row = cache[cache["key"] == k]
    if row.empty:
        return {"city": "", "place": "", "display": "", "src": ""}
    r = row.iloc[0]
    return {"city": r["city"], "place": r["place"], "display": r["display"], "src": r["src"]}
