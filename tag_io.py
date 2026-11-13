"""音频容器识别、元数据读取与原子安全写入（零外部加密依赖）。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from ctypes import wintypes

from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2File
from mutagen.asf import ASF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TPOS, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack


COVER_FRONT_TYPE = 3
ARTIST_PIC_TYPE = 8


AUDIO_EXTS = {
    '.flac', '.mp3', '.mp2', '.m4a', '.mp4', '.m4b', '.alac',
    '.ogg', '.oga', '.opus', '.wav', '.wave', '.aiff', '.aif', '.aifc',
    '.wma', '.asf', '.ape', '.wv',
}

SUPPORTS_EMBEDDED_LYRICS = {'flac', 'mp3', 'mp4', 'ogg', 'opus', 'asf', 'aiff', 'wav', 'ape', 'wv'}
PREFER_EXTERNAL_LRC_CONTAINERS = {'wav', 'ape', 'wv'}
CONVERT_TARGET_FORMATS = ('flac', 'mp3', 'm4a', 'ogg', 'opus', 'wav', 'aiff')

_EXT_FAMILY = {
    'flac': 'flac', 'mp3': 'mp3', 'mp2': 'mp3', 'mpa': 'mp3',
    'm4a': 'mp4', 'mp4': 'mp4', 'm4b': 'mp4', 'alac': 'mp4',
    'ogg': 'ogg', 'oga': 'ogg', 'opus': 'ogg',
    'wav': 'wav', 'wave': 'wav', 'aiff': 'aiff', 'aif': 'aiff', 'aifc': 'aiff',
    'wma': 'asf', 'asf': 'asf', 'ape': 'ape', 'wv': 'wv',
}
_CONTAINER_FAMILY = {'opus': 'ogg', 'mp4': 'mp4', 'asf': 'asf'}

LOADERS = {
    'flac': FLAC, 'mp3': MP3, 'mp4': MP4, 'ogg': OggVorbis, 'opus': OggOpus,
    'wav': WAVE, 'aiff': AIFF, 'asf': ASF, 'ape': APEv2File, 'wv': WavPack,
}

_NETEASE_163_KEY = b"#14ljk_!\\]&0U<'("

# ==================== 纯 Python AES-128-ECB 解密（替代 pycryptodome） ====================
_SBOX = (
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
)
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1b) & 0xff if a & 0x80 else (a << 1) & 0xff


def _mul(a: int, b: int) -> int:
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    rk = list(key)
    for r in range(10):
        w = [_SBOX[rk[-3]] ^ _RCON[r], _SBOX[rk[-2]], _SBOX[rk[-1]], _SBOX[rk[-4]]]
        for k in range(4):
            rk.append(rk[-16] ^ w[k])
        for _ in range(12):
            rk.append(rk[-16] ^ rk[-4])

    out = bytearray()
    for off in range(0, len(data), 16):
        s = list(data[off:off + 16])
        for i in range(16):
            s[i] ^= rk[160 + i]
        for rnd in range(9, -1, -1):
            s = [
                s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
                s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3],
            ]
            s = [_INV_SBOX[b] ^ rk[rnd * 16 + i] for i, b in enumerate(s)]
            if rnd > 0:
                for c in range(0, 16, 4):
                    a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
                    s[c] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
                    s[c + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
                    s[c + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
                    s[c + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)
        out.extend(s)
    return bytes(out)


def decode_163_key(text: str) -> dict | None:
    """解码网易云音乐内嵌在 COMMENT/DESCRIPTION 里的 `163 key(Don't modify):...`。"""
    m = re.search(r"163 key\(Don't modify\):([A-Za-z0-9+/=]+)", text or '')
    if not m:
        return None
    try:
        enc = base64.b64decode(m.group(1))
        if not enc or len(enc) % 16:
            return None
        raw = _aes128_ecb_decrypt(_NETEASE_163_KEY, enc)
        pad = raw[-1]
        if 1 <= pad <= 16:
            raw = raw[:-pad]
        decoded = raw.decode('utf-8', errors='ignore')
        if decoded.startswith('music:'):
            decoded = decoded[6:]
        data = json.loads(decoded)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ==================== 容器嗅探与标签读写 ====================
def sniff_container(path: str) -> str | None:
    """只读文件头识别真实音频容器，识别不了返回 None。"""
    try:
        with open(path, 'rb') as fp:
            head = fp.read(16)
            if os.fstat(fp.fileno()).st_size <= 0 or len(head) < 4:
                return None
            if head[:4] == b'OggS':
                fp.seek(0)
                return 'opus' if b'OpusHead' in fp.read(128) else 'ogg'
    except OSError:
        return None
    if head[:4] == b'fLaC':
        return 'flac'
    if head[:3] == b'ID3' or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return 'mp3'
    if len(head) >= 8 and head[4:8] == b'ftyp':
        return 'mp4'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return 'wav'
    if len(head) >= 12 and head[:4] == b'FORM' and head[8:12] in (b'AIFF', b'AIFC'):
        return 'aiff'
    if head[:4] == b'MAC ':
        return 'ape'
    if head[:4] == b'wvpk':
        return 'wv'
    if head[:4] == b'0&\xb2u':
        return 'asf'
    return None


def extension_mismatch(path: str, container: str | None = None) -> bool:
    container = container or sniff_container(path)
    if not container:
        return False
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    cur = _EXT_FAMILY.get(ext)
    exp = _CONTAINER_FAMILY.get(container, container)
    return bool(cur and exp and cur != exp)


def container_family(audio, container: str | None = None) -> str:
    """将具体容器名或 mutagen 对象归并为统一标签族（id3/vorbis/mp4/asf/ape）。"""
    c = (container or '').lower()
    if c in ('flac', 'ogg', 'opus'):
        return 'vorbis'
    if c == 'mp4' or isinstance(audio, MP4):
        return 'mp4'
    if c == 'asf':
        return 'asf'
    if c in ('ape', 'wv'):
        return 'ape'
    tags_obj = getattr(audio, 'tags', audio) if audio is not None else None
    if c in ('mp3', 'wav', 'aiff') or isinstance(tags_obj, ID3):
        return 'id3'
    return ''


def ensure_tags(audio):
    """确保 mutagen 音频对象已初始化 tags 容器并返回。"""
    if audio is None:
        return None
    if getattr(audio, 'tags', None) is None and hasattr(audio, 'add_tags'):
        audio.add_tags()
    return getattr(audio, 'tags', None)


def iter_tag_values(val) -> list:
    if val is None:
        return []
    if hasattr(val, '__iter__') and not isinstance(val, (str, bytes, bytearray)):
        return list(val)
    return [val]


def delete_keys_ci(tags, *keys: str) -> bool:
    """按大小写无关方式删除指定键，返回是否至少删掉一项。"""
    if tags is None or not keys:
        return False
    targets = {k.lower() for k in keys}
    matched = [k for k in list(tags.keys()) if str(k).lower() in targets]
    for k in matched:
        del tags[k]
    return bool(matched)


def _as_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return ', '.join(str(x).strip() for x in value if x not in (None, '') and str(x).strip())
    return str(value).strip()


def _tag_text(tags, *keys: str) -> str:
    if tags is None:
        return ''
    for key in keys:
        try:
            val = tags.get(key)
        except Exception:
            val = None
        if val:
            txt = _as_text(val)
            if txt:
                return txt
    return ''


def _id3_text(tags, *frame_ids: str) -> str:
    if not tags:
        return ''
    for fid in frame_ids:
        try:
            frames = tags.getall(fid)
        except Exception:
            frames = []
        parts = [_as_text(getattr(f, 'text', None) or str(f)) for f in frames]
        parts = [p for p in parts if p]
        if parts:
            return ', '.join(parts)
    return ''


def _parse_track_int(raw: str) -> int | None:
    head = str(raw or '').strip().split('/')[0].strip()
    return int(head) if head.isdigit() else None


_KV_MAP = {
    'vorbis': {
        'title': ('TITLE',), 'artist': ('ARTIST',), 'albumartist': ('ALBUMARTIST', 'ALBUM ARTIST'),
        'album': ('ALBUM',), 'date': ('DATE', 'YEAR'),
        'composer': ('COMPOSER',), 'performer': ('PERFORMER',), 'genre': ('GENRE',),
        'tracknumber': ('TRACKNUMBER', 'TRACK'), 'discnumber': ('DISCNUMBER', 'DISC'),
    },
    'asf': {
        'title': ('Title',), 'artist': ('Author',), 'albumartist': ('WM/AlbumArtist',),
        'album': ('WM/AlbumTitle',), 'date': ('WM/Year',),
        'composer': ('WM/Composer',), 'performer': ('WM/Performer', 'Performer'), 'genre': ('WM/Genre',),
        'tracknumber': ('WM/TrackNumber',), 'discnumber': ('WM/PartOfSet',), 'comment': ('Description',),
    },
    'ape': {
        'title': ('Title', 'TITLE'), 'artist': ('Artist', 'ARTIST'),
        'albumartist': ('Album Artist', 'ALBUM ARTIST', 'ALBUMARTIST'), 'album': ('Album', 'ALBUM'),
        'date': ('Year', 'DATE'),
        'composer': ('Composer', 'COMPOSER'), 'performer': ('Performer', 'PERFORMER'), 'genre': ('Genre', 'GENRE'),
        'tracknumber': ('Track', 'TRACK', 'TRACKNUMBER'),
        'discnumber': ('Disc', 'DISC', 'DISCNUMBER'), 'comment': ('Comment', 'COMMENT'),
    },
}
_ID3_MAP = {
    'title': ('TIT2',), 'artist': ('TPE1',), 'albumartist': ('TPE2',), 'album': ('TALB',),
    'date': ('TDRC', 'TYER'), 'composer': ('TCOM',), 'genre': ('TCON',),
    'tracknumber': ('TRCK',), 'discnumber': ('TPOS',), 'comment': ('COMM',),
}


def _read_id3_performer(tags) -> str:
    vals: list[str] = []
    for fid in ('TXXX', 'TMCL', 'TIPL', 'IPLS'):
        try:
            frames = tags.getall(fid)
        except Exception:
            frames = []
        for f in frames:
            if fid == 'TXXX':
                if str(getattr(f, 'desc', '') or '').strip().upper() == 'PERFORMER':
                    txt = _as_text(getattr(f, 'text', None))
                    if txt:
                        vals.append(txt)
            else:
                for pair in getattr(f, 'people', None) or []:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                        role, person = str(pair[0] or '').strip(), str(pair[1] or '').strip()
                        if person:
                            vals.append(f'{person} ({role})' if role else person)
    return ', '.join(dict.fromkeys(vals))


_EMBEDDED_LYRICS_KEYS = {
    'vorbis': ('LYRICS', 'UNSYNCEDLYRICS', 'UNSYNCED LYRICS'),
    'asf': ('WM/Lyrics',),
    'ape': ('Lyrics', 'LYRICS'),
}


def _read_embedded_lyrics(tags, container: str | None) -> str:
    if tags is None:
        return ''
    fam = container_family(tags, container)
    if fam == 'id3':
        try:
            frames = tags.getall('USLT')
        except Exception:
            frames = []
        parts = [str(getattr(f, 'text', '') or '').strip() for f in frames]
        return '\n\n'.join(p for p in parts if p)
    if fam == 'mp4':
        return _as_text(tags.get('\xa9lyr'))
    keys = _EMBEDDED_LYRICS_KEYS.get(fam, ())
    return _tag_text(tags, *keys) if keys else ''


def get_lrc_path(audio_path: str) -> str:
    return os.path.splitext(audio_path)[0] + '.lrc'


def read_lrc_file(audio_path: str) -> str:
    lrc_path = get_lrc_path(audio_path)
    if not os.path.isfile(lrc_path):
        return ''
    try:
        with open(lrc_path, 'rb') as fp:
            raw = fp.read()
        for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
            try:
                return raw.decode(enc).strip()
            except UnicodeDecodeError:
                continue
        return raw.decode('utf-8', errors='replace').strip()
    except OSError:
        return ''


def read_pictures(audio, container: str | None = None) -> list[dict]:
    out: list[dict] = []
    try:
        if isinstance(audio, FLAC) or container == 'flac':
            for p in getattr(audio, 'pictures', []) or []:
                out.append({'mime': p.mime or '', 'type': int(p.type), 'data': p.data})
        elif isinstance(audio, MP4) or container == 'mp4':
            for c in (getattr(audio, 'tags', None) or {}).get('covr', []):
                mime = 'image/png' if getattr(c, 'imageformat', None) == MP4Cover.FORMAT_PNG else 'image/jpeg'
                out.append({'mime': mime, 'type': COVER_FRONT_TYPE, 'data': bytes(c)})
        elif isinstance(getattr(audio, 'tags', None), ID3):
            for k in audio.tags.getall('APIC'):
                out.append({'mime': k.mime or '', 'type': int(k.type), 'data': k.data})
        elif getattr(audio, 'tags', None) is not None and 'metadata_block_picture' in audio.tags:
            for v in audio.tags['metadata_block_picture']:
                pic = Picture(base64.b64decode(v))
                out.append({'mime': pic.mime or '', 'type': int(pic.type), 'data': pic.data})
        elif getattr(audio, 'tags', None) is not None:
            for cov_key, p_type in (('Cover Art (front)', COVER_FRONT_TYPE), ('Cover Art (artist)', ARTIST_PIC_TYPE), ('Artist Art', ARTIST_PIC_TYPE)):
                val = audio.tags.get(cov_key)
                if val:
                    data = bytes(val.value) if hasattr(val, 'value') else bytes(val)
                    if b'\x00' in data[:64]:
                        data = data.split(b'\x00', 1)[1]
                    out.append({'mime': 'image/jpeg', 'type': p_type, 'data': data})
    except Exception:
        pass
    return out


def read_audio_info(path: str) -> dict:
    """读取音频基础标签、扩展元数据、歌词、时长与音轨号状态。"""
    info = {
        'path': path, 'container': None, 'extension_mismatch': False,
        'title': '', 'artist': '', 'albumartist': '', 'album': '', 'date': '',
        'composer': '', 'performer': '', 'genre': '',
        'tracknumber': '', 'track_int': None, 'discnumber': '', 'disc_int': None,
        'comment': '', 'raw_comment': '', 'has_163_key': False,
        'lyrics': '', 'has_embedded_lyrics': False, 'has_lrc_file': False, 'has_lyrics': False,
        'netease_meta': None, 'duration': 0.0, 'error': None,
        'pics': [],
    }
    if not os.path.exists(path):
        info['error'] = '文件不存在'
        return info
    container = sniff_container(path)
    info['container'] = container
    loader = LOADERS.get(container) if container else None
    if not container or loader is None:
        info['error'] = '无法识别的音频容器' if not container else f'暂不支持 {container} 格式'
        return info
    info['extension_mismatch'] = extension_mismatch(path, container)
    try:
        audio = loader(path)
        info['duration'] = float(getattr(audio.info, 'length', 0.0) or 0.0)
    except Exception as err:
        info['error'] = f'{type(err).__name__}: {err}'
        return info

    tags = getattr(audio, 'tags', None)
    emb_lyrics = ''
    if tags:
        if container in ('flac', 'ogg', 'opus', 'asf', 'ape', 'wv'):
            fam = 'vorbis' if container in ('flac', 'ogg', 'opus') else ('asf' if container == 'asf' else 'ape')
            for field, keys in _KV_MAP[fam].items():
                info[field] = _tag_text(tags, *keys)
            if fam == 'vorbis':
                cmt = _tag_text(tags, 'COMMENT')
                desc = _tag_text(tags, 'DESCRIPTION')
                info['comment'] = cmt or ('' if '163 key' in desc else desc)
                info['raw_comment'] = _tag_text(tags, 'DESCRIPTION', 'COMMENT')
            else:
                info['raw_comment'] = info['comment']
        elif isinstance(tags, ID3):
            for field, fids in _ID3_MAP.items():
                info[field] = _id3_text(tags, *fids)
            info['performer'] = _read_id3_performer(tags)
            info['raw_comment'] = info['comment']
        elif container == 'mp4':
            for field, key in (('title', '\xa9nam'), ('artist', '\xa9ART'), ('albumartist', 'aART'),
                               ('album', '\xa9alb'), ('date', '\xa9day'), ('composer', '\xa9wrt'),
                               ('genre', '\xa9gen'), ('comment', '\xa9cmt')):
                info[field] = _as_text(tags.get(key))
            perf = tags.get('----:com.apple.iTunes:PERFORMER')
            if perf:
                parts = []
                for item in (perf if isinstance(perf, (list, tuple)) else [perf]):
                    txt = bytes(item).decode('utf-8', errors='replace').strip() if isinstance(item, (bytes, bytearray)) else str(item).strip()
                    if txt:
                        parts.append(txt)
                info['performer'] = ', '.join(parts)
            info['raw_comment'] = info['comment']
            for field, key in (('tracknumber', 'trkn'), ('discnumber', 'disk')):
                val = tags.get(key)
                if val and isinstance(val, list) and val[0] and val[0][0]:
                    info[field] = str(val[0][0])
        emb_lyrics = _read_embedded_lyrics(tags, container)
    info['pics'] = read_pictures(audio, container)

    lrc_text = read_lrc_file(path)
    info['has_embedded_lyrics'] = bool(emb_lyrics)
    info['has_lrc_file'] = bool(lrc_text)
    info['has_lyrics'] = bool(emb_lyrics or lrc_text)
    info['lyrics'] = emb_lyrics or lrc_text
    info['track_int'] = _parse_track_int(info['tracknumber'])
    info['disc_int'] = _parse_track_int(info['discnumber'])
    raw_cmt = info['raw_comment'] or info['comment']
    if raw_cmt and '163 key' in raw_cmt:
        info['has_163_key'] = True
        info['netease_meta'] = decode_163_key(raw_cmt)
    return info


def has_valid_tracknumber(info: dict) -> bool:
    num = info.get('track_int')
    return num is not None and num > 0


# ==================== 文件占用检测 & 原子写入 ====================
def find_lockers(path: str) -> list[tuple[int, str]]:
    if os.name != 'nt':
        return []
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]
        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', FILETIME)]
        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ('Process', RM_UNIQUE_PROCESS), ('strAppName', wintypes.WCHAR * 256),
                ('strServiceShortName', wintypes.WCHAR * 64), ('ApplicationType', wintypes.DWORD),
                ('AppStatus', wintypes.ULONG), ('TSSessionId', wintypes.DWORD), ('bRestartable', wintypes.BOOL),
            ]
        rm = ctypes.WinDLL('Rstrtmgr')
        sess, key = wintypes.DWORD(), ctypes.create_unicode_buffer(33)
        if rm.RmStartSession(ctypes.byref(sess), 0, key) != 0:
            return []
        try:
            files = (wintypes.LPCWSTR * 1)(os.path.abspath(path))
            if rm.RmRegisterResources(sess, 1, files, 0, None, 0, None) != 0:
                return []
            needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD()
            if rm.RmGetList(sess, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons)) != 234 or not needed.value:
                return []
            infos = (RM_PROCESS_INFO * needed.value)()
            count = wintypes.UINT(needed.value)
            if rm.RmGetList(sess, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons)) != 0:
                return []
            own = os.getpid()
            return [(int(infos[i].Process.dwProcessId), str(infos[i].strAppName))
                    for i in range(count.value) if int(infos[i].Process.dwProcessId) != own]
        finally:
            rm.RmEndSession(sess)
    except Exception:
        return []


def describe_lockers(path: str) -> str:
    seen: list[str] = []
    for pid, name in find_lockers(path):
        lbl = f'{name} (PID {pid})' if name else f'PID {pid}'
        if lbl not in seen:
            seen.append(lbl)
    return '、'.join(seen)


def bump_mtime(path: str, min_prev_mtime: float = 0.0) -> float:
    """将修改时间严格前推，兼容 exFAT/FAT32 的 2 秒精度。"""
    try:
        cur = os.path.getmtime(path)
    except OSError:
        cur = 0.0
    base = max(cur, min_prev_mtime)
    target = max(time.time(), base + 2.0)
    for step in (0.0, 2.0, 4.0, 6.0):
        t = target if step == 0.0 else base + step + 2.0
        os.utime(path, (t, t))
        try:
            actual = os.path.getmtime(path)
        except OSError:
            return t
        if actual > base:
            return actual
    return target


def _remove_file_with_retry(path: str, attempts: int = 5, base_delay: float = 0.2) -> bool:
    """删除临时文件，遇到瞬时扫描占用时短暂退避重试，避免残留 .musictmp。"""
    if not path:
        return True
    for r in range(max(1, attempts)):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if r + 1 < attempts:
                time.sleep(base_delay * (r + 1))
        except OSError:
            return False
    return not os.path.exists(path)


_FOLDER_MUSICTMP_CACHE: dict[str, tuple[float, bool]] = {}


def cleanup_stale_musictmp_for(audio_path: str) -> int:
    """清理同目录下与指定音频文件同名的历史 .musictmp 临时副本。"""
    if not audio_path or not os.path.isfile(audio_path):
        return 0
    folder = os.path.dirname(audio_path) or '.'
    now = time.monotonic()
    cached = _FOLDER_MUSICTMP_CACHE.get(folder)
    if cached is not None and (now - cached[0]) < 2.0 and not cached[1]:
        return 0

    stem = os.path.splitext(os.path.basename(audio_path))[0]
    pat = re.compile(r'^' + re.escape(stem) + r'\.[a-z0-9_]+\.musictmp$', re.IGNORECASE)
    removed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    has_musictmp = any(fn.endswith('.musictmp') for fn in names)
    _FOLDER_MUSICTMP_CACHE[folder] = (now, has_musictmp)
    if not has_musictmp:
        return 0
    for fn in names:
        if pat.match(fn) and _remove_file_with_retry(os.path.join(folder, fn), attempts=3, base_delay=0.15):
            removed += 1
    return removed


def atomic_mutate_audio(
    path: str,
    mutator_fn,
    verify_fn=None,
    attempts: int = 4,
    retry_delay: float = 0.5,
) -> tuple[bool, str]:
    """改写标签/封面并回读校验（优先快速就地安全写入，失败时退避至原子临时副本）。"""
    if not path or not os.path.exists(path):
        return False, '文件不存在'
    try:
        orig_mtime = os.path.getmtime(path)
    except OSError:
        orig_mtime = 0.0
    container = sniff_container(path)
    loader = LOADERS.get(container) if container else None
    if not container or loader is None:
        return False, '无法识别的音频容器' if not container else f'暂不支持写入 {container} 格式'

    last_issue = '写入失败'

    # 1. 快速优先路径：直接就地更新（零拷贝整文件，性能提升 5~20 倍）
    for direct_attempt in range(max(1, attempts)):
        try:
            audio = loader(path)
            if getattr(audio, 'tags', None) is None and hasattr(audio, 'add_tags'):
                audio.add_tags()
            changed, msg = mutator_fn(audio, container, path)
            if not changed:
                return True, msg or '无需修改'
            audio.save(v2_version=3) if container == 'mp3' else audio.save()
            try:
                bump_mtime(path, min_prev_mtime=orig_mtime)
            except OSError:
                pass
            if verify_fn is not None:
                ok_dst, err_dst = verify_fn(path, container)
                if not ok_dst:
                    last_issue = err_dst or '写入后回读校验未生效'
                    break  # 回退到临时副本模式
            return True, msg or '已写入'
        except PermissionError:
            if direct_attempt + 1 < attempts:
                time.sleep(min(retry_delay * (2 ** direct_attempt), 2.0))
                continue
            holders = describe_lockers(path)
            last_issue = '文件被占用' + (f'：{holders}' if holders else '')
            return False, last_issue
        except Exception as err:
            last_issue = f'{type(err).__name__}: {err}'
            break  # 回退到临时副本模式

    # 2. 安全回退路径：.musictmp 临时副本改写并原子替换
    stem = os.path.splitext(os.path.basename(path))[0]
    folder = os.path.dirname(path) or '.'
    for attempt in range(max(1, attempts)):
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix=stem + '.', suffix='.musictmp', dir=folder)
            os.close(fd)
            shutil.copy2(path, temp_path)
            audio = loader(temp_path)
            if getattr(audio, 'tags', None) is None and hasattr(audio, 'add_tags'):
                audio.add_tags()
            changed, msg = mutator_fn(audio, container, temp_path)
            if not changed:
                return True, msg or '无需修改'
            audio.save(v2_version=3) if container == 'mp3' else audio.save()

            if verify_fn is not None:
                ok_v, err_v = verify_fn(temp_path, container)
                if not ok_v:
                    last_issue = err_v or '临时文件校验失败'
                    continue

            last_perm = None
            for r in range(4):
                try:
                    os.replace(temp_path, path)
                    temp_path = None
                    break
                except PermissionError as err:
                    last_perm = err
                    if r < 3:
                        time.sleep(0.4 * (r + 1))
            if temp_path is not None:
                raise last_perm

            try:
                bump_mtime(path, min_prev_mtime=orig_mtime)
            except OSError:
                pass
            if verify_fn is not None:
                ok_dst, err_dst = verify_fn(path, container)
                if not ok_dst:
                    last_issue = err_dst or '替换后回读校验未生效'
                    continue
            return True, msg or '已写入'
        except PermissionError:
            holders = describe_lockers(path)
            last_issue = '文件被占用' + (f'：{holders}' if holders else '')
        except Exception as err:
            last_issue = f'{type(err).__name__}: {err}'
        finally:
            if temp_path and os.path.exists(temp_path):
                _remove_file_with_retry(temp_path)
        if attempt + 1 < attempts:
            time.sleep(min(retry_delay * (2 ** attempt), 3.0))
    return False, last_issue


_TRACK_KV_MAP = {
    'vorbis': ('TRACKNUMBER', 'DISCNUMBER', ('DISCNUMBER', 'DISC')),
    'asf': ('WM/TrackNumber', 'WM/PartOfSet', ('WM/PartOfSet',)),
    'ape': ('Track', 'Disc', ('Disc', 'DISC', 'DISCNUMBER')),
}


def apply_track_to_audio(audio, container: str, tracknumber: int | None = None,
                         discnumber: int | None = None, write_disc_if_missing: bool = False) -> None:
    tags = ensure_tags(audio)
    fam = container_family(audio, container)
    tn = str(int(tracknumber)) if tracknumber is not None else ''
    dn = str(int(discnumber)) if discnumber else ''
    if fam in _TRACK_KV_MAP and tags is not None:
        tn_key, dn_key, dn_read_keys = _TRACK_KV_MAP[fam]
        if tn:
            tags[tn_key] = [tn]
        if write_disc_if_missing and dn and not _tag_text(tags, *dn_read_keys):
            tags[dn_key] = [dn]
    elif fam == 'id3' and tags is not None:
        if tn:
            tags.setall('TRCK', [TRCK(encoding=3, text=[tn])])
        if write_disc_if_missing and dn and not _id3_text(tags, 'TPOS'):
            tags.setall('TPOS', [TPOS(encoding=3, text=[dn])])
    elif fam == 'mp4' and tags is not None:
        if tn:
            ex = tags.get('trkn')
            tot = ex[0][1] if (ex and isinstance(ex[0], tuple) and len(ex[0]) > 1) else 0
            tags['trkn'] = [(int(tracknumber), int(tot or 0))]
        if write_disc_if_missing and discnumber and not tags.get('disk'):
            tags['disk'] = [(int(discnumber), 0)]
    else:
        raise RuntimeError(f'暂不支持写入 {container} 格式')


def write_tracknumber(
    path: str,
    tracknumber: int,
    discnumber: int | None = None,
    write_disc_if_missing: bool = False,
    overwrite: bool = False,
    attempts: int = 4,
    retry_delay: float = 0.5,
) -> tuple[bool, str]:
    """原子写入音轨号并回读校验。"""
    state = read_audio_info(path)
    if state.get('error'):
        return False, str(state['error'])
    target_tn = int(tracknumber)
    if not overwrite and has_valid_tracknumber(state):
        return (True, '已有相同音轨号') if state.get('track_int') == target_tn else (False, f"已有音轨号 ({state.get('tracknumber')})，跳过覆盖")

    return atomic_mutate_audio(
        path,
        lambda aud, c, _tmp: (apply_track_to_audio(aud, c, target_tn, discnumber, write_disc_if_missing) or True, f'已写入音轨号 {target_tn}'),
        verify_fn=lambda p, _c: (True, '') if read_audio_info(p).get('track_int') == target_tn else (False, '音轨号校验失败'),
        attempts=attempts,
        retry_delay=retry_delay,
    )


# ==================== 文件名解析、歌词合并/外挂 & 格式转换 ====================
_TEMP_SUFFIX_RE = re.compile(r'^(.+)\.[a-z0-9_]{6,10}$', re.IGNORECASE)
_IDENT_RE = re.compile(r' - ([A-Za-z0-9]{5,32})(?: \(\d+\))?$')
_TRACK_PREFIX_RE = re.compile(r'^(?:(?:\d{1,2}-)?\d{1,2}|0\d{2})\s*(?:\.\s*|-\s+|_\s*)(?=\S)')
_TRACK_DOT_PREFIX_RE = re.compile(r'^(?:(?:\d{1,2}-)?\d{1,2}|0\d{2})\.(?=\S)')
_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\s+')
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')
_GENERIC_DIRS = {
    'music', 'downloads', 'cloudmusicdownload', 'vipsongsdownload', '已下载音乐',
    'applemusicclient', 'kugoumusicclient', 'kuwomusicclient', 'migumusicclient',
    'neteasemusicclient', 'qqmusicclient', 'spotifymusicclient',
}
_LRC_TIME_TOKEN_RE = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')


def _clean_filename_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _TEMP_SUFFIX_RE.match(stem)
    return m.group(1) if m else stem


def _strip_safe_track_prefix(text: str) -> str:
    s = str(text or '').strip()
    m = _TRACK_PREFIX_RE.match(s)
    if m:
        return s[m.end():].strip()
    m2 = _TRACK_DOT_PREFIX_RE.match(s)
    if m2:
        return s[m2.end():].strip()
    return s


def parse_filename_clues(filename_or_path: str, folder: str = '') -> dict:
    """从文件名与目录名安全推断 song_id / title / artist / album（不误伤数字开头歌名）。"""
    fname = os.path.basename(filename_or_path)
    fld = folder or os.path.dirname(filename_or_path)
    stem = _clean_filename_stem(fname)
    m = _IDENT_RE.search(stem)
    ident, part = (m.group(1), stem[:m.start()].strip()) if m else ('', stem)
    title, artist, album = '', '', ''

    bm = re.match(r'^(.+?)\s*-\s*([^\[\]]+?)\[([^\[\]]+)\]$', part)
    if bm:
        title, artist, album = bm.group(1).strip(), bm.group(2).strip(), bm.group(3).strip().replace('~', '/')
    elif not ident:
        cleaned = _strip_safe_track_prefix(part)
        if ' - ' in cleaned:
            left, right = cleaned.split(' - ', 1)
            if re.match(r'^\d{1,3}$', left.strip()) and int(left.strip()) <= 99:
                title = right.strip()
            else:
                artist, title = left.strip(), right.strip()
        elif cleaned.count('-') == 1 and _CJK_RE.search(cleaned):
            left, right = cleaned.split('-', 1)
            ls, rs = left.strip(), right.strip()
            if ls and rs and not ls.isdigit():
                artist, title = ls, rs
            else:
                title = cleaned
        else:
            title = cleaned
    else:
        title = part

    if not album and fld:
        dn = os.path.basename(fld).strip()
        if dn and dn.lower() not in _GENERIC_DIRS:
            cd = _DATE_DIR_RE.sub('', dn).strip()
            if cd and cd.lower() not in _GENERIC_DIRS:
                album = cd
    return {'identifier': ident, 'inferred_title': title, 'inferred_artist': artist, 'inferred_album': album}


def _norm_lrc_ts(mm: str, ss: str, frac: str | None) -> tuple[int, int, int]:
    f = (frac or '0').ljust(3, '0')[:3]
    return int(mm), int(ss), int(f) // 10


def merge_lyrics(original: str, translation: str) -> str:
    """按 LRC 时间标签将原文歌词与翻译歌词逐行合并。"""
    orig = str(original or '').replace('\r\n', '\n').strip()
    trans = str(translation or '').replace('\r\n', '\n').strip()
    if not orig:
        return trans
    if not trans:
        return orig

    trans_exact: dict[str, str] = {}
    trans_norm: dict[tuple[int, int, int], str] = {}
    for raw_line in trans.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = list(_LRC_TIME_TOKEN_RE.finditer(line))
        if not tokens:
            continue
        text = _LRC_TIME_TOKEN_RE.sub('', line).strip()
        if not text or text == '//':
            continue
        for tok in tokens:
            trans_exact.setdefault(tok.group(0), text)
            trans_norm.setdefault(_norm_lrc_ts(tok.group(1), tok.group(2), tok.group(3)), text)

    if not trans_exact:
        return orig

    out: list[str] = []
    for raw_line in orig.splitlines():
        line = raw_line.rstrip()
        out.append(line)
        tokens = list(_LRC_TIME_TOKEN_RE.finditer(line))
        if not tokens:
            continue
        orig_text = _LRC_TIME_TOKEN_RE.sub('', line).strip()
        if not orig_text:
            continue
        tr_text = ''
        for tok in tokens:
            tr_text = trans_exact.get(tok.group(0)) or trans_norm.get(_norm_lrc_ts(tok.group(1), tok.group(2), tok.group(3))) or ''
            if tr_text:
                break
        if tr_text and tr_text != orig_text:
            prefix = ''.join(tok.group(0) for tok in tokens)
            out.append(f'{prefix}{tr_text}')
    return '\n'.join(out)


def write_lrc_file(
    audio_path: str,
    lyrics: str,
    *,
    title: str = '',
    artist: str = '',
    album: str = '',
    overwrite: bool = True,
) -> tuple[bool, str]:
    """将歌词原子写入同名 .lrc 文件（自动补充缺失的 [ti:]/[ar:]/[al:] 头）。"""
    txt = str(lyrics or '').replace('\r\n', '\n').strip()
    if not txt:
        return False, '歌词内容为空'
    lrc_path = get_lrc_path(audio_path)
    if not overwrite and os.path.isfile(lrc_path):
        return True, '外置 .lrc 已存在，跳过覆盖'

    low = txt.lower()
    headers: list[str] = []
    if title and '[ti:' not in low:
        headers.append(f'[ti:{title}]')
    if artist and '[ar:' not in low:
        headers.append(f'[ar:{artist}]')
    if album and '[al:' not in low:
        headers.append(f'[al:{album}]')
    content = ('\n'.join(headers) + '\n' + txt if headers else txt) + '\n'

    folder = os.path.dirname(lrc_path) or '.'
    stem = os.path.splitext(os.path.basename(lrc_path))[0]
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=stem + '.', suffix='.lrctmp', dir=folder)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(content)
        os.replace(tmp_path, lrc_path)
        tmp_path = None
        return True, f'已保存外置歌词 {os.path.basename(lrc_path)}'
    except OSError as err:
        return False, f'写入 .lrc 失败: {err}'
    finally:
        if tmp_path and os.path.exists(tmp_path):
            _remove_file_with_retry(tmp_path, attempts=3, base_delay=0.1)


def delete_lrc_file(audio_path: str) -> tuple[bool, str]:
    """删除同名外置 .lrc 文件。"""
    lrc_path = get_lrc_path(audio_path)
    if not os.path.isfile(lrc_path):
        return True, '无外置 .lrc 文件'
    if _remove_file_with_retry(lrc_path, attempts=4, base_delay=0.15):
        return True, f'已删除 {os.path.basename(lrc_path)}'
    return False, f'删除 {os.path.basename(lrc_path)} 失败'


def find_ffmpeg() -> str | None:
    return shutil.which('ffmpeg')


def convert_audio_file(
    input_path: str,
    target_format: str,
    *,
    overwrite: bool = False,
    delete_source: bool = False,
    copy_sidecar_lrc: bool = True,
    post_hook=None,
) -> tuple[bool, str, str]:
    """使用 ffmpeg 将音频转为目标格式（保留元数据，支持后置封面/标签校验与原子替换）。"""
    fmt = str(target_format or '').strip().lower().lstrip('.')
    if fmt not in CONVERT_TARGET_FORMATS:
        return False, f'不支持的目标格式: {fmt}', ''
    if not input_path or not os.path.isfile(input_path):
        return False, '源文件不存在', ''
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        return False, '未找到 ffmpeg，请先安装 ffmpeg 并加入系统 PATH', ''

    folder = os.path.dirname(input_path) or '.'
    stem = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(folder, f'{stem}.{fmt}')
    if os.path.normcase(os.path.abspath(input_path)) == os.path.normcase(os.path.abspath(output_path)):
        return True, f'已是 .{fmt} 格式，跳过', output_path
    if os.path.exists(output_path) and not overwrite:
        return False, f'目标文件已存在（开启覆盖可替换）: {os.path.basename(output_path)}', output_path

    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=stem + '.', suffix=f'.musictmp.{fmt}', dir=folder)
        os.close(fd)
        cmd = [
            ffmpeg_bin, '-y', '-hide_banner', '-loglevel', 'error',
            '-i', input_path,
            '-map', '0:a:0',
            '-map_metadata', '0',
            temp_path,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if proc.returncode != 0:
            err_txt = (proc.stderr or proc.stdout or '').strip()
            return False, f'ffmpeg 转换失败: {err_txt or f"exit={proc.returncode}"}', ''

        if post_hook is not None:
            ok_hook, hook_msg = post_hook(input_path, temp_path, fmt)
            if not ok_hook:
                return False, hook_msg or '转换后处理失败', ''

        chk = read_audio_info(temp_path)
        if chk.get('error'):
            return False, f"转换输出校验失败: {chk['error']}", ''

        os.replace(temp_path, output_path)
        temp_path = None
        try:
            bump_mtime(output_path)
        except OSError:
            pass

        if copy_sidecar_lrc:
            src_lrc = get_lrc_path(input_path)
            dst_lrc = get_lrc_path(output_path)
            if os.path.isfile(src_lrc) and os.path.normcase(src_lrc) != os.path.normcase(dst_lrc):
                if overwrite or not os.path.exists(dst_lrc):
                    try:
                        shutil.copy2(src_lrc, dst_lrc)
                    except OSError:
                        pass

        if delete_source:
            _remove_file_with_retry(input_path, attempts=4, base_delay=0.2)

        return True, f'已转换为 {os.path.basename(output_path)}', output_path
    except Exception as err:
        return False, f'{type(err).__name__}: {err}', ''
    finally:
        if temp_path and os.path.exists(temp_path):
            _remove_file_with_retry(temp_path, attempts=3, base_delay=0.15)
