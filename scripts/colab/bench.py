#!/usr/bin/env python3
"""在 Colab 的一次 GPU 租用内实测 audio.cpp 的推理速度，输出可用于跨卡比较的 JSON。

设计取舍：
  * 只量 RTF 与峰值显存，不量模型文件大小——文件大小不能预测速度。
  * 每条提示都用固定 seed，换卡重跑得到的是同一条音频，比较才有意义。
  * SFX 与 TTS 分开统计：前者短、受启动开销影响大，后者长、体现稳态吞吐。

    python3 scripts/colab/bench.py --gpu-label A100
    python3 scripts/colab/bench.py --gpu-label T4 --repeats 2 --skip-provision
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# 固定提示：都取自真实工作流里的 cue 与台词，避免用"hello world"量出虚高的速度。
SFX_CASES = [
    ("hit", "heavy cavalry charging across dry cracked earth, hooves, distant", 4),
    ("hit", "massed weapon foley, blades striking, shields taking hits, close", 3),
    ("hit", "a soft-footed landing on an armoured carapace, close, outdoors", 3),
    ("bed", "cold antarctic wind over snow and ice, gusting, distant winter sea", 20),
]
TTS_CASE = "The clangor of two clashing swords drowned out the cacophony of the dying storm."


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """运行外部命令；失败时把 stdout/stderr 一起带上，方便在 Colab 日志里定位。"""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def gpu_info() -> dict:
    """取卡名与总显存；没有 nvidia-smi 时返回空字典而不是报错。"""
    if not shutil.which("nvidia-smi"):
        return {}
    out = sh(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]).stdout.strip()
    if not out:
        return {}
    name, total, driver = [part.strip() for part in out.splitlines()[0].split(",")]
    return {"name": name, "memory_total_mib": int(total.replace(" MiB", "")), "driver": driver}


def peak_vram_mib() -> int:
    """采样一次当前显存占用。长任务里由调用方在结束后调用，读到的是峰值附近。"""
    if not shutil.which("nvidia-smi"):
        return 0
    out = sh(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).stdout.strip()
    try:
        return int(out.splitlines()[0])
    except (ValueError, IndexError):
        return 0


def run_provision(script: Path) -> None:
    """调用 provision.sh 准备引擎与模型；已有构建时脚本内部会跳过编译。"""
    result = sh(["bash", str(script)])
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("provision.sh 失败")
    print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "provision 完成")


def bench_sfx(cli: Path, model: Path, out_dir: Path, repeats: int) -> list[dict]:
    """逐个 cue 渲染并记录墙钟；duration 是提示的一部分，必须如实传给模型。"""
    rows = []
    for index, (role, prompt, seconds) in enumerate(SFX_CASES):
        for repeat in range(repeats):
            out = out_dir / f"sfx-{index}-{repeat}.wav"
            started = time.monotonic()
            result = sh([
                str(cli), "--task", "gen", "--family", "stable_audio",
                "--model", str(model), "--backend", "cuda",
                "--text", prompt, "--duration-seconds", str(seconds),
                "--request-option", f"seed={1000 + index}", "--out", str(out),
            ])
            wall = time.monotonic() - started
            ok = result.returncode == 0 and out.exists()
            rows.append({
                "kind": "sfx", "role": role, "seconds": seconds, "repeat": repeat,
                "wall_s": round(wall, 3), "rtf": round(seconds / wall, 3) if wall > 0 else None,
                "ok": ok, "error": "" if ok else (result.stderr or result.stdout)[-400:],
            })
    return rows


def bench_tts(cli: Path, model: Path, ref: Path | None, out_dir: Path, repeats: int) -> list[dict]:
    """用 breeze 的音色指令模式渲染一条台词；有参考音频时改走克隆，贴近真实用法。"""
    rows = []
    for repeat in range(repeats):
        out = out_dir / f"tts-{repeat}.wav"
        cmd = [
            str(cli), "--task", "clon" if ref else "tts", "--family", "breeze_tts",
            "--model", str(model), "--backend", "cuda", "--text", TTS_CASE,
            "--request-option", "instruction=A cold, exhausted commander; flat and quiet.",
            "--request-option", "seed=7", "--out", str(out),
        ]
        if ref:
            cmd += ["--voice-ref", str(ref), "--reference-text", "Some call me nature."]
        started = time.monotonic()
        result = sh(cmd)
        wall = time.monotonic() - started
        ok = result.returncode == 0 and out.exists()
        # 用生成文件的时长算 RTF，避免假设输出长度与输入字符数的关系。
        seconds = 0.0
        if ok:
            probe = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(out)])
            try:
                seconds = float(probe.stdout.strip())
            except ValueError:
                seconds = 0.0
        rows.append({
            "kind": "tts", "role": "tts", "seconds": round(seconds, 3), "repeat": repeat,
            "wall_s": round(wall, 3), "rtf": round(seconds / wall, 3) if wall and seconds else None,
            "ok": ok, "error": "" if ok else (result.stderr or result.stdout)[-400:],
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """按 (kind, role) 聚合：成功次数、平均墙钟、平均 RTF。"""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(f"{row['kind']}:{row['role']}", []).append(row)
    summary = {}
    for key, items in groups.items():
        ok_items = [item for item in items if item["ok"]]
        rtfs = [item["rtf"] for item in ok_items if item["rtf"]]
        summary[key] = {
            "runs": len(items),
            "failed": len(items) - len(ok_items),
            "mean_wall_s": round(statistics.fmean(item["wall_s"] for item in ok_items), 3) if ok_items else None,
            "mean_rtf": round(statistics.fmean(rtfs), 3) if rtfs else None,
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Colab 上实测 audio.cpp 的 GPU 配置")
    parser.add_argument("--gpu-label", default=os.environ.get("GPU_LABEL", "unknown"),
                        help="写进结果的卡型标签，例如 T4 / L4 / A100 / G4")
    parser.add_argument("--repeats", type=int, default=1, help="每条提示重复次数")
    parser.add_argument("--engine-root", default=os.environ.get("AUDIOCPP_ROOT", "/content/audio.cpp"))
    parser.add_argument("--models-root", default=os.environ.get("MODELS_ROOT", "/content/models"))
    parser.add_argument("--out", default="", help="结果 JSON 路径，默认 /content/bench-<label>.json")
    parser.add_argument("--skip-provision", action="store_true", help="引擎与模型已就绪时跳过一次准备")
    args = parser.parse_args()

    engine_root = Path(args.engine_root)
    models_root = Path(args.models_root)
    cli = engine_root / "build" / "bin" / "audiocpp_cli"
    provision = Path(__file__).resolve().parent / "provision.sh"

    info = gpu_info()
    print(f"GPU: {info.get('name', '未检测到')} · 显存 {info.get('memory_total_mib', 0)} MiB")

    if not args.skip_provision:
        run_provision(provision)
    for binary in (cli, models_root / "Stable-Audio-3-Small-SFX-GGUF/stable-audio-3-small-sfx-q8_0.gguf",
                   models_root / "Breeze-TTS-2-GGUF/breeze-tts-2-q8_0.gguf"):
        if not binary.exists():
            raise SystemExit(f"缺少 {binary}；去掉 --skip-provision 重新准备")

    out_dir = Path("/content/bench-out")
    out_dir.mkdir(parents=True, exist_ok=True)
    # 参考音频可选：Breeze 在没有参考时走音色设计，有参考时走克隆，两者速度不同，都记录下来。
    ref = models_root / "Breeze-TTS-2-GGUF/ref_voice.wav"
    rows = bench_sfx(cli, models_root / "Stable-Audio-3-Small-SFX-GGUF/stable-audio-3-small-sfx-q8_0.gguf", out_dir, args.repeats)
    rows += bench_tts(cli, models_root / "Breeze-TTS-2-GGUF/breeze-tts-2-q8_0.gguf", ref if ref.exists() else None, out_dir, args.repeats)

    report = {
        "gpu_label": args.gpu_label,
        "gpu": info,
        "engine_commit": sh(["git", "-C", str(engine_root), "rev-parse", "--short", "HEAD"]).stdout.strip(),
        "peak_vram_mib": peak_vram_mib(),
        "repeats": args.repeats,
        "summary": summarize(rows),
        "runs": rows,
    }
    target = Path(args.out or f"/content/bench-{args.gpu_label}.json")
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print(f"结果写入 {target}")
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
