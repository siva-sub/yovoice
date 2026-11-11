#!/usr/bin/env python3
"""run-bench.sh 的行为测试：假 colab 记录命令，证明默认 dry-run 不租卡。

    python3 scripts/colab/test_run_bench.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_BENCH = HERE / "run-bench.sh"

FAKE_COLAB = '''#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_COLAB_LOG:?}"
case "${1:-}" in
  version)
    echo "Version: 0.7.4"
    ;;
  usage)
    if [[ "${FAKE_COLAB_USAGE_RC:-0}" != "0" ]]; then
      echo "not logged in" >&2
      exit "${FAKE_COLAB_USAGE_RC}"
    fi
    echo "Current balance: 265.33 compute units"
    ;;
  new)
    if [[ "${FAKE_COLAB_NEW_RC:-0}" != "0" ]]; then
      echo "fake new failed" >&2
      exit "${FAKE_COLAB_NEW_RC}"
    fi
    ;;
  *)
    ;;
esac
'''


class RunBenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.colab = self.fake_bin / "colab"
        self.colab.write_text(FAKE_COLAB, encoding="utf-8")
        self.colab.chmod(0o755)
        self.log = self.root / "colab.log"
        self.records = self.root / "vol6.jsonl"
        self.records.write_text(
            '{"ch": "1", "idx": 0, "text": "hello world"}\n', encoding="utf-8",
        )
        self.out = self.root / "bench-T4.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_bench(self, *extra: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update({
            "COLAB_BIN": str(self.colab),
            "PYTHON_BIN": sys.executable,
            "FAKE_COLAB_LOG": str(self.log),
        })
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [
                "bash", str(RUN_BENCH),
                "--gpu", "T4",
                "--records", str(self.records),
                "--out", str(self.out),
                *extra,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_dry_run_prints_commands_and_never_calls_new(self) -> None:
        result = self.run_bench("--balance", "265.33")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for text in ("colab new --gpu T4", "provision.sh", "bench.py", "--yes", "1.68"):
            self.assertIn(text, result.stdout)
        self.assertNotIn("new", self.calls())
        self.assertFalse(self.out.exists())

    def test_dry_run_reads_read_only_usage(self) -> None:
        result = self.run_bench()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("265.33", result.stdout)
        calls = self.calls()
        self.assertIn("usage", calls)
        self.assertNotIn("new", calls)

    def test_yes_executes_full_sequence_with_fake_colab(self) -> None:
        result = self.run_bench("--balance", "265.33", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertTrue(calls[0].startswith("new --gpu T4 --session acpp-bench-t4"), calls)
        self.assertTrue(any("upload" in call and "provision.sh" in call for call in calls), calls)
        self.assertTrue(any("upload" in call and "bench.py" in call for call in calls), calls)
        self.assertTrue(any(call.startswith("exec ") for call in calls), calls)
        self.assertTrue(any(call.startswith("download ") for call in calls), calls)
        self.assertTrue(any(call.startswith("stop ") for call in calls), calls)
        self.assertIn("完成", result.stdout)

    def test_yes_stops_when_new_fails(self) -> None:
        result = self.run_bench(
            "--balance", "265.33", "--yes",
            env_extra={"FAKE_COLAB_NEW_RC": "42"},
        )
        self.assertNotEqual(result.returncode, 0)
        calls = self.calls()
        self.assertTrue(calls[0].startswith("new "), calls)
        self.assertFalse(any("upload" in call for call in calls), calls)

    def test_yes_requires_known_balance(self) -> None:
        result = self.run_bench(
            "--yes",
            env_extra={"FAKE_COLAB_USAGE_RC": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("无法读取余额", result.stderr)
        self.assertNotIn("new", self.calls())

    def test_missing_gpu_is_argument_error(self) -> None:
        result = subprocess.run(
            ["bash", str(RUN_BENCH)], text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--gpu", result.stderr)

    def test_invalid_gpu_is_argument_error(self) -> None:
        result = subprocess.run(
            ["bash", str(RUN_BENCH), "--gpu", "H100"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("不支持的卡型", result.stderr)


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套一层 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
