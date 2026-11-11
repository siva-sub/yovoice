#!/usr/bin/env python3
"""breeze-batch.py 的纯逻辑测试：不联网、不依赖任何模型。"""

from __future__ import annotations

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "breeze-batch.py"
_spec = spec_from_file_location("breeze_batch", SCRIPT)
if _spec is None or _spec.loader is None:  # 文件不存在或无法加载时早失败，胜过在用例里报奇怪错误
    raise ImportError(f"加载不到 {SCRIPT}")
batch = module_from_spec(_spec)
_spec.loader.exec_module(batch)


class SplitPcmTest(unittest.TestCase):
    def test_splits_exactly(self):
        data = bytes(range(10))
        self.assertEqual(batch.split_pcm(data, [3, 4, 3]), [data[:3], data[3:7], data[7:]])

    def test_size_mismatch_raises(self):
        # 切错一个字节会让后续所有段落错位，所以必须报错而不是截断
        with self.assertRaises(ValueError):
            batch.split_pcm(b"0123456789", [3, 4])

    def test_empty(self):
        self.assertEqual(batch.split_pcm(b"", []), [])

    def test_data_without_sizes_raises(self):
        with self.assertRaises(ValueError):
            batch.split_pcm(b"abc", [])

    def test_non_positive_size_raises(self):
        with self.assertRaises(ValueError):
            batch.split_pcm(b"abcdef", [3, 0, 3])


class ParseSizesTest(unittest.TestCase):
    def test_parses_header(self):
        self.assertEqual(batch.parse_sizes("48000,51200, 4096"), [48000, 51200, 4096])

    def test_empty_header(self):
        self.assertEqual(batch.parse_sizes(""), [])


class BatchItemsTest(unittest.TestCase):
    def test_real_volume_needs_69_requests(self):
        # 本卷 8,794 条 cue：批量端点把请求数从 8,794 压到 69
        items = list(range(8794))
        groups = batch.batch_items(items, batch.MAX_BATCH_TEXTS)
        self.assertEqual(len(groups), 69)
        self.assertEqual(sum(len(g) for g in groups), 8794)
        self.assertEqual(len(groups[-1]), 8794 - 128 * 68)

    def test_exact_multiple_has_no_empty_tail(self):
        groups = batch.batch_items(list(range(256)), 128)
        self.assertEqual([len(g) for g in groups], [128, 128])

    def test_limit_is_128(self):
        self.assertEqual(batch.MAX_BATCH_TEXTS, 128)

    def test_zero_size_raises(self):
        with self.assertRaises(ValueError):
            batch.batch_items([1, 2], 0)


class DurationAndRtfTest(unittest.TestCase):
    def test_audio_seconds(self):
        # 24 kHz 16-bit 单声道：每秒 48000 字节
        self.assertAlmostEqual(batch.audio_seconds(b"\x00" * 48000), 1.0)

    def test_official_rtf_032_means_3x_faster(self):
        rtf = batch.real_time_factor(wall_seconds=0.32 * 600, audio_seconds_total=600)
        self.assertAlmostEqual(rtf, 0.32)
        self.assertAlmostEqual(1 / rtf, 3.125)

    def test_baseline_rtf_two_times_slower(self):
        # 基准的 0.48× 实时 = RTF 约 2.08。用本卷实测时长 1317 分钟（不是取整的 22 小时），
        # 1317 分钟 / 0.48 ≈ 45.7 小时墙钟——这是成本表里那个数字的来源。
        audio = 1317 * 60
        rtf = batch.real_time_factor(wall_seconds=audio / 0.48, audio_seconds_total=audio)
        self.assertAlmostEqual(rtf, 1 / 0.48, places=6)
        self.assertAlmostEqual(rtf * audio / 3600, 45.7, places=1)

    def test_zero_audio_raises(self):
        with self.assertRaises(ValueError):
            batch.real_time_factor(1.0, 0.0)


class MultipartTest(unittest.TestCase):
    def test_contains_fields_and_file(self):
        body, content_type = batch.build_multipart(
            {"texts": '["a","b"]', "seed": "42"},
            {"ref_audio": ("ref.wav", b"RIFF....")},
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        text = body.decode("utf-8", "ignore")
        self.assertIn('name="texts"', text)
        self.assertIn('name="ref_audio"; filename="ref.wav"', text)
        self.assertIn('name="seed"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
