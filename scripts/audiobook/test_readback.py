#!/usr/bin/env python3
"""readback.py 的行为测试：用假 analyze 子命令验证拼装、阈值与缺转写报错。

不跑 ASR、不连网；音频与转写只需存在，真正的响度/WER 读数由假 analyze 写入。

    python3 scripts/audiobook/test_readback.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("readback", HERE / "readback.py")
if spec is None or spec.loader is None:
    raise SystemExit("无法加载 readback.py")
readback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readback)

FAKE_ANALYZE = '''#!/usr/bin/env python3
"""假 analyze.py：把同目录的 fixture 复制到 --json 目标，并记录收到的参数。"""
import json
import sys
from pathlib import Path

base = Path(__file__).resolve().parent
args = sys.argv[1:]
with (base / "calls.log").open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

command = args[0]
out = Path(args[args.index("--json") + 1])
if not (base / "no_output").exists():
    fixture = base / f"{command}.json"
    out.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
sys.exit(int((base / f"{command}.rc").read_text(encoding="utf-8").strip() or "0"))
'''


def write_fake_analyze(
    root: Path,
    *,
    wer: float = 0.02,
    wer_rc: int = 0,
    lufs: float | None = -19.0,
    peak: float | None = -6.0,
    silences: list[dict] | None = None,
    clipped: int = 0,
) -> Path:
    """在临时目录放一份假 analyze 与两份 fixture。"""
    path = root / "fake_analyze.py"
    path.write_text(FAKE_ANALYZE, encoding="utf-8")
    path.chmod(0o755)
    (root / "loudness.json").write_text(json.dumps({
        "silence_db": -50.0,
        "min_silence": 1.5,
        "files": [{
            "file": "ch_1061_v6.wav",
            "duration_s": 2.0,
            "lufs_approx": lufs,
            "true_peak_dbtp_approx": peak,
            "silences": silences or [],
            "clipped_samples": clipped,
        }],
    }), encoding="utf-8")
    (root / "wer.json").write_text(json.dumps({
        "records": "vol6.jsonl",
        "threshold": 0.05,
        "chapters": [{"chapter": "1061", "wer": wer, "passed": wer <= 0.05}],
        "overall_wer": wer,
    }), encoding="utf-8")
    (root / "loudness.rc").write_text("0", encoding="utf-8")
    (root / "wer.rc").write_text(str(wer_rc), encoding="utf-8")
    return path


class ReadbackTest(unittest.TestCase):
    def make_files(self, root: Path) -> tuple[Path, Path, Path]:
        records = root / "vol6.jsonl"
        records.write_text(
            json.dumps({"ch": "1061", "idx": 0, "text": "hello world"}) + "\n",
            encoding="utf-8",
        )
        transcript = root / "ch_1061.json"
        transcript.write_text(json.dumps({"text": "hello world"}), encoding="utf-8")
        audio = root / "ch_1061_v6.wav"
        # 假 analyze 不读音频，只要路径存在即可；真解码由 analyze.py 自己负责。
        audio.write_bytes(b"")
        return audio, transcript, records

    def run_main(self, root: Path, *extra: str) -> tuple[int, str, str]:
        audio, transcript, records = self.make_files(root)
        out = root / "qa.json"
        command = [
            "--audio", str(audio),
            "--records", str(records),
            "--transcript", str(transcript),
            "--analyze", str(root / "fake_analyze.py"),
            *extra,
        ]
        if "--json" not in extra:
            command += ["--json", str(out)]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = readback.main(command)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_pass_builds_go_qarecord_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root)
            code, _, _ = self.run_main(root)
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            set(payload), {"wer", "lufs", "peak", "silences", "verdict", "at", "note"},
        )
        self.assertEqual(payload["wer"], 0.02)
        self.assertEqual(payload["lufs"], -19.0)
        self.assertEqual(payload["peak"], -6.0)
        self.assertIsInstance(payload["silences"], int)
        self.assertEqual(payload["silences"], 0)
        self.assertEqual(payload["verdict"], "pass")
        self.assertTrue(payload["at"].endswith("Z"))

    def test_wer_failure_still_reads_json_and_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root, wer=0.20, wer_rc=1)
            code, _, _ = self.run_main(root)
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], "fail")
        self.assertIn("WER", payload["note"])

    def test_missing_transcript_is_an_error_not_wer_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root)
            audio, _, records = self.make_files(root)
            out = root / "qa.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = readback.main([
                    "--audio", str(audio),
                    "--records", str(records),
                    "--transcript", str(root / "missing.json"),
                    "--analyze", str(root / "fake_analyze.py"),
                    "--json", str(out),
                ])
        self.assertEqual(code, 1)
        self.assertIn("找不到转写 JSON", stderr.getvalue())
        self.assertFalse((root / "calls.log").exists())
        self.assertFalse(out.exists())

    def test_missing_analyze_json_reports_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root)
            (root / "no_output").write_text("", encoding="utf-8")
            code, _, stderr = self.run_main(root)
        self.assertEqual(code, 1)
        self.assertIn("analyze loudness 没有生成 JSON", stderr)

    def test_thresholds_and_silence_flags_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root, lufs=-22.0)
            code, _, _ = self.run_main(
                root,
                "--wer-threshold", "0.5",
                "--lufs-target", "-22",
                "--lufs-tolerance", "0.5",
                "--peak-max", "-3",
                "--silence-db", "-45",
                "--min-silence", "2",
            )
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
            calls = (root / "calls.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verdict"], "pass")
        self.assertIn("--threshold 0.5", calls[1])
        self.assertIn("--chapter 1061", calls[1])
        self.assertIn("--silence-db -45", calls[0])
        self.assertIn("--min-silence 2", calls[0])

    def test_lufs_outside_default_tolerance_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root, lufs=-22.0)
            code, _, _ = self.run_main(root)
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], "fail")
        self.assertIn("LUFS", payload["note"])

    def test_silences_are_counted_and_noted(self) -> None:
        silence = {"start_s": 1.0, "end_s": 3.0, "duration_s": 2.0, "edge": False}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root, silences=[silence, {**silence, "start_s": 4.0}])
            code, _, _ = self.run_main(root)
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["silences"], 2)
        self.assertIn("静音", payload["note"])

    def test_missing_signal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root, lufs=None, peak=None)
            code, _, _ = self.run_main(root)
            payload = json.loads((root / "qa.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], "fail")
        self.assertIn("没有可测", payload["note"])

    def test_without_json_flag_prints_json_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fake_analyze(root)
            audio, transcript, records = self.make_files(root)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = readback.main([
                    "--audio", str(audio),
                    "--records", str(records),
                    "--transcript", str(transcript),
                    "--analyze", str(root / "fake_analyze.py"),
                ])
            payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["wer"], 0.02)
        self.assertIn("silences", payload)


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套一层 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
