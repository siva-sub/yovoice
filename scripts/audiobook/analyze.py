#!/usr/bin/env python3
"""P0 只读分析：成本、章缝、响度、回读 WER。

四个子命令都只读已有产物，不改任何代码与数据：

  cost      读 vol6.jsonl，按章汇总字数 → 音频小时 → GPU 小时 → 计算单元，对照结余
  seam      解码相邻两章，比较前一章末 5 秒与后一章首 5 秒的对数谱指纹余弦与响度差
  loudness  逐文件量 RMS/LUFS 近似、真峰值近似、长静音与削波
  wer       读 yovoice 转写 JSON，与 vol6.jsonl 的 cue 文本做词级比对，列出错词

RTF 表是稳态实时倍率（RTFx，见 ADR-0003）：GPU 小时 = 音频小时 ÷ RTF。
不要把 0.48 当成“每字符 GPU 秒”。

本机没有 soundfile：wav 用标准库 wave 读，m4b/m4a 用 ffmpeg 解码；没有 ffmpeg 时
明确报错并提示改用 wav，不静默返回空结果。

    python3 scripts/audiobook/analyze.py cost \
        --records ~/ss_audiobook/data/vol6.jsonl --balance 265
    python3 scripts/audiobook/analyze.py seam \
        ~/ss_audiobook/pilot/ch_1061_v6.m4b ~/ss_audiobook/pilot/ch_1062_v6.m4b \
        --records ~/ss_audiobook/data/vol6.jsonl
    python3 scripts/audiobook/analyze.py loudness ~/ss_audiobook/pilot/ch_1061_v6.m4b
    python3 scripts/audiobook/analyze.py wer \
        --records ~/ss_audiobook/data/vol6.jsonl --transcript ch_1061.json --chapter 1061
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

# 稳态 RTFx 来自 ADR-0003 的第三方基准表；GPU 小时 = 音频秒数 / RTF / 3600。
RTF = {
    "breeze-tts-2": 0.48,
    "ming-3.0-0.5b": 1.01,
    "qwen3-tts-1.7b": 3.58,
    "longcat-audiodit-1b": 6.69,
    "drama-3": 7.6,
}
# Colab 计算单元折算价（单元 / GPU 小时，2026-10 官方价目）。账号档位或价格变化时只改这张表。
CARD_UNITS_PER_HOUR = {
    "T4": 1.68,
    "L4": 4.82,
    "G4": 4.82,
    "A100": 11.77,
    "H100": 19.24,
}
# 本卷实测：1,122,289 字符对应 1,317 分钟音频（ADR-0003），约 14.2 字符/秒。
# 没有实测时长时用它把字数折成音频小时；已知总时长请用 cost --audio-hours 覆盖。
CHARS_PER_SECOND = 14.2
DEFAULT_MODEL = "breeze-tts-2"
DEFAULT_CARD = "A100"

# 音频分析统一到 24 kHz 单声道：TTS 成品通常就是这个采样率，足够覆盖语音频带。
SAMPLE_RATE = 24000
# 章缝观察窗与启发式阈值。余弦阈值只是起始值，不是 ADR 判定标准；报告里同时给原始数。
SEAM_SECONDS = 5.0
SEAM_COSINE_WARN = 0.75
SEAM_LOUDNESS_WARN_DB = 3.0
SEAM_SILENCE_DB = -50.0
# 静音与削波的默认口径来自 ADR-0013：超过 1.5 s 的空洞值得报，接近满刻度的样本记削波。
SILENCE_DB = -50.0
SILENCE_SECONDS = 1.5
CLIP_LEVEL = 0.999
# ADR-0013 的默认 WER 闸门；专有名词多的小说应逐章看错词，而不是只看这个数。
WER_THRESHOLD = 0.05


# ---------- 通用 ----------


def load_jsonl(path: Path) -> list[dict]:
    """逐行读 JSONL；坏行直接报出文件与行号，不跳过。"""
    if not path.exists():
        raise SystemExit(f"找不到记录文件 {path}")
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number} 不是合法 JSON：{error}") from error
    return rows


def chapter_sort_key(chapter: object) -> tuple[int, object]:
    """章节优先按数字排；非数字章节排在后面，避免把 “番外” 当成 0。"""
    try:
        return (0, int(str(chapter)))
    except ValueError:
        return (1, str(chapter))


def group_chapters(rows: list[dict]) -> dict[str, list[dict]]:
    """按章分组并保持章内 idx 顺序。"""
    by_chapter: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if "ch" not in row or "text" not in row:
            raise SystemExit(f"记录缺少 ch/text 字段：{str(row)[:120]}")
        by_chapter[str(row["ch"])].append(row)
    for chapter_rows in by_chapter.values():
        chapter_rows.sort(key=lambda row: int(row.get("idx", 0)))
    return dict(sorted(by_chapter.items(), key=lambda item: chapter_sort_key(item[0])))


def write_json(path: str, payload: object) -> None:
    """写 UTF-8 JSON；不创建目录，路径写错时不静默落到别处。"""
    target = Path(path)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


# ---------- 成本 ----------


def gpu_hours(audio_seconds: float, rtf: float) -> float:
    """音频秒数 ÷ RTFx ÷ 3600。22 小时音频在 0.48× 下约 45.7 GPU 小时。"""
    if rtf <= 0:
        raise ValueError("RTF 必须大于 0")
    return audio_seconds / rtf / 3600.0


def compute_units(gpu_hour_count: float, card: str) -> float:
    """GPU 小时 × 卡型单价（计算单元/小时）。"""
    if card not in CARD_UNITS_PER_HOUR:
        raise ValueError(f"未知卡型 {card}")
    return gpu_hour_count * CARD_UNITS_PER_HOUR[card]


def summarize_cost(
    rows: list[dict],
    balance: float,
    model: str = DEFAULT_MODEL,
    card: str = DEFAULT_CARD,
    chars_per_second: float = CHARS_PER_SECOND,
    audio_hours: float = 0.0,
    source: str = "",
) -> dict:
    """按章汇总字数，折算音频小时、GPU 小时与计算单元，并对照结余。

    audio_hours > 0 时表示已知整卷实际音频时长，按各章字数比例分摊；否则用
    chars_per_second 估算。RTF 只按模型取表，不随卡型变化——卡型表只负责单价，
    换卡带来的吞吐差必须实测（ADR-0003 待办），这里不假装知道。
    """
    if model not in RTF:
        raise ValueError(f"未知模型 {model}")
    if chars_per_second <= 0:
        raise ValueError("chars_per_second 必须大于 0")

    by_chapter = group_chapters(rows)
    total_chars = sum(len(str(row["text"])) for row in rows)
    total_cues = len(rows)
    if audio_hours > 0:
        seconds_per_char = audio_hours * 3600.0 / total_chars if total_chars else 0.0
        basis = "audio-hours"
    else:
        seconds_per_char = 1.0 / chars_per_second
        basis = "chars"

    chapters: list[dict] = []
    total_audio_seconds = 0.0
    for chapter, chapter_rows in by_chapter.items():
        chars = sum(len(str(row["text"])) for row in chapter_rows)
        audio_seconds = chars * seconds_per_char
        total_audio_seconds += audio_seconds
        hours = gpu_hours(audio_seconds, RTF[model])
        chapters.append({
            "chapter": chapter,
            "cues": len(chapter_rows),
            "chars": chars,
            "audio_seconds": audio_seconds,
            "gpu_hours": hours,
            "units": compute_units(hours, card),
        })

    models: list[dict] = []
    for name, rtf in RTF.items():
        hours = gpu_hours(total_audio_seconds, rtf)
        units = {card_name: compute_units(hours, card_name) for card_name in CARD_UNITS_PER_HOUR}
        models.append({
            "model": name,
            "rtf": rtf,
            "gpu_hours": hours,
            "units": units,
            "fits": {card_name: value <= balance for card_name, value in units.items()},
        })

    selected = next(entry for entry in models if entry["model"] == model)
    # 按章节顺序累计，回答“现有结余够渲到第几章”，比按全书平均更贴近实际排期。
    spent = 0.0
    affordable = 0
    for chapter in chapters:
        if spent + chapter["units"] > balance:
            break
        spent += chapter["units"]
        affordable += 1

    selected_units = selected["units"][card]
    shortfall = selected_units - balance
    fitting = sorted(
        (entry for entry in models if entry["fits"][card]),
        key=lambda entry: entry["gpu_hours"],
    )
    if shortfall <= 0:
        conclusion = (
            f"按 {model} 在 {card} 上约需 {selected_units:.1f} 单元，结余 {balance:g}，"
            f"够用（剩 {balance - selected_units:.1f}）。"
        )
    else:
        conclusion = (
            f"按 {model} 在 {card} 上约需 {selected_units:.1f} 单元，结余 {balance:g}，"
            f"不足，差 {shortfall:.1f}；结余约够按顺序渲 {affordable}/{len(chapters)} 章。"
        )
        if fitting:
            fastest = fitting[0]
            conclusion += (
                f" 成本上可换 {fastest['model']}，压到 {fastest['gpu_hours']:.1f} GPU 小时 / "
                f"{fastest['units'][card]:.1f} 单元。"
            )
        else:
            cheapest = min(models, key=lambda entry: entry["units"][card])
            conclusion += (
                f" 当前结余连最快的 {cheapest['model']} 都不够"
                f"（{cheapest['units'][card]:.1f} 单元）。"
            )

    return {
        "source": source,
        "basis": basis,
        "chars_per_second": chars_per_second if basis == "chars" else None,
        "audio_hours": total_audio_seconds / 3600.0,
        "chapters": len(chapters),
        "cues": total_cues,
        "chars": total_chars,
        "balance": balance,
        "model": model,
        "card": card,
        "rtf": RTF,
        "card_units_per_hour": CARD_UNITS_PER_HOUR,
        "models": models,
        "selected": selected,
        "affordable_chapters": affordable,
        "chapter_rows": chapters,
        "conclusion": conclusion,
    }


def print_cost(summary: dict, detail: bool = False) -> None:
    """人类可读表：总账在前，逐章明细按需展开。"""
    print(f"成本预估：{summary['source'] or '(未注明来源)'}")
    print(
        f"{summary['chapters']} 章 / {summary['cues']:,} 条 cue / "
        f"{summary['chars']:,} 字符"
    )
    if summary["basis"] == "chars":
        print(
            f"音频估算：{summary['chars_per_second']:g} 字符/秒 → "
            f"{summary['audio_hours']:.2f} 小时（默认系数按 ADR-0003 的 "
            "1,122,289 字符 / 1,317 分钟反推）"
        )
    else:
        print(f"音频时长：按 --audio-hours 指定为 {summary['audio_hours']:.2f} 小时")
    print("算法：GPU 小时 = 音频小时 ÷ 稳态 RTFx；计算单元 = GPU 小时 × 卡型单价")
    print()
    header = f"{'模型':20}{'RTF':>7}{'GPU 小时':>11}{'单元(' + summary['card'] + ')':>14}  相对结余"
    print(header)
    for entry in summary["models"]:
        units = entry["units"][summary["card"]]
        mark = "← 当前" if entry["model"] == summary["model"] else ""
        fit = "够" if entry["fits"][summary["card"]] else "不足"
        print(f"{entry['model']:20}{entry['rtf']:>7.2f}{entry['gpu_hours']:>11.1f}{units:>14.1f}  {fit} {mark}")
    print()
    print("卡型单价（计算单元/GPU 小时）：" + " · ".join(
        f"{name}={price:g}" for name, price in CARD_UNITS_PER_HOUR.items()
    ))
    selected = summary["selected"]
    converted = " / ".join(
        f"{name} {selected['units'][name]:.1f}" for name in CARD_UNITS_PER_HOUR
    )
    print(f"按 {summary['model']} 换算到各卡：{converted}")
    print()
    print(summary["conclusion"])
    print(
        "注意：卡型只换算单价，不换算速度；RTF 表来自基准卡，换卡后的吞吐需要实测"
        "（ADR-0003 待办）。模型选择还要看许可证与车道策略（ADR-0003），"
        "这里只比较成本，不替用户选车道。"
    )
    if detail:
        print()
        print(f"{'章节':>6}{'cue':>6}{'字符':>8}{'音频分钟':>10}{'GPU 小时':>10}{'单元':>9}")
        for row in summary["chapter_rows"]:
            print(
                f"{row['chapter']:>6}{row['cues']:>6}{row['chars']:>8}"
                f"{row['audio_seconds'] / 60:>10.1f}{row['gpu_hours']:>10.2f}{row['units']:>9.1f}"
            )


# ---------- 音频解码 ----------


def resample_linear(data: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """线性插值重采样。只用于统一分析采样率，不用于交付音频。"""
    if source_rate == target_rate or data.size == 0:
        return data.astype(np.float32, copy=False)
    count = max(int(round(data.size * target_rate / source_rate)), 1)
    positions = np.arange(count, dtype=np.float64) * source_rate / target_rate
    values = np.interp(positions, np.arange(data.size, dtype=np.float64), data)
    return values.astype(np.float32)


def read_wav(path: Path, target_rate: int = 0) -> tuple[np.ndarray, int]:
    """用标准库 wave 读 PCM（8/16/24/32 位），多声道取均值，返回 [-1,1] 浮点。"""
    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = np.where(values & 0x800000, values - 0x1000000, values)
        data = values.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise SystemExit(f"{path.name}: 只支持 8/16/24/32 位 PCM，实际 {width * 8} 位")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if target_rate and target_rate != sample_rate:
        data = resample_linear(data, sample_rate, target_rate)
        sample_rate = target_rate
    return data, sample_rate


def decode_audio(path: Path, target_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, int]:
    """wav 直接读；其余格式必须经 ffmpeg 解码成单声道 16 位 PCM。"""
    if not path.exists():
        raise SystemExit(f"找不到音频文件：{path}")
    if path.suffix.lower() == ".wav":
        return read_wav(path, target_rate)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit(
            f"找不到 ffmpeg：无法解码 {path.name}。请安装 ffmpeg，"
            "或先把音频转成 wav 再用本脚本。"
        )
    command = [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
        "-ac", "1", "-ar", str(target_rate), "-f", "s16le", "-",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"ffmpeg 解码失败：{path.name}\n{message[:800]}")
    data = np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0
    if data.size == 0:
        raise SystemExit(f"ffmpeg 没有从 {path.name} 解出任何样本")
    return data, target_rate


# ---------- 指纹与响度 ----------


def log_spectrum_fingerprint(
    data: np.ndarray,
    sample_rate: int,
    window_seconds: float = 0.25,
    max_hz: float = 8000.0,
) -> np.ndarray:
    """对数谱指纹：分帧取均值后去均值，余弦相似度对整体增益不敏感。

    与 anchor-audit.py 的口径不同：这里用 0.25 s 短帧并保留到 8 kHz，因为章缝
    要比较的是音色与房间感，而不是只看 500 Hz 以下的基频。
    """
    window = max(int(sample_rate * window_seconds), 16)
    hop = max(window // 2, 1)
    if data.size == 0:
        return np.zeros(0, dtype=np.float64)
    if data.size < window:
        frames = [np.pad(data, (0, window - data.size))]
    else:
        frames = [data[start:start + window] for start in range(0, data.size - window + 1, hop)]
    bin_count = max(int(max_hz * window / sample_rate), 1)
    spectra = []
    for frame in frames:
        centered = frame - float(np.mean(frame))
        spectrum = np.abs(np.fft.rfft(centered * np.hanning(window)))
        spectra.append(np.log(spectrum[:bin_count] + 1e-8))
    mean = np.mean(spectra, axis=0)
    return mean - float(np.mean(mean))


def fingerprint_cosine(left: np.ndarray, right: np.ndarray) -> float | None:
    """两条指纹的余弦；任一条近乎为零向量时返回 None（例如边界是静音）。"""
    if left.size == 0 or right.size == 0 or left.size != right.size:
        return None
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm < 1e-9 or right_norm < 1e-9:
        return None
    return float(np.dot(left, right) / (left_norm * right_norm))


def rms_dbfs(data: np.ndarray) -> float | None:
    """样本 RMS 的 dBFS；空信号或全零返回 None。"""
    if data.size == 0:
        return None
    mean_square = float(np.mean(np.square(data.astype(np.float64))))
    if mean_square <= 0:
        return None
    return 10.0 * math.log10(mean_square)


def lufs_approx(data: np.ndarray) -> float | None:
    """LUFS 近似：只算均方加 -0.691 偏移，不做 K 加权与门限。

    对白与音乐混合的节目会与真 LUFS 差 1–2 dB，这里只用于章缝/批次之间的相对比较，
    不能当交付判据。交付仍以 ADR-0013 的响度报告为准。
    """
    if data.size == 0:
        return None
    mean_square = float(np.mean(np.square(data.astype(np.float64))))
    if mean_square <= 0:
        return None
    return -0.691 + 10.0 * math.log10(mean_square)


def peak_dbfs(data: np.ndarray) -> float | None:
    """样本峰值 dBFS。"""
    if data.size == 0:
        return None
    peak = float(np.max(np.abs(data)))
    if peak <= 0:
        return None
    return 20.0 * math.log10(peak)


def true_peak_dbtp_approx(
    data: np.ndarray,
    sample_rate: int,
    oversample: int = 4,
    block_seconds: float = 1.0,
) -> float | None:
    """真峰值近似：样本峰 + 4 倍线性插值过采样。

    线性插值会低估真实峰值（典型不到 1 dB），所以只能用来发现“接近 0 dBFS”的文件，
    不能替代 ADR-0013 要求的真峰值表。长度不足时退回样本峰。
    """
    if data.size == 0:
        return None
    sample_peak = float(np.max(np.abs(data)))
    if data.size > 1:
        block = max(int(sample_rate * block_seconds), 8)
        for start in range(0, data.size - 1, block):
            segment = data[start:start + block + 1].astype(np.float64)
            if segment.size < 2:
                continue
            positions = np.arange(0.0, segment.size - 1, 1.0 / oversample)
            values = np.interp(positions, np.arange(segment.size, dtype=np.float64), segment)
            block_peak = float(np.max(np.abs(values)))
            if block_peak > sample_peak:
                sample_peak = block_peak
    if sample_peak <= 0:
        return None
    return 20.0 * math.log10(sample_peak)


def count_clipped(data: np.ndarray, level: float = CLIP_LEVEL) -> int:
    """绝对值达到阈值的样本数。16 位满刻度会读成 0.99997，所以默认阈值 0.999。"""
    if data.size == 0:
        return 0
    return int(np.count_nonzero(np.abs(data) >= level))


def find_silences(
    data: np.ndarray,
    sample_rate: int,
    threshold_db: float = SILENCE_DB,
    min_seconds: float = SILENCE_SECONDS,
    frame_seconds: float = 0.1,
) -> list[dict]:
    """找连续低于阈值的静音段，只返回不短于 min_seconds 的。"""
    frame = max(int(sample_rate * frame_seconds), 1)
    count = data.size // frame
    if count == 0:
        return []
    frames = data[:count * frame].astype(np.float64).reshape(count, frame)
    mean_square = np.mean(np.square(frames), axis=1)
    levels = 10.0 * np.log10(np.maximum(mean_square, 1e-24))
    silences: list[dict] = []
    start: int | None = None
    for index, quiet in enumerate(levels < threshold_db):
        if quiet and start is None:
            start = index
        elif not quiet and start is not None:
            _append_silence(silences, start, index, frame_seconds, count, min_seconds)
            start = None
    if start is not None:
        _append_silence(silences, start, count, frame_seconds, count, min_seconds)
    return silences


def _append_silence(
    silences: list[dict],
    start: int,
    end: int,
    frame_seconds: float,
    frame_count: int,
    min_seconds: float,
) -> None:
    duration = (end - start) * frame_seconds
    if duration + 1e-9 < min_seconds:
        return
    silences.append({
        "start_s": round(start * frame_seconds, 3),
        "end_s": round(end * frame_seconds, 3),
        "duration_s": round(duration, 3),
        # 章首/章尾的静音可能是正常留白，单独标出来，避免和章内空洞混在一起。
        "edge": start == 0 or end == frame_count,
    })


def loudness_report(
    path: Path,
    threshold_db: float = SILENCE_DB,
    min_seconds: float = SILENCE_SECONDS,
) -> dict:
    """量一个文件的响度、峰值、长静音与削波。"""
    data, sample_rate = decode_audio(path)
    return {
        "file": str(path),
        "name": path.name,
        "sample_rate": sample_rate,
        "duration_s": round(data.size / sample_rate, 3) if sample_rate else 0.0,
        "rms_dbfs": _round(rms_dbfs(data), 2),
        "lufs_approx": _round(lufs_approx(data), 2),
        "sample_peak_dbfs": _round(peak_dbfs(data), 2),
        "true_peak_dbtp_approx": _round(true_peak_dbtp_approx(data, sample_rate), 2),
        "silences": find_silences(data, sample_rate, threshold_db, min_seconds),
        "clipped_samples": count_clipped(data),
    }


def print_loudness(results: list[dict], threshold_db: float, min_seconds: float) -> None:
    print(
        f"{'文件':30}{'时长':>8}{'RMS':>8}{'LUFS~':>8}{'样本峰':>8}"
        f"{'真峰~':>8}{'静音':>6}{'削波':>7}"
    )
    for row in results:
        rms = f"{row['rms_dbfs']:.1f}" if row["rms_dbfs"] is not None else "-"
        lufs = f"{row['lufs_approx']:.1f}" if row["lufs_approx"] is not None else "-"
        peak = f"{row['sample_peak_dbfs']:.1f}" if row["sample_peak_dbfs"] is not None else "-"
        true_peak = (
            f"{row['true_peak_dbtp_approx']:.1f}"
            if row["true_peak_dbtp_approx"] is not None else "-"
        )
        print(
            f"{row['name'][:30]:30}{row['duration_s']:>8.1f}{rms:>8}{lufs:>8}"
            f"{peak:>8}{true_peak:>8}{len(row['silences']):>6}{row['clipped_samples']:>7}"
        )
    print(
        f"\n注：LUFS 未做 K 加权与门限；真峰用 4 倍线性插值过采样，均为近似值。"
        f"静音口径：低于 {threshold_db:g} dBFS 且不短于 {min_seconds:g} s。"
    )
    for row in results:
        for silence in row["silences"]:
            edge = "（章首/章尾）" if silence["edge"] else ""
            print(
                f"  {row['name']}: 静音 {silence['start_s']:.1f}–{silence['end_s']:.1f} s，"
                f"{silence['duration_s']:.1f} s{edge}"
            )


# ---------- 章缝 ----------


def chapter_from_name(path: Path) -> str:
    """从 ch_1061_v6.m4b 这类文件名里取章节号；取不到时返回空串。"""
    match = re.search(r"(\d{3,5})", path.stem)
    return match.group(1) if match else ""


def compare_seam(
    previous: np.ndarray,
    following: np.ndarray,
    sample_rate: int,
    seconds: float = SEAM_SECONDS,
) -> dict:
    """比较前一章末 window 与后一章首 window 的指纹、响度与峰值。"""
    width = max(int(seconds * sample_rate), 1)
    tail = previous[-width:] if previous.size else previous
    head = following[:width] if following.size else following
    tail_fingerprint = log_spectrum_fingerprint(tail, sample_rate)
    head_fingerprint = log_spectrum_fingerprint(head, sample_rate)
    cosine = fingerprint_cosine(tail_fingerprint, head_fingerprint)
    tail_lufs = lufs_approx(tail)
    head_lufs = lufs_approx(head)
    delta = (
        head_lufs - tail_lufs
        if tail_lufs is not None and head_lufs is not None else None
    )
    tail_rms = rms_dbfs(tail)
    head_rms = rms_dbfs(head)
    silent = (
        ((tail_rms is None or tail_rms < SEAM_SILENCE_DB)
         and (head_rms is None or head_rms < SEAM_SILENCE_DB))
        or tail_rms is None or head_rms is None
    )
    return {
        "cosine": _round(cosine, 3),
        "tail_lufs_approx": _round(tail_lufs, 2),
        "head_lufs_approx": _round(head_lufs, 2),
        "lufs_delta": _round(delta, 2),
        "tail_peak_dbfs": _round(peak_dbfs(tail), 2),
        "head_peak_dbfs": _round(peak_dbfs(head), 2),
        "tail_seconds": round(tail.size / sample_rate, 3) if sample_rate else 0.0,
        "head_seconds": round(head.size / sample_rate, 3) if sample_rate else 0.0,
        "silent_edge": silent,
    }


def seam_verdict(metrics: dict, same_speaker: bool | None = None) -> str:
    """按报告数给一句提示；阈值是启发式，最终仍要看原始余弦与响度。

    跨角色边界天然带内容差异（上一章末句是低语、下一章首句是标题旁白），
    不能把低余弦或响度差直接判成渲染缺陷，所以单独给一句“不判缺陷”。
    """
    if metrics["silent_edge"] or metrics["cosine"] is None:
        return "不可判（边界静音）"
    spectral_drop = metrics["cosine"] < SEAM_COSINE_WARN
    loudness_jump = (
        metrics["lufs_delta"] is not None
        and abs(metrics["lufs_delta"]) > SEAM_LOUDNESS_WARN_DB
    )
    if same_speaker is False:
        if spectral_drop or loudness_jump:
            return "跨角色（差异含内容因素，不判缺陷）"
        return "正常"
    if same_speaker is True:
        if spectral_drop:
            return "可疑（同角色频谱跳变）"
        if loudness_jump:
            return "可疑（同角色响度跳变）"
        return "正常"
    # 没有 vol6.jsonl 标不出边界说话人时，只提示，不判缺陷。
    if spectral_drop:
        return "关注（频谱差）"
    if loudness_jump:
        return "关注（响度差）"
    return "正常"


def boundary_speakers(
    records: dict[str, list[dict]],
    previous: Path,
    following: Path,
) -> tuple[str, str] | None:
    """用 vol6.jsonl 标出章缝两侧的 cue 说话人；文件名认不出章节时返回 None。"""
    previous_chapter = chapter_from_name(previous)
    following_chapter = chapter_from_name(following)
    previous_rows = records.get(previous_chapter)
    following_rows = records.get(following_chapter)
    if not previous_rows or not following_rows:
        return None
    return str(previous_rows[-1].get("speaker", "")), str(following_rows[0].get("speaker", ""))


def run_seam(args: argparse.Namespace) -> int:
    paths = [Path(name) for name in args.files]
    if len(paths) < 2:
        raise SystemExit("seam 至少需要两个音频文件（按章节顺序）")
    records: dict[str, list[dict]] = {}
    if args.records:
        records = group_chapters(load_jsonl(Path(args.records)))

    results: list[dict] = []
    previous_data: np.ndarray | None = None
    previous_path: Path | None = None
    for path in paths:
        data, sample_rate = decode_audio(path, args.sample_rate)
        if previous_data is not None and previous_path is not None:
            metrics = compare_seam(previous_data, data, sample_rate, args.seconds)
            speakers = boundary_speakers(records, previous_path, path) if records else None
            same_speaker = None
            if speakers is not None:
                same_speaker = speakers[0] == speakers[1]
            results.append({
                "from": str(previous_path),
                "to": str(path),
                "from_chapter": chapter_from_name(previous_path),
                "to_chapter": chapter_from_name(path),
                "from_speaker": speakers[0] if speakers else "",
                "to_speaker": speakers[1] if speakers else "",
                **metrics,
                "verdict": seam_verdict(metrics, same_speaker),
            })
        previous_data = data
        previous_path = path

    print(
        f"{'章缝':42}{'余弦':>7}{'尾LUFS':>8}{'头LUFS':>8}"
        f"{'ΔLUFS':>7}{'尾峰':>7}{'头峰':>7}  判定"
    )
    for row in results:
        cosine = f"{row['cosine']:.3f}" if row["cosine"] is not None else "-"
        tail_lufs = f"{row['tail_lufs_approx']:.1f}" if row["tail_lufs_approx"] is not None else "-"
        head_lufs = f"{row['head_lufs_approx']:.1f}" if row["head_lufs_approx"] is not None else "-"
        delta = f"{row['lufs_delta']:+.1f}" if row["lufs_delta"] is not None else "-"
        tail_peak = f"{row['tail_peak_dbfs']:.1f}" if row["tail_peak_dbfs"] is not None else "-"
        head_peak = f"{row['head_peak_dbfs']:.1f}" if row["head_peak_dbfs"] is not None else "-"
        speakers = (
            f" [{row['from_speaker']}→{row['to_speaker']}]"
            if row["from_speaker"] or row["to_speaker"] else ""
        )
        label = f"{row['from_chapter'] or '?'}→{row['to_chapter'] or '?'}{speakers}"
        print(
            f"{label[:42]:42}{cosine:>7}{tail_lufs:>8}{head_lufs:>8}"
            f"{delta:>7}{tail_peak:>7}{head_peak:>7}  {row['verdict']}"
        )
    print(
        f"\n注：余弦是同口径对数谱指纹（0.25 s 帧、到 8 kHz）的余弦；"
        f"低于 {SEAM_COSINE_WARN:g} 或响度差超过 {SEAM_LOUDNESS_WARN_DB:g} dB 只作提示。"
        "章缝两侧本来就可能换说话人，跨角色低余弦不是缺陷；同角色边界突然掉下去才值得听。"
    )
    if args.json:
        write_json(args.json, {"seconds": args.seconds, "pairs": results})
        print(f"结果写入 {args.json}")
    return 0


# ---------- 回读 WER ----------


_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """规范化后切词：大小写、全半角、标点不参与比较；中文按单字计。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    tokens: list[str] = []
    for token in _WORD.findall(normalized):
        if all(_CJK.match(char) for char in token):
            tokens.extend(token)
        else:
            tokens.append(token)
    return tokens


def align_words(reference: list[str], hypothesis: list[str]) -> dict:
    """词级 Levenshtein，带回溯明细；替换、删除、插入各记 1 次错误。"""
    rows, columns = len(reference), len(hypothesis)
    table = [[0] * (columns + 1) for _ in range(rows + 1)]
    for index in range(rows + 1):
        table[index][0] = index
    for index in range(columns + 1):
        table[0][index] = index
    for i in range(1, rows + 1):
        for j in range(1, columns + 1):
            substitution = 0 if reference[i - 1] == hypothesis[j - 1] else 1
            table[i][j] = min(
                table[i - 1][j] + 1,
                table[i][j - 1] + 1,
                table[i - 1][j - 1] + substitution,
            )

    errors: list[dict] = []
    substitutions = deletions = insertions = 0
    i, j = rows, columns
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            substitution = 0 if reference[i - 1] == hypothesis[j - 1] else 1
            if table[i][j] == table[i - 1][j - 1] + substitution:
                if substitution:
                    substitutions += 1
                    errors.append({
                        "op": "替换", "ref": reference[i - 1], "hyp": hypothesis[j - 1],
                        "ref_index": i - 1,
                    })
                i -= 1
                j -= 1
                continue
        if i > 0 and table[i][j] == table[i - 1][j] + 1:
            deletions += 1
            errors.append({
                "op": "删除", "ref": reference[i - 1], "hyp": "",
                "ref_index": i - 1,
            })
            i -= 1
        else:
            insertions += 1
            errors.append({
                "op": "插入", "ref": "", "hyp": hypothesis[j - 1],
                "ref_index": i,
            })
            j -= 1
    errors.reverse()
    return {
        "distance": substitutions + deletions + insertions,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "errors": errors,
    }


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    """WER = 错误数 / 参考词数；参考为空时识别出任何词记 1.0，否则 0.0。"""
    if not reference:
        return 1.0 if hypothesis else 0.0
    return align_words(reference, hypothesis)["distance"] / len(reference)


def cue_tokens(rows: list[dict]) -> tuple[list[str], list[dict], list[int]]:
    """把一章的 cue 文本拼成参考词流，并记住每个 cue 的起始词下标。"""
    ordered = sorted(rows, key=lambda row: int(row.get("idx", 0)))
    tokens: list[str] = []
    starts: list[int] = []
    for row in ordered:
        starts.append(len(tokens))
        tokens.extend(tokenize(str(row["text"])))
    return tokens, ordered, starts


def cue_for_index(cues: list[dict], starts: list[int], index: int) -> dict | None:
    """参考词下标落在哪个 cue；插入错误落在词间，取后面的 cue。"""
    if not cues or not starts:
        return None
    position = bisect.bisect_right(starts, index) - 1
    position = min(max(position, 0), len(cues) - 1)
    return cues[position]


def transcript_text(payload: object) -> str:
    """从 yovoice TranscriptContent 或常见包装里取转写文本。"""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text
        words = payload.get("words")
        if isinstance(words, list) and words:
            joined = " ".join(str(word.get("word", "")) for word in words if isinstance(word, dict))
            if joined.strip():
                return joined
        for key in ("content", "result", "transcript", "data"):
            nested = payload.get(key)
            if isinstance(nested, (dict, str)):
                return transcript_text(nested)
    raise SystemExit("转写 JSON 里找不到 text 或 words 字段")


def compare_transcript(
    transcript_path: Path,
    rows: list[dict],
    chapter: str,
    threshold: float = WER_THRESHOLD,
    max_errors: int = 50,
) -> dict:
    """一章的转写与 cue 文本做词级比对。"""
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{transcript_path} 不是合法 JSON：{error}") from error
    reference, cues, starts = cue_tokens(rows)
    hypothesis = tokenize(transcript_text(payload))
    alignment = align_words(reference, hypothesis)
    rate = (
        alignment["distance"] / len(reference)
        if reference else (1.0 if hypothesis else 0.0)
    )
    detailed: list[dict] = []
    for error in alignment["errors"]:
        cue = cue_for_index(cues, starts, error["ref_index"])
        detailed.append({
            **error,
            "cue": cue.get("id", f"{chapter}-{cue.get('idx', '')}") if cue else "",
            "speaker": cue.get("speaker", "") if cue else "",
        })
    return {
        "chapter": chapter,
        "transcript": str(transcript_path),
        "reference_words": len(reference),
        "hypothesis_words": len(hypothesis),
        "distance": alignment["distance"],
        "substitutions": alignment["substitutions"],
        "deletions": alignment["deletions"],
        "insertions": alignment["insertions"],
        "wer": rate,
        "threshold": threshold,
        "passed": rate <= threshold,
        "errors_total": len(detailed),
        "errors": detailed[:max_errors],
    }


def run_wer(args: argparse.Namespace) -> int:
    records = group_chapters(load_jsonl(Path(args.records)))
    transcripts = [Path(name) for name in args.transcript]
    if args.chapter and len(transcripts) != 1:
        raise SystemExit("--chapter 只能在只给一个 --transcript 时使用")
    chapters: list[dict] = []
    for transcript in transcripts:
        chapter = args.chapter or chapter_from_name(transcript)
        if not chapter:
            raise SystemExit(
                f"无法从 {transcript.name} 认出章节号；请把文件名写成 ch_1061.json "
                "或用 --chapter 指定（只给一个转写文件时）"
            )
        if chapter not in records:
            raise SystemExit(f"vol6.jsonl 里没有第 {chapter} 章")
        chapters.append(compare_transcript(
            transcript, records[chapter], chapter, args.threshold, args.max_errors,
        ))

    total_reference = sum(chapter["reference_words"] for chapter in chapters)
    total_distance = sum(chapter["distance"] for chapter in chapters)
    overall = total_distance / total_reference if total_reference else 0.0
    print(
        f"{'章节':>6}{'参考词':>8}{'识别词':>8}{'替换':>6}{'删除':>6}"
        f"{'插入':>6}{'WER':>8}  判定"
    )
    for chapter in chapters:
        mark = "通过" if chapter["passed"] else "超标"
        print(
            f"{chapter['chapter']:>6}{chapter['reference_words']:>8}"
            f"{chapter['hypothesis_words']:>8}{chapter['substitutions']:>6}"
            f"{chapter['deletions']:>6}{chapter['insertions']:>6}"
            f"{chapter['wer'] * 100:>7.2f}%  {mark}"
        )
    if len(chapters) > 1:
        print(f"{'合计':>6}{total_reference:>8}{'':>8}{'':>6}{'':>6}{'':>6}{overall * 100:>7.2f}%")
    print(f"\n阈值：WER ≤ {args.threshold * 100:.2f}%；错词只列前 {args.max_errors} 条。")
    for chapter in chapters:
        if not chapter["errors"]:
            continue
        print(f"\n第 {chapter['chapter']} 章错词：")
        for error in chapter["errors"]:
            reference = error["ref"] or "∅"
            hypothesis = error["hyp"] or "∅"
            print(
                f"  cue {error['cue']} [{error['speaker'] or '?'}] "
                f"{error['op']}：{reference} → {hypothesis}"
            )
        if chapter["errors_total"] > len(chapter["errors"]):
            print(f"  ……另有 {chapter['errors_total'] - len(chapter['errors'])} 条未列出")
    if args.json:
        write_json(args.json, {
            "records": args.records,
            "threshold": args.threshold,
            "chapters": chapters,
            "overall_wer": overall,
        })
        print(f"\n结果写入 {args.json}")
    return 0 if all(chapter["passed"] for chapter in chapters) else 1


# ---------- 命令行 ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P0 只读分析：成本 / 章缝 / 响度 / WER")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cost = subparsers.add_parser("cost", help="按章字数预估 GPU 小时与计算单元")
    cost.add_argument("--records", required=True, help="vol6.jsonl 记录文件")
    cost.add_argument("--balance", required=True, type=float, help="当前计算单元结余")
    cost.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(RTF), help="当前模型")
    cost.add_argument("--card", default=DEFAULT_CARD, choices=sorted(CARD_UNITS_PER_HOUR), help="卡型")
    cost.add_argument(
        "--chars-per-second", type=float, default=CHARS_PER_SECOND,
        help=f"字数折音频秒的系数（默认 {CHARS_PER_SECOND}，来自本卷实测）",
    )
    cost.add_argument(
        "--audio-hours", type=float, default=0.0,
        help="已知整卷实际音频时长时用它覆盖字数估算（按章字数比例分摊）",
    )
    cost.add_argument("--json", default="", help="结果写入的 JSON 路径")
    cost.add_argument("--detail", action="store_true", help="打印逐章明细")
    cost.set_defaults(handler=run_cost)

    seam = subparsers.add_parser("seam", help="比较相邻章节边界的指纹与响度")
    seam.add_argument("files", nargs="+", help="按章节顺序排列的音频文件（wav/m4b/m4a）")
    seam.add_argument("--seconds", type=float, default=SEAM_SECONDS, help="边界观察窗秒数")
    seam.add_argument("--sample-rate", type=int, default=SAMPLE_RATE, help="分析采样率")
    seam.add_argument("--records", default="", help="vol6.jsonl，用于标出边界说话人")
    seam.add_argument("--json", default="", help="结果写入的 JSON 路径")
    seam.set_defaults(handler=run_seam)

    loudness = subparsers.add_parser("loudness", help="逐文件响度、峰值、静音与削波")
    loudness.add_argument("files", nargs="+", help="音频文件（wav/m4b/m4a）")
    loudness.add_argument("--sample-rate", type=int, default=SAMPLE_RATE, help="分析采样率")
    loudness.add_argument("--silence-db", type=float, default=SILENCE_DB, help="静音阈值 dBFS")
    loudness.add_argument("--min-silence", type=float, default=SILENCE_SECONDS, help="最短静音秒数")
    loudness.add_argument("--json", default="", help="结果写入的 JSON 路径")
    loudness.set_defaults(handler=run_loudness)

    wer = subparsers.add_parser("wer", help="转写回读与 cue 文本做词级比对")
    wer.add_argument("--records", required=True, help="vol6.jsonl 记录文件")
    wer.add_argument(
        "--transcript", nargs="+", required=True,
        help="yovoice transcribe 输出的 JSON；文件名里的章节号用于匹配",
    )
    wer.add_argument("--chapter", default="", help="只给一个转写文件时直接指定章节")
    wer.add_argument("--threshold", type=float, default=WER_THRESHOLD, help="WER 阈值")
    wer.add_argument("--max-errors", type=int, default=50, help="每章最多列出的错词数")
    wer.add_argument("--json", default="", help="结果写入的 JSON 路径")
    wer.set_defaults(handler=run_wer)
    return parser


def run_cost(args: argparse.Namespace) -> int:
    rows = load_jsonl(Path(args.records))
    summary = summarize_cost(
        rows,
        balance=args.balance,
        model=args.model,
        card=args.card,
        chars_per_second=args.chars_per_second,
        audio_hours=args.audio_hours,
        source=args.records,
    )
    print_cost(summary, args.detail)
    if args.json:
        write_json(args.json, summary)
        print(f"\n结果写入 {args.json}")
    return 0


def run_loudness(args: argparse.Namespace) -> int:
    results = [
        loudness_report(Path(name), args.silence_db, args.min_silence)
        for name in args.files
    ]
    print_loudness(results, args.silence_db, args.min_silence)
    if args.json:
        write_json(args.json, {"silence_db": args.silence_db, "min_silence": args.min_silence, "files": results})
        print(f"\n结果写入 {args.json}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except (ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
