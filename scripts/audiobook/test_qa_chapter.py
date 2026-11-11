#!/usr/bin/env python3
"""qa-chapter.py 的行为测试：合成音频 / 记录 / 转写，不依赖本卷数据。

    python3 scripts/audiobook/test_qa_chapter.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("qa_chapter", HERE / "qa-chapter.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 qa-chapter.py")
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)

SAMPLE_RATE = 24000
# 半幅正弦的 LUFS 近似约 −19；峰值约 −15 dBFS，给真峰留足余量。
TARGET_AMPLITUDE = 0.172


def write_wav(path: Path, data: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Path:
    """写 16 位单声道 PCM，供 analyze.loudness_report 读回。"""
    clipped = np.clip(data, -1.0, 1.0)
    pcm = (clipped * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path


def tone(seconds: float, amplitude: float = TARGET_AMPLITUDE, frequency: float = 220.0) -> np.ndarray:
    times = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    envelope = np.minimum(1.0, np.minimum(times / 0.02, (seconds - times) / 0.02))
    return amplitude * np.sin(2 * np.pi * frequency * times) * envelope


def audio_report(lufs: float | None = -19.0, peak: float | None = -6.0, silences: list[dict] | None = None, clipped: int = 0) -> dict:
    return {
        "file": "ch_1.wav",
        "duration_s": 2.0,
        "lufs_approx": lufs,
        "true_peak_dbtp_approx": peak,
        "silences": silences or [],
        "clipped_samples": clipped,
    }


def wer_report(wer: float = 0.0) -> dict:
    return {
        "wer": wer,
        "errors": [] if wer == 0 else [{"cue": "1-0", "speaker": "narrator", "op": "替换", "ref": "a", "hyp": "b"}],
        "errors_total": 0 if wer == 0 else 1,
        "reference_words": 10,
        "hypothesis_words": 10,
        "transcript": "ch_1.json",
    }


class EvaluateTest(unittest.TestCase):
    def test_pass_when_all_thresholds_hold(self) -> None:
        result = qa.evaluate("1", audio_report(), wer_report(0.02))
        self.assertEqual(result["verdict"], "pass", result["checks"])
        self.assertEqual(result["wer"], 0.02)
        self.assertEqual(result["lufs"], -19.0)
        self.assertEqual(result["peak"], -6.0)
        self.assertIn("silences", result)
        self.assertTrue(result["at"].endswith("Z"))

    def test_wer_over_threshold_fails(self) -> None:
        result = qa.evaluate("1", audio_report(), wer_report(0.10))
        self.assertEqual(result["verdict"], "fail")
        self.assertFalse(result["checks"]["wer"]["ok"])

    def test_lufs_outside_tolerance_fails(self) -> None:
        result = qa.evaluate("1", audio_report(lufs=-22.0), wer_report())
        self.assertEqual(result["verdict"], "fail")
        self.assertFalse(result["checks"]["lufs"]["ok"])

    def test_peak_over_limit_fails(self) -> None:
        result = qa.evaluate("1", audio_report(peak=-1.0), wer_report())
        self.assertEqual(result["verdict"], "fail")
        self.assertFalse(result["checks"]["peak"]["ok"])

    def test_silent_chapter_fails(self) -> None:
        result = qa.evaluate("1", audio_report(lufs=None, peak=None), wer_report())
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(result["checks"]["silence"]["whole_chapter"])

    def test_clipping_fails(self) -> None:
        result = qa.evaluate("1", audio_report(clipped=42), wer_report())
        self.assertEqual(result["verdict"], "fail")
        self.assertFalse(result["checks"]["clipping"]["ok"])

    def test_long_silence_is_reported_but_not_blocking(self) -> None:
        silence = {"start_s": 1.0, "end_s": 3.0, "duration_s": 2.0, "edge": False}
        result = qa.evaluate("1", audio_report(silences=[silence]), wer_report())
        self.assertEqual(result["verdict"], "pass", result["checks"])
        self.assertEqual(result["silences"], [silence])
        self.assertTrue(any("静音" in note for note in result["notes"]))

    def test_thresholds_are_parameterized(self) -> None:
        result = qa.evaluate(
            "1", audio_report(lufs=-22.0), wer_report(0.10),
            lufs_target=-22.0, lufs_tolerance=0.5, peak_max=-3.0, wer_threshold=0.2,
        )
        self.assertEqual(result["verdict"], "pass", result["checks"])
        self.assertEqual(result["thresholds"]["lufs"], -22.0)
        self.assertEqual(result["thresholds"]["wer"], 0.2)


class CliTest(unittest.TestCase):
    def fixture(self, root: Path, transcript: str, audio: np.ndarray) -> tuple[Path, Path, Path]:
        records = root / "vol.jsonl"
        records.write_text(
            json.dumps({"ch": "1", "idx": 0, "text": "the quick brown fox"}) + "\n",
            encoding="utf-8",
        )
        transcript_path = root / "ch_1.json"
        transcript_path.write_text(json.dumps({"text": transcript}), encoding="utf-8")
        audio_path = write_wav(root / "ch_1.wav", audio)
        return records, transcript_path, audio_path

    def run_qa(self, root: Path, transcript: str, audio: np.ndarray, *extra: str) -> tuple[int, dict]:
        records, transcript_path, audio_path = self.fixture(root, transcript, audio)
        out = root / "qa.json"
        code = qa.main([
            "--audio", str(audio_path),
            "--records", str(records),
            "--transcript", str(transcript_path),
            "--json", str(out),
            *extra,
        ])
        return code, json.loads(out.read_text(encoding="utf-8"))

    def test_matching_transcript_and_level_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_qa(Path(tmp), "the quick brown fox", tone(2.0))
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["verdict"], "pass")
            self.assertEqual(payload["chapter"], "1")
            self.assertEqual(payload["wer"], 0.0)
            self.assertAlmostEqual(float(payload["lufs"]), -19.0, delta=1.0)
            self.assertLessEqual(float(payload["peak"]), -3.0)
            for key in ("wer", "lufs", "peak", "silences", "verdict", "at"):
                self.assertIn(key, payload)

    def test_wrong_transcript_fails_and_lists_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_qa(Path(tmp), "the quick brown cat", tone(2.0))
            self.assertEqual(code, 1)
            self.assertEqual(payload["verdict"], "fail")
            self.assertGreater(payload["wer"], 0.05)
            self.assertTrue(payload["errors"])

    def test_loud_audio_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_qa(Path(tmp), "the quick brown fox", tone(2.0, amplitude=0.9))
            self.assertEqual(code, 1)
            self.assertEqual(payload["verdict"], "fail")
            self.assertFalse(payload["checks"]["peak"]["ok"])

    def test_silent_audio_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_qa(Path(tmp), "the quick brown fox", np.zeros(SAMPLE_RATE * 2))
            self.assertEqual(code, 1)
            self.assertTrue(payload["checks"]["silence"]["whole_chapter"])

    def test_long_gap_is_reported_in_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = np.concatenate([tone(0.5, amplitude=0.3), np.zeros(SAMPLE_RATE * 2), tone(0.5, amplitude=0.3)])
            code, payload = self.run_qa(Path(tmp), "the quick brown fox", audio)
            self.assertEqual(code, 0, payload["checks"])
            self.assertEqual(len(payload["silences"]), 1)
            self.assertEqual(payload["verdict"], "pass")

    def test_threshold_flags_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_qa(
                Path(tmp), "the quick brown cat", tone(2.0),
                "--wer-threshold", "0.5",
            )
            self.assertEqual(code, 0, payload["checks"])
            self.assertEqual(payload["verdict"], "pass")


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
