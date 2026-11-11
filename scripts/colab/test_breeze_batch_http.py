#!/usr/bin/env python3
"""breeze-batch.py 的端到端集成测试：本地假服务，不联网、不租卡、不跑模型。

补上纯逻辑测试覆盖不到的部分：multipart 编码 → HTTP → X-Segment-Bytes 解析 →
按段切分 → 写 WAV → RTF 计算，以及 409 忙时退避重试。

假服务模仿官方 breeze_infer/api.py 的 /v1/audio/speech/batch 契约：
请求是 multipart/form-data（含 texts JSON 数组），响应是拼接的 s16le PCM，
段长写在 X-Segment-Bytes 头里。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.parse import unquote

SCRIPT = Path(__file__).resolve().parent / "breeze-batch.py"
_spec = spec_from_file_location("breeze_batch", SCRIPT)
if _spec is None or _spec.loader is None:
    raise ImportError(f"加载不到 {SCRIPT}")
batch = module_from_spec(_spec)
_spec.loader.exec_module(batch)

# 每秒 24 kHz × 2 字节 = 48000 字节；测试里每段给 0.25 秒
SEGMENT_SECONDS = 0.25
SEGMENT_BYTES = int(48000 * SEGMENT_SECONDS)

TEXTS_RE = re.compile(rb'name="texts"\r\n\r\n(.*?)\r\n--', re.S)


class FakeBreeze:
    """最小可用的假 Breeze 服务。busy_first=True 时第一次请求返回 409。"""

    def __init__(self, busy_first: bool = False):
        self.busy_first = busy_first
        self.calls = 0
        self.seen_texts: list[list[str]] = []
        batches = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - 基类签名要求形参名为 format
                return  # 静默，避免污染测试输出

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                batches.calls += 1
                if batches.busy_first and batches.calls == 1:
                    self.send_response(409)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path != "/v1/audio/speech/batch":
                    self.send_error(404)
                    return
                match = TEXTS_RE.search(body)
                if not match:
                    self.send_error(400, "no texts field")
                    return
                texts = json.loads(unquote(match.group(1).decode("utf-8")))
                batches.seen_texts.append(texts)
                pcm = b"\x01\x02" * int(SEGMENT_BYTES / 2) * len(texts)
                sizes = ",".join([str(SEGMENT_BYTES)] * len(texts))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("X-Segment-Bytes", sizes)
                self.send_header("Content-Length", str(len(pcm)))
                self.end_headers()
                self.wfile.write(pcm)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class FakeResponse:
    def __init__(self, code: int):
        self.code = code


@dataclass
class FakeArgs:
    """breeze-batch.py 的 post_batch 只用这几个字段。"""

    ref_audio: Path | None = None
    ref_text: str = ""
    instruction: str | None = None
    cfg_scale: float = 1.0
    seed: int = 42
    timeout: float = 10.0
    retries: int = 3
    backoff: float = 0.01


def make_args(**over) -> FakeArgs:
    return FakeArgs(**over)


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cues = self.dir / "cues.jsonl"
        rows = [
            {"ch": 1061, "idx": i, "text": f"line {i}", "speaker": "sunny", "kind": "dialogue"}
            for i in range(5)
        ]
        self.cues.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_client(self, argv: list[str]) -> int:
        import sys
        saved = sys.argv
        sys.argv = ["breeze-batch.py", *argv]
        try:
            return batch.main()
        finally:
            sys.argv = saved

    def test_writes_one_wav_per_cue_with_correct_duration(self):
        with FakeBreeze() as fake:
            code = self.run_client([
                "--cues", str(self.cues), "--endpoint", fake.url,
                "--out-dir", str(self.dir / "out"), "--batch-size", "3", "--run",
            ])
            self.assertEqual(code, 0)
            # 5 条 cue、批宽 3 → 2 批
            self.assertEqual(len(fake.seen_texts), 2)
            self.assertEqual([len(t) for t in fake.seen_texts], [3, 2])
        files = sorted((self.dir / "out").glob("*.wav"))
        self.assertEqual(len(files), 5)
        # 每段 0.25 秒；用 wave 读回来核对，而不是相信长度
        import wave
        with wave.open(str(files[0]), "rb") as fp:
            self.assertEqual(fp.getframerate(), 24000)
            self.assertAlmostEqual(fp.getnframes() / 24000, SEGMENT_SECONDS, places=3)

    def test_retries_on_409_and_still_completes(self):
        with FakeBreeze(busy_first=True) as fake:
            code = self.run_client([
                "--cues", str(self.cues), "--endpoint", fake.url,
                "--out-dir", str(self.dir / "out2"), "--run",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(fake.calls, 2)  # 第一次 409，第二次成功
            self.assertEqual(len(fake.seen_texts), 1)
        self.assertEqual(len(list((self.dir / "out2").glob("*.wav"))), 5)

    def test_dry_run_sends_nothing(self):
        with FakeBreeze() as fake:
            code = self.run_client([
                "--cues", str(self.cues), "--endpoint", fake.url,
                "--out-dir", str(self.dir / "out3"),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(fake.calls, 0)
        self.assertFalse((self.dir / "out3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
