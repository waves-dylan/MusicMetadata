"""SACD ISO 绿色工具集成、TOC 解析、无损分轨与智能修复流水线单元测试。"""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.models import PlayerEnv
from infra.tools.sacd import (
    SacdAreaInfo,
    SacdIsoToc,
    SacdTrackToc,
    check_disk_space,
    extract_sacd_iso,
    find_sacd_extract,
    parse_sacd_extract_stdout,
    parse_time_to_seconds,
    read_sacd_iso_toc,
)
from pipelines.options import SacdExtractOptions
from pipelines.sacd_pipeline import execute_sacd_pipeline
from tests.common import TempTrackTestCase


# 典型的 sacd_extract -P 原始标准输出日志（含中文、立体声与多声道混合区域）
SAMPLE_SACD_EXTRACT_STDOUT = """
sacd_extract client 0.3.9.3-107-g01c90ddafb0256f3eb194d2c62e7ee5ba14b1c12

Enhanced by euflo ....starting!

Current (working) directory (for the app and 'sacd_extract.cfg' file): D:\\Programing\\Projects\\foobar-musicdl\\MusicMetadata
git repository: "https://github.com/EuFlo/sacd-ripper.git"
Configuration settings:
	Artist will be added in folder name (artist=0) no
	Performer will be added in filename of track (performer=0) no
	Padding-less (nopad=0) no
	Pauses included (pauses=0) no
	Concatenate (concatenate=0) no
	ID3tag (id3tag = 4)

Disc Information:
	Version:  1.20
	Creation date: 2005-08-18
	Disc Catalog Number: SONY-SACD-8888
	Disc Category: General
	Disc Genre: Pop

Album Information:
	Title: 惠威试音碟超级典藏版
	Artist: 群星 (Hi-Fi Vocalists)
	Publisher: 惠威极品音响
	Album Catalog Number: HIVI-DSD-001
	Sequence Number: 1
	Set Size: 1
	Album Category: General
	Album Genre: Audiophile Pop

Area count: 2

	Area Information [0]:

	Version:  1.20
	Track Count: 3
	Total play time: 14:32:45 [mins:secs:frames]
	Speaker config: 2 Channel

	Track list [0]:
		Title[0]: 渡口
		Performer[0]: 蔡琴
		Track_Start_Time_Code: 00:00:00 [mins:secs:frames]
		Duration: 03:45:12 [mins:secs:frames]

		Title[1]: 加州旅馆 (Hotel California)
		Performer[1]: Eagles
		Track_Start_Time_Code: 03:45:12 [mins:secs:frames]
		Duration: 06:30:20 [mins:secs:frames]

		Title[2]: 天堂
		Performer[2]: 腾格尔
		Track_Start_Time_Code: 10:15:32 [mins:secs:frames]
		Duration: 04:17:13 [mins:secs:frames]

	Area Information [1]:

	Version:  1.20
	Track Count: 2
	Total play time: 10:15:32 [mins:secs:frames]
	Speaker config: 5 Channel

	Track list [1]:
		Title[0]: 渡口 (5.1 Surround)
		Performer[0]: 蔡琴
		Track_Start_Time_Code: 00:00:00 [mins:secs:frames]
		Duration: 03:45:12 [mins:secs:frames]

		Title[1]: 加州旅馆 (5.1 Surround)
		Performer[1]: Eagles
		Track_Start_Time_Code: 03:45:12 [mins:secs:frames]
		Duration: 06:30:20 [mins:secs:frames]

Program terminates!
"""


class TestSacdIsoSupport(TempTrackTestCase):
    """测试 SACD 工具发现、TOC 目录解析与分轨提取。"""

    def setUp(self):
        super().setUp()
        self.tmp_dir = self.tmp

    def test_find_sacd_extract(self):
        """测试 sacd_extract 工具发现机制。"""
        # 项目内嵌 tools 应该能被检测到
        exe = find_sacd_extract()
        self.assertIsNotNone(exe, "应该能够在 MusicMetadata/tools/ 目录中发现内置的 sacd_extract.exe")
        self.assertTrue(os.path.isfile(exe), f"检测到的可执行文件必须存在: {exe}")

        # 测试自定义环境变量覆盖
        custom_dummy = os.path.join(self.tmp_dir, "custom_sacd.exe")
        Path(custom_dummy).write_bytes(b"MZ_TEST")
        with patch.dict(os.environ, {"SACD_EXTRACT_PATH": custom_dummy}):
            found_custom = find_sacd_extract()
            self.assertEqual(os.path.abspath(found_custom), os.path.abspath(custom_dummy))

    def test_parse_time_to_seconds(self):
        """测试 SACD 时间码 (mm:ss:ff) 转换为秒。"""
        self.assertAlmostEqual(parse_time_to_seconds("03:30:00"), 210.0, places=2)
        # 15 frames = 15/75 = 0.2s
        self.assertAlmostEqual(parse_time_to_seconds("01:00:15"), 60.2, places=2)
        self.assertEqual(parse_time_to_seconds(""), 0.0)

    def test_parse_sacd_extract_stdout(self):
        """测试解析典型 sacd_extract -P 结构文本。"""
        fake_iso = Path(self.tmp_dir) / "test_album.iso"
        fake_iso.touch()

        toc = parse_sacd_extract_stdout(SAMPLE_SACD_EXTRACT_STDOUT, fake_iso)

        # 校验专辑全局信息
        self.assertEqual(toc.album_title, "惠威试音碟超级典藏版")
        self.assertEqual(toc.album_artist, "群星 (Hi-Fi Vocalists)")
        self.assertEqual(toc.publisher, "惠威极品音响")
        self.assertEqual(toc.catalog_number, "HIVI-DSD-001")
        self.assertEqual(toc.year, "2005")
        self.assertEqual(toc.genre, "Audiophile Pop")

        # 校验区域与声道
        self.assertTrue(toc.has_stereo)
        self.assertTrue(toc.has_multichannel)

        # 校验立体声音轨
        st_tracks = toc.stereo_tracks
        self.assertEqual(len(st_tracks), 3)
        self.assertEqual(st_tracks[0].index, 1)
        self.assertEqual(st_tracks[0].title, "渡口")
        self.assertEqual(st_tracks[0].performer, "蔡琴")
        self.assertEqual(st_tracks[0].duration_str, "03:45:12")
        self.assertGreater(st_tracks[0].duration_seconds, 220)

        self.assertEqual(st_tracks[1].index, 2)
        self.assertEqual(st_tracks[1].title, "加州旅馆 (Hotel California)")
        self.assertEqual(st_tracks[1].performer, "Eagles")

        self.assertEqual(st_tracks[2].index, 3)
        self.assertEqual(st_tracks[2].title, "天堂")
        self.assertEqual(st_tracks[2].performer, "腾格尔")

        # 校验多声道音轨
        mch_tracks = toc.multichannel_tracks
        self.assertEqual(len(mch_tracks), 2)
        self.assertEqual(mch_tracks[0].title, "渡口 (5.1 Surround)")

        # 校验容量预估 (14.5 分钟立体声约为 600MB 级别)
        est_2ch = toc.estimate_dsf_size_mb("2ch")
        self.assertGreater(est_2ch, 500)
        self.assertLess(est_2ch, 800)

        est_mch = toc.estimate_dsf_size_mb("mch")
        self.assertGreater(est_mch, 1000)

    def test_check_disk_space(self):
        """测试磁盘空间预检。"""
        # 测试正常容量 (要求 10MB，当前目录必然满足)
        ok, free_b, req_b = check_disk_space(self.tmp_dir, 10 * 1024 * 1024)
        self.assertTrue(ok)
        self.assertGreater(free_b, 10 * 1024 * 1024)

        # 测试极端容量 (要求 10,000 TB，必然不满足)
        bad_ok, _, _ = check_disk_space(self.tmp_dir, 10_000 * 1024 * 1024 * 1024 * 1024)
        self.assertFalse(bad_ok)

    def test_extract_sacd_iso_mock_execution(self):
        """测试分轨执行流程、流式进度解析与文件收集。"""
        fake_iso = Path(self.tmp_dir) / "demo.iso"
        fake_iso.write_bytes(b"FAKE_SACD_ISO_CONTENT")
        out_dir = Path(self.tmp_dir) / "output"

        # 构造预期的 DSF 文件在提取过程中生成
        def fake_popen(*args, **kwargs):
            out_dir.mkdir(parents=True, exist_ok=True)
            self._make_minimal_dsf("output/01 - 渡口.dsf")
            self._make_minimal_dsf("output/02 - 加州旅馆.dsf")

            mock_proc = MagicMock()
            mock_proc.poll.side_effect = [None, None, None, 0]
            mock_proc.wait.return_value = 0
            lines = [
                b"Processing [01 - \xe6\xb8\xa1\xe5\x8f\xa3.dsf] (1/2)..\n",
                b"Completed: 50% (file sectors processed: 500 / total sectors:1000), Total: 25%\n",
                b"Processing [02 - \xe5\x8a\xa0\xe5\xb7\x9e\xe6\x97\x85\xe9\xa6\x86.dsf] (2/2)..\n",
                b"Completed: 100% (file sectors processed: 1000 / total sectors:1000), Total: 100%\n",
            ]
            mock_proc.stdout.readline.side_effect = lines + [b""]
            return mock_proc

        progresses: list[tuple[float, str]] = []

        def on_prog(pct, desc):
            progresses.append((pct, desc))

        with patch("subprocess.Popen", side_effect=fake_popen):
            ok, msg, gen_files = extract_sacd_iso(
                fake_iso,
                out_dir,
                channel_area="2ch",
                progress_callback=on_prog,
                exe_path="sacd_extract.exe",
            )

        self.assertTrue(ok, f"提取应成功: {msg}")
        self.assertEqual(len(gen_files), 2)
        self.assertEqual(gen_files[0].name, "01 - 渡口.dsf")
        self.assertEqual(gen_files[1].name, "02 - 加州旅馆.dsf")
        self.assertTrue(any("50%" in desc or "1/2" in desc for _, desc in progresses))
        self.assertTrue(any(pct >= 1.0 for pct, _ in progresses))

    def test_extract_sacd_iso_cancelled(self):
        """测试提取过程中的用户中断与取消机制。"""
        fake_iso = Path(self.tmp_dir) / "demo.iso"
        fake_iso.write_bytes(b"FAKE_SACD_ISO_CONTENT")
        out_dir = Path(self.tmp_dir) / "output_cancel"

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.return_value = b"Processing [01 - Track.dsf] (1/10)..\n"
        mock_proc.wait.return_value = -1

        with patch("subprocess.Popen", return_value=mock_proc):
            ok, msg, gen_files = extract_sacd_iso(
                fake_iso,
                out_dir,
                is_cancelled_callback=(lambda: True),
                exe_path="sacd_extract.exe",
            )

        self.assertFalse(ok)
        self.assertIn("取消", msg)
        mock_proc.terminate.assert_called_once()

    def test_execute_sacd_pipeline_dry_run(self):
        """测试 SACD 流水线预演模式。"""
        fake_iso = Path(self.tmp_dir) / "album.iso"
        fake_iso.write_bytes(b"SACD_ISO")

        env = PlayerEnv(player_name="测试播放器", library_folders=[self.tmp_dir])
        opts = SacdExtractOptions(dry_run=True, channel_area="2ch")

        with patch("pipelines.sacd_pipeline.read_sacd_iso_toc") as mock_toc_reader:
            mock_toc = parse_sacd_extract_stdout(SAMPLE_SACD_EXTRACT_STDOUT, fake_iso)
            mock_toc_reader.return_value = mock_toc

            logs: list[str] = []
            res = execute_sacd_pipeline(env, fake_iso, opts, log_cb=logs.append)

            self.assertTrue(res["success"])
            self.assertTrue(res["dry_run"])
            self.assertEqual(res["track_count"], 3)
            self.assertTrue(any("预演" in line for line in logs))

    def test_execute_sacd_pipeline_end_to_end_with_tags(self):
        """测试 SACD 完整流水线：提取 ➜ 写入原生 TOC 标签。"""
        fake_iso = Path(self.tmp_dir) / "test_full.iso"
        fake_iso.write_bytes(b"SACD_ISO_DATA")
        out_dir = Path(self.tmp_dir) / "DSF_Tracks"
        out_dir.mkdir(parents=True, exist_ok=True)

        f1 = self._make_minimal_dsf("DSF_Tracks/01 - 渡口.dsf")
        f2 = self._make_minimal_dsf("DSF_Tracks/02 - 加州旅馆.dsf")

        mock_toc = parse_sacd_extract_stdout(SAMPLE_SACD_EXTRACT_STDOUT, fake_iso)

        with patch("pipelines.sacd_pipeline.read_sacd_iso_toc", return_value=mock_toc), \
             patch("pipelines.sacd_pipeline.extract_sacd_iso", return_value=(True, "OK", [Path(f1), Path(f2)])):

            env = PlayerEnv(player_name="测试播放器", library_folders=[self.tmp_dir])
            opts = SacdExtractOptions(
                output_dir=str(out_dir),
                apply_toc_tags=True,
                auto_repair=False,
                fetch_cover=False,
                fetch_lyrics=False,
            )

            res = execute_sacd_pipeline(env, fake_iso, opts)
            self.assertTrue(res["success"])
            self.assertEqual(res["track_count"], 2)

            # 回读 f1 验证 ID3v2.3 标签是否被正确注入
            from tag_io import read_audio_info
            info1 = read_audio_info(f1)
            self.assertEqual(info1.get("title"), "渡口")
            self.assertEqual(info1.get("artist"), "蔡琴")
            self.assertEqual(info1.get("album"), "惠威试音碟超级典藏版")
            self.assertEqual(info1.get("tracknumber"), "1")

    def test_cli_sacd_info(self):
        """测试 CLI 模式下的 --sacd-info 输出。"""
        import argparse
        import cli

        fake_iso = Path(self.tmp_dir) / "cli_demo.iso"
        fake_iso.write_bytes(b"SACD_ISO")

        mock_toc = parse_sacd_extract_stdout(SAMPLE_SACD_EXTRACT_STDOUT, fake_iso)
        args = argparse.Namespace(sacd_info=str(fake_iso), sacd_channel="2ch")

        with patch("cli.read_sacd_iso_toc", return_value=mock_toc), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = cli._run_sacd_info_cli(args)
            self.assertEqual(ret, 0)
            output = mock_out.getvalue()
            self.assertIn("惠威试音碟超级典藏版", output)
            self.assertIn("渡口", output)
            self.assertIn("蔡琴", output)

    def test_cli_sacd_extract(self):
        """测试 CLI 模式下的 --sacd-extract 调用。"""
        import argparse
        import cli

        fake_iso = Path(self.tmp_dir) / "cli_extract.iso"
        fake_iso.write_bytes(b"SACD_ISO")

        args = argparse.Namespace(
            sacd_extract=str(fake_iso),
            sacd_channel="2ch",
            sacd_out=os.path.join(self.tmp_dir, "out_dsf"),
            sacd_tracks="1,2",
            no_tags=False,
            no_art=True,
            lyrics=False,
            external_lrc=False,
            dry_run=True,
            player="generic",
            folder=[],
        )

        mock_res = {
            "success": True,
            "dry_run": True,
            "output_dir": os.path.join(self.tmp_dir, "out_dsf"),
            "track_count": 2,
        }

        with patch("cli.find_sacd_extract", return_value="sacd_extract.exe"), \
             patch("cli.execute_sacd_pipeline", return_value=mock_res), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = cli._run_sacd_extract_cli(args)
            self.assertEqual(ret, 0)
            self.assertIn("预计提取 2 首 DSF 曲目", mock_out.getvalue())

    def test_sacd_dialog_init(self):
        """测试 SacdExtractDialog 弹窗界面初始化与 TOC 回填。"""
        import tkinter as tk
        from ui.dialogs.sacd_dlg import SacdExtractDialog

        fake_iso = Path(self.tmp_dir) / "dlg_demo.iso"
        fake_iso.write_bytes(b"SACD_ISO")

        mock_toc = parse_sacd_extract_stdout(SAMPLE_SACD_EXTRACT_STDOUT, fake_iso)

        try:
            root = tk.Tk()
            root.withdraw()
        except Exception:
            self.skipTest("无图形显示环境，跳过 GUI 弹窗初始化测试")

        try:
            with patch("ui.dialogs.sacd_dlg.read_sacd_iso_toc", return_value=mock_toc):
                dlg = SacdExtractDialog(root, default_iso=str(fake_iso))
                dlg._apply_toc(mock_toc)

                self.assertEqual(dlg.lbl_disc_title.cget("text"), "💿 专辑: 惠威试音碟超级典藏版")
                self.assertIn("群星", dlg.lbl_artist.cget("text"))
                # 校验曲目 Treeview 已有 3 行数据
                children = dlg.tree.get_children()
                self.assertEqual(len(children), 3)
                first_row = dlg.tree.item(children[0])["values"]
                self.assertEqual(first_row[2], "蔡琴")

                dlg._on_cancel()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
