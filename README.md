# SongMetadata

统一集成 **foobar2000 元数据检查与缓存刷新**、**封面规范化修复**（含单专辑 `cover.jpg` 与分组标签补全）以及 **多平台音轨号联网补全** 的手动媒体库修复工具（GUI + CLI）。

## 使用方法

- **图形界面（推荐）**：直接双击运行 `SongMetadata.exe`（或运行 `python app.py`）。
  - 打开后不会自动扫描媒体库；点击 **「扫描媒体库」**（或右上角 **「选择目录...」**）后才开始扫描，扫描完成后可在 **「修复项选择」** 栏按需单独勾选/取消：**封面**、**日期**、**专辑艺术家**、**艺术家**、**作曲**、**表演者**、**流派**、**音轨号**、**碟片编号**（支持「全选」/「全不选」及「刷新 foobar 缓存」）。
  - 点击 **「一键修复已选项目」** 按勾选模块统一修复；或点击 **「仅刷新 foobar 缓存」** 仅推进文件修改时间并触发 foobar2000 重载磁盘元数据（不修改音频标签与封面）。
- **命令行模式**：
  - 仅扫描预览待修复项（不改动任何文件）：`python app.py --dry-run`
  - 仅检查并刷新 foobar 元数据缓存：`python app.py --sync-only`
  - 一键执行全部修复：`python app.py --auto`
  - 自由组合开关：`--no-track`、`--no-art`、`--no-date`、`--no-albumartist`、`--no-artist`、`--no-composer`、`--no-performer`、`--no-genre`、`--no-disc`、`--no-tags`、`--no-extra-tags`、`--no-sync`、`--folder "F:\Music"`

## 核心能力

1. **foobar2000 媒体库全字段核对与刷新**：自动解析 `library-v2.0\folders` 与 `metadb.sqlite`，不仅检测文件大小或修改时间差异，还会比对 `title`、`artist`、`album`、`albumartist`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment` 等全字段；刷新时兼容 `exFAT`/`FAT32` 的 2 秒修改时间粒度，确保 foobar2000 立即识别变更。
2. **封面修复与缺图联网补全**：
   - 自动按专辑分组并投票选出代表封面，单专辑目录生成/规范化 1000px 内 `cover.jpg`，多专辑混放目录绝不写外置图并安全撤回误放的 `cover.jpg`（备份为 `disabled-cover.jpg.bak`）。
   - 修复缺封面、损坏图、非前置封面 (`type != 3`)、多图重复、MIME 不符、超大图（>4MP 或 >2MB 压缩至 1400px 内 JPEG）以及同专辑封面不一致问题；当本地与目录均无封面时自动联网下载高清专辑封面并按单专辑规则补充 `cover.jpg`。
3. **跨平台合并获取与全字段元数据补全**：优先结合 `163 key`、歌曲 ID 与同目录 `.pkl` 缓存，再跨网易云音乐、QQ 音乐、Apple Music / iTunes、酷狗、酷我及 MusicBrainz 多平台合并补全缺失的**封面、发布日期 (`date`)、专辑艺术家 (`albumartist`)、艺术家 (`artist`)、作曲 (`composer`)、表演者 (`performer`)、流派 (`genre`)、音轨号 (`tracknumber`) 与碟片编号 (`discnumber`)**；自动保护原有非空标签与网易云 `163 key(Don't modify):...` 数据不被覆盖。
4. **单次原子安全写入**：同一首曲目需要的多项标签/封面/音轨号/碟片号修改合并在 `.musictmp` 临时副本上一次完成并回读校验后原子替换，避免重复复制大体积无损文件；文件被占用时通过 Windows Restart Manager 提示占用进程，默认跳过整轨全长文件与下载临时副本。
