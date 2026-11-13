# MusicMetadata 音乐元数据

[![python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/) [![platform](https://img.shields.io/badge/platform-Windows-blue)](https://github.com/waves-dylan/MusicMetadata) [![license](https://img.shields.io/github/license/waves-dylan/MusicMetadata)](https://github.com/waves-dylan/MusicMetadata/blob/main/LICENSE) [![stars](https://img.shields.io/github/stars/waves-dylan/MusicMetadata?style=flat&label=stars)](https://github.com/waves-dylan/MusicMetadata) [![forks](https://img.shields.io/github/forks/waves-dylan/MusicMetadata?style=flat&label=forks)](https://github.com/waves-dylan/MusicMetadata/forks) [![watchers](https://img.shields.io/github/watchers/waves-dylan/MusicMetadata?style=flat&label=watchers)](https://github.com/waves-dylan/MusicMetadata) [![downloads](https://img.shields.io/github/downloads/waves-dylan/MusicMetadata/total?style=flat&label=downloads)](https://github.com/waves-dylan/MusicMetadata/releases)

统一集成 **本地音乐库元数据检查与批量补全**（通用适配 MusicBee、MediaMonkey、AIMP、JRiver、Strawberry、Dopamine、Fooyin、foobar2000 等管理目录，并支持 foobar2000 `metadb.sqlite` 缓存核对与热重载）、**封面与艺术家写真规范化修复**（含单专辑 `cover.jpg` / `artist.jpg` 与分组标签补全）、**海内外多平台音轨与双语歌词联网补全**、**批量手工标签设置 / 局部清除** 与 **音频格式转换** 的桌面媒体库修复工具（GUI + CLI）。

---

## 目录

- [核心能力](#核心能力)
- [支持格式与数据源](#支持格式与数据源)
- [图形界面使用](#图形界面使用)
- [命令行参考](#命令行参考)
- [架构设计与分层结构](#架构设计与分层结构)
- [源码运行、测试与打包](#源码运行测试与打包)
- [项目完整目录树](#项目完整目录树)

---

## 核心能力

1. **通用本地音乐库核对与可选 foobar2000 深度集成**
   - 自动扫描任意本地音乐目录（支持通过 GUI **「选择目录...」** / **「管理目录...」** 或 CLI `--folder` 多次指定多目录，并自动记住上次配置；未配置且未安装 foobar2000 时回退扫描系统 `~/Music` 目录）。
   - 检测到 foobar2000 时额外解析 `library-v2.0\folders` 与 `metadb.sqlite`，比对 `title`、`artist`、`album`、`albumartist`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment` 等全字段及歌词状态；未检测到 foobar2000 时自动隐藏专属缓存控件并使用通用界面文案。
   - 写入后兼容 `exFAT` / `FAT32` 的 2 秒修改时间粒度推进时间戳，帮助监听目录变化的本地播放器感知文件更新，并在 foobar2000 运行时触发自动重载。
2. **封面与艺术家图片规范化修复与缺图联网补全**
   - **专辑正面封面 (Cover Front, Type 3)**：按专辑分组投票选出代表封面，单专辑目录生成或规范化 1000px 内 `cover.jpg`，多专辑混放目录不写外置图并安全撤回误放的 `cover.jpg`（备份为 `disabled-cover.jpg.bak`）。修复缺封面、损坏图、非前置封面 (`type != 3`)、多图重复、MIME 不符、超大图（`>4MP` 或 `>2MB` 压缩至 1400px 内 JPEG）以及同专辑封面不一致问题。
   - **艺术家图片 (Artist Art, Type 8)**：支持联网获取高清艺人头像/写真，统一规范化为最大边 ≤1000px 的优质 JPEG（单曲体积增量控制在 50~200KB），与正面封面独立共存内嵌至 FLAC、MP3、WAV、AIFF、OGG、Opus 容器；单专辑目录自动生成规范外置 `artist.jpg`，多艺术家混放目录自动跳过并撤回备份为 `disabled-artist.jpg.bak`。
3. **跨平台合并获取歌曲元数据、双语歌词与安全文件名回填**
   - 优先读取内嵌网易云 `163 key`、文件名歌曲 ID 与同目录 `.pkl` 缓存，结合安全文件名解析（保留 `1973 - James Blunt`、`2002年的第一场雪`、`7 Years`、`Anti-Hero` 等歌名，不误删非前缀数字）回填缺失的 `title` 与 `artist`，再跨多平台合并补全缺失的**封面、艺术家写真、发布日期 (`date`)、专辑艺术家 (`albumartist`)、艺术家 (`artist`)、作曲 (`composer`)、流派 (`genre`)、音轨号 (`tracknumber`)、碟片编号 (`discnumber`) 与 LRC 歌词**。
   - 支持 **原文 + 翻译双语 LRC 时间轴逐行合并**，支持 `auto`（`wav`/`ape`/`wv` 自动外挂 `.lrc`，其余容器内嵌）、`embed`、`lrc`、`both` 四种写入模式；写入外置 `.lrc` 时自动补充 `[ti:]` / `[ar:]` / `[al:]` 头信息。
   - 默认保护原有非空标签与网易云 `163 key(Don't modify):...` 数据不被误覆盖。
4. **高性能就地写入、并发架构与网络加速**
   - **极速就地写入与安全退避**：改写标签与封面时优先零拷贝就地更新（性能提升 5~20 倍），遭遇文件占用或异常时自动安全退避至原子临时副本 (`.musictmp`) 替换，并通过 Windows Restart Manager 检测占用进程。
   - **长连接池与流式解压**：内置 `HTTPConnectionPool` 实现 HTTP Keep-Alive 连接复用、空闲连接回收、SSL 上下文共享与 Gzip / Deflate 流式解压，大幅降低网络检索 RTT。
   - **多线程并行并发**：图片批量拉取（12 线程并发调度）与音轨写入（自适应 CPU 核心数最高 8 线程并发），大幅缩短大规模曲库修复耗时。
   - **反向专辑与艺人多级缓存**：跨曲目复用解析结果，同专辑或同艺人后续曲目命中内存缓存直接复用；在线源支持失败熔断机制 (`ProviderCircuitBreaker`)，避免单点故障导致整体流程卡死。
5. **批量手工设值、选择性删标与格式转换**
   - 支持通过弹窗或 `--set` 批量覆写标准字段或自定义标签；支持通过 `--delete` 或弹窗按类别选择性删除部分元数据（含 `artist_cover` 与 `--delete-folder-artist` 联动删除同目录 `artist.jpg`）。
   - 支持通过 `ffmpeg`（搭配 `mutagen` 封面/标签/歌词补偿校验与同名 `.lrc` 复制）安全转换音频格式。

---

## 支持格式与数据源

- **支持音频容器**：`.flac`、`.mp3`、`.mp2`、`.m4a`、`.mp4`、`.m4b`、`.alac`、`.ogg`、`.oga`、`.opus`、`.wav`、`.wave`、`.aiff`、`.aif`、`.aifc`、`.wma`、`.asf`、`.ape`、`.wv`。
  - **格式转换目标 (`--convert`)**：`flac`、`mp3`、`m4a`、`ogg`、`opus`、`wav`、`aiff`（需系统 `PATH` 中可调用 `ffmpeg`）。
- **在线数据源 (`providers/`)**：
  - **主流程数据源 (`PRIMARY_PROVIDERS`)**：`netease`（网易云音乐，支持官方接口与 `--netease-api` 自定义反代）、`qq`（QQ 音乐，支持桌面端 `musicu.fcg` 搜索兜底与双语歌词合并）、`itunes`（Apple Music / iTunes）、`mb`（MusicBrainz）。
  - **扩展国内数据源 (`EXTENDED_CN_PROVIDERS`)**：`kugou`（酷狗音乐在线搜索、专辑信息、Base64 歌词与歌手头像）、`migu`（咪咕音乐搜索与歌词）、`baidu`（千千音乐 MD5 签名搜索与歌词）。
  - **扩展国际数据源 (`EXTENDED_INTL_PROVIDERS`)**：
    - `tadb`（TheAudioDB 开放音乐数据库，内置公开 Key 即开即用，支持专辑、曲目、单曲独立直搜、流派、作曲者与高清艺人写真）
    - `fanart`（Fanart.tv 社区高清视觉库，内置项目 Key 即开即用，通过 MBID 直连匹配 1000px 正方形高赞艺人头像与背景）
    - `deezer`（Deezer 免 Key 公开专辑/单曲/流派与高清封面/艺人图）
    - `ytmusic`（YouTube Music 免登录 InnerTube 搜索、封面与歌词）
    - `lrclib`（LRCLIB 开源同步 LRC 歌词补全）
    - `spotify`（默认免 Key 仅用 Embed 页面读取 `.pkl` 专辑；配置 `--spotify-client-id` / `--spotify-client-secret` 或环境变量 `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` 后启用官方 Web API 搜索与 Track ID 直查）
  - **本地 `.pkl` 缓存兼容**：兼容识别来自 `kuwo`、Spotify、Apple Music、网易云、QQ、酷狗的同目录 `.pkl` 元数据缓存。

---

## 图形界面使用

直接双击运行 [MusicMetadata.exe](MusicMetadata.exe)（Windows 免安装单文件版本）或运行 `python app.py`：

1. **扫描与目录管理**：
   - 点击 **「扫描媒体库」** 开始检查；点击右上角 **「选择目录...」** 或 **「管理目录...」**。
   - 在 **「管理目录...」** 弹窗中可直观添加多音乐目录、一键添加系统默认音乐目录（`~/Music`）、删除或清空目录，所有配置跨平台持久化保存。
   - 勾选 **「只显示待处理曲目」** 可快速过滤列表，隐藏无异常曲目，专注处理待修复项。
2. **勾选修复模块与音源**：
   - 在 **「修复项选择」** 栏按需勾选 **封面**、**日期**、**专辑艺术家**、**艺术家**、**作曲**、**流派**、**音轨号**、**碟片编号**。
   - 在辅助选项中按需勾选 **「获取艺术家图」**（内嵌 Picture Type = 8 艺术家写真并生成单专辑 `artist.jpg`）与 **「补全歌词」**（模式可选 `auto` / `embed` / `lrc` / `both`）；若环境检测到 foobar2000，还会显示 **「刷新 foobar 缓存」**。
   - 在 **「音源」** 下拉框选择预设，包括：`默认`、`网易云优先`、`QQ音乐优先`、`Apple/iTunes优先`、`MusicBrainz优先`、`欧美/国际优先`（优先按 `Apple/iTunes → TheAudioDB → Fanart.tv → Deezer → YouTube Music → LRCLIB → MusicBrainz → Spotify` 查询）、`YouTube Music优先`、`Deezer优先`、`TheAudioDB优先`、`Fanart.tv优先`、`酷狗优先`、`咪咕优先`、`千千优先` 与 `自定义`。
   - 点击 **「数据源...」** 可自定义优先源、仅用源、跳过源、网易云自定义反代地址，以及配置 Spotify、TheAudioDB（可选）、Fanart.tv Project / Client Key（可选）。
3. **执行修复**：点击 **「一键修复已选项目」** 执行选中模块；检测到 foobar2000 时也可点击 **「仅刷新 foobar 缓存」** 仅推进修改时间并触发 foobar2000 重载（不修改文件标签和封面）。
4. **独立批量工具**（针对列表中已勾选曲目）：
   - **「设置标签...」**：批量添加/覆写多个标准字段（`title`、`artist`、`album`、`albumartist`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment`、`lyrics`）或自定义标签名，默认保留网易云 `163 key`，支持仅预演。
   - **「删除部分元数据...」**：独立弹窗（所有类别**默认不勾选**，非预演需二次确认），可按类别批量清除内嵌封面、封面-艺术家 (`artist_cover`)、标题、艺术家、专辑艺术家、专辑、日期、作曲、表演者、流派、音轨号、碟片编号、歌词（可选是否保留外置 `.lrc`）与注释（默认保留网易云 `163 key`，支持联动删除同目录 `cover.jpg` 与 `artist.jpg`）。
   - **「格式转换...」**：调用 `ffmpeg` 批量转码为目标格式，支持“覆盖已存在目标文件”与“校验成功后删除源文件”确认开关，自动保留元数据、封面、艺术家图与外置 `.lrc`。

---

## 命令行参考

建议在源码目录通过 `python app.py ...` 运行命令行模式（以便在终端查看实时日志输出）：

- **基础修复与预览**
  - 仅扫描预览待修复项（不修改文件）：`python app.py --dry-run`
  - 仅检查并刷新 foobar2000 元数据缓存：`python app.py --sync-only`
  - 一键执行默认自动修复：`python app.py --auto`
  - 自由组合跳过开关与多目录扫描：`--no-track`、`--no-art`、`--no-artist-art`、`--no-tags`、`--no-extra-tags`、`--no-date`、`--no-albumartist`、`--no-artist`、`--no-composer`、`--no-genre`、`--no-disc`、`--no-lyrics`、`--no-sync`、`--folder "F:\Music"`（可多次传入以指定多目录）、`--include-full-album`
- **艺术家图片（封面-艺术家）与外置 artist.jpg**
  - 开启艺术家图片获取与内嵌/生成（默认关闭，按需启用）：`python app.py --auto --artist-art`
  - 规格说明：自动从网易云、QQ 音乐、TheAudioDB、Fanart.tv、Deezer、酷狗等在线数据源检索艺术家写真/头像，统一规范化为最大边不超过 1000px，以质量 100（JPEG 格式）优先输出，单曲文件体积增量控制在 50~200KB 范围；支持在 FLAC、MP3、WAV、AIFF、OGG、Opus 等支持多图的容器内嵌 Picture Type = 8 (`Artist/performer`) 帧，与正面封面 (Type = 3) 互不覆盖、无损共存；在单专辑目录自动输出规范外置 `artist.jpg`（多艺术家混放目录安全撤回备份）。
- **歌词与数据源控制**
  - 开启歌词补全（默认 `auto`）：`python app.py --auto --lyrics`
  - 指定歌词模式（`auto` / `embed` / `lrc` / `both`）：`python app.py --auto --lyrics --lyrics-mode both`
  - 仅保存外置 `.lrc`（等价于 `--lyrics --lyrics-mode lrc`）：`python app.py --auto --external-lrc`
  - 控制国内外数据源与可选凭据：
    - 国内优先：`python app.py --auto --prefer netease,qq,kugou --skip-source mb`
    - 欧美/国际优先：`python app.py --auto --prefer itunes,tadb,fanart,deezer,ytmusic,lrclib,mb,spotify --lyrics`
    - 指定音源 + 自定义网易云 API：`python app.py --auto --source netease,migu,baidu --netease-api http://127.0.0.1:3000`
    - 启用 Spotify / TheAudioDB / Fanart.tv 自定义 Key：`python app.py --auto --prefer fanart,tadb,spotify --tadb-api-key <KEY> --fanart-api-key <KEY> --fanart-client-key <KEY>`（注：TheAudioDB 与 Fanart.tv 默认已内置可用 Key，开箱即用；Spotify 可传入 `--spotify-client-id` / `--spotify-client-secret`）
- **手工设置标签 (`--set`)**
  - `python app.py --set genre=Electronic --set date=2024 --set CUSTOM_MOOD=Chill --folder "F:\Music"`
  - 可配合 `--dry-run`、`--no-protect-163`、`--no-sync`、`--include-full-album`
- **格式转换 (`--convert`)**
  - `python app.py --convert flac --folder "F:\Music"`
  - 可配合 `--force`（覆盖已存在目标文件）、`--delete-source`（校验成功后删除源文件）、`--dry-run`、`--no-sync`
- **删除部分元数据 (`--delete`)**
  - `python app.py --delete composer,performer,lyrics,comment,artist_cover --folder "F:\Music"`
  - 可选类别：`cover`、`artist_cover`、`title`、`artist`、`albumartist`、`album`、`date`、`composer`、`performer`、`genre`、`tracknumber`、`discnumber`、`comment`、`lyrics`
  - 可配合 `--dry-run`、`--delete-folder-cover`、`--delete-folder-artist`、`--no-delete-lrc`、`--no-protect-163`、`--no-sync`、`--include-full-album`

---

## 架构设计与分层结构

项目采用高内聚、低耦合的分层架构体系，将过去交织在单文件中的算法、管线、网络、视图解耦，各层职责明确：

1. **核心领域层 (`core/`)**
   - `core.constants`：全局收拢音频容器定义、字段映射表、图片限制规格与状态码。
   - `core.models`：强类型领域实体定义，包含 `LibraryTrack`、`TrackMatch`、`FoobarEnv`、`FolderArtPlan` 等。
   - `core.config`：跨平台用户配置序列化、字体推断与资源管理器高亮揭示。
2. **基础支撑层 (`infra/`)**
   - `infra.net`：通用网络基础设施，包含长连接池 (`HTTPConnectionPool`)、熔断机制 (`ProviderCircuitBreaker`) 与多级缓存管理器。
   - `infra.scanner`：本地音乐目录深度遍历与同专一致性核对引擎。
   - `infra.integrations.foobar`：foobar2000 宿主环境发现、`metadb.sqlite` 只读缓存比对与命令行热重载集成。
3. **领域服务层 (`services/`)**
   - `services.art`：图片格式嗅探、感知哈希（aHash）、高质量 JPEG 规范化缩放、目录级封面与写真规划分析 (`planner`)、原子写回与格式校验 (`writer`)。
   - `services.lookup`：多在线音源调度、候选匹配打分与元数据智能聚合服务。
4. **业务管线层 (`pipelines/`)**
   - `pipelines.options`：强类型参数选项模型（`FixOptions`、`DeleteOptions`、`SetOptions`、`ConvertOptions`）。
   - `pipelines.executor`：自适应 CPU 核心数的可即时取消并发线程池调度器。
   - `pipelines.repair` / `delete` / `set_tags` / `convert`：各业务场景的批量执行流水线。
5. **表现层 (`ui/`)**
   - `ui.main_window`：基于 Tkinter 的响应式现代双栏主界面 (`MainWindow`)、异步事件队列分发与单元格数据呈现。
   - `ui.dialogs`：按业务独立划分的模态弹窗（删除确认、手工设标、格式转换、音源优先级与音乐目录管理）。
6. **极简清晰的顶层入口 (Minimal Top-Level)**
   - 消除所有历史过渡冗余文件，根目录下仅保留极简启动入口 `app.py`、命令行引擎 `cli.py` 与容器底层 IO `tag_io.py`，全项目与单元测试 100% 采用清晰的现代分层包导入规范。

---

## 源码运行、测试与打包

- **运行环境**：Python 3.10+（仅依赖 [requirements.txt](requirements.txt) 中的 `mutagen>=1.47.0` 与 `Pillow>=10.0.0`，AES 解密与 HTTP 请求均使用纯 Python / 标准库实现）；如需使用格式转换功能，请安装 `ffmpeg` 并确保其位于系统 `PATH` 中。
- **安装依赖并启动**：
  ```bash
  pip install -r requirements.txt
  python app.py
  ```
- **运行单元测试（全量 51 项测试套件）**：
  ```bash
  python -m unittest discover -s tests -v
  ```
- **打包单文件免安装 EXE**（自动递归依赖封包，输出并就地更新根目录 `MusicMetadata.exe`）：
  ```bash
  pip install pyinstaller
  pyinstaller --clean --noconfirm MusicMetadata.spec
  ```

---

## 项目完整目录树

```text
MusicMetadata/
├── core/                                # 核心领域层
│   ├── __init__.py
│   ├── constants.py                     # 全局常量、标签表、容器映射、图片规格
│   ├── models.py                        # 核心业务数据实体模型 (LibraryTrack, TrackMatch 等)
│   └── config.py                        # 用户配置持久化、系统目录定位与系统级 reveal
│
├── infra/                               # 基础支撑层
│   ├── net/                             # 通用网络层
│   │   ├── __init__.py
│   │   ├── pool.py                      # HTTP 长连接池与流式解压
│   │   ├── circuit_breaker.py           # 数据源自愈熔断器
│   │   └── cache.py                     # 内存与反向专辑多级缓存
│   ├── scanner/                         # 本地音频扫描
│   │   ├── __init__.py
│   │   └── local_scanner.py             # 目录遍历、全专一致性合并、mtime 探测
│   └── integrations/                    # 外部软件集成
│       ├── __init__.py
│       └── foobar.py                    # foobar2000 进程探测、metadb 只读比对与刷新
│
├── services/                            # 领域服务层
│   ├── art/                             # 封面与写真视觉服务
│   │   ├── __init__.py
│   │   ├── image_ops.py                 # 图片嗅探、JPEG 规范缩放、aHash 感知哈希
│   │   ├── planner.py                   # 目录外置图与内嵌封面规划分析引擎
│   │   └── writer.py                    # 音频容器原子写入、校验与转码补偿
│   └── lookup/                          # 在线元数据聚合服务
│       └── __init__.py
│
├── pipelines/                           # 业务处理流水线
│   ├── __init__.py
│   ├── options.py                       # 操作选项模型 (FixOptions, DeleteOptions 等)
│   ├── executor.py                      # 可取消线程池并发调度器
│   ├── repair.py                        # 批量元数据修复、封面/写真拉取管线
│   ├── delete.py                        # 批量元数据局部删除管线
│   ├── set_tags.py                      # 批量标签手工覆写管线
│   └── convert.py                       # 音频格式转换管线
│
├── providers/                           # 在线音乐数据源插件
│   ├── __init__.py                      # 音源注册表与优先级解析
│   ├── base.py                          # 插件基类、模糊评分与文本归一化
│   ├── netease.py                       # 网易云音乐 (支持官方接口 / 自定义反代)
│   ├── qqmusic.py                       # QQ 音乐 (桌面端接口 + 双语歌词)
│   ├── itunes.py                        # Apple Music / iTunes
│   ├── musicbrainz.py                   # MusicBrainz 开放音源
│   ├── kugou.py                         # 酷狗音乐
│   ├── migu.py                          # 咪咕音乐
│   ├── baidu.py                         # 千千音乐
│   ├── deezer.py                        # Deezer 国际音源
│   ├── theaudiodb.py                    # TheAudioDB 社区数据库
│   ├── fanart.py                        # Fanart.tv 高清正方形视觉源
│   ├── ytmusic.py                       # YouTube Music
│   ├── lrclib.py                        # LRCLIB 同步歌词
│   ├── spotify.py                       # Spotify (Web API + Embed)
│   └── kuwo.py                          # 酷我音乐本地缓存
│
├── ui/                                  # 表现展示层
│   ├── __init__.py
│   ├── main_window.py                   # Tkinter 主窗口响应式界面与事件循环
│   └── dialogs/                         # 独立模态业务弹窗
│       ├── __init__.py
│       ├── base.py                      # 弹窗基类与深色模式 message box
│       ├── delete_dlg.py                # 局部删除勾选弹窗
│       ├── set_dlg.py                   # 手工设标弹窗
│       ├── convert_dlg.py               # 格式转换设置弹窗
│       ├── source_dlg.py                # 数据源优先级与凭证弹窗
│       └── folder_dlg.py                # 音乐目录管理器弹窗
│
├── tests/                               # 全面自动化单元测试套件 (51 项测试)
│   ├── common.py
│   ├── test_tag_io.py
│   ├── test_art_repair.py
│   ├── test_providers_lookup.py
│   ├── test_cli_gui.py
│   └── test_online_perf_optimizations.py
│
├── assets/                              # 静态资源 (app_icon.ico / png)
├── app.py                               # 极简程序总入口 (GUI + CLI 统一分流)
├── cli.py                               # 命令行参数解析引擎
├── tag_io.py                            # 底层音频容器读写与转码支持
├── MusicMetadata.spec                   # PyInstaller 自动依赖打包配置
├── MusicMetadata.exe                    # Windows 独立免安装单文件程序
└── requirements.txt                     # 最小运行依赖声明
```
