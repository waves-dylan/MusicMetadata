"""测试公共辅助工具、最小有效音频样本构造器与 HTTP/Tk 模拟辅助。"""

from __future__ import annotations

import gc
import io
import os
import shutil
import struct
import tempfile
import unittest
from contextlib import contextmanager

from PIL import Image

from track_lookup import clear_caches

SAMPLE_163_KEY = (
    "163 key(Don't modify):L64FU3W4YxX3ZFTmbZ+8/eam10XVQrPtLahYtNC7enAJ9mlpBgvX0sr9GKudLNFQ"
    "VqcxU007NFWqDNgyHWMFmx/37KP97Ivn3ajP/wIGzSmzQKvvty3u0gg+28Au2RZXnwfUFj1HoVoD5tNzv43Fe"
    "Rhba8rBx+opuKBKoylQgQ06mjmx9CF5o9WhjPHqM5amcFZH8uRY1bC5xQrntTzvfOb47xtQDR+8/DhYvOjZ6"
    "GrVD4wgUjmx9Y+fcSSg7cMwu0uPzCfPrs5aYeyLvNjJZD4giJqztMUu+JHGWPVEOCGh27ppeYGQkUFY/yX7b"
    "OxO7/fM4KP8rSr3NVHX1DJ7YpohwTuET67c398SW3CJzpZTVRs7XjCrjnkjH47dmPDDcTAl1NgcXX9zDWya"
    "igNOisUzGCa4hBKK3ws64NUj2b1/RC5+6s5tboJINozD5ikBiV3SRqy4aHIQvEQ2I5JhdUW8xXqdYKhPvkDu"
    "vlS8tuPEFz9cBUmprnw7+K1CiXQzCEzcNVM1G2krN+jUBL2XHmEyiQAyt9BMxC8OAdQJS3eEBRLUPr+wvmWt"
    "0daegjPsfErLy61v+0SVDqyHokAJQbYX/rdcDato6fIaIrw9xAk="
)


class FakeHTTPResponse:
    """urllib.request.urlopen 上下文管理器返回值替身。"""

    def __init__(self, body: bytes | str):
        self.data = body.encode('utf-8') if isinstance(body, str) else body

    def read(self) -> bytes:
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@contextmanager
def offscreen_tk():
    """在离屏位置安全创建并销毁 tkinter.Tk（退出前先 gc.collect 避免 Tcl_AsyncDelete）。"""
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        gc.collect()
        root.destroy()


class TempTrackTestCase(unittest.TestCase):
    """提供临时目录与最小有效 MP3/FLAC/WAV/JPEG 样本的测试基类。"""

    def setUp(self):
        clear_caches()
        self.tmp = tempfile.mkdtemp(prefix='fb_track_test_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_minimal_mp3(self, filename: str = 'test.mp3') -> str:
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b'\x00' * 413
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(frame * 3)
        return path

    def _make_minimal_flac(self, filename: str = 'test.flac') -> str:
        si = bytearray(34)
        struct.pack_into('>HH', si, 0, 4096, 4096)
        val = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
        struct.pack_into('>Q', si, 10, val)
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(b'fLaC' + struct.pack('>I', 0x80000022) + bytes(si))
        return path

    def _make_minimal_wav(self, filename: str = 'test.wav') -> str:
        pcm_data = b'\x00\x00' * 2205
        fmt_chunk = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 44100, 88200, 2, 16)
        data_chunk = b'data' + struct.pack('<I', len(pcm_data)) + pcm_data
        riff = b'RIFF' + struct.pack('<I', 4 + len(fmt_chunk) + len(data_chunk)) + b'WAVE' + fmt_chunk + data_chunk
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(riff)
        return path

    @staticmethod
    def _make_jpeg_bytes(width: int = 200, height: int = 200, color: tuple[int, int, int] = (40, 120, 200)) -> bytes:
        im = Image.new('RGB', (width, height), color)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90)
        return buf.getvalue()
