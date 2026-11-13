"""图像底层处理：规范化 JPEG、感知哈希比对、尺寸缩放与图片元数据嗅探。"""

from __future__ import annotations

import io
import struct

from PIL import Image

from core.constants import (
    ARTIST_JPEG_QUALITY,
    ARTIST_MAX_BYTES,
    ARTIST_MAX_SIDE,
    JPEG_QUALITY,
    MAX_SIDE,
    SAME_ART_DISTANCE,
)

_SNIFF_IMAGE_CACHE: dict[tuple, tuple[str, int, int] | None] = {}
_AHASH_CACHE: dict[tuple, int] = {}
_NORM_JPEG_CACHE: dict[tuple, bytes | None] = {}
_NORM_ARTIST_JPEG_CACHE: dict[tuple, bytes | None] = {}


def _lanczos():
    """兼容多版本 PIL 的高质量重采样算法。"""
    return getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', getattr(Image, 'LANCZOS', 1))


def sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """嗅探图片二进制格式与 (宽, 高) 尺寸，支持 JPEG/PNG 头部超快速解析。"""
    if not data or len(data) < 16:
        return None
    cache_key = (len(data), data[:48], data[-48:])
    if cache_key in _SNIFF_IMAGE_CACHE:
        return _SNIFF_IMAGE_CACHE[cache_key]

    # Fast header check for JPEG
    if data[:2] == b'\xff\xd8':
        try:
            idx = 2
            d_len = len(data)
            while idx < d_len - 8:
                if data[idx] != 0xff:
                    idx += 1
                    continue
                marker = data[idx + 1]
                if marker in (0xc0, 0xc1, 0xc2, 0xc3):
                    h, w = struct.unpack('>HH', data[idx + 5:idx + 9])
                    res = ('JPEG', int(w), int(h))
                    if len(_SNIFF_IMAGE_CACHE) > 5000:
                        _SNIFF_IMAGE_CACHE.clear()
                    _SNIFF_IMAGE_CACHE[cache_key] = res
                    return res
                if marker in (0xd9, 0xda):
                    break
                block_len = struct.unpack('>H', data[idx + 2:idx + 4])[0]
                idx += 2 + block_len
        except Exception:
            pass

    # Fast header check for PNG
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
        try:
            w, h = struct.unpack('>II', data[16:24])
            res = ('PNG', int(w), int(h))
            if len(_SNIFF_IMAGE_CACHE) > 5000:
                _SNIFF_IMAGE_CACHE.clear()
            _SNIFF_IMAGE_CACHE[cache_key] = res
            return res
        except Exception:
            pass

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        res = (str(im.format or ''), int(im.size[0]), int(im.size[1]))
    except Exception:
        res = None

    if len(_SNIFF_IMAGE_CACHE) > 5000:
        _SNIFF_IMAGE_CACHE.clear()
    _SNIFF_IMAGE_CACHE[cache_key] = res
    return res


def normalize_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes | None:
    """将封面规范化为最大边不超过 max_side 的优化 JPEG 数据。"""
    if not data:
        return None
    cache_key = (len(data), data[:48], data[-48:], max_side)
    if cache_key in _NORM_JPEG_CACHE:
        return _NORM_JPEG_CACHE[cache_key]
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        if max(im.size) > max_side:
            im = im.copy()
            im.thumbnail((max_side, max_side), _lanczos())
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=JPEG_QUALITY, optimize=True, progressive=False)
        out = buf.getvalue()
        if len(_NORM_JPEG_CACHE) > 2000:
            _NORM_JPEG_CACHE.clear()
        _NORM_JPEG_CACHE[cache_key] = out
        return out
    except Exception:
        return None


def normalize_artist_jpeg(
    data: bytes,
    max_side: int = ARTIST_MAX_SIDE,
    quality: int = ARTIST_JPEG_QUALITY,
    max_bytes: int = ARTIST_MAX_BYTES,
) -> bytes | None:
    """将艺术家图片规范化为最大边不超过 max_side、以质量 100 优先输出、体积严格控在 max_bytes 内的 JPEG。"""
    if not data:
        return None
    cache_key = (len(data), data[:48], data[-48:], max_side, quality, max_bytes)
    if cache_key in _NORM_ARTIST_JPEG_CACHE:
        return _NORM_ARTIST_JPEG_CACHE[cache_key]
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        cur_side = max_side
        if max(im.size) > cur_side:
            im = im.copy()
            im.thumbnail((cur_side, cur_side), _lanczos())
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=quality, optimize=True, progressive=False)
        out = buf.getvalue()

        # 若体积超出上限（默认 200KB），自适应阶梯调优以严格控重在 200KB 内
        if max_bytes and len(out) > max_bytes:
            for q in (96, 92, 88, 85, 80):
                buf = io.BytesIO()
                im.save(buf, 'JPEG', quality=q, optimize=True, progressive=False)
                candidate = buf.getvalue()
                if len(candidate) <= max_bytes:
                    out = candidate
                    break
            else:
                step_im = im.copy()
                while len(out) > max_bytes and max(step_im.size) > 300:
                    cur_side = int(max(step_im.size) * 0.85)
                    step_im.thumbnail((cur_side, cur_side), _lanczos())
                    buf = io.BytesIO()
                    step_im.save(buf, 'JPEG', quality=85, optimize=True, progressive=False)
                    out = buf.getvalue()

        if len(_NORM_ARTIST_JPEG_CACHE) > 2000:
            _NORM_ARTIST_JPEG_CACHE.clear()
        _NORM_ARTIST_JPEG_CACHE[cache_key] = out
        return out
    except Exception:
        return None


def ahash(data: bytes) -> int:
    """计算 64 位感知哈希值用于图片相似度快速比对。"""
    if not data:
        return 0
    cache_key = (len(data), data[:48], data[-48:])
    if cache_key in _AHASH_CACHE:
        return _AHASH_CACHE[cache_key]
    px = Image.open(io.BytesIO(data)).convert('L').resize((9, 8), _lanczos()).tobytes()
    bits = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    if len(_AHASH_CACHE) > 5000:
        _AHASH_CACHE.clear()
    _AHASH_CACHE[cache_key] = bits
    return bits


def ham(a: int, b: int) -> int:
    """计算两个哈希值之间的汉明距离。"""
    return bin(a ^ b).count('1')


def same_art(data_a: bytes, data_b: bytes) -> bool:
    """判断两幅图像是否为相同或高度相似艺术封面。"""
    try:
        return ham(ahash(data_a), ahash(data_b)) <= SAME_ART_DISTANCE
    except Exception:
        return False
