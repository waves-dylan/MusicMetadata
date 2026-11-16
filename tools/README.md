# MusicMetadata Tools

本目录收纳 MusicMetadata 所需的绿色、免安装、单文件辅助工具。

## 1. sacd_extract.exe
- **版本**: `0.3.9.3-107-win64` (Enhanced by EuFlo)
- **开源主页**: [sacd-ripper / sacd-ripper](https://github.com/sacd-ripper/sacd-ripper)
- **架构**: x86_64 Windows
- **用途**: 用于从 SACD ISO 镜像中秒级读取光盘目录（TOC/ScarletBook），以及将 DST 无损压缩音频高效解压并转封装为 Sony DSF 格式单曲音轨。
- **配置文件**: `sacd_extract.cfg` 默认配置为 `id3tag=4`、关闭冗余文件夹和文件名附加前缀，保持纯净输出。

## 2. 自动更新与下载 (Bootstrap)
若在无二进制环境（如仅拉取 Git 代码库且未提交大型二进制）下运行，可使用本目录下的 `bootstrap.py` 自动拉取对应版本的绿色工具：
```bash
python tools/bootstrap.py
```
