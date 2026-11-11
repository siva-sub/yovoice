#!/usr/bin/env python3
"""Breeze TTS 2 官方 HTTP 批量端点客户端。

为什么需要它：官方服务的 `/v1/audio/speech/batch` 一次能合成 128 条文本，且深度解码走
CUDA Graph 批量路径；我们逐条调用 CLI 等于把这条路径关掉（见 docs/breeze-colab-efficiency.md）。

用法（默认 dry-run，只打印将与哪些批次，不联网）：
    python3 scripts/colab/breeze-batch.py --cues data/vol6.jsonl \
        --ref-audio anchors/anchor_sunny.wav --ref-text "..." --max-cues 20 --dry-run

真正执行时加 --run，并给出 --endpoint（Colab 上起好的 Breeze API 服务）。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

# 官方 api.py 的常量：一次批量最多 128 条。
MAX_BATCH_TEXTS = 128
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # s16le


def split_pcm(data: bytes, sizes: list[int]) -> list[bytes]:
    """按 X-Segment-Bytes 切分拼接的 s16le PCM。

    严格校验：少一个字节都会让后续所有段落错位，所以宁可直接报错也不静默截断。
    """
    if not sizes:
        if data:
            raise ValueError(f"没有段落尺寸但收到 {len(data)} 字节")
        return []
    if any(n <= 0 for n in sizes):
        raise ValueError("段落尺寸必须为正")
    if sum(sizes) != len(data):
        raise ValueError(f"段落尺寸合计 {sum(sizes)} 与收到字节 {len(data)} 不一致")
    out, offset = [], 0
    for n in sizes:
        out.append(data[offset:offset + n])
        offset += n
    return out


def parse_sizes(header: str) -> list[int]:
    """解析 `X-Segment-Bytes: 48000,51200`。"""
    return [int(part) for part in header.replace(" ", "").split(",") if part]


def batch_items(items: list, size: int = MAX_BATCH_TEXTS) -> list[list]:
    """按批量上限切分，返回批次列表。顺带用于估算请求数（8,794 → 69）。"""
    if size <= 0:
        raise ValueError("批量上限必须为正")
    return [items[i:i + size] for i in range(0, len(items), size)]


def audio_seconds(data: bytes) -> float:
    return len(data) / (SAMPLE_RATE * SAMPLE_WIDTH)


def real_time_factor(wall_seconds: float, audio_seconds_total: float) -> float:
    """RTF = 墙钟时间 / 音频时长。0.32 表示生成比实时快约 3.1 倍。"""
    if audio_seconds_total <= 0:
        raise ValueError("音频时长必须为正")
    return wall_seconds / audio_seconds_total


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fp:
        fp.setnchannels(1)
        fp.setsampwidth(SAMPLE_WIDTH)
        fp.setframerate(SAMPLE_RATE)
        fp.writeframes(pcm)


def load_cues(path: Path, limit: int | None) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:limit] if limit else rows


def build_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    """构造 multipart/form-data。不引入第三方依赖。"""
    boundary = "----breezebatch" + str(int(time.time() * 1000))
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode() + b"\r\n"
    for name, (filename, data) in files.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def post_batch(endpoint: str, texts: list[str], args, timeout: float) -> tuple[bytes, list[int]]:
    """发一批，返回 (拼接 PCM, 段落字节数)。409 表示服务忙，退避后重试。"""
    fields = {
        "texts": json.dumps(texts, ensure_ascii=False),
        "cfg_scale": str(args.cfg_scale),
        "seed": str(args.seed),
        "ref_text": args.ref_text or "",
    }
    if args.instruction:
        fields["instruction"] = args.instruction
    files = {}
    if args.ref_audio:
        files["ref_audio"] = (Path(args.ref_audio).name, Path(args.ref_audio).read_bytes())
    body, content_type = build_multipart(fields, files)

    url = endpoint.rstrip("/") + "/v1/audio/speech/batch"
    for attempt in range(args.retries):
        request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                sizes = parse_sizes(response.headers.get("X-Segment-Bytes", ""))
                return response.read(), sizes
        except urllib.error.HTTPError as err:
            if err.code == 409 and attempt + 1 < args.retries:
                time.sleep(args.backoff * (attempt + 1))
                continue
            raise
    raise RuntimeError("重试用尽")


def main() -> int:
    parser = argparse.ArgumentParser(description="Breeze TTS 2 批量渲染客户端")
    parser.add_argument("--cues", type=Path, required=True, help="cues JSONL（每条含 text）")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--ref-audio", type=Path, help="参考音频（克隆时必填）")
    parser.add_argument("--ref-text", help="参考音频的逐字稿")
    parser.add_argument("--instruction", help="音色指令；按 ADR-0006 默认不给")
    parser.add_argument("--cfg-scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_TEXTS)
    parser.add_argument("--max-cues", type=int, help="只处理前 N 条，用于小样验证")
    parser.add_argument("--out-dir", type=Path, default=Path("breeze-batch-out"))
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--backoff", type=float, default=2.0)
    parser.add_argument("--run", action="store_true", help="真正发请求；不加则只做 dry-run")
    args = parser.parse_args()

    cues = load_cues(args.cues, args.max_cues)
    if not cues:
        raise SystemExit("没有可渲染的 cue")
    groups = batch_items(cues, args.batch_size)
    print(f"cue {len(cues)} 条 → {len(groups)} 批（每批上限 {args.batch_size}）")

    if not args.run:
        for index, group in enumerate(groups, 1):
            preview = group[0]["text"][:24].replace("\n", " ")
            print(f"  批 {index}: {len(group)} 条，例如「{preview}…」")
        print("dry-run：未发送任何请求。加 --run 才会真正调用端点。")
        return 0

    started = time.monotonic()
    total_audio = 0.0
    for index, group in enumerate(groups, 1):
        texts = [row["text"] for row in group]
        pcm, sizes = post_batch(args.endpoint, texts, args, args.timeout)
        if len(sizes) != len(group):
            raise SystemExit(f"批 {index}: 返回 {len(sizes)} 段，期望 {len(group)} 段")
        segments = split_pcm(pcm, sizes)
        for row, segment in zip(group, segments):
            name = f"ch{row.get('ch', 0)}_{row.get('idx', 0)}.wav"
            write_wav(args.out_dir / name, segment)
            total_audio += audio_seconds(segment)
        print(f"  批 {index}/{len(groups)} 完成：{len(sizes)} 段，累计音频 {total_audio/60:.1f} 分钟")

    wall = time.monotonic() - started
    rtf = real_time_factor(wall, total_audio)
    print(f"\n墙钟 {wall/60:.2f} 分钟，音频 {total_audio/60:.2f} 分钟，RTF {rtf:.3f}"
          f"（≈{1/rtf:.2f}× 实时）" if rtf > 0 else "")
    print(f"输出目录：{args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
