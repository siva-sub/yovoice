#!/usr/bin/env python3
"""单章音频验收（ADR-0013）：把 loudness 与 WER 两个子命令合成一份可入库的 JSON。

产出字段与 ADR-0013 的 `qa` 记录对齐，供 Go 侧 `RecordQA` 录入：

    wer / lufs / peak / silences / verdict / at

判定口径：

  * 回读 WER ≤ 5%（默认，可参数化）；错词逐条列出，读错人名、吞词都在这里显形
  * LUFS 在 −19 ± 1 LU 内（默认，可参数化）
  * 真峰值 ≤ −3 dBTP（默认，可参数化）
  * 整章静音或存在削波即失败
  * 章内 ≥ 1.5 s 的空洞只报告、不单独判失败：它可能是正常留白，也可能是 take 失败，
    最终由校对式通听确认（ADR-0013 第 3、4 条）

注意：这里复用的是 `analyze.py` 的 `lufs_approx` 与 `true_peak_dbtp_approx`——不做 K 加权、
真峰用 4 倍线性插值，都是近似值。它们足够抓“整章偏响/偏轻/接近满刻度”，但**不能替代**
交付前的真 LUFS / 真峰值表；JSON 的 `notes` 会带上这条限制。

    python3 scripts/audiobook/qa-chapter.py \
        --audio ch_1061_v6.m4b \
        --records ~/ss_audiobook/data/vol6.jsonl \
        --transcript ch_1061.json \
        --json ch_1061.qa.json

没有转写文件时无法算 WER，脚本会明确报错，而不是把 WER 当成 0。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_analyze():
    """按文件路径加载 analyze.py，复用它的 loudness / wer 实现。"""
    spec = importlib.util.spec_from_file_location("analyze", HERE / "analyze.py")
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analyze = load_analyze()

DEFAULT_LUFS_TARGET = -19.0
# ADR-0013 只写目标值，没有给容差；这里取行业常用的 ±1 LU，并由 --lufs-tolerance 覆盖。
DEFAULT_LUFS_TOLERANCE = 1.0
DEFAULT_PEAK_MAX_DBTP = -3.0
DEFAULT_WER_THRESHOLD = analyze.WER_THRESHOLD
EPS = 1e-9


def utc_now() -> str:
    """RFC3339 UTC 时间，与 Go 侧 `time.RFC3339` 对齐。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def chapter_from_path(path: Path) -> str:
    """先从文件名取章节；analyze 只认 3 位以上数字，这里再兜一层 1–2 位的单章号。"""
    chapter = analyze.chapter_from_name(path)
    if chapter:
        return chapter
    match = re.search(r"(\d+)", path.stem)
    return match.group(1) if match else ""


def evaluate(
    chapter: str,
    audio_report: dict,
    wer_report: dict,
    lufs_target: float = DEFAULT_LUFS_TARGET,
    lufs_tolerance: float = DEFAULT_LUFS_TOLERANCE,
    peak_max: float = DEFAULT_PEAK_MAX_DBTP,
    wer_threshold: float = DEFAULT_WER_THRESHOLD,
    at: str = "",
) -> dict:
    """把两份报告合成 QA 结论；纯函数，测试不需要音频文件。"""
    wer = float(wer_report["wer"])
    lufs = audio_report.get("lufs_approx")
    peak = audio_report.get("true_peak_dbtp_approx")
    silences = list(audio_report.get("silences") or [])
    clipped = int(audio_report.get("clipped_samples") or 0)
    silent_chapter = lufs is None and peak is None

    wer_ok = wer <= wer_threshold + EPS
    lufs_ok = lufs is not None and abs(float(lufs) - lufs_target) <= lufs_tolerance + EPS
    peak_ok = peak is not None and float(peak) <= peak_max + EPS
    clipping_ok = clipped == 0
    silence_ok = not silent_chapter
    verdict = "pass" if all((wer_ok, lufs_ok, peak_ok, clipping_ok, silence_ok)) else "fail"

    notes = [
        "lufs 与 peak 来自 analyze.py 的近似口径（无 K 加权、4 倍插值真峰），"
        "交付前仍需真 LUFS / 真峰值表复核",
    ]
    if silent_chapter:
        notes.append("整章没有可测信号，可能是 take 失败或文件为空")
    if silences:
        notes.append(f"发现 {len(silences)} 段 ≥ {analyze.SILENCE_SECONDS:g} s 的静音；"
                     "可能是留白，也可能是缺陷，交人工通听确认")
    if clipped:
        notes.append(f"削波样本 {clipped} 个；数字削波不可接受")
    if not lufs_ok and not silent_chapter:
        notes.append(f"LUFS 偏离目标 {lufs_target:g} ± {lufs_tolerance:g}")
    if not peak_ok and peak is not None:
        notes.append(f"真峰 {float(peak):.2f} dBTP 超过 {peak_max:g} dBTP")

    return {
        "chapter": chapter,
        "wer": round(wer, 4),
        "lufs": None if lufs is None else round(float(lufs), 2),
        "peak": None if peak is None else round(float(peak), 2),
        "silences": silences,
        "verdict": verdict,
        "at": at or utc_now(),
        "thresholds": {
            "wer": wer_threshold,
            "lufs": lufs_target,
            "lufs_tolerance": lufs_tolerance,
            "peak": peak_max,
        },
        "checks": {
            "wer": {
                "value": round(wer, 4),
                "threshold": wer_threshold,
                "ok": wer_ok,
            },
            "lufs": {
                "value": None if lufs is None else round(float(lufs), 2),
                "target": lufs_target,
                "tolerance": lufs_tolerance,
                "ok": lufs_ok,
            },
            "peak": {
                "value": None if peak is None else round(float(peak), 2),
                "max": peak_max,
                "ok": peak_ok,
            },
            "clipping": {"samples": clipped, "ok": clipping_ok},
            "silence": {
                "count": len(silences),
                "whole_chapter": silent_chapter,
                "ok": silence_ok,
            },
        },
        "errors": wer_report.get("errors", []),
        "errors_total": int(wer_report.get("errors_total", 0)),
        "reference_words": int(wer_report.get("reference_words", 0)),
        "hypothesis_words": int(wer_report.get("hypothesis_words", 0)),
        "duration_s": audio_report.get("duration_s"),
        "audio": audio_report.get("file", ""),
        "transcript": wer_report.get("transcript", ""),
        "records": "",
        "notes": notes,
    }


def print_summary(result: dict) -> None:
    print(f"单章验收：第 {result['chapter']} 章")
    checks = result["checks"]
    wer_mark = "✓" if checks["wer"]["ok"] else "✗"
    print(
        f"  WER      {result['wer'] * 100:6.2f}%  ≤ {checks['wer']['threshold'] * 100:.2f}%  {wer_mark}"
    )
    lufs_value = "—" if result["lufs"] is None else f"{result['lufs']:.1f}"
    lufs_mark = "✓" if checks["lufs"]["ok"] else "✗"
    print(
        f"  LUFS~    {lufs_value:>6}   {checks['lufs']['target']:g} ± "
        f"{checks['lufs']['tolerance']:g}      {lufs_mark}"
    )
    peak_value = "—" if result["peak"] is None else f"{result['peak']:.1f}"
    peak_mark = "✓" if checks["peak"]["ok"] else "✗"
    print(f"  真峰~    {peak_value:>6} dBTP ≤ {checks['peak']['max']:g}  {peak_mark}")
    clip_mark = "✓" if checks["clipping"]["ok"] else "✗"
    print(
        f"  静音     {checks['silence']['count']} 段；削波 {checks['clipping']['samples']}  {clip_mark}"
    )
    if result["errors"]:
        print(f"  错词（前 {len(result['errors'])} 条）：")
        for error in result["errors"]:
            print(
                f"    cue {error['cue']} [{error['speaker'] or '?'}] "
                f"{error['op']}：{error['ref'] or '∅'} → {error['hyp'] or '∅'}"
            )
        if result["errors_total"] > len(result["errors"]):
            print(f"    ……另有 {result['errors_total'] - len(result['errors'])} 条未列出")
    for note in result["notes"]:
        print(f"  注：{note}")
    print(f"\n结论：{'通过' if result['verdict'] == 'pass' else '不通过'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单章音频验收：WER + 响度 + 真峰 + 静音")
    parser.add_argument("--audio", required=True, help="章节音频（wav/m4b/m4a）")
    parser.add_argument("--records", required=True, help="vol6.jsonl 记录文件")
    parser.add_argument("--transcript", required=True, help="该章回读转写 JSON")
    parser.add_argument("--chapter", default="", help="章节号；默认从文件名取")
    parser.add_argument("--lufs-target", type=float, default=DEFAULT_LUFS_TARGET, help="目标 LUFS")
    parser.add_argument("--lufs-tolerance", type=float, default=DEFAULT_LUFS_TOLERANCE, help="LUFS 容差")
    parser.add_argument("--peak-max", type=float, default=DEFAULT_PEAK_MAX_DBTP, help="真峰上限 dBTP")
    parser.add_argument("--wer-threshold", type=float, default=DEFAULT_WER_THRESHOLD, help="WER 阈值")
    parser.add_argument("--silence-db", type=float, default=analyze.SILENCE_DB, help="静音阈值 dBFS")
    parser.add_argument("--min-silence", type=float, default=analyze.SILENCE_SECONDS, help="最短静音秒数")
    parser.add_argument("--max-errors", type=int, default=50, help="最多列出的错词数")
    parser.add_argument("--json", default="", help="验收 JSON 写入路径")
    args = parser.parse_args(argv)

    records = analyze.group_chapters(analyze.load_jsonl(Path(args.records)))
    audio_path = Path(args.audio)
    transcript_path = Path(args.transcript)
    chapter = (
        args.chapter
        or chapter_from_path(audio_path)
        or chapter_from_path(transcript_path)
    )
    if not chapter:
        raise SystemExit(
            "无法从文件名认出章节号；请把文件写成 ch_1061.* 或用 --chapter 指定"
        )
    if chapter not in records:
        raise SystemExit(f"{args.records} 里没有第 {chapter} 章")

    audio_report = analyze.loudness_report(audio_path, args.silence_db, args.min_silence)
    wer_report = analyze.compare_transcript(
        transcript_path, records[chapter], chapter, args.wer_threshold, args.max_errors,
    )
    result = evaluate(
        chapter,
        audio_report,
        wer_report,
        lufs_target=args.lufs_target,
        lufs_tolerance=args.lufs_tolerance,
        peak_max=args.peak_max,
        wer_threshold=args.wer_threshold,
    )
    result["records"] = args.records
    print_summary(result)
    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"验收 JSON 写入 {args.json}")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
