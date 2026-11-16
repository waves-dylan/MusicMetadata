"""自动化下载与校验绿色辅助工具（sacd_extract 等）。"""

from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import urllib.request
import zipfile

SACD_EXTRACT_URL = (
    "https://github.com/sacd-ripper/sacd-ripper/releases/download/0.3.9.3/sacd_extract-0.3.9.3-107-win64.zip"
)


def ensure_sacd_extract(tools_dir: Path | str | None = None, force: bool = False) -> Path:
    """确保 sacd_extract.exe 存在。若不存在或 force=True，则从官方 release 下载。"""
    if tools_dir is None:
        target_dir = Path(__file__).resolve().parent
    else:
        target_dir = Path(tools_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    exe_path = target_dir / "sacd_extract.exe"
    if exe_path.exists() and not force:
        return exe_path

    print(f"[Bootstrap] 正在下载 sacd_extract 工具包: {SACD_EXTRACT_URL}")
    try:
        req = urllib.request.Request(
            SACD_EXTRACT_URL,
            headers={"User-Agent": "MusicMetadata-Bootstrap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in ("sacd_extract.exe", "sacd_extract.cfg", "readme.txt", "changelog.txt"):
                if member in z.namelist():
                    extracted_path = target_dir / member
                    extracted_path.write_bytes(z.read(member))
                    print(f"[Bootstrap] 解压成功: {extracted_path.name}")

        if not exe_path.exists():
            raise FileNotFoundError(f"解压后未找到 {exe_path}")

        print(f"[Bootstrap] sacd_extract 准备就绪: {exe_path}")
        return exe_path
    except Exception as exc:
        print(f"[Bootstrap] 下载 sacd_extract 失败: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    ensure_sacd_extract(force="--force" in sys.argv)
