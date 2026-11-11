#!/usr/bin/env python3
"""回读验收：把章节音频与转写 JSON 拼成 Go `schema.QARecord` 形状的 JSON。

本脚本不跑 ASR：转写 JSON 必须先由 `yovoice transcribe`（或等价工具）生成。
WER 与响度分别调用 `analyze.py` 的 `wer` / `loudness` 子命令，读它们的 `--json`
输出；这样口径只有一处，脚本只负责阈值判定与拼装。

    python3 scripts/audiobook/readback.py \
        --audio ch_1061_v6.m4b \
        --records ~/ss_audiobook/data/vol6.jsonl \
        --transcript ch_1061.json \
        --json ch_1061.qa.json

阈值全部可调；默认值与 ADR-0013 一致：WER ≤ 5%、LUFS −19 ± 1 LU、
真峰 ≤ −3 dBTP。静音只计数并提示，不单独判失败。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ANALYZE = HERE / "analyze.py"

DEFAULT_WER_THRESHOLD = 0.05
DEFAULT_LUFS_TARGET = -19.0
# ADR-0013 只写目标值，没有给容差；这里取行业常用的 ±1 LU，并由 --lufs-tolerance 覆盖。
DEFAULT_LUFS_TOLERANCE = 1.0
DEFAULT_PEAK_MAX_DBTP = -3.0
DEFAULT_SILENCE_DB = -50.0
DEFAULT_MIN_SILENCE = 1.5
EPS = 1e-9


def utc_now() -> str:
    """与 Go `time.RFC3339` 对齐的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def chapter_from_path(path: Path) -> str:
    """从 ch_1061_v6.m4b / ch_1061.json 取章节号；取不到返回空串。"""
    match = re.search(r"(\d+)", path.stem)
    return str(int(match.group(1))) if match else ""


def _run_json(analyze: Path, args: list[str], target: Path, label: str) -> dict:
    """跑一个 analyze 子命令并读它的 --json 结果。

    `analyze.py wer` 在 WER 超标时会故意返回 1，这不是执行失败；只要 JSON 落盘就
    继续读。真正失败（缺文件、解码错误）不会生成 JSON，这里会带上 stderr 报错。
    """
    command = [sys.executable, str(analyze), *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if not target.exists():
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"{label} 没有生成 JSON（退出码 {result.returncode}）："
            f"{detail[-800:] or '无输出'}"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} 生成的 JSON 不合法：{error}") from error
    return payload


def readback(
    audio: Path,
    records: Path,
    transcript: Path,
    chapter: str = "",
    analyze_path: Path = DEFAULT_ANALYZE,
    wer_threshold: float = DEFAULT_WER_THRESHOLD,
    lufs_target: float = DEFAULT_LUFS_TARGET,
    lufs_tolerance: float = DEFAULT_LUFS_TOLERANCE,
    peak_max: float = DEFAULT_PEAK_MAX_DBTP,
    silence_db: float = DEFAULT_SILENCE_DB,
    min_silence: float = DEFAULT_MIN_SILENCE,
) -> dict:
    """生成 QARecord 形状的 dict；缺转写文件直接报错，不静默跳过 WER。"""
    if not transcript.is_file():
        raise ValueError(
            f"找不到转写 JSON：{transcript}。回读需要先跑 ASR；"
            "本脚本不跑 ASR，也不会把缺失转写当成 WER=0"
        )
    if not audio.is_file():
        raise ValueError(f"找不到章节音频：{audio}")
    if not records.is_file():
        raise ValueError(f"找不到记录文件：{records}")
    if not analyze_path.is_file():
        raise ValueError(f"找不到 analyze.py：{analyze_path}")

    chapter = chapter or chapter_from_path(audio) or chapter_from_path(transcript)
    if not chapter:
        raise ValueError("无法从文件名认出章节号；请用 --chapter 指定")

    with tempfile.TemporaryDirectory(prefix="readback-") as tmp:
        tmp_path = Path(tmp)
        loudness_json = tmp_path / "loudness.json"
        wer_json = tmp_path / "wer.json"

        loudness_args = [
            "loudness", str(audio),
            "--sample-rate", "24000",
            "--silence-db", f"{silence_db:g}",
            "--min-silence", f"{min_silence:g}",
            "--json", str(loudness_json),
        ]
        loudness = _run_json(analyze_path, loudness_args, loudness_json, "analyze loudness")

        wer_args = [
            "wer",
            "--records", str(records),
            "--transcript", str(transcript),
            "--chapter", chapter,
            "--threshold", f"{wer_threshold:g}",
            "--json", str(wer_json),
        ]
        wer_payload = _run_json(analyze_path, wer_args, wer_json, "analyze wer")

    files = loudness.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise ValueError("analyze loudness 的输出里应恰好有一个文件")
    chapters = wer_payload.get("chapters")
    if not isinstance(chapters, list) or len(chapters) != 1:
        raise ValueError("analyze wer 的输出里应恰好有一章")
    audio_report = files[0]
    wer_report = chapters[0]

    try:
        wer = float(wer_report["wer"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("analyze wer 的输出缺少 wer") from error
    lufs_value = audio_report.get("lufs_approx")
    peak_value = audio_report.get("true_peak_dbtp_approx")
    lufs = None if lufs_value is None else float(lufs_value)
    peak = None if peak_value is None else float(peak_value)
    silence_list = audio_report.get("silences") or []
    if not isinstance(silence_list, list):
        raise ValueError("analyze loudness 的 silences 不是列表")
    silences = len(silence_list)
    clipped = int(audio_report.get("clipped_samples") or 0)

    wer_ok = wer <= wer_threshold + EPS
    lufs_ok = lufs is not None and abs(lufs - lufs_target) <= lufs_tolerance + EPS
    peak_ok = peak is not None and peak <= peak_max + EPS
    has_signal = lufs is not None and peak is not None
    verdict = "pass" if wer_ok and lufs_ok and peak_ok and has_signal else "fail"

    notes: list[str] = []
    if not wer_ok:
        notes.append(f"WER {wer * 100:.2f}% 超过阈值 {wer_threshold * 100:.2f}%")
    if not has_signal:
        notes.append("整章没有可测的响度/峰值，可能是 take 失败或空文件")
    else:
        if not lufs_ok:
            notes.append(f"LUFS {lufs:.2f} 偏离 {lufs_target:g} ± {lufs_tolerance:g}")
        if not peak_ok:
            notes.append(f"真峰 {peak:.2f} dBTP 超过 {peak_max:g} dBTP")
    if silences:
        notes.append(f"发现 {silences} 段 ≥ {min_silence:g} s 的静音，交人工通听确认")
    if clipped:
        notes.append(f"削波样本 {clipped} 个")
    if not notes:
        notes.append("WER、LUFS、真峰均在阈值内")

    return {
        "wer": round(wer, 4),
        "lufs": None if lufs is None else round(lufs, 2),
        "peak": None if peak is None else round(peak, 2),
        "silences": silences,
        "verdict": verdict,
        "at": utc_now(),
        "note": "；".join(notes),
    }


def print_summary(chapter: str, result: dict) -> None:
    """人类可读摘要；JSON 仍以文件或 stdout 为准。"""
    print(f"回读验收：第 {chapter} 章")
    print(f"  WER    {result['wer'] * 100:6.2f}%")
    lufs = "—" if result["lufs"] is None else f"{result['lufs']:.2f}"
    peak = "—" if result["peak"] is None else f"{result['peak']:.2f}"
    print(f"  LUFS~  {lufs:>6}")
    print(f"  真峰~  {peak:>6} dBTP")
    print(f"  静音   {result['silences']} 段")
    print(f"  结论   {'通过' if result['verdict'] == 'pass' else '不通过'}")
    if result["note"]:
        print(f"  说明   {result['note']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回读验收：调用 analyze.py 的 wer/loudness，输出 Go QARecord 形状",
    )
    parser.add_argument("--audio", required=True, help="章节音频（wav/m4b/m4a）")
    parser.add_argument("--records", required=True, help="vol6.jsonl 记录文件")
    parser.add_argument("--transcript", required=True, help="该章回读转写 JSON")
    parser.add_argument("--chapter", default="", help="章节号；默认从音频或转写文件名取")
    parser.add_argument("--wer-threshold", type=float, default=DEFAULT_WER_THRESHOLD, help="WER 阈值")
    parser.add_argument("--lufs-target", type=float, default=DEFAULT_LUFS_TARGET, help="目标 LUFS")
    parser.add_argument("--lufs-tolerance", type=float, default=DEFAULT_LUFS_TOLERANCE, help="LUFS 容差")
    parser.add_argument("--peak-max", type=float, default=DEFAULT_PEAK_MAX_DBTP, help="真峰上限 dBTP")
    parser.add_argument("--silence-db", type=float, default=DEFAULT_SILENCE_DB, help="静音阈值 dBFS")
    parser.add_argument("--min-silence", type=float, default=DEFAULT_MIN_SILENCE, help="最短静音秒数")
    parser.add_argument("--json", default="", help="QA JSON 写入路径；不写时 JSON 打到 stdout")
    parser.add_argument(
        "--analyze", default=str(DEFAULT_ANALYZE),
        help="analyze.py 路径（默认同目录；测试可覆盖）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    audio = Path(args.audio)
    transcript = Path(args.transcript)
    try:
        result = readback(
            audio=audio,
            records=Path(args.records),
            transcript=transcript,
            chapter=args.chapter,
            analyze_path=Path(args.analyze),
            wer_threshold=args.wer_threshold,
            lufs_target=args.lufs_target,
            lufs_tolerance=args.lufs_tolerance,
            peak_max=args.peak_max,
            silence_db=args.silence_db,
            min_silence=args.min_silence,
        )
    except (ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1

    chapter = args.chapter or chapter_from_path(audio) or chapter_from_path(transcript)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print_summary(chapter, result)
        print(f"\n验收 JSON 写入 {args.json}")
    else:
        # 默认只输出 JSON，方便直接交给 Go 侧或 jq。
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
