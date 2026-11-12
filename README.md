# SongMetadata 歌曲元数据

[![python](https://img.shields.io/badge/python-3-blue)](https://www.python.org/) [![platform](https://img.shields.io/badge/platform-Windows-blue)](https://github.com/waves-dylan/SongMetadata) [![license](https://img.shields.io/github/license/waves-dylan/SongMetadata)](https://github.com/waves-dylan/SongMetadata/blob/main/LICENSE) [![stars](https://img.shields.io/github/stars/waves-dylan/SongMetadata?style=flat&label=stars)](https://github.com/waves-dylan/SongMetadata) [![forks](https://img.shields.io/github/forks/waves-dylan/SongMetadata?style=flat&label=forks)](https://github.com/waves-dylan/SongMetadata/forks) [![watchers](https://img.shields.io/github/watchers/waves-dylan/SongMetadata?style=flat&label=watchers)](https://github.com/waves-dylan/SongMetadata)

统一集成 **foobar2000 媒体库歌曲元数据检查、获取与缓存刷新**、**封面规范化修复**（含单专辑 `cover.jpg` 与分组标签补全）、**多平台音轨与双语歌词联网补全**、**批量手工标签设置** 与 **音频格式转换** 的手动媒体库修复工具（GUI + CLI）。

## 目录

- [核心能力](#核心能力)
- [支持格式与数据源](#支持格式与数据源)
- [图形界面使用](#图形界面使用)
- [命令行参考](#命令行参考)
- [源码运行、测试与打包](#源码运行测试与打包)
- [项目结构](#项目结构)

## 核心能力

1. **foobar2000 媒体库全字段核对与刷新**
   - 自动解析 `library-v2.0\folders` 与 `metadb.sqlite`，不仅检测文件大小或修改时间差异，还会比对 `title`、`artist`、`album`、`albumartist`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment` 等全字段以及内嵌/外置歌词状态。
   - 刷新缓存时兼容 `exFAT` / `FAT32` 的 2 秒修改时间粒度，并可触发运行中的 `foobar2000` 自动重载磁盘元数据。
2. **封面修复与缺图联网补全**
   - 自动按专辑分组投票选出代表封面：单专辑目录生成或规范化 1000px 内 `cover.jpg`，多专辑混放目录不写外置图并安全撤回误放的 `cover.jpg`（备份为 `disabled-cover.jpg.bak`）。
   - 修复缺封面、损坏图、非前置封面 (`type != 3`)、多图重复、MIME 不符、超大图（`>4MP` 或 `>2MB` 压缩至 1400px 内 JPEG）以及同专辑封面不一致问题；本地无可用封面时自动联网下载高清专辑封面。
1. **跨平台合并获取歌曲元数据、双语歌词与安全文件名回填**
   - 优先读取内嵌网易云 `163 key`、文件名歌曲 ID 与同目录 `.pkl` 缓存，结合安全文件名解析（保留 `1973 - James Blunt`、`2002年的第一场雪`、`7 Years`、`Anti-Hero` 等歌名，不误删非前缀数字）回填缺失的 `title` 与 `artist`，再跨多平台合并补全缺失的**封面、发布日期 (`date`)、专辑艺术家 (`albumartist`)、艺术家 (`artist`)、作曲 (`composer`)、流派 (`genre`)、音轨号 (`tracknumber`)、碟片编号 (`discnumber`) 与 LRC 歌词**。
   - 支持 **原文 + 翻译双语 LRC 时间轴逐行合并**，支持 `auto`（`wav`/`ape`/`wv` 自动外挂 `.lrc`，其余容器内嵌）、`embed`、`lrc`、`both` 四种写入模式；写入外置 `.lrc` 时自动补充 `[ti:]` / `[ar:]` / `[al:]` 头信息。
   - 默认保护原有非空标签与网易云 `163 key(Don't modify):...` 数据不被误覆盖。
4. **单次原子写入、手工设值与格式转换**
   - 同曲目的标签、封面、歌词、音轨号与碟片号修改在 `.musictmp` 临时副本上一次完成并回读校验后原子替换；文件被占用时通过 Windows Restart Manager 提示占用进程，默认跳过整轨全长文件与下载临时副本。
   - 支持通过弹窗或 `--set` 批量覆写标准字段或自定义标签；支持通过 `ffmpeg`（搭配 `mutagen` 封面/标签/歌词补偿校验与同名 `.lrc` 复制）安全转换音频格式。

## 支持格式与数据源

- **支持音频容器**：`.flac`、`.mp3`、`.mp2`、`.m4a`、`.mp4`、`.m4b`、`.alac`、`.ogg`、`.oga`、`.opus`、`.wav`、`.wave`、`.aiff`、`.aif`、`.aifc`、`.wma`、`.asf`、`.ape`、`.wv`。
  - **格式转换目标 (`--convert`)**：`flac`、`mp3`、`m4a`、`ogg`、`opus`、`wav`、`aiff`（需系统 `PATH` 中可调用 `ffmpeg`）。
- **在线数据源 (`providers/`)**：
  - **主流程数据源**：`netease`（网易云音乐，支持官方接口与 `--netease-api` 自定义反代）、`qq`（QQ 音乐，支持桌面端 `musicu.fcg` 搜索兜底与双语歌词合并）、`itunes`（Apple Music / iTunes）、`mb`（MusicBrainz）。
  - **扩展国内数据源**：`kugou`（酷狗音乐在线搜索、专辑信息与 Base64 歌词）、`migu`（咪咕音乐搜索与歌词）、`baidu`（千千音乐 MD5 签名搜索与歌词）。
  - **本地 `.pkl` 缓存兼容**：同时兼容识别来自 `kuwo`、Spotify、Apple Music、网易云、QQ、酷狗的同目录 `.pkl` 元数据缓存。

## 图形界面使用

直接双击运行 `SongMetadata.exe`（Windows 无控制台窗口版本）或运行 `python app.py`：

1. **扫描**：启动后不会自动改写或扫描，点击 **「扫描媒体库」**（或右上角 **「选择目录...」** 指定文件夹）开始检查。
2. **勾选修复模块**：在 **「修复项选择」** 栏按需勾选 **封面**、**日期**、**专辑艺术家**、**艺术家**、**作曲**、**流派**、**音轨号**、**碟片编号**，在辅助选项可勾选 **「刷新 foobar 缓存」**、**「补全歌词」**（可选 `auto` / `embed` / `lrc` / `both`）以及选择 **「音源」** 预设或点击 **「数据源...」** 设置优先源、仅用源、跳过源与自定义网易云 API 地址。
3. **执行修复或缓存刷新**：点击 **「一键修复已选项目」** 执行选中模块；点击 **「仅刷新 foobar 缓存」** 仅推进修改时间并触发 foobar2000 重载（不修改文件标签和封面）。
4. **独立批量工具**（针对列表中已勾选曲目）：
   - **「设置标签...」**：批量添加/覆写多个标准字段（`title`、`artist`、`album`、`albumartist`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment`、`lyrics`）或自定义标签名，默认保留网易云 `163 key`，支持仅预演。
   - **「删除部分元数据...」**：独立弹窗（所有类别**默认不勾选**，非预演需二次确认），可按类别批量清除内嵌封面、标题、艺术家、专辑艺术家、专辑、日期、作曲、表演者、流派、音轨号、碟片编号、歌词（可选是否保留外置 `.lrc`）与注释（默认保留网易云 `163 key`，可选删除同目录 `cover.jpg`）。
   - **「格式转换...」**：调用 `ffmpeg` 批量转码为目标格式，支持“覆盖已存在目标文件”与“校验成功后删除源文件”确认开关，自动保留元数据、封面与外置 `.lrc`。

## 命令行参考

建议在源码目录通过 `python app.py ...` 运行命令行模式（以便在终端查看实时日志输出）：

- **基础修复与预览**
  - 仅扫描预览待修复项（不修改文件）：`python app.py --dry-run`
  - 仅检查并刷新 foobar2000 元数据缓存：`python app.py --sync-only`
  - 一键执行默认自动修复：`python app.py --auto`
  - 自由组合跳过开关：`--no-track`、`--no-art`、`--no-tags`、`--no-extra-tags`、`--no-date`、`--no-albumartist`、`--no-artist`、`--no-composer`、`--no-genre`、`--no-disc`、`--no-lyrics`、`--no-sync`、`--folder "F:\Music"`、`--include-full-album`
- **歌词与数据源控制**
  - 开启歌词补全（默认 `auto`）：`python app.py --auto --lyrics`
  - 指定歌词模式（`auto` / `embed` / `lrc` / `both`）：`python app.py --auto --lyrics --lyrics-mode both`
  - 仅保存外置 `.lrc`（等价于 `--lyrics --lyrics-mode lrc`）：`python app.py --auto --external-lrc`
  - 控制数据源与自定义网易云接口：
    - `python app.py --auto --prefer netease,qq,kugou --skip-source mb`
    - `python app.py --auto --source netease,migu,baidu --netease-api http://127.0.0.1:3000`
- **手工设置标签 (`--set`)**
  - `python app.py --set genre=Electronic --set date=2024 --set CUSTOM_MOOD=Chill --folder "F:\Music"`
  - 可配合 `--dry-run`、`--no-protect-163`、`--no-sync`、`--include-full-album`
- **格式转换 (`--convert`)**
  - `python app.py --convert flac --folder "F:\Music"`
  - 可配合 `--force`（覆盖已存在目标文件）、`--delete-source`（校验成功后删除源文件）、`--dry-run`、`--no-sync`
- **删除部分元数据 (`--delete`)**
  - `python app.py --delete composer,performer,lyrics,comment --folder "F:\Music"`
  - 可选类别：`cover`、`title`、`artist`、`albumartist`、`album`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment`、`lyrics`
  - 可配合 `--dry-run`、`--delete-folder-cover`、`--no-delete-lrc`、`--no-protect-163`、`--no-sync`、`--include-full-album`

## 源码运行、测试与打包

- **运行环境**：Python 3.10+（仅依赖 [requirements.txt](requirements.txt) 中的 `mutagen>=1.47.0` 与 `Pillow>=10.0.0`，AES 解密与 HTTP 请求均使用纯 Python / 标准库实现）；如需使用格式转换功能，请安装 `ffmpeg` 并确保其位于系统 `PATH` 中。
- **安装依赖并启动**：
  ```bash
  pip install -r requirements.txt
  python app.py
  ```
- **运行单元测试**：
  ```bash
  python -m unittest discover -s tests -v
  ```
- **打包单文件 EXE**（输出至 `dist/SongMetadata.exe`，随后可覆盖根目录 `SongMetadata.exe`）：
  ```bash
  pip install pyinstaller
  pyinstaller --clean --noconfirm SongMetadata.spec
  ```

## 项目结构

```text
SongMetadata/
├── app.py               # tkinter 主窗口 GUI 与程序总入口
├── runner.py            # 修复、删除、设值与格式转换批量执行引擎与并发调度
├── dialogs.py           # 删除元数据、手工设值、格式转换与数据源配置弹窗
├── cli.py               # 命令行参数解析与 CLI 批量执行入口
├── foobar_library.py    # foobar2000 媒体库与 metadb.sqlite 扫描、状态判定及刷新同步
├── art_repair.py        # 专辑封面规范化、分组标签修复、批量设值/删除与格式转换协调
├── tag_io.py            # 容器识别、AES-128 163 key 解密、原子读写、LRC 与 ffmpeg 转码
├── track_lookup.py      # 多平台查询调度、字段合并、.pkl 缓存读取与封面下载
├── providers/           # 模块化在线音源插件 (base / netease / qqmusic / itunes / musicbrainz / kugou / migu / baidu / kuwo / spotify)
├── tests/               # 模块化离线单元测试套件 (common / test_tag_io / test_art_repair / test_providers_lookup / test_cli_gui)
├── SongMetadata.spec    # PyInstaller 单文件打包配置
└── requirements.txt     # 最小运行依赖清单
```
