#!/usr/bin/env python3
"""preflight.sh 的行为测试：用假 colab/ffmpeg 与临时记录，不连网、不租卡。

    python3 scripts/colab/test_preflight.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFLIGHT = HERE / "preflight.sh"
SAFETY_LINE = "未执行任何 GPU 作业；执行前看 docs/colab-runbook.md"

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
    cat <<'OUT'
Current balance: 265.33 compute units
Usage rate: 0.00/hr
Active assignments: 0
OUT
    ;;
  *)
    exit 1
    ;;
esac
'''

FAKE_FFMPEG = '''#!/usr/bin/env bash
exit 0
'''


def write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.config = self.home / ".config" / "colab-cli"
        self.config.mkdir(parents=True)
        (self.config / "token.json").write_text(
            '{"refresh_token": "fake-refresh"}', encoding="utf-8",
        )
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.colab = write_executable(self.fake_bin / "colab", FAKE_COLAB)
        self.ffmpeg = write_executable(self.fake_bin / "ffmpeg", FAKE_FFMPEG)
        self.log = self.root / "colab.log"
        self.records = self.root / "vol6.jsonl"
        self.records.write_text(
            '{"ch": "1", "idx": 0, "text": "hello world"}\n', encoding="utf-8",
        )
        self.models = self.root / "models"
        (self.models / "Breeze-TTS-2-GGUF").mkdir(parents=True)
        (self.models / "Breeze-TTS-2-GGUF" / "breeze-tts-2-q8_0.gguf").write_bytes(b"x")
        (self.models / "Stable-Audio-3-Small-SFX-GGUF").mkdir()
        (self.models / "Stable-Audio-3-Small-SFX-GGUF" / "stable-audio-3-small-sfx-q8_0.gguf").write_bytes(b"x")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_preflight(self, *extra: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "COLAB_CONFIG_DIR": str(self.config),
            "COLAB_BIN": str(self.colab),
            "PYTHON_BIN": sys.executable,
            "FFMPEG_BIN": str(self.ffmpeg),
            "FAKE_COLAB_LOG": str(self.log),
        })
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [
                "bash", str(PREFLIGHT),
                "--records", str(self.records),
                "--models-dir", str(self.models),
                "--min-free-gb", "0",
                *extra,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_safety_last_line(self, stdout: str) -> None:
        self.assertEqual(stdout.splitlines()[-1], SAFETY_LINE, stdout)

    def test_go_when_all_checks_pass(self) -> None:
        result = self.run_preflight("--balance", "265.33")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("结论：GO", result.stdout)
        self.assert_safety_last_line(result.stdout)
        # 预检绝不能申请会话。
        self.assertNotIn("new", self.log.read_text(encoding="utf-8"))

    def test_go_uses_read_only_usage_for_balance_and_login(self) -> None:
        (self.config / "token.json").unlink()
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("余额 265.33", result.stdout)
        calls = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("usage", calls)
        self.assertNotIn("new", calls)
        self.assert_safety_last_line(result.stdout)

    def test_no_go_when_ffmpeg_missing(self) -> None:
        result = self.run_preflight(
            "--balance", "265.33",
            env_extra={"FFMPEG_BIN": str(self.root / "missing-ffmpeg")},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("结论：NO-GO", result.stdout)
        self.assertIn("ffmpeg", result.stdout)
        self.assert_safety_last_line(result.stdout)

    def test_no_go_when_gguf_cache_missing(self) -> None:
        result = self.run_preflight(
            "--balance", "265.33",
            "--models-dir", str(self.root / "missing-models"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("GGUF 缓存", result.stdout)
        self.assert_safety_last_line(result.stdout)

    def test_no_go_when_login_and_usage_fail(self) -> None:
        (self.config / "token.json").unlink()
        result = self.run_preflight(
            env_extra={"FAKE_COLAB_USAGE_RC": "1"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("登录态", result.stdout)
        self.assertIn("余额", result.stdout)
        self.assert_safety_last_line(result.stdout)

    def test_invalid_balance_is_argument_error(self) -> None:
        result = self.run_preflight("--balance", "not-a-number")
        self.assertEqual(result.returncode, 2)
        self.assertIn("余额必须是正数", result.stderr)


if __name__ == "__main__":
    # unittest.main 自己会结束进程，再套一层 sys.exit 会被静态检查判成类型不匹配。
    unittest.main(verbosity=2)
