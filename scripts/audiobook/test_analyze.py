#!/usr/bin/env python3
"""analyze.py 的行为测试：用合成信号验证口径，不依赖本卷数据。

覆盖四件事：
  * 成本换算：22 小时音频在 0.48× 下约 45.7 GPU 小时（ADR-0003）
  * 指纹方向：正弦与白噪声的余弦应明显低于正弦与自身
  * 响度口径：半幅正弦的 RMS/峰值、长静音、削波
  * WER：已知编辑距离的替换/删除/插入，以及错词落回 cue

真实卷六数据只用于一条 skipUnless 用例；其余用例都能在空目录里跑。

    python3 scripts/audiobook/test_analyze.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("analyze", HERE / "analyze.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 analyze.py")
analyze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyze)

SAMPLE_RATE = 24000
DATA = Path.home() / "ss_audiobook" / "data"
RECORDS = DATA / "vol6.jsonl"


def write_wav(path: Path, data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    """写 16 位单声道 PCM，供被测函数读回。"""
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path


def tone(
    seconds: float,
    frequency: float = 220.0,
    amplitude: float = 0.5,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    times = np.arange(int(seconds * sample_rate)) / sample_rate
    # 加一点包络，避免起止突变被当成帧边界噪声。
    envelope = np.minimum(1.0, np.minimum(times / 0.02, (seconds - times) / 0.02))
    return amplitude * np.sin(2 * np.pi * frequency * times) * envelope


def sine(
    seconds: float,
    frequency: float = 220.0,
    amplitude: float = 0.5,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """不带包络的纯正弦，用于断言 RMS/峰值这类精确电平。"""
    times = np.arange(int(seconds * sample_rate)) / sample_rate
    return amplitude * np.sin(2 * np.pi * frequency * times)


def noise(seconds: float, amplitude: float = 0.3, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    rng = np.random.default_rng(7)
    return amplitude * rng.standard_normal(int(seconds * sample_rate))


class CostTest(unittest.TestCase):
    def test_gpu_hours_matches_adr(self) -> None:
        # ADR-0003：1,317 分钟（约 22 小时）在 0.48× 下是 45.7 GPU 小时。
        hours = analyze.gpu_hours(22 * 3600.0, analyze.RTF["breeze-tts-2"])
        self.assertAlmostEqual(hours, 45.7, delta=45.7 * 0.01)

    def test_audio_hours_from_chars_matches_volume(self) -> None:
        # 1,122,289 字符 / 14.2 字符每秒 ≈ 21.95 小时，正是 ADR 的 1,317 分钟。
        hours = 1122289 / analyze.CHARS_PER_SECOND / 3600.0
        self.assertAlmostEqual(hours, 21.95, delta=0.05)

    def test_compute_units_use_card_price(self) -> None:
        self.assertAlmostEqual(analyze.compute_units(10.0, "A100"), 117.7, places=3)
        self.assertAlmostEqual(analyze.compute_units(10.0, "T4"), 16.8, places=3)

    def test_summary_counts_and_audio_estimate(self) -> None:
        rows = [
            {"ch": "2", "idx": 0, "text": "c" * 20},
            {"ch": "1", "idx": 1, "text": "b" * 10},
            {"ch": "1", "idx": 0, "text": "a" * 10},
        ]
        summary = analyze.summarize_cost(
            rows, balance=100.0, chars_per_second=10.0, source="synthetic.jsonl",
        )
        self.assertEqual(summary["chapters"], 2)
        self.assertEqual(summary["cues"], 3)
        self.assertEqual(summary["chars"], 40)
        self.assertEqual([row["chapter"] for row in summary["chapter_rows"]], ["1", "2"])
        self.assertAlmostEqual(summary["audio_hours"], 4.0 / 3600.0, places=9)
        self.assertAlmostEqual(
            summary["selected"]["gpu_hours"], 4.0 / 3600.0 / 0.48, places=9,
        )
        self.assertEqual(summary["basis"], "chars")

    def test_audio_hours_override_is_split_by_chars(self) -> None:
        rows = [
            {"ch": "1", "idx": 0, "text": "a" * 10},
            {"ch": "2", "idx": 0, "text": "b" * 30},
        ]
        summary = analyze.summarize_cost(rows, balance=100.0, audio_hours=2.0)
        self.assertEqual(summary["basis"], "audio-hours")
        self.assertAlmostEqual(summary["audio_hours"], 2.0, places=9)
        first, second = summary["chapter_rows"]
        self.assertAlmostEqual(first["audio_seconds"], 1800.0, places=6)
        self.assertAlmostEqual(second["audio_seconds"], 5400.0, places=6)

    def test_shortfall_and_alternative(self) -> None:
        rows = [{"ch": "1", "idx": 0, "text": "a" * 1_000_000}]
        summary = analyze.summarize_cost(rows, balance=265.0)
        self.assertGreater(summary["selected"]["units"]["A100"], 265.0)
        self.assertIn("不足", summary["conclusion"])
        self.assertIn("换", summary["conclusion"])
        self.assertEqual(summary["affordable_chapters"], 0)

    def test_affordable_chapters_are_counted_in_order(self) -> None:
        rows = [
            {"ch": "1", "idx": 0, "text": "a" * 1000},
            {"ch": "2", "idx": 0, "text": "b" * 1000},
        ]
        # 每章约 0.48 单元，结余 0.6 只够第一章。
        summary = analyze.summarize_cost(rows, balance=0.6)
        self.assertEqual(summary["affordable_chapters"], 1)

    @unittest.skipUnless(RECORDS.exists(), "没有本卷数据，跳过")
    def test_real_volume_summary(self) -> None:
        summary = analyze.summarize_cost(
            analyze.load_jsonl(RECORDS), balance=265.0, source=str(RECORDS),
        )
        self.assertEqual(summary["chapters"], 170)
        self.assertEqual(summary["cues"], 8970)
        self.assertEqual(summary["chars"], 1122289)
        self.assertAlmostEqual(summary["audio_hours"], 21.95, delta=0.05)
        breeze = next(entry for entry in summary["models"] if entry["model"] == "breeze-tts-2")
        self.assertAlmostEqual(breeze["gpu_hours"], 45.7, delta=0.5)
        self.assertIn("不足", summary["conclusion"])


class FingerprintTest(unittest.TestCase):
    def test_sine_is_closer_to_itself_than_noise(self) -> None:
        sine = analyze.log_spectrum_fingerprint(tone(5.0), SAMPLE_RATE)
        same = analyze.log_spectrum_fingerprint(tone(5.0), SAMPLE_RATE)
        white = analyze.log_spectrum_fingerprint(noise(5.0), SAMPLE_RATE)
        self.assertGreater(analyze.fingerprint_cosine(sine, same), 0.99)
        self.assertGreater(
            analyze.fingerprint_cosine(sine, same),
            analyze.fingerprint_cosine(sine, white),
        )
        self.assertLess(analyze.fingerprint_cosine(sine, white), 0.6)

    def test_different_frequency_lowers_cosine(self) -> None:
        low = analyze.log_spectrum_fingerprint(tone(5.0, 220.0), SAMPLE_RATE)
        high = analyze.log_spectrum_fingerprint(tone(5.0, 880.0), SAMPLE_RATE)
        same = analyze.fingerprint_cosine(low, low)
        self.assertLess(analyze.fingerprint_cosine(low, high), same - 0.2)

    def test_silence_has_no_direction(self) -> None:
        silence = analyze.log_spectrum_fingerprint(np.zeros(SAMPLE_RATE * 5), SAMPLE_RATE)
        tone_print = analyze.log_spectrum_fingerprint(tone(5.0), SAMPLE_RATE)
        self.assertIsNone(analyze.fingerprint_cosine(silence, tone_print))


class SeamTest(unittest.TestCase):
    def test_same_tone_seam_is_normal(self) -> None:
        metrics = analyze.compare_seam(tone(2.0), tone(2.0), SAMPLE_RATE, seconds=1.0)
        self.assertGreater(metrics["cosine"], 0.95)
        self.assertLess(abs(metrics["lufs_delta"]), 0.5)
        self.assertEqual(analyze.seam_verdict(metrics, same_speaker=True), "正常")

    def test_loudness_jump_is_reported(self) -> None:
        metrics = analyze.compare_seam(
            tone(2.0, 220.0, 0.5), tone(2.0, 220.0, 0.02), SAMPLE_RATE, seconds=1.0,
        )
        # 同频同谱只改增益：指纹几乎不变，响度差约 28 dB，必须由响度项抓到。
        self.assertGreater(metrics["cosine"], 0.95)
        self.assertGreater(abs(metrics["lufs_delta"]), 20.0)
        self.assertIn("响度", analyze.seam_verdict(metrics, same_speaker=True))

    def test_silent_boundary_is_unjudgeable(self) -> None:
        metrics = analyze.compare_seam(
            tone(2.0), np.zeros(SAMPLE_RATE * 2), SAMPLE_RATE, seconds=1.0,
        )
        self.assertTrue(metrics["silent_edge"])
        self.assertIsNone(metrics["cosine"])
        self.assertIn("静音", analyze.seam_verdict(metrics))

    def test_cross_speaker_low_cosine_is_not_a_defect(self) -> None:
        metrics = {"cosine": 0.2, "lufs_delta": 0.0, "silent_edge": False}
        self.assertIn("跨角色", analyze.seam_verdict(metrics, same_speaker=False))
        self.assertIn("可疑", analyze.seam_verdict(metrics, same_speaker=True))

    def test_unknown_speaker_only_warns(self) -> None:
        metrics = {"cosine": 0.2, "lufs_delta": 0.0, "silent_edge": False}
        self.assertIn("关注", analyze.seam_verdict(metrics))
        self.assertNotIn("可疑", analyze.seam_verdict(metrics))

    def test_chapter_number_from_filename(self) -> None:
        self.assertEqual(analyze.chapter_from_name(Path("ch_1061_v6.m4b")), "1061")
        self.assertEqual(analyze.chapter_from_name(Path("random.json")), "")


class LoudnessTest(unittest.TestCase):
    def test_half_scale_sine_levels(self) -> None:
        data = sine(2.0, 220.0, 0.5)
        self.assertAlmostEqual(analyze.rms_dbfs(data), -9.03, delta=0.05)
        self.assertAlmostEqual(analyze.lufs_approx(data), -9.72, delta=0.05)
        self.assertAlmostEqual(analyze.peak_dbfs(data), -6.02, delta=0.02)
        self.assertEqual(analyze.count_clipped(data), 0)

    def test_true_peak_is_not_below_sample_peak(self) -> None:
        data = noise(1.0, 0.3)
        sample_peak = analyze.peak_dbfs(data)
        true_peak = analyze.true_peak_dbtp_approx(data, SAMPLE_RATE)
        self.assertGreaterEqual(true_peak, sample_peak - 1e-6)

    def test_full_scale_is_counted_as_clipping(self) -> None:
        data = np.ones(1000)
        self.assertEqual(analyze.count_clipped(data), 1000)
        self.assertAlmostEqual(analyze.peak_dbfs(data), 0.0, places=6)
        self.assertAlmostEqual(analyze.true_peak_dbtp_approx(data, SAMPLE_RATE), 0.0, places=6)

    def test_long_silence_is_detected(self) -> None:
        data = np.concatenate([tone(0.5), np.zeros(SAMPLE_RATE * 2), tone(0.5)])
        silences = analyze.find_silences(data, SAMPLE_RATE, -50.0, 1.5)
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0]["start_s"], 0.5, delta=0.15)
        self.assertAlmostEqual(silences[0]["duration_s"], 2.0, delta=0.15)
        self.assertFalse(silences[0]["edge"])

    def test_loudness_report_reads_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "half.wav", sine(1.0, 220.0, 0.5))
            report = analyze.loudness_report(path)
            self.assertAlmostEqual(report["duration_s"], 1.0, delta=0.01)
            self.assertAlmostEqual(report["rms_dbfs"], -9.03, delta=0.1)
            for key in ("lufs_approx", "sample_peak_dbfs", "true_peak_dbtp_approx",
                        "silences", "clipped_samples"):
                self.assertIn(key, report)


class DecodeTest(unittest.TestCase):
    def test_wav_roundtrip_and_resample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "tone.wav", tone(1.0))
            data, sample_rate = analyze.decode_audio(path)
            self.assertEqual(sample_rate, SAMPLE_RATE)
            self.assertEqual(data.size, SAMPLE_RATE)
            resampled, target_rate = analyze.decode_audio(path, 12000)
            self.assertEqual(target_rate, 12000)
            self.assertEqual(resampled.size, 12000)

    def test_missing_ffmpeg_is_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chapter.m4b"
            path.write_bytes(b"not real audio")
            with mock.patch.object(analyze.shutil, "which", return_value=None):
                with self.assertRaises(SystemExit) as caught:
                    analyze.decode_audio(path)
            message = str(caught.exception)
            self.assertIn("ffmpeg", message)
            self.assertIn("wav", message)

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            analyze.decode_audio(Path("/nonexistent/ch_0000.wav"))
        self.assertIn("找不到", str(caught.exception))


class WerTest(unittest.TestCase):
    def test_known_edit_distance(self) -> None:
        reference = analyze.tokenize("the quick brown fox jumps over the lazy dog")
        hypothesis = analyze.tokenize("the quick brown fox jumped over lazy dog")
        alignment = analyze.align_words(reference, hypothesis)
        self.assertEqual(alignment["distance"], 2)
        self.assertEqual(alignment["substitutions"], 1)
        self.assertEqual(alignment["deletions"], 1)
        self.assertEqual(alignment["insertions"], 0)
        self.assertAlmostEqual(analyze.word_error_rate(reference, hypothesis), 2 / 9)

    def test_insertion_and_deletion(self) -> None:
        reference = analyze.tokenize("a b c")
        inserted = analyze.align_words(reference, analyze.tokenize("a x b c"))
        deleted = analyze.align_words(reference, analyze.tokenize("a b"))
        self.assertEqual(inserted["insertions"], 1)
        self.assertEqual(inserted["distance"], 1)
        self.assertEqual(deleted["deletions"], 1)
        self.assertEqual(deleted["distance"], 1)

    def test_punctuation_and_case_are_ignored(self) -> None:
        self.assertEqual(
            analyze.word_error_rate(
                analyze.tokenize("Hello, WORLD!"), analyze.tokenize("hello world"),
            ),
            0.0,
        )

    def test_empty_reference(self) -> None:
        self.assertEqual(analyze.word_error_rate([], []), 0.0)
        self.assertEqual(analyze.word_error_rate([], ["extra"]), 1.0)

    def test_transcript_text_variants(self) -> None:
        self.assertEqual(analyze.transcript_text({"text": "hello"}), "hello")
        self.assertEqual(
            analyze.transcript_text({"words": [{"word": "hello"}, {"word": "world"}]}),
            "hello world",
        )
        self.assertEqual(analyze.transcript_text({"content": {"text": "nested"}}), "nested")
        with self.assertRaises(SystemExit):
            analyze.transcript_text({"captions": []})

    def test_errors_map_back_to_cue(self) -> None:
        rows = [
            {"id": "1-0", "idx": 0, "speaker": "narrator", "text": "the quick"},
            {"id": "1-1", "idx": 1, "speaker": "sunny", "text": "brown fox"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "ch_1.json"
            transcript.write_text(json.dumps({"text": "the quick fox"}), encoding="utf-8")
            result = analyze.compare_transcript(transcript, rows, "1", threshold=0.05)
        self.assertEqual(result["reference_words"], 4)
        self.assertEqual(result["distance"], 1)
        self.assertAlmostEqual(result["wer"], 0.25)
        self.assertFalse(result["passed"])
        self.assertEqual(result["errors"][0]["op"], "删除")
        self.assertEqual(result["errors"][0]["ref"], "brown")
        self.assertEqual(result["errors"][0]["cue"], "1-1")
        self.assertEqual(result["errors"][0]["speaker"], "sunny")


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套一层 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
