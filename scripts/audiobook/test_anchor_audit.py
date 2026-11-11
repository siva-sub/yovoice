#!/usr/bin/env python3
"""anchor-audit.py 的行为测试：用合成信号验证指标口径，不依赖本卷音频。

判据必须能分开「清亮的浊音」与「噪声样气声」，否则这道闸门没有意义：

    python3 scripts/audiobook/test_anchor_audit.py
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("anchor_audit", HERE / "anchor-audit.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 anchor-audit.py")
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)

SAMPLE_RATE = 24000


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


def tone(seconds: float, frequency: float = 220.0, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    times = np.arange(int(seconds * sample_rate)) / sample_rate
    # 加一点包络，避免起止突变被当成帧边界噪声。
    envelope = np.minimum(1.0, np.minimum(times / 0.02, (seconds - times) / 0.02))
    return 0.5 * np.sin(2 * np.pi * frequency * times) * envelope


def noise(seconds: float, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    rng = np.random.default_rng(7)
    return 0.3 * rng.standard_normal(int(seconds * sample_rate))


class MetricTest(unittest.TestCase):
    def test_tone_is_voiced_and_tonal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "tone.wav", tone(2.0))
            data, sample_rate = audit_module.read_wav(path)
            stats = audit_module.frame_stats(data, sample_rate)
            self.assertGreater(stats["periodicity"], 0.8, "正弦波应被判为高度周期性")
            self.assertGreater(stats["voiced_frac"], 0.9, "正弦波应几乎全部是浊音")
            self.assertLess(stats["flatness"], 0.1, "正弦波应接近纯音而非噪声")

    def test_noise_is_unvoiced_and_flat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "noise.wav", noise(2.0))
            data, sample_rate = audit_module.read_wav(path)
            stats = audit_module.frame_stats(data, sample_rate)
            self.assertLess(stats["voiced_frac"], 0.2, "白噪声不应被判为浊音")
            self.assertGreater(stats["flatness"], 0.3, "白噪声的谱应接近平坦")

    def test_median_f0_tracks_frequency(self) -> None:
        for frequency in (110.0, 220.0, 330.0):
            with tempfile.TemporaryDirectory() as tmp:
                path = write_wav(Path(tmp) / f"tone-{int(frequency)}.wav", tone(2.0, frequency))
                data, sample_rate = audit_module.read_wav(path)
                measured = audit_module.median_f0(data, sample_rate)
                self.assertAlmostEqual(measured, frequency, delta=frequency * 0.1)

    def test_short_anchor_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "short.wav", tone(0.5))
            result = audit_module.audit(path)
            self.assertEqual(result["verdict"], "fail")
            self.assertTrue(any("太短" in issue for issue in result["issues"]))

    def test_breathy_anchor_fails_but_rate_only_advises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 噪声即气声样本：浊音比极低，必须进硬闸门。
            path = write_wav(Path(tmp) / "breathy.wav", noise(2.0))
            result = audit_module.audit(path, text="a b c", expect_f0=120.0)
            self.assertEqual(result["verdict"], "fail")
            self.assertTrue(any("浊音比" in issue for issue in result["issues"]))
            # F0 与语速只提示：参照系不可靠时不该拦下锚点。
            self.assertTrue(all("语速" not in issue for issue in result["issues"]))
            self.assertTrue(all("F0" not in issue for issue in result["issues"]))

    def test_clean_anchor_passes_with_advisories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "clean.wav", tone(2.0, 200.0))
            # 期望 F0 差一倍，只应出现在提示里，不影响判定。
            result = audit_module.audit(path, text="one two three four", expect_f0=100.0)
            self.assertEqual(result["verdict"], "pass", result["issues"])
            self.assertTrue(result["advisory"], "偏离目标应至少给出一条提示")

    def test_audit_reports_required_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "keys.wav", tone(1.5))
            result = audit_module.audit(path, text="one two", expect_f0=220.0)
            for key in ("file", "duration_s", "median_f0_hz", "words_per_second", "periodicity",
                        "flatness", "voiced_frac", "band_2_4k_ratio", "verdict", "issues", "advisory"):
                self.assertIn(key, result)


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套一层 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
